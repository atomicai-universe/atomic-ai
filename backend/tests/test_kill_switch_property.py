"""Property-based test for the emergency kill-switch (task 14.4).

Implements **Property 28: Emergency kill-switch** from the design.

# Feature: atomic-ai, Property 28: For all running agent sessions, activating
# the kill-switch sets the session status to a terminated terminal value, leaves
# none of the session's approval requests in `pending` status, and produces an
# Audit_Log entry recording the acting super admin and the targeted session.

**Validates: Requirements 13.2, 13.3, 13.4**

- Req 13.2: killing a ``running`` :class:`~app.db.models.AgentSession` sets its
  status to the terminal :attr:`AgentSessionStatus.TERMINATED`.
- Req 13.3: every :class:`~app.db.models.ApprovalRequest` for that session still
  ``pending`` is moved to ``rejected``; already-resolved requests
  (``approved``/``rejected``) are left exactly as they were.
- Req 13.4: exactly one ``admin.session_killed`` audit row is written for the
  session's workspace, naming the acting super admin (``user_id``) and the
  targeted session (``metadata.agent_session_id``).

Because the effect is a real multi-row database mutation (session status +
approval status transitions + an audit insert with a DB-side append-only
trigger), this property is exercised against a *real*, throwaway
``postgres:18.6-alpine`` with the project's Alembic migration applied — the same
throwaway-container pattern used by ``tests/test_admin_sessions.py`` (a distinct
container name and host port so the two suites never collide). The suite skips
gracefully when Docker is unavailable.

Rather than a Hypothesis generator, the "for all running sessions" universal is
covered by **parametrizing over representative instances** of the input space
that matters for the effect: the number of *pending* approvals ``P`` and the
number of *already-resolved* approvals ``R`` bound to the same running session.
Each ``(P, R)`` case builds an independent running session (in its own freshly
seeded workspace) so the shared module database never leaks across cases, and
every assertion is scoped to that case's own session/workspace.

- ``P in {0, 1, 3}`` — no pending, a single pending, several pending. Covers the
  cancel-nothing, cancel-one, and cancel-many shapes of Req 13.3, and the
  ``KillResult.cancelled_approvals == P`` count.
- ``R in {0, 2}`` — no pre-resolved requests, and a mix of one ``approved`` +
  one ``rejected``. Covers "leave already-resolved untouched" (Req 13.3) with
  both terminal statuses represented.

Guard examples pin the documented non-happy paths: killing a non-running
(``completed``) session raises a 409 :class:`~app.core.errors.APIError` and
writes no ``session_killed`` audit row for that workspace; killing a missing id
raises 404.

Requirements: 13.2, 13.3, 13.4.
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
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.errors import APIError
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
from app.services import admin_service

# ---------------------------------------------------------------------------
# Throwaway Postgres infrastructure (skips without Docker)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_kill_switch_property_test"
_HOST_PORT = 55458  # unique, non-default host port for this suite
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


@pytest_asyncio.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A NullPool-backed session so each connection runs on the test loop."""
    engine = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


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
) -> AgentSession:
    agent_session = AgentSession(
        workspace_id=workspace.id,
        triggered_by_user_id=user.id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        execution_logs=[{"step": 1, "thought": "running"}],
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


# The (P, R) instances that stand in for "all running sessions": P pending
# approvals (cancel-none / cancel-one / cancel-many) crossed with R already
# resolved approvals (none / a mix of approved + rejected).
_PENDING_COUNTS = [0, 1, 3]
_RESOLVED_COUNTS = [0, 2]
_CASES = [(p, r) for p in _PENDING_COUNTS for r in _RESOLVED_COUNTS]


# ---------------------------------------------------------------------------
# Property 28
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("pending_count", "resolved_count"), _CASES)
async def test_kill_switch_property(
    db_session: AsyncSession, pending_count: int, resolved_count: int
) -> None:
    """Property 28: killing a running session terminates it, rejects every
    pending approval, leaves resolved ones untouched, and audits the kill
    exactly once (Req 13.2, 13.3, 13.4)."""
    # --- Seed an isolated running session in its own workspace --------------
    superadmin = await _make_user(db_session, "SuperAdmin", is_superadmin=True)
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "KillWS", owner=owner)
    agent_session = await _make_agent_session(db_session, workspace=ws, user=owner)

    pending = [
        await _make_approval(
            db_session, workspace=ws, agent_session=agent_session, user=owner,
            status=ApprovalStatus.PENDING,
        )
        for _ in range(pending_count)
    ]

    # A mix of already-resolved requests: alternate approved / rejected so both
    # terminal statuses are represented when resolved_count > 0.
    resolved: list[tuple[uuid.UUID, ApprovalStatus]] = []
    for i in range(resolved_count):
        original = (
            ApprovalStatus.APPROVED if i % 2 == 0 else ApprovalStatus.REJECTED
        )
        req = await _make_approval(
            db_session, workspace=ws, agent_session=agent_session, user=owner,
            status=original, reviewer=owner,
        )
        resolved.append((req.id, original))

    await db_session.commit()

    # --- Activate the kill-switch -------------------------------------------
    result = await admin_service.kill_session(
        db_session,
        agent_session_id=agent_session.id,
        acting_superadmin_id=superadmin.id,
    )
    await db_session.commit()

    # --- (13.2) The session is terminated -----------------------------------
    status = await db_session.scalar(
        sa.select(AgentSession.status).where(AgentSession.id == agent_session.id)
    )
    assert status is AgentSessionStatus.TERMINATED
    assert result.status is AgentSessionStatus.TERMINATED
    assert result.agent_session_id == agent_session.id

    # --- (13.3) Every pending approval is now rejected ----------------------
    for req in pending:
        now = await db_session.scalar(
            sa.select(ApprovalRequest.status).where(ApprovalRequest.id == req.id)
        )
        assert now is ApprovalStatus.REJECTED

    # No approval for THIS session is left pending.
    still_pending = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ApprovalRequest)
        .where(
            ApprovalRequest.agent_session_id == agent_session.id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        )
    )
    assert still_pending == 0

    # --- (13.3) Already-resolved approvals keep their original status -------
    for req_id, original in resolved:
        now = await db_session.scalar(
            sa.select(ApprovalRequest.status).where(ApprovalRequest.id == req_id)
        )
        assert now is original

    # KillResult reports exactly the number of pending approvals cancelled.
    assert result.cancelled_approvals == pending_count

    # --- (13.4) Exactly one audit row scoped to THIS workspace --------------
    audits = (
        await db_session.execute(
            sa.select(SystemAuditLog).where(
                SystemAuditLog.action == "admin.session_killed",
                SystemAuditLog.workspace_id == ws.id,
            )
        )
    ).scalars().all()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.user_id == superadmin.id
    assert audit.log_metadata == {"agent_session_id": str(agent_session.id)}


