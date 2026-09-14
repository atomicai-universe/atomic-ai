"""Tests for super-admin session inspection and the emergency kill-switch (task 14.3).

These drive :mod:`app.api.admin`'s ``GET /api/v1/admin/sessions`` and
``POST /api/v1/admin/sessions/{id}/kill`` against a *real*, throwaway
``postgres:18.6-alpine`` (on a non-default host port) with the project's Alembic
migration applied, so the super-admin gate, the cross-workspace live-session
listing (with reasoning traces), and the kill-switch (terminate + cancel pending
approvals + audit) are exercised end to end against a real database. The suite
skips gracefully when Docker is unavailable.

The app is built with only the admin router mounted and two dependencies
overridden:

- ``get_session`` -> a per-request session bound to the migrated test database
  using a fresh ``NullPool`` engine, so every request runs its connection on the
  request's own event loop (avoiding asyncpg cross-loop errors under
  ``httpx.ASGITransport``); and
- ``app.api.deps.require_session`` -> a per-test override injecting a
  :class:`~app.core.tenancy.RequestContext` for either a super admin or an
  ordinary user, so the ``require_superadmin`` gate is exercised directly.

Assertions cover:

- a non-super-admin is rejected **403** on the listing and kill endpoints
  (Req 12.1, 12.2 via the gate);
- the listing returns the ``running`` session *with* its ``execution_logs``
  trace and excludes completed/terminated sessions (Req 13.1);
- kill sets the session to ``terminated``, moves its ``pending`` approvals to
  ``rejected`` while leaving an already-approved one untouched (Req 13.2, 13.3),
  and writes an ``admin.session_killed`` audit row naming the acting super admin
  and the targeted session (Req 13.4);
- kill on a non-running session -> **409**; kill on a missing session -> **404**.

Requirements: 13.1, 13.2, 13.3, 13.4.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.admin import router as admin_router
from app.api.deps import require_session
from app.core.errors import install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import (
    AgentSession,
    AgentSessionStatus,
    ApprovalRequest,
    ApprovalStatus,
    AuthProvider,
    MemberRole,
    SystemAuditLog,
    User,
    Workspace,
    WorkspaceMember,
)
from app.db.session import get_session

# ---------------------------------------------------------------------------
# Throwaway Postgres infrastructure (skips without Docker)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_admin_sessions_test"
_HOST_PORT = 55456  # non-default so we never touch another database
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105 - throwaway container credential
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}"
    f"@localhost:{_HOST_PORT}/{_PG_DB}"
)


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
                [
                    "docker", "exec", _CONTAINER_NAME,
                    "pg_isready", "-U", _PG_USER, "-d", _PG_DB,
                ],
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
        "ENCRYPTION_KEY": "test-encryption-key-value-0123456789",
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
    """Start a throwaway Postgres, apply migrations, yield the DSN, tear down."""
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


# ---------------------------------------------------------------------------
# App + context-injection helpers
# ---------------------------------------------------------------------------


def _build_app(dsn: str) -> FastAPI:
    """Build an app with the admin router and a per-request DB session override.

    The ``get_session`` override creates a fresh ``NullPool`` engine per request
    so each connection is opened and closed on the same event loop the request
    runs on, avoiding asyncpg "got Future attached to a different loop" errors
    under ``httpx.ASGITransport``.
    """
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(admin_router)

    async def _get_session_override() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(dsn, future=True, poolclass=NullPool)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        try:
            async with factory() as sess:
                yield sess
        finally:
            await engine.dispose()

    app.dependency_overrides[get_session] = _get_session_override
    return app


def _act_as(
    app: FastAPI,
    *,
    user_id: uuid.UUID,
    is_superadmin: bool,
) -> None:
    """Override ``require_session`` to inject a context for ``user_id``."""
    ctx = RequestContext(
        user_id=user_id,
        active_workspace_id=None,
        is_superadmin=is_superadmin,
        roles={},
    )

    async def _require_session_override() -> RequestContext:
        return ctx

    app.dependency_overrides[require_session] = _require_session_override


@pytest_asyncio.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A NullPool-backed session for test setup/verification on the test loop."""
    engine = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_client(migrated_database: str):
    """Yield an httpx AsyncClient bound to the app, plus the app for overrides."""
    import httpx

    app = _build_app(migrated_database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        yield app, client


# ---------------------------------------------------------------------------
# Seed helpers (create users first; workspace.created_by_user_id is NOT NULL)
# ---------------------------------------------------------------------------


async def _make_user(
    session: AsyncSession,
    name: str,
    *,
    is_superadmin: bool = False,
    provider: AuthProvider = AuthProvider.GOOGLE,
) -> User:
    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
        name=name,
        auth_provider=provider,
        is_superadmin=is_superadmin,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_workspace(
    session: AsyncSession, name: str, *, owner: User
) -> Workspace:
    ws = Workspace(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        created_by_user_id=owner.id,
    )
    session.add(ws)
    await session.flush()
    session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role=MemberRole.OWNER)
    )
    await session.flush()
    return ws


