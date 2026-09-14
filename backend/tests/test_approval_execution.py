"""Tests for approval EDIT / REGENERATE / EXECUTE (PART 3 + PART 4).

Two layers:

1. **DB-free unit tests** for the pure helpers
   (:func:`_decode_reply_fields`, :func:`_rebuild_reply_arguments`,
   :func:`_gmail_call_ok`) — no Docker required.
2. **DB-backed tests** against a throwaway ``postgres:18.6-alpine`` (non-default
   port) with migrations applied, seeding a real Gmail Integration (encrypted)
   and a pending ``gmail_reply`` approval, then asserting:

   - ``edit_request`` rebuilds a clean draft from edited fields (round-trip),
     validates empty to/body -> 422;
   - ``execute_and_approve_request(save_to_draft)`` calls the drafts endpoint,
     marks the source message read, and marks the approval approved;
   - ``execute_and_approve_request(send)`` calls messages/send + marks read;
   - a Gmail failure leaves the approval PENDING and raises 502;
   - ``regenerate_request`` (with an injected body generator) rebuilds the draft.

   The Gmail HTTP layer is injected as a fake so NO real network is used, and
   ``oauth_refresh.ensure_access_token`` is monkeypatched to a passthrough.

Requirements: PART 3, PART 4 (Req 10.3, 10.5, 10.7).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from email import policy
from email.parser import BytesParser
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
    User,
    Workspace,
)
from app.services import approval_service, gmail_message, integration_vault

# ---------------------------------------------------------------------------
# DB-free unit tests for the pure helpers
# ---------------------------------------------------------------------------


def _decode(raw: str):
    padded = raw + "=" * (-len(raw) % 4)
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(padded))


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


def test_decode_reply_fields_extracts_everything() -> None:
    fields = approval_service._decode_reply_fields(_sample_arguments())
    assert fields["to"] == "orig@sender.com"
    assert fields["subject"] == "Re: Question"
    assert fields["body"].strip() == "Original reply body."
    assert fields["in_reply_to"] == "<abc@mail>"
    assert fields["thread_id"] == "THREAD1"
    assert fields["source_message_id"] == "MSG123"


def test_rebuild_reply_arguments_is_clean_and_preserves_ids() -> None:
    args = approval_service._rebuild_reply_arguments(
        to="a@b.com",
        subject="Re: Question",
        body="A brand new reply.",
        in_reply_to="<abc@mail>",
        thread_id="THREAD1",
        cc=None,
        source_message_id="MSG123",
    )
    assert args["source_message_id"] == "MSG123"
    assert args["thread_id"] == "THREAD1"
    msg = _decode(args["body"]["message"]["raw"])
    assert msg["Content-Transfer-Encoding"].lower() == "base64"
    assert msg.get_content().strip() == "A brand new reply."
    assert args["body"]["message"]["threadId"] == "THREAD1"


@pytest.mark.parametrize(
    ("result", "ok"),
    [
        ({"status_code": 200, "ok": True, "body": {}}, True),
        ({"status_code": 201, "ok": True, "body": {}}, True),
        ({"status_code": 401, "ok": False, "body": {}}, False),
        ({"error": "request_failed", "message": "boom"}, False),
        ({"status_code": 500, "ok": False}, False),
        ("not-a-dict", False),
    ],
)
def test_gmail_call_ok(result, ok) -> None:
    assert approval_service._gmail_call_ok(result) is ok


# ---------------------------------------------------------------------------
# Fake Gmail HTTP layer (records calls; NO network)
# ---------------------------------------------------------------------------


class _FakeGmail:
    """Records call_provider_api invocations and returns a scripted result."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    async def __call__(
        self,
        *,
        provider_name: str,
        credentials: dict,
        config: dict,
        method: str,
        path: str,
        query=None,
        body=None,
    ) -> dict:
        self.calls.append({"method": method, "path": path, "body": body})
        if self.ok:
            return {"status_code": 200, "ok": True, "body": {"id": "created"}}
        return {"status_code": 500, "ok": False, "body": {"error": "boom"}}

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


# ---------------------------------------------------------------------------
# DB-backed setup (skip if Docker unavailable)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_approval_exec_test"
_HOST_PORT = 55459  # non-default so we never touch another database
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@localhost:{_HOST_PORT}/{_PG_DB}"
)

