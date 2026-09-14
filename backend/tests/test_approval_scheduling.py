"""Tests for "Approve and Schedule" (scheduled sends) — CHANGE 2.

Covers, WITHOUT a DB migration and WITHOUT a new enum value (a scheduled reply
is a still-PENDING approval whose ``arguments`` carry ``scheduled_send_at`` /
``scheduled_by_user_id``):

- :func:`approval_service.schedule_request` — future time stores the schedule
  and keeps the request pending; past time -> 422; already-resolved -> 409;
  missing -> 404; non-Owner/Admin role -> 403.
- :func:`approval_service.list_due_scheduled` — returns only pending rows whose
  ``scheduled_send_at <= now`` (not future, not resolved, not un-scheduled).
- :func:`app.agents.tasks.send_scheduled_replies` — sends due ones (via a fake
  Gmail layer), leaves not-yet-due untouched, and a failing send stays pending
  without aborting the sweep.

Reuses the throwaway-Postgres harness from ``test_approval_execution.py``: a
non-default-port ``postgres:18.6-alpine`` with migrations applied. The Gmail
HTTP layer is injected as a fake so NO real network is used, and
``oauth_refresh.ensure_access_token`` is monkeypatched to a passthrough.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.errors import APIError
from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    IntegrationCategory,
    MemberRole,
    User,
    Workspace,
)
from app.services import approval_service, gmail_message, integration_vault

# ---------------------------------------------------------------------------
# Throwaway Postgres harness (mirrors test_approval_execution.py)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_approval_sched_test"
_HOST_PORT = 55461  # non-default so we never touch another database
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@localhost:{_HOST_PORT}/{_PG_DB}"
)
_ENC_KEY = "test-encryption-key-value-0123456789"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _force_remove_container() -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _wait_until_ready(timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            last = str(exc)
            time.sleep(1.0)
            continue
        if result.returncode == 0:
            return
        last = (result.stdout or "") + (result.stderr or "")
        time.sleep(1.0)
    raise RuntimeError(f"Postgres not ready in {timeout_s}s: {last!r}")


def _run_migrations() -> None:
    env = {
        **os.environ,
        "DATABASE_URL": _TEST_DSN,
        "REDIS_URL": "redis://localhost:6379/0",
        "ENCRYPTION_KEY": _ENC_KEY,
        "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret-value",
        "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret-value",
    }
    result = subprocess.run(
        [".venv/bin/alembic", "-c", "alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_BACKEND_DIR),
        env=env,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("Docker CLI/daemon not available")
    _force_remove_container()
    try:
        start = subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", _CONTAINER_NAME,
                "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e", f"POSTGRES_USER={_PG_USER}",
                "-e", f"POSTGRES_DB={_PG_DB}",
                "-p", f"{_HOST_PORT}:5432",
                _PG_IMAGE,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if start.returncode != 0:
            pytest.skip(
                f"could not start Postgres container: {start.stderr.strip()[:300]}"
            )
        _wait_until_ready()
        _run_migrations()
        yield _TEST_DSN
    finally:
        _force_remove_container()


@pytest.fixture(autouse=True)
def _config_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from cryptography.fernet import Fernet

    from app import config as _config
    from app.core import encryption as _enc

    for key, value in {
        "DATABASE_URL": _TEST_DSN,
        "REDIS_URL": "redis://localhost:6379/0",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret-value",
        "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret-value",
    }.items():
        monkeypatch.setenv(key, value)

    _config.get_settings.cache_clear()
    _enc.set_encryption_service(_enc.EncryptionService(Fernet.generate_key()))
    try:
        yield
    finally:
        _config.get_settings.cache_clear()
        _enc.reset_encryption_service()


@pytest.fixture(autouse=True)
def _no_oauth_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _passthrough(provider, credentials, config=None):
        return dict(credentials)

    from app.services import oauth_refresh

    monkeypatch.setattr(oauth_refresh, "ensure_access_token", _passthrough)


@pytest_asyncio.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _sample_arguments() -> dict:
    payload = gmail_message.build_draft_payload(
        to="orig@sender.com",
        subject="Question",
        body="Original reply body.",
        in_reply_to="<abc@mail>",
        thread_id="THREAD1",
    )
    return {
        "method": "POST",
        "path": "/gmail/v1/users/me/drafts",
        "body": payload,
        "source_message_id": "MSG123",
        "thread_id": "THREAD1",
    }


async def _make_user(session: AsyncSession) -> User:
    user = User(email=f"{uuid.uuid4().hex}@x.test", name="U", auth_provider="google")
    session.add(user)
    await session.flush()
    return user


async def _make_workspace(session: AsyncSession, creator_id: uuid.UUID) -> Workspace:
    ws = Workspace(
        name=f"ws-{uuid.uuid4().hex[:8]}",
        slug=f"ws-{uuid.uuid4().hex}",
        created_by_user_id=creator_id,
    )
    session.add(ws)
    await session.flush()
    return ws


async def _seed_gmail_integration(
    session: AsyncSession, *, ws: Workspace, user: User
) -> None:
    await integration_vault.store(
        session,
        workspace_id=ws.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        credentials={"access_token": "tok", "refresh_token": "r", "client_id": "c", "client_secret": "s"},
    )


async def _make_pending_reply(
    session: AsyncSession, *, ws: Workspace, user: User
) -> ApprovalRequest:
    req = ApprovalRequest(
        workspace_id=ws.id,
        agent_session_id=None,
        triggered_by_user_id=user.id,
        tool_name=approval_service.GMAIL_REPLY_TOOL,
        arguments=_sample_arguments(),
        status=ApprovalStatus.PENDING,
    )
    session.add(req)
    await session.flush()
    return req


class _FakeGmail:
    """Records call_provider_api invocations and returns a scripted result."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    async def __call__(self, *, provider_name, credentials, config, method, path,
                       query=None, body=None) -> dict:
        self.calls.append({"method": method, "path": path, "body": body})
        if self.ok:
            return {"status_code": 200, "ok": True, "body": {"id": "created"}}
        return {"status_code": 500, "ok": False, "body": {"error": "boom"}}

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


