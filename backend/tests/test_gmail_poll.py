"""Tests for the Gmail poll cost-control helpers (app.services.gmail_poll).

Two layers, mirroring test_approval_execution.py:

1. **DB-free unit tests** for :func:`list_unread_ids` with an injected fake
   ``call_provider_api`` (parses ids; fail-soft on errors). NO Docker.
2. **DB-backed tests** against a throwaway ``postgres:18.6-alpine`` with
   migrations applied (so the ``processed_messages`` table exists), asserting:
   - ``filter_new_ids`` excludes ids already in ``processed_messages`` for the
     integration (and is tenant-scoped by integration_id);
   - ``record_seen`` is idempotent (re-inserting the same ids is a no-op via the
     UNIQUE constraint);
   - ``record_seen_one`` records a single id;
   - ``poll_integrations`` records new ids + enqueues exactly one scoped run, and
     an already-seen unread id is NOT re-enqueued on the next poll.

The Gmail HTTP layer + credential resolution are injected as fakes so NO real
network or Bedrock is used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db.models import (
    IntegrationCategory,
    ProcessedMessage,
    User,
    Workspace,
)
from app.services import gmail_poll

# ---------------------------------------------------------------------------
# DB-free unit tests: list_unread_ids
# ---------------------------------------------------------------------------


def _list_response(ids: list[str]) -> dict:
    return {
        "status_code": 200,
        "ok": True,
        "body": {"messages": [{"id": mid} for mid in ids]},
    }


@pytest.mark.asyncio
async def test_list_unread_ids_parses_ids() -> None:
    async def _call(**kwargs):
        assert kwargs["method"] == "GET"
        assert kwargs["path"] == "/gmail/v1/users/me/messages"
        assert kwargs["query"]["q"] == "is:unread"
        return _list_response(["a", "b", "c"])

    ids = await gmail_poll.list_unread_ids(
        credentials={"access_token": "t"}, config={}, call_provider_api=_call
    )
    assert ids == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_list_unread_ids_empty_inbox() -> None:
    async def _call(**kwargs):
        return {"status_code": 200, "ok": True, "body": {}}  # no "messages" key

    ids = await gmail_poll.list_unread_ids(
        credentials={}, config={}, call_provider_api=_call
    )
    assert ids == []


@pytest.mark.asyncio
async def test_list_unread_ids_failsoft_on_error() -> None:
    async def _call(**kwargs):
        return {"error": "request_failed", "message": "boom"}

    ids = await gmail_poll.list_unread_ids(
        credentials={}, config={}, call_provider_api=_call
    )
    assert ids == []


# ---------------------------------------------------------------------------
# DB-backed setup (skip if Docker unavailable) — mirrors test_approval_execution
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_gmail_poll_test"
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


async def _make_integration(session: AsyncSession, *, ws: Workspace, user: User):
    from app.db.models import Integration

    integ = Integration(
        workspace_id=ws.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
    )
    session.add(integ)
    await session.flush()
    return integ


async def _clear_integrations_and_seen(session: AsyncSession) -> None:
    """Remove all Integration + ProcessedMessage rows from the shared test DB.

    poll_integrations sweeps EVERY active integration in the database, so a
    behavior test must start from a clean slate — otherwise integrations left by
    a previous test (with different processed_messages state) get swept too and
    skew the enqueue count. CASCADE handles nothing here since we delete the
    integrations directly; processed_messages are cleared first to satisfy the FK.
    """
    from sqlalchemy import delete

    from app.db.models import Integration

    await session.execute(delete(ProcessedMessage))
    await session.execute(delete(Integration))
    await session.commit()


# ---------------------------------------------------------------------------
# DB-backed tests: filter_new_ids / record_seen / record_seen_one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_new_ids_excludes_already_seen(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    integ = await _make_integration(session, ws=ws, user=user)
    await session.commit()

    # Mark m1 + m3 as already seen.
    await gmail_poll.record_seen(
        session, workspace_id=ws.id, integration_id=integ.id, ids=["m1", "m3"]
    )
    await session.commit()

    new = await gmail_poll.filter_new_ids(
        session, integration_id=integ.id, ids=["m1", "m2", "m3", "m4"]
    )
    # Only genuinely-new ids remain, in input order.
    assert new == ["m2", "m4"]


@pytest.mark.asyncio
async def test_filter_new_ids_is_tenant_scoped(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    integ_a = await _make_integration(session, ws=ws, user=user)
    integ_b = await _make_integration(session, ws=ws, user=user)
    await session.commit()

    # m1 seen for integration A only.
    await gmail_poll.record_seen(
        session, workspace_id=ws.id, integration_id=integ_a.id, ids=["m1"]
    )
    await session.commit()

    # For integration B, m1 is still "new" (isolation by integration_id).
    new_b = await gmail_poll.filter_new_ids(
        session, integration_id=integ_b.id, ids=["m1", "m2"]
    )
    assert new_b == ["m1", "m2"]


@pytest.mark.asyncio
async def test_record_seen_is_idempotent(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    integ = await _make_integration(session, ws=ws, user=user)
    await session.commit()

    await gmail_poll.record_seen(
        session, workspace_id=ws.id, integration_id=integ.id, ids=["dup", "dup", "x"]
    )
    await session.commit()
    # Re-insert the same ids: UNIQUE(integration_id, provider_message_id) => no-op.
    await gmail_poll.record_seen(
        session, workspace_id=ws.id, integration_id=integ.id, ids=["dup", "x"]
    )
    await session.commit()

    rows = (
        await session.execute(
            select(ProcessedMessage).where(
                ProcessedMessage.integration_id == integ.id
            )
        )
    ).scalars().all()
    stored = sorted(r.provider_message_id for r in rows)
    assert stored == ["dup", "x"]  # exactly one row per id


@pytest.mark.asyncio
async def test_record_seen_one(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    integ = await _make_integration(session, ws=ws, user=user)
    await session.commit()

    n = await gmail_poll.record_seen_one(
        session, workspace_id=ws.id, integration_id=integ.id, message_id="only"
    )
    assert n == 1
    await session.commit()
    # Falsy id is a no-op.
    assert (
        await gmail_poll.record_seen_one(
            session, workspace_id=ws.id, integration_id=integ.id, message_id=None
        )
        == 0
    )
    remaining = await gmail_poll.filter_new_ids(
        session, integration_id=integ.id, ids=["only", "fresh"]
    )
    assert remaining == ["fresh"]


# ---------------------------------------------------------------------------
# DB-backed behavior tests: poll_integrations pre-check + record + enqueue
# ---------------------------------------------------------------------------


def _session_scope_factory(sess: AsyncSession):
    """An async-context-manager factory that always yields the SAME test session.

    poll_integrations opens ``session_scope`` once to list integrations and again
    (nested) for the Gmail pre-check; both reuse this one migrated test session so
    the processed_messages rows are visible across the calls. We DON'T close it
    here (the fixture owns it); commit so the rows are durable like production.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _factory():
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise

    return _factory