# A stable 32-byte key so the Integration_Vault can encrypt/decrypt in tests.
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
    """Provide a valid config env (the shared conftest strips it) + reset caches.

    The autouse ``_clear_config_env`` fixture removes all config env vars before
    each test, but this test process needs the Encryption_Service (which reads
    ``ENCRYPTION_KEY`` via cached settings) to store/decrypt the seeded Gmail
    integration. We set a known env and reset the settings + encryption caches
    so the key is honored, then reset again on teardown.
    """
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
    # Install a process-wide EncryptionService built from a REAL Fernet key so
    # both the seed (store) and the execution path (use_credential) round-trip.
    _enc.set_encryption_service(_enc.EncryptionService(Fernet.generate_key()))
    try:
        yield
    finally:
        _config.get_settings.cache_clear()
        _enc.reset_encryption_service()


@pytest.fixture(autouse=True)
def _no_oauth_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough ensure_access_token so no token endpoint is contacted."""
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


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_request_rebuilds_clean_draft(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    updated = await approval_service.edit_request(
        session,
        approval_request_id=req.id,
        subject="Edited subject",
        body="An edited reply body.",
    )
    assert updated.status is ApprovalStatus.PENDING
    args = updated.arguments
    assert args["source_message_id"] == "MSG123"
    msg = _decode(args["body"]["message"]["raw"])
    assert msg.get_content().strip() == "An edited reply body."
    # This is a reply (in_reply_to preserved), so a single Re: prefix is kept.
    assert msg["Subject"] == "Re: Edited subject"
    # Recipient preserved from original.
    assert msg["To"] == "orig@sender.com"


@pytest.mark.asyncio
async def test_edit_request_rejects_empty_body(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    with pytest.raises(APIError) as exc:
        await approval_service.edit_request(
            session, approval_request_id=req.id, body="   "
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_approve_draft_creates_draft_marks_read_and_approves(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await _seed_gmail_integration(session, ws=ws, user=user)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    fake = _FakeGmail(ok=True)
    updated = await approval_service.execute_and_approve_request(
        session,
        approval_request_id=req.id,
        reviewer_user_id=user.id,
        execution_action="save_to_draft",
        call_provider_api=fake,
    )
    assert updated.status is ApprovalStatus.APPROVED
    paths = fake.paths()
    assert "/gmail/v1/users/me/drafts" in paths
    assert "/gmail/v1/users/me/messages/MSG123/modify" in paths
    # The read-marking call removes the UNREAD label.
    modify = next(c for c in fake.calls if "modify" in c["path"])
    assert modify["body"] == {"removeLabelIds": ["UNREAD"]}


@pytest.mark.asyncio
async def test_approve_send_sends_marks_read_and_approves(
    session: AsyncSession,
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await _seed_gmail_integration(session, ws=ws, user=user)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    fake = _FakeGmail(ok=True)
    updated = await approval_service.execute_and_approve_request(
        session,
        approval_request_id=req.id,
        reviewer_user_id=user.id,
        execution_action="send",
        call_provider_api=fake,
    )
    assert updated.status is ApprovalStatus.APPROVED
    send_call = next(c for c in fake.calls if c["path"].endswith("/messages/send"))
    assert "raw" in send_call["body"]
    assert send_call["body"]["threadId"] == "THREAD1"
    assert "/gmail/v1/users/me/messages/MSG123/modify" in fake.paths()


@pytest.mark.asyncio
async def test_gmail_failure_leaves_pending(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await _seed_gmail_integration(session, ws=ws, user=user)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    fake = _FakeGmail(ok=False)
    with pytest.raises(APIError) as exc:
        await approval_service.execute_and_approve_request(
            session,
            approval_request_id=req.id,
            reviewer_user_id=user.id,
            execution_action="save_to_draft",
            call_provider_api=fake,
        )
    assert exc.value.status_code == 502
    refreshed = await session.get(ApprovalRequest, req.id)
    assert refreshed.status is ApprovalStatus.PENDING
    # No read-marking happened because the draft failed.
    assert all("modify" not in p for p in fake.paths())


@pytest.mark.asyncio
async def test_regenerate_request_rebuilds_with_new_body(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    async def _gen(fields):
        return "A regenerated, different reply."

    updated = await approval_service.regenerate_request(
        session, approval_request_id=req.id, generate_body=_gen
    )
    assert updated.status is ApprovalStatus.PENDING
    msg = _decode(updated.arguments["body"]["message"]["raw"])
    assert msg.get_content().strip() == "A regenerated, different reply."
    # Source ids preserved so the guard/execution keep working.
    assert updated.arguments["source_message_id"] == "MSG123"