async def _make_agent_session(
    session: AsyncSession,
    *,
    workspace: Workspace,
    user: User,
    status: AgentSessionStatus = AgentSessionStatus.RUNNING,
    execution_logs: object = None,
) -> AgentSession:
    agent_session = AgentSession(
        workspace_id=workspace.id,
        triggered_by_user_id=user.id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        execution_logs=execution_logs,
        status=status,
    )
    session.add(agent_session)
    await session.flush()
    return agent_session


async def _make_approval(
    session: AsyncSession,
    *,
    workspace: Workspace,
    agent_session: AgentSession,
    user: User,
    status: ApprovalStatus,
    reviewer: User | None = None,
) -> ApprovalRequest:
    request = ApprovalRequest(
        workspace_id=workspace.id,
        agent_session_id=agent_session.id,
        triggered_by_user_id=user.id,
        reviewed_by_user_id=reviewer.id if reviewer is not None else None,
        tool_name="dangerous.tool",
        arguments={"k": "v"},
        status=status,
    )
    session.add(request)
    await session.flush()
    return request


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_superadmin_is_forbidden_on_session_endpoints(
    app_client, db_session
) -> None:
    """A non-super-admin gets 403 on listing and kill (Req 12.1, 12.2)."""
    app, client = app_client
    ordinary = await _make_user(db_session, "Ordinary")
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "NW", owner=owner)
    agent_session = await _make_agent_session(db_session, workspace=ws, user=owner)
    await db_session.commit()

    _act_as(app, user_id=ordinary.id, is_superadmin=False)

    resp = await client.get("/api/v1/admin/sessions")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    resp = await client.post(f"/api/v1/admin/sessions/{agent_session.id}/kill")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_list_sessions_returns_running_with_traces_only(
    app_client, db_session
) -> None:
    """Listing returns the running session with its trace; terminal ones are excluded (Req 13.1)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    owner_a = await _make_user(db_session, "OwnerA")
    owner_b = await _make_user(db_session, "OwnerB", provider=AuthProvider.GITHUB)
    ws_a = await _make_workspace(db_session, "Alpha", owner=owner_a)
    ws_b = await _make_workspace(db_session, "Beta", owner=owner_b)

    trace = [{"step": 1, "thought": "inspect me"}, {"step": 2, "action": "call"}]
    running = await _make_agent_session(
        db_session, workspace=ws_a, user=owner_a, execution_logs=trace
    )
    # A running session in another workspace to prove the listing is global.
    running_b = await _make_agent_session(
        db_session, workspace=ws_b, user=owner_b, execution_logs={"note": "b"}
    )
    # Terminal sessions must not appear.
    completed_session = await _make_agent_session(
        db_session, workspace=ws_a, user=owner_a,
        status=AgentSessionStatus.COMPLETED,
    )
    terminated_session = await _make_agent_session(
        db_session, workspace=ws_a, user=owner_a,
        status=AgentSessionStatus.TERMINATED,
    )
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.get("/api/v1/admin/sessions")
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["sessions"]
    by_id = {s["id"]: s for s in sessions}

    # This test's two running sessions (spanning both workspaces) are listed.
    # The listing is global and the test database is shared across the module,
    # so other tests' running sessions may also appear; assert our two are
    # present as a subset rather than exact global equality.
    assert {str(running.id), str(running_b.id)} <= set(by_id)
    # This test's terminal sessions are never listed.
    assert str(completed_session.id) not in by_id
    assert str(terminated_session.id) not in by_id

    entry = by_id[str(running.id)]
    assert entry["status"] == "running"
    assert entry["workspace_id"] == str(ws_a.id)
    assert entry["triggered_by_user_id"] == str(owner_a.id)
    # Reasoning trace is included (Req 13.1).
    assert entry["execution_logs"] == trace


@pytest.mark.asyncio
async def test_kill_terminates_session_cancels_pending_and_audits(
    app_client, db_session
) -> None:
    """Kill sets TERMINATED, rejects pending approvals (leaving resolved ones), audits (Req 13.2-13.4)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "Alpha", owner=owner)
    agent_session = await _make_agent_session(
        db_session, workspace=ws, user=owner, execution_logs=[{"s": 1}]
    )
    pending_1 = await _make_approval(
        db_session, workspace=ws, agent_session=agent_session, user=owner,
        status=ApprovalStatus.PENDING,
    )
    pending_2 = await _make_approval(
        db_session, workspace=ws, agent_session=agent_session, user=owner,
        status=ApprovalStatus.PENDING,
    )
    already_approved = await _make_approval(
        db_session, workspace=ws, agent_session=agent_session, user=owner,
        status=ApprovalStatus.APPROVED, reviewer=owner,
    )
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.post(f"/api/v1/admin/sessions/{agent_session.id}/kill")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_session_id"] == str(agent_session.id)
    assert body["status"] == "terminated"
    assert body["cancelled_approvals"] == 2

    # Session is terminated (Req 13.2).
    status = await db_session.scalar(
        sa.select(AgentSession.status).where(AgentSession.id == agent_session.id)
    )
    assert status is AgentSessionStatus.TERMINATED

    # Pending approvals are now rejected; the already-approved one is untouched (Req 13.3).
    p1 = await db_session.scalar(
        sa.select(ApprovalRequest.status).where(ApprovalRequest.id == pending_1.id)
    )
    p2 = await db_session.scalar(
        sa.select(ApprovalRequest.status).where(ApprovalRequest.id == pending_2.id)
    )
    approved = await db_session.scalar(
        sa.select(ApprovalRequest.status).where(
            ApprovalRequest.id == already_approved.id
        )
    )
    assert p1 is ApprovalStatus.REJECTED
    assert p2 is ApprovalStatus.REJECTED
    assert approved is ApprovalStatus.APPROVED

    # An audit row records the acting super admin and the targeted session (Req 13.4).
    audit = (
        await db_session.execute(
            sa.select(SystemAuditLog).where(
                SystemAuditLog.action == "admin.session_killed"
            )
        )
    ).scalar_one()
    assert audit.user_id == admin.id
    assert audit.workspace_id == ws.id
    assert audit.log_metadata == {"agent_session_id": str(agent_session.id)}