def _gmail_seams(unread_ids: list[str]):
    async def _resolve(_session, *, workspace_id):
        return (uuid.uuid4(), {"access_token": "t"}, {})

    async def _call(**kwargs):
        return _list_response(unread_ids)

    return _resolve, _call


@pytest.mark.asyncio
async def test_poll_records_new_and_enqueues_one_scoped_run(
    session: AsyncSession, monkeypatch
) -> None:
    from app.agents import tasks
    import app.db.session as dbsess
    import app.services.job_queue_bridge as jq

    # poll_integrations reads ALL active integrations in the DB; isolate this
    # test from rows other tests left in the shared (module-scoped) database so
    # the sweep only sees our integration.
    await _clear_integrations_and_seen(session)
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    integ = await _make_integration(session, ws=ws, user=user)
    # Force the poll-eligible path for gmail-on-localhost.
    from app.services.webhook_subscriptions import WEBHOOK_STATUS_KEY

    integ.config = {WEBHOOK_STATUS_KEY: "poll"}
    await session.commit()

    scope = _session_scope_factory(session)
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(dbsess, "session_scope", scope)

    enqueued: list[dict] = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)

    resolve, call = _gmail_seams(["n1", "n2"])
    count = await tasks.poll_integrations(
        {}, resolve_gmail_credentials=resolve, call_provider_api=call
    )

    assert count == 1
    assert len(enqueued) == 1
    # The enqueued run's prompt is scoped to EXACTLY the new ids.
    prompt = enqueued[0]["prompt"]
    assert "n1" in prompt and "n2" in prompt
    # And both ids are now recorded as seen for this integration.
    remaining = await gmail_poll.filter_new_ids(
        session, integration_id=integ.id, ids=["n1", "n2"]
    )
    assert remaining == []


@pytest.mark.asyncio
async def test_poll_already_seen_unread_not_reenqueued(
    session: AsyncSession, monkeypatch
) -> None:
    from app.agents import tasks
    import app.db.session as dbsess
    import app.services.job_queue_bridge as jq

    # Isolate from other tests' integrations in the shared DB (see above).
    await _clear_integrations_and_seen(session)
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    integ = await _make_integration(session, ws=ws, user=user)
    from app.services.webhook_subscriptions import WEBHOOK_STATUS_KEY

    integ.config = {WEBHOOK_STATUS_KEY: "poll"}
    # Pre-record the only unread id as already seen.
    await gmail_poll.record_seen(
        session, workspace_id=ws.id, integration_id=integ.id, ids=["seen1"]
    )
    await session.commit()

    scope = _session_scope_factory(session)
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(dbsess, "session_scope", scope)

    enqueued: list[dict] = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)

    # The inbox still shows the SAME unread id — but it was already handled.
    resolve, call = _gmail_seams(["seen1"])
    count = await tasks.poll_integrations(
        {}, resolve_gmail_credentials=resolve, call_provider_api=call
    )

    assert count == 0
    assert enqueued == []