# ---------------------------------------------------------------------------
# schedule_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_request_future_sets_metadata_keeps_pending(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    when = datetime.now(timezone.utc) + timedelta(hours=2)
    updated = await approval_service.schedule_request(
        session,
        approval_request_id=req.id,
        reviewer_user_id=user.id,
        scheduled_send_at=when,
        reviewer_role=MemberRole.OWNER,
    )

    assert updated.status is ApprovalStatus.PENDING
    args = updated.arguments
    assert args["scheduled_by_user_id"] == str(user.id)
    # Stored as a UTC ISO string that round-trips to the same instant.
    stored = datetime.fromisoformat(args["scheduled_send_at"])
    assert stored.tzinfo is not None
    assert abs((stored - when).total_seconds()) < 1
    # Original draft payload is untouched.
    assert args["source_message_id"] == "MSG123"


@pytest.mark.asyncio
async def test_schedule_request_accepts_naive_utc(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    # A naive datetime (no tzinfo) that represents a future UTC instant.
    naive_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    updated = await approval_service.schedule_request(
        session,
        approval_request_id=req.id,
        reviewer_user_id=user.id,
        scheduled_send_at=naive_future,
        reviewer_role=MemberRole.ADMIN,
    )
    stored = datetime.fromisoformat(updated.arguments["scheduled_send_at"])
    assert stored.tzinfo is not None  # normalized to tz-aware UTC


@pytest.mark.asyncio
async def test_schedule_request_past_time_422(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    with pytest.raises(APIError) as exc:
        await approval_service.schedule_request(
            session,
            approval_request_id=req.id,
            reviewer_user_id=user.id,
            scheduled_send_at=past,
            reviewer_role=MemberRole.OWNER,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_schedule_request_already_resolved_409(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    req.status = ApprovalStatus.APPROVED
    await session.commit()

    with pytest.raises(APIError) as exc:
        await approval_service.schedule_request(
            session,
            approval_request_id=req.id,
            reviewer_user_id=user.id,
            scheduled_send_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reviewer_role=MemberRole.OWNER,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_schedule_request_missing_404(session: AsyncSession) -> None:
    user = await _make_user(session)
    await session.commit()
    with pytest.raises(APIError) as exc:
        await approval_service.schedule_request(
            session,
            approval_request_id=uuid.uuid4(),
            reviewer_user_id=user.id,
            scheduled_send_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reviewer_role=MemberRole.OWNER,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_schedule_request_non_resolver_role_403(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    with pytest.raises(APIError) as exc:
        await approval_service.schedule_request(
            session,
            approval_request_id=req.id,
            reviewer_user_id=user.id,
            scheduled_send_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reviewer_role=MemberRole.VIEWER,
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# list_due_scheduled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_due_scheduled_returns_only_due_pending(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)

    now = datetime.now(timezone.utc)

    # Due (past-scheduled, pending).
    due = await _make_pending_reply(session, ws=ws, user=user)
    # Future-scheduled, pending -> NOT due.
    future = await _make_pending_reply(session, ws=ws, user=user)
    # Un-scheduled pending -> excluded.
    unsched = await _make_pending_reply(session, ws=ws, user=user)
    # Due-scheduled but already resolved -> excluded.
    resolved = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    await approval_service.schedule_request(
        session, approval_request_id=due.id, reviewer_user_id=user.id,
        scheduled_send_at=now + timedelta(minutes=1), reviewer_role=MemberRole.OWNER,
    )
    # Force the due one's stored time into the past directly (schedule_request
    # only accepts future times) by rewriting its arguments.
    row = await session.get(ApprovalRequest, due.id)
    new_args = dict(row.arguments)
    new_args["scheduled_send_at"] = (now - timedelta(minutes=5)).isoformat()
    row.arguments = new_args
    await session.commit()

    await approval_service.schedule_request(
        session, approval_request_id=future.id, reviewer_user_id=user.id,
        scheduled_send_at=now + timedelta(hours=3), reviewer_role=MemberRole.OWNER,
    )
    await approval_service.schedule_request(
        session, approval_request_id=resolved.id, reviewer_user_id=user.id,
        scheduled_send_at=now + timedelta(minutes=1), reviewer_role=MemberRole.OWNER,
    )
    r = await session.get(ApprovalRequest, resolved.id)
    r_args = dict(r.arguments)
    r_args["scheduled_send_at"] = (now - timedelta(minutes=5)).isoformat()
    r.arguments = r_args
    r.status = ApprovalStatus.APPROVED
    await session.commit()

    due_list = await approval_service.list_due_scheduled(session, now=now)
    due_ids = {r.id for r in due_list}
    assert due.id in due_ids
    assert future.id not in due_ids
    assert unsched.id not in due_ids
    assert resolved.id not in due_ids


# ---------------------------------------------------------------------------
# send_scheduled_replies (worker cron)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_scheduled_replies_sends_due_leaves_future(
    session: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents import tasks as agent_tasks

    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await _seed_gmail_integration(session, ws=ws, user=user)

    now = datetime.now(timezone.utc)
    due = await _make_pending_reply(session, ws=ws, user=user)
    future = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    await approval_service.schedule_request(
        session, approval_request_id=due.id, reviewer_user_id=user.id,
        scheduled_send_at=now + timedelta(minutes=1), reviewer_role=MemberRole.OWNER,
    )
    row = await session.get(ApprovalRequest, due.id)
    a = dict(row.arguments)
    a["scheduled_send_at"] = (now - timedelta(minutes=5)).isoformat()
    row.arguments = a
    await session.commit()

    await approval_service.schedule_request(
        session, approval_request_id=future.id, reviewer_user_id=user.id,
        scheduled_send_at=now + timedelta(hours=3), reviewer_role=MemberRole.OWNER,
    )

    fake = _FakeGmail(ok=True)

    # Patch the service's Gmail call so send_scheduled_replies (which does NOT
    # accept an injectable) still performs no real network. execute_and_approve
    # defaults to agent_tools.call_provider_api.
    from app.services import agent_tools
    monkeypatch.setattr(agent_tools, "call_provider_api", fake)

    # send_scheduled_replies opens its OWN session via session_scope; point that
    # at our test engine's sessionmaker.
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_scope():
        async with factory() as s:
            yield s

    monkeypatch.setattr("app.db.session.session_scope", _fake_scope)

    sent = await agent_tasks.send_scheduled_replies({})
    assert sent == 1

    # Due one is now approved; future one still pending.
    refreshed_due = await session.get(ApprovalRequest, due.id)
    refreshed_future = await session.get(ApprovalRequest, future.id)
    await session.refresh(refreshed_due)
    await session.refresh(refreshed_future)
    assert refreshed_due.status is ApprovalStatus.APPROVED
    assert refreshed_future.status is ApprovalStatus.PENDING
    assert any(p.endswith("/messages/send") for p in fake.paths())


@pytest.mark.asyncio
async def test_send_scheduled_replies_failure_leaves_pending(
    session: AsyncSession, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents import tasks as agent_tasks

    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await _seed_gmail_integration(session, ws=ws, user=user)

    now = datetime.now(timezone.utc)
    due = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    await approval_service.schedule_request(
        session, approval_request_id=due.id, reviewer_user_id=user.id,
        scheduled_send_at=now + timedelta(minutes=1), reviewer_role=MemberRole.OWNER,
    )
    row = await session.get(ApprovalRequest, due.id)
    a = dict(row.arguments)
    a["scheduled_send_at"] = (now - timedelta(minutes=5)).isoformat()
    row.arguments = a
    await session.commit()

    fake = _FakeGmail(ok=False)  # Gmail send fails.
    from app.services import agent_tools
    monkeypatch.setattr(agent_tools, "call_provider_api", fake)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_scope():
        async with factory() as s:
            yield s

    monkeypatch.setattr("app.db.session.session_scope", _fake_scope)

    sent = await agent_tasks.send_scheduled_replies({})
    assert sent == 0  # nothing succeeded; sweep did not abort

    refreshed = await session.get(ApprovalRequest, due.id)
    await session.refresh(refreshed)
    assert refreshed.status is ApprovalStatus.PENDING  # left pending for retry