# ---------------------------------------------------------------------------
# Guard examples (documented non-happy paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_non_running_session_conflicts_and_does_not_audit(
    db_session: AsyncSession,
) -> None:
    """Killing a non-running (completed) session raises 409 and writes no
    ``session_killed`` audit row for that workspace."""
    superadmin = await _make_user(db_session, "SuperAdmin", is_superadmin=True)
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "GuardWS", owner=owner)
    completed = await _make_agent_session(
        db_session, workspace=ws, user=owner, status=AgentSessionStatus.COMPLETED
    )
    await db_session.commit()

    with pytest.raises(APIError) as excinfo:
        await admin_service.kill_session(
            db_session,
            agent_session_id=completed.id,
            acting_superadmin_id=superadmin.id,
        )
    assert excinfo.value.status_code == 409

    # Nothing was audited for this workspace on the 409 path.
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
async def test_kill_missing_session_not_found(db_session: AsyncSession) -> None:
    """Killing a session id that does not exist raises 404."""
    superadmin = await _make_user(db_session, "SuperAdmin", is_superadmin=True)
    await db_session.commit()

    with pytest.raises(APIError) as excinfo:
        await admin_service.kill_session(
            db_session,
            agent_session_id=uuid.uuid4(),
            acting_superadmin_id=superadmin.id,
        )
    assert excinfo.value.status_code == 404