@pytest.mark.asyncio
async def test_kill_on_non_running_session_conflicts(app_client, db_session) -> None:
    """Killing a session that is not running returns 409 (documented choice)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "Alpha", owner=owner)
    completed = await _make_agent_session(
        db_session, workspace=ws, user=owner, status=AgentSessionStatus.COMPLETED
    )
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.post(f"/api/v1/admin/sessions/{completed.id}/kill")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"

    # No audit row was written for THIS non-running session. The audit action
    # is global and the module's test database is shared, so scope the check to
    # this test's workspace to assert nothing was recorded for the 409 path.
    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(SystemAuditLog)
        .where(
            SystemAuditLog.action == "admin.session_killed",
            SystemAuditLog.workspace_id == ws.id,
        )
    )
    assert count == 0


@pytest.mark.asyncio
async def test_kill_on_missing_session_not_found(app_client, db_session) -> None:
    """Killing a session that does not exist returns 404."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.post(f"/api/v1/admin/sessions/{uuid.uuid4()}/kill")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_sweep_stale_running_sessions_terminates_old_only(db_session) -> None:
    """sweep_stale_running_sessions terminates OLD running sessions only.

    A running session older than the threshold is orphaned (worker died mid-run)
    and must be marked TERMINATED; a recent running session (a genuinely
    in-flight run) is left alone; terminal sessions are untouched.
    """
    from datetime import timedelta

    from app.services import admin_service

    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "Alpha", owner=owner)

    old_running = await _make_agent_session(db_session, workspace=ws, user=owner)
    recent_running = await _make_agent_session(db_session, workspace=ws, user=owner)
    completed = await _make_agent_session(
        db_session, workspace=ws, user=owner, status=AgentSessionStatus.COMPLETED
    )
    await db_session.flush()

    now = datetime.now(timezone.utc)
    # Backdate the "old" one well beyond the threshold; keep the other fresh.
    old_running.created_at = now - timedelta(hours=2)
    recent_running.created_at = now - timedelta(minutes=1)
    await db_session.flush()

    swept = await admin_service.sweep_stale_running_sessions(db_session, now=now)
    assert swept == 1

    # The old running one is terminated with a stale_swept marker.
    refreshed_old = await db_session.get(AgentSession, old_running.id)
    assert refreshed_old.status is AgentSessionStatus.TERMINATED
    assert isinstance(refreshed_old.execution_logs, dict)
    assert refreshed_old.execution_logs.get("cleanup", {}).get("stale_swept") is True

    # The recent running one is left running.
    refreshed_recent = await db_session.get(AgentSession, recent_running.id)
    assert refreshed_recent.status is AgentSessionStatus.RUNNING

    # A terminal session is untouched.
    refreshed_completed = await db_session.get(AgentSession, completed.id)
    assert refreshed_completed.status is AgentSessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_sweep_stale_running_sessions_noop_when_none_old(db_session) -> None:
    """No old running sessions -> sweep returns 0 and changes nothing."""
    from app.services import admin_service

    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "Beta", owner=owner)
    recent = await _make_agent_session(db_session, workspace=ws, user=owner)
    await db_session.flush()

    swept = await admin_service.sweep_stale_running_sessions(db_session)
    assert swept == 0
    refreshed = await db_session.get(AgentSession, recent.id)
    assert refreshed.status is AgentSessionStatus.RUNNING
