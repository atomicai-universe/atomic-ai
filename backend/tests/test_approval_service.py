"""Tests for the Approval_Hub resolution state machine (task 13.3).

Two layers of coverage:

1. **Pure resolver tests** (no DB, always run): exercise
   :func:`app.services.approval_service.resolve_approval_transition` directly.
   These form the basis for Property 24 (task 13.4): only a ``pending`` request
   may transition to a terminal state, and any resolution of an already-terminal
   request is a conflict (Req 10.7).

   - pending + approve -> ``apply``
   - pending + reject  -> ``apply``
   - approved/rejected + any action -> ``conflict``

2. **DB-backed resolution tests** against a *real*, throwaway
   ``postgres:18.6-alpine`` on a non-default host port (55452) with the
   project's Alembic migration applied. They seed a user + workspace
   (``created_by_user_id`` is NOT NULL) and a ``pending`` ApprovalRequest, then
   assert:

   - approve -> status ``approved`` + reviewer recorded + an ``approval.approved``
     audit row (Req 10.3, 15.3).
   - reject -> status ``rejected`` + reviewer recorded + an ``approval.rejected``
     audit row (Req 10.4, 15.3).
   - re-resolving a terminal request -> 409 conflict (Req 10.7).
   - resolving a missing request -> 404.
   - a Viewer/Member reviewer_role -> 403 (Req 10.5).

   The module skips gracefully when Docker is unavailable. Uses a NullPool async
   engine so no connection outlives a test.

Requirements: 10.3, 10.4, 10.5, 10.6, 10.7, 15.3.
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

from app.core.errors import APIError
from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    MemberRole,
    SystemAuditLog,
    User,
    Workspace,
)
from app.services.approval_service import (
    approve_request,
    reject_request,
    resolve_approval_transition,
    resolve_request,
)

# ---------------------------------------------------------------------------
# Pure resolver tests (no DB, always run) — Property 24 basis (Req 10.7)
# ---------------------------------------------------------------------------


def test_pending_approve_applies() -> None:
    assert resolve_approval_transition(ApprovalStatus.PENDING, "approve") == "apply"


def test_pending_reject_applies() -> None:
    assert resolve_approval_transition(ApprovalStatus.PENDING, "reject") == "apply"


@pytest.mark.parametrize(
    "terminal",
    [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED],
)
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_terminal_any_action_conflicts(
    terminal: ApprovalStatus, action: str
) -> None:
    # Re-resolving an already-terminal request is always a conflict (Req 10.7).
    assert resolve_approval_transition(terminal, action) == "conflict"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DB-backed setup (skip if Docker unavailable)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_approval_service_test"
_HOST_PORT = 55452  # non-default so we never touch another database
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
    last_output = ""
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            last_output = str(exc)
            time.sleep(1.0)
            continue
        if result.returncode == 0:
            return
        last_output = (result.stdout or "") + (result.stderr or "")
        time.sleep(1.0)
    raise RuntimeError(
        f"Postgres container did not become ready in {timeout_s}s: {last_output!r}"
    )


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
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committed-per-test session bound to the throwaway database."""
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        yield sess


async def _make_user(session: AsyncSession, email: str | None = None) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex}@x.test",
        name="Test User",
        auth_provider="google",
    )
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


async def _make_pending_request(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    triggered_by_user_id: uuid.UUID,
    tool_name: str = "send_email",
) -> ApprovalRequest:
    request = ApprovalRequest(
        workspace_id=workspace_id,
        agent_session_id=None,
        triggered_by_user_id=triggered_by_user_id,
        reviewed_by_user_id=None,
        tool_name=tool_name,
        arguments={"to": "a@b.test"},
        status=ApprovalStatus.PENDING,
    )
    session.add(request)
    await session.flush()
    return request


async def _audit_rows(
    session: AsyncSession, approval_request_id: uuid.UUID
) -> list[SystemAuditLog]:
    result = await session.execute(select(SystemAuditLog))
    rows = list(result.scalars().all())
    return [
        r
        for r in rows
        if isinstance(r.log_metadata, dict)
        and r.log_metadata.get("approval_request_id") == str(approval_request_id)
    ]


