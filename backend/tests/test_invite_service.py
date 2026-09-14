"""Tests for the invite state machine and role management (task 7.3).

Two layers of coverage:

1. **Pure unit tests** (no DB, always run): exercise
   :func:`resolve_invite_acceptance`, the side-effect-free invite state machine
   that underpins the DB accept flow (Property 16/17 basis, task 7.4). These
   cover the ``accept`` / ``expired`` / ``already_resolved`` decisions including
   the inclusive expiry boundary.

2. **DB-backed integration tests** against a *real*, throwaway
   ``postgres:18.6-alpine`` on a non-default host port (55441) with the
   project's Alembic migration applied, so the ``UNIQUE`` token constraint,
   enum types, and cross-table member creation are exercised for real. The
   module skips gracefully when Docker is unavailable.

Asserted behaviors:

- ``create_invite`` creates a ``pending`` invite with a unique token and an
  allowed role; requesting an Owner role is rejected (Req 5.1, 5.2, 5.7).
- ``accept_invite`` on a valid pending token creates a member with the invited
  role and moves the invite to ``accepted`` (Req 5.2, 5.3).
- accepting an already-accepted invite is rejected (Req 5.6).
- an invite whose ``expires_at`` is in the past is rejected on accept and
  flipped to ``expired`` (Req 5.5).
- an unknown token is rejected (Req 5.4).
- ``update_member_role`` changes an existing member's role.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.errors import APIError
from app.db.models import (
    InviteRole,
    InviteStatus,
    MemberRole,
    User,
    WorkspaceInvite,
    WorkspaceMember,
)
from app.services.workspace_service import (
    accept_invite,
    create_invite,
    create_workspace,
    resolve_invite_acceptance,
    update_member_role,
)

# ---------------------------------------------------------------------------
# Pure unit tests for the invite state machine (no DB) — always run
# ---------------------------------------------------------------------------


def test_resolver_pending_not_expired_accepts() -> None:
    """A pending invite with a future expiry resolves to ``accept`` (Req 5.2)."""
    now = datetime(2025, 1, 1, tzinfo=UTC)
    future = now + timedelta(days=1)
    assert resolve_invite_acceptance(InviteStatus.PENDING, future, now) == "accept"


def test_resolver_pending_no_expiry_accepts() -> None:
    """A pending invite with no expiry always resolves to ``accept``."""
    now = datetime(2025, 1, 1, tzinfo=UTC)
    assert resolve_invite_acceptance(InviteStatus.PENDING, None, now) == "accept"


def test_resolver_pending_past_expiry_is_expired() -> None:
    """A pending invite past its expiry resolves to ``expired`` (Req 5.5)."""
    now = datetime(2025, 1, 1, tzinfo=UTC)
    past = now - timedelta(seconds=1)
    assert resolve_invite_acceptance(InviteStatus.PENDING, past, now) == "expired"


def test_resolver_expiry_boundary_is_inclusive() -> None:
    """An invite exactly at its expiry is treated as expired (Req 5.5)."""
    now = datetime(2025, 1, 1, tzinfo=UTC)
    assert resolve_invite_acceptance(InviteStatus.PENDING, now, now) == "expired"


def test_resolver_accepted_is_already_resolved() -> None:
    """An already-accepted invite resolves to ``already_resolved`` (Req 5.6)."""
    now = datetime(2025, 1, 1, tzinfo=UTC)
    future = now + timedelta(days=1)
    assert (
        resolve_invite_acceptance(InviteStatus.ACCEPTED, future, now)
        == "already_resolved"
    )


def test_resolver_expired_status_is_already_resolved() -> None:
    """An invite already flagged ``expired`` resolves to ``already_resolved`` (Req 5.6)."""
    now = datetime(2025, 1, 1, tzinfo=UTC)
    future = now + timedelta(days=1)
    assert (
        resolve_invite_acceptance(InviteStatus.EXPIRED, future, now)
        == "already_resolved"
    )


# ---------------------------------------------------------------------------
# DB-backed integration test infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_invite_service_test"
_HOST_PORT = 55441  # non-default so we never touch another database
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
    eng = create_async_engine(migrated_database, future=True, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A committed-per-test session bound to the throwaway database."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
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


# ---------------------------------------------------------------------------
# create_invite (Req 5.1, 5.2, 5.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_invite_is_pending_with_unique_token(
    session: AsyncSession,
) -> None:
    """create_invite yields a pending invite with a unique token (Req 5.1, 5.2)."""
    owner = await _make_user(session)
    workspace = await create_workspace(session, name="Team A", creator_user_id=owner.id)
    await session.flush()

    invite1 = await create_invite(
        session,
        workspace_id=workspace.id,
        email="a@x.test",
        role=InviteRole.MEMBER,
    )
    invite2 = await create_invite(
        session,
        workspace_id=workspace.id,
        email="b@x.test",
        role=InviteRole.VIEWER,
    )
    await session.commit()

    assert invite1.status is InviteStatus.PENDING
    assert invite2.status is InviteStatus.PENDING
    assert invite1.token != invite2.token
    assert invite1.expires_at is not None
    # Default expiry is roughly 7 days out.
    assert invite1.expires_at > datetime.now(UTC) + timedelta(days=6)


@pytest.mark.asyncio
async def test_create_invite_rejects_owner_role(session: AsyncSession) -> None:
    """Requesting an Owner role via invite is rejected (Req 5.7)."""
    owner = await _make_user(session)
    workspace = await create_workspace(session, name="Team B", creator_user_id=owner.id)
    await session.flush()

    with pytest.raises(APIError) as excinfo:
        # MemberRole.OWNER is deliberately not an InviteRole; passing it must be
        # rejected at the service boundary.
        await create_invite(
            session,
            workspace_id=workspace.id,
            email="c@x.test",
            role=MemberRole.OWNER,  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# accept_invite (Req 5.2, 5.3, 5.4, 5.5, 5.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_valid_invite_creates_member_and_accepts(
    session: AsyncSession,
) -> None:
    """Accepting a valid pending token creates a member and flips to accepted (Req 5.2, 5.3)."""
    owner = await _make_user(session)
    invitee = await _make_user(session)
    workspace = await create_workspace(session, name="Team C", creator_user_id=owner.id)
    await session.flush()

    invite = await create_invite(
        session,
        workspace_id=workspace.id,
        email="d@x.test",
        role=InviteRole.ADMIN,
    )
    await session.commit()

    member = await accept_invite(session, token=invite.token, user_id=invitee.id)
    await session.commit()

    assert member.workspace_id == workspace.id
    assert member.user_id == invitee.id
    assert member.role is MemberRole.ADMIN

    refreshed = await session.get(WorkspaceInvite, invite.id)
    assert refreshed is not None
    assert refreshed.status is InviteStatus.ACCEPTED

    # The member row is persisted.
    persisted = await session.scalar(
        sa.select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == invitee.id,
        )
    )
    assert persisted is not None
    assert persisted.role is MemberRole.ADMIN


@pytest.mark.asyncio
async def test_accept_already_accepted_invite_is_rejected(
    session: AsyncSession,
) -> None:
    """Re-accepting an already-accepted invite is rejected (Req 5.6)."""
    owner = await _make_user(session)
    invitee = await _make_user(session)
    workspace = await create_workspace(session, name="Team D", creator_user_id=owner.id)
    await session.flush()

    invite = await create_invite(
        session,
        workspace_id=workspace.id,
        email="e@x.test",
        role=InviteRole.MEMBER,
    )
    await session.commit()

    await accept_invite(session, token=invite.token, user_id=invitee.id)
    await session.commit()

    with pytest.raises(APIError) as excinfo:
        await accept_invite(session, token=invite.token, user_id=invitee.id)
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_accept_expired_invite_is_rejected_and_flipped(
    session: AsyncSession,
) -> None:
    """An invite past its expiry is rejected on accept and flipped to expired (Req 5.5)."""
    owner = await _make_user(session)
    invitee = await _make_user(session)
    workspace = await create_workspace(session, name="Team E", creator_user_id=owner.id)
    await session.flush()

    invite = await create_invite(
        session,
        workspace_id=workspace.id,
        email="f@x.test",
        role=InviteRole.VIEWER,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await session.commit()

    with pytest.raises(APIError) as excinfo:
        await accept_invite(session, token=invite.token, user_id=invitee.id)
    assert excinfo.value.status_code == 410
    await session.commit()

    refreshed = await session.get(WorkspaceInvite, invite.id)
    assert refreshed is not None
    assert refreshed.status is InviteStatus.EXPIRED

    # No member was created for the expired invite.
    member = await session.scalar(
        sa.select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == invitee.id,
        )
    )
    assert member is None


@pytest.mark.asyncio
async def test_accept_unknown_token_is_rejected(session: AsyncSession) -> None:
    """An unknown invite token is rejected with an invalid-invitation error (Req 5.4)."""
    invitee = await _make_user(session)
    with pytest.raises(APIError) as excinfo:
        await accept_invite(session, token=b"no-such-token", user_id=invitee.id)
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# update_member_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_member_role_changes_role(session: AsyncSession) -> None:
    """update_member_role changes an existing member's role."""
    owner = await _make_user(session)
    invitee = await _make_user(session)
    workspace = await create_workspace(session, name="Team F", creator_user_id=owner.id)
    await session.flush()

    invite = await create_invite(
        session,
        workspace_id=workspace.id,
        email="g@x.test",
        role=InviteRole.VIEWER,
    )
    await session.commit()
    await accept_invite(session, token=invite.token, user_id=invitee.id)
    await session.commit()

    updated = await update_member_role(
        session,
        workspace_id=workspace.id,
        target_user_id=invitee.id,
        new_role=MemberRole.ADMIN,
    )
    await session.commit()

    assert updated.role is MemberRole.ADMIN
    persisted = await session.scalar(
        sa.select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == invitee.id,
        )
    )
    assert persisted is not None
    assert persisted.role is MemberRole.ADMIN


@pytest.mark.asyncio
async def test_update_missing_member_raises_404(session: AsyncSession) -> None:
    """Updating a nonexistent membership raises 404."""
    owner = await _make_user(session)
    workspace = await create_workspace(session, name="Team G", creator_user_id=owner.id)
    await session.commit()

    with pytest.raises(APIError) as excinfo:
        await update_member_role(
            session,
            workspace_id=workspace.id,
            target_user_id=uuid.uuid4(),
            new_role=MemberRole.MEMBER,
        )
    assert excinfo.value.status_code == 404