# ---------------------------------------------------------------------------
# DB-backed resolution tests (Req 10.3, 10.4, 10.5, 10.6, 10.7, 15.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_sets_approved_records_reviewer_and_audits(
    session: AsyncSession,
) -> None:
    triggerer = await _make_user(session)
    reviewer = await _make_user(session)
    ws = await _make_workspace(session, triggerer.id)
    request = await _make_pending_request(
        session, workspace_id=ws.id, triggered_by_user_id=triggerer.id
    )
    await session.commit()

    updated = await approve_request(
        session,
        approval_request_id=request.id,
        reviewer_user_id=reviewer.id,
    )

    assert updated.status == ApprovalStatus.APPROVED
    assert updated.reviewed_by_user_id == reviewer.id

    audits = await _audit_rows(session, request.id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action == "approval.approved"
    assert audit.workspace_id == ws.id
    assert audit.user_id == reviewer.id
    assert audit.log_metadata["tool_name"] == "send_email"


@pytest.mark.asyncio
async def test_reject_sets_rejected_records_reviewer_and_audits(
    session: AsyncSession,
) -> None:
    triggerer = await _make_user(session)
    reviewer = await _make_user(session)
    ws = await _make_workspace(session, triggerer.id)
    request = await _make_pending_request(
        session, workspace_id=ws.id, triggered_by_user_id=triggerer.id
    )
    await session.commit()

    updated = await reject_request(
        session,
        approval_request_id=request.id,
        reviewer_user_id=reviewer.id,
    )

    assert updated.status == ApprovalStatus.REJECTED
    assert updated.reviewed_by_user_id == reviewer.id

    audits = await _audit_rows(session, request.id)
    assert len(audits) == 1
    assert audits[0].action == "approval.rejected"


@pytest.mark.asyncio
async def test_re_resolving_terminal_request_conflicts(
    session: AsyncSession,
) -> None:
    triggerer = await _make_user(session)
    reviewer = await _make_user(session)
    ws = await _make_workspace(session, triggerer.id)
    request = await _make_pending_request(
        session, workspace_id=ws.id, triggered_by_user_id=triggerer.id
    )
    await session.commit()

    await approve_request(
        session,
        approval_request_id=request.id,
        reviewer_user_id=reviewer.id,
    )

    with pytest.raises(APIError) as exc:
        await reject_request(
            session,
            approval_request_id=request.id,
            reviewer_user_id=reviewer.id,
        )
    assert exc.value.status_code == 409

    # State is unchanged: still approved, still the first reviewer, one audit row.
    refreshed = await session.get(ApprovalRequest, request.id)
    assert refreshed.status == ApprovalStatus.APPROVED
    audits = await _audit_rows(session, request.id)
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_missing_request_raises_404(session: AsyncSession) -> None:
    reviewer = await _make_user(session)
    await session.commit()

    with pytest.raises(APIError) as exc:
        await resolve_request(
            session,
            approval_request_id=uuid.uuid4(),
            reviewer_user_id=reviewer.id,
            action="approve",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.VIEWER, MemberRole.MEMBER])
async def test_insufficient_role_raises_403(
    session: AsyncSession, role: MemberRole
) -> None:
    triggerer = await _make_user(session)
    reviewer = await _make_user(session)
    ws = await _make_workspace(session, triggerer.id)
    request = await _make_pending_request(
        session, workspace_id=ws.id, triggered_by_user_id=triggerer.id
    )
    await session.commit()

    with pytest.raises(APIError) as exc:
        await approve_request(
            session,
            approval_request_id=request.id,
            reviewer_user_id=reviewer.id,
            reviewer_role=role,
        )
    assert exc.value.status_code == 403

    # Forbidden attempt must not have mutated the request or written an audit.
    refreshed = await session.get(ApprovalRequest, request.id)
    assert refreshed.status == ApprovalStatus.PENDING
    assert refreshed.reviewed_by_user_id is None
    audits = await _audit_rows(session, request.id)
    assert len(audits) == 0
