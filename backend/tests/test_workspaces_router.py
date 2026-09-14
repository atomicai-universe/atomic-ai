"""Tests for the workspaces router: CRUD, invites, accept, roles (task 7.5).

These tests drive the router against a *real*, throwaway ``postgres:18.6-alpine``
(on a non-default host port) with the project's Alembic migration applied, so
RBAC decisions and the ``system_audit_logs`` rows written on state changes are
exercised against a real database (Req 4.5, 15.1). The suite skips gracefully
when Docker is unavailable.

The app is built with the workspaces router mounted and two dependencies
overridden:

- ``get_session`` -> a session bound to the migrated test database, so writes
  and audit rows truly persist; and
- ``require_session`` -> a per-test override that injects a
  :class:`~app.core.tenancy.RequestContext` for a chosen user with a chosen set
  of workspace roles, so we can act as an Owner, Member, Viewer, Admin, or a
  non-member without going through the OAuth/session machinery.

Assertions cover:

- creating a workspace works for any authenticated user and writes a
  ``workspace.created`` audit row (Req 15.1);
- invite creation is rejected 403 for a non-Owner member (Member/Viewer/Admin)
  and 404 for a non-member, but succeeds for the Owner and writes a
  ``workspace.invite_created`` audit row (Req 4.5, 15.1);
- accepting an invite creates the membership;
- a role update is Owner-only (403 for a non-Owner) and works for the Owner;
- deleting a workspace is Owner-only and writes a ``workspace.deleted`` row that
  survives the deletion (Req 3.6, 15.1, 20.4).

Requirements: 4.5, 15.1 (also 3.1, 3.6, 5.x, 20.4).
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
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import workspaces as workspaces_module
from app.api.deps import require_session
from app.api.workspaces import (
    AUDIT_ACTION_INVITE_ACCEPTED,
    AUDIT_ACTION_INVITE_CREATED,
    AUDIT_ACTION_MEMBER_ROLE_UPDATED,
    AUDIT_ACTION_WORKSPACE_CREATED,
    AUDIT_ACTION_WORKSPACE_DELETED,
    router as workspaces_router,
)
from app.core.errors import install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import (
    AuthProvider,
    MemberRole,
    SystemAuditLog,
    User,
    WorkspaceMember,
)
from app.db.session import get_session

# ---------------------------------------------------------------------------
# Throwaway Postgres infrastructure (skips without Docker)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_workspaces_router_test"
_HOST_PORT = 55446  # non-default so we never touch another database
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
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, future=True, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )


# ---------------------------------------------------------------------------
# App + context-injection helpers
# ---------------------------------------------------------------------------


def _build_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """Build an app with the workspaces router and a DB-backed session override."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(workspaces_router)

    async def _get_session_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_override
    return app


def _act_as(
    app: FastAPI,
    *,
    user_id: uuid.UUID,
    roles: dict[uuid.UUID, MemberRole] | None = None,
    is_superadmin: bool = False,
) -> None:
    """Override ``require_session`` to inject a context for ``user_id``.

    ``roles`` maps workspace_id -> the caller's role, exactly what
    ``ctx.member_role`` consults; an absent workspace means "not a member".
    """
    ctx = RequestContext(
        user_id=user_id,
        active_workspace_id=None,
        is_superadmin=is_superadmin,
        roles=roles or {},
    )

    async def _require_session_override() -> RequestContext:
        return ctx

    app.dependency_overrides[require_session] = _require_session_override


async def _make_user(session: AsyncSession, name: str) -> uuid.UUID:
    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
        name=name,
        auth_provider=AuthProvider.GOOGLE,
        is_superadmin=False,
    )
    session.add(user)
    await session.flush()
    return user.id


async def _audit_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    action: str,
    workspace_id: uuid.UUID | None = None,
) -> list[SystemAuditLog]:
    async with session_factory() as sess:
        stmt = sa.select(SystemAuditLog).where(SystemAuditLog.action == action)
        if workspace_id is not None:
            stmt = stmt.where(SystemAuditLog.workspace_id == workspace_id)
        return list((await sess.scalars(stmt)).all())


@pytest_asyncio.fixture
async def app_client(session_factory: async_sessionmaker[AsyncSession]):
    """Yield an httpx AsyncClient bound to the app, plus the app for overrides."""
    import httpx

    app = _build_app(session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        yield app, client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_succeeds_and_writes_audit(
    app_client, session_factory
) -> None:
    """Any authenticated user can create a workspace; audit row written (Req 15.1)."""
    app, client = app_client
    async with session_factory() as sess:
        user_id = await _make_user(sess, "Creator")
        await sess.commit()

    _act_as(app, user_id=user_id)
    resp = await client.post("/api/v1/workspaces", json={"name": "Acme HQ"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Acme HQ"
    assert body["slug"].startswith("acme-hq")
    workspace_id = uuid.UUID(body["id"])

    # The creator is the sole Owner.
    async with session_factory() as sess:
        member = await sess.scalar(
            sa.select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        assert member is not None
        assert member.role is MemberRole.OWNER

    rows = await _audit_rows(
        session_factory,
        action=AUDIT_ACTION_WORKSPACE_CREATED,
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    assert rows[0].user_id == user_id
    assert rows[0].log_metadata == {"name": "Acme HQ", "slug": body["slug"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER, MemberRole.ADMIN])
async def test_invite_creation_rejected_for_non_owner(
    app_client, session_factory, role: MemberRole
) -> None:
    """A non-Owner member cannot create invites — 403 (Req 4.5)."""
    app, client = app_client
    async with session_factory() as sess:
        owner_id = await _make_user(sess, "Owner")
        from app.services.workspace_service import create_workspace

        ws = await create_workspace(sess, name="Roles WS", creator_user_id=owner_id)
        actor_id = await _make_user(sess, f"Actor-{role.value}")
        sess.add(
            WorkspaceMember(workspace_id=ws.id, user_id=actor_id, role=role)
        )
        await sess.commit()
        workspace_id = ws.id

    _act_as(app, user_id=actor_id, roles={workspace_id: role})
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invites",
        json={"email": "invitee@x.test", "role": "member"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    # No invite audit row should have been written.
    rows = await _audit_rows(
        session_factory,
        action=AUDIT_ACTION_INVITE_CREATED,
        workspace_id=workspace_id,
    )
    assert rows == []


@pytest.mark.asyncio
async def test_invite_creation_rejected_for_non_member(
    app_client, session_factory
) -> None:
    """A caller who is not a member gets 404 (existence not disclosed, Req 4.3)."""
    app, client = app_client
    async with session_factory() as sess:
        owner_id = await _make_user(sess, "Owner")
        from app.services.workspace_service import create_workspace

        ws = await create_workspace(sess, name="Hidden WS", creator_user_id=owner_id)
        outsider_id = await _make_user(sess, "Outsider")
        await sess.commit()
        workspace_id = ws.id

    _act_as(app, user_id=outsider_id, roles={})  # not a member of anything
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invites",
        json={"email": "invitee@x.test", "role": "member"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_invite_creation_succeeds_for_owner_and_writes_audit(
    app_client, session_factory
) -> None:
    """The Owner can create an invite; a ``invite_created`` audit row is written (Req 4.5, 15.1)."""
    app, client = app_client
    async with session_factory() as sess:
        owner_id = await _make_user(sess, "Owner")
        from app.services.workspace_service import create_workspace

        ws = await create_workspace(sess, name="Owner WS", creator_user_id=owner_id)
        await sess.commit()
        workspace_id = ws.id

    _act_as(app, user_id=owner_id, roles={workspace_id: MemberRole.OWNER})
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invites",
        json={"email": "invitee@x.test", "role": "admin"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "invitee@x.test"
    assert body["role"] == "admin"
    assert body["status"] == "pending"
    # A hex token is returned to the Owner so they can forward it.
    assert isinstance(body["token"], str) and body["token"]
    bytes.fromhex(body["token"])  # decodes cleanly

    rows = await _audit_rows(
        session_factory,
        action=AUDIT_ACTION_INVITE_CREATED,
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    assert rows[0].user_id == owner_id
    # The audit metadata records the invite but never the token.
    assert rows[0].log_metadata == {"email": "invitee@x.test", "role": "admin"}
    assert "token" not in (rows[0].log_metadata or {})


@pytest.mark.asyncio
async def test_accept_invite_creates_membership(
    app_client, session_factory
) -> None:
    """Accepting a valid invite creates the membership with the invited role."""
    app, client = app_client
    async with session_factory() as sess:
        owner_id = await _make_user(sess, "Owner")
        from app.services.workspace_service import create_invite, create_workspace

        ws = await create_workspace(sess, name="Invite WS", creator_user_id=owner_id)
        invite = await create_invite(
            sess, workspace_id=ws.id, email="joiner@x.test", role="member"
        )
        invitee_id = await _make_user(sess, "Joiner")
        await sess.commit()
        workspace_id = ws.id
        token_hex = invite.token.hex()

    _act_as(app, user_id=invitee_id, roles={})
    resp = await client.post(
        "/api/v1/workspaces/invites/accept", json={"token": token_hex}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == str(workspace_id)
    assert body["user_id"] == str(invitee_id)
    assert body["role"] == "member"

    # Membership persisted.
    async with session_factory() as sess:
        member = await sess.scalar(
            sa.select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == invitee_id,
            )
        )
        assert member is not None
        assert member.role is MemberRole.MEMBER

    rows = await _audit_rows(
        session_factory,
        action=AUDIT_ACTION_INVITE_ACCEPTED,
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    assert rows[0].user_id == invitee_id


@pytest.mark.asyncio
async def test_role_update_is_owner_only(app_client, session_factory) -> None:
    """A non-Owner cannot change roles (403); the Owner can (Req 4.5, 15.1)."""
    app, client = app_client
    async with session_factory() as sess:
        owner_id = await _make_user(sess, "Owner")
        from app.services.workspace_service import create_workspace

        ws = await create_workspace(sess, name="RoleMgmt WS", creator_user_id=owner_id)
        target_id = await _make_user(sess, "Target")
        sess.add(
            WorkspaceMember(
                workspace_id=ws.id, user_id=target_id, role=MemberRole.MEMBER
            )
        )
        await sess.commit()
        workspace_id = ws.id

    # An Admin (non-Owner) is rejected.
    _act_as(app, user_id=target_id, roles={workspace_id: MemberRole.ADMIN})
    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{target_id}",
        json={"role": "admin"},
    )
    assert resp.status_code == 403, resp.text

    # The Owner succeeds and the change persists + audit row written.
    _act_as(app, user_id=owner_id, roles={workspace_id: MemberRole.OWNER})
    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{target_id}",
        json={"role": "admin"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"

    async with session_factory() as sess:
        member = await sess.scalar(
            sa.select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == target_id,
            )
        )
        assert member.role is MemberRole.ADMIN

    rows = await _audit_rows(
        session_factory,
        action=AUDIT_ACTION_MEMBER_ROLE_UPDATED,
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    assert rows[0].user_id == owner_id


@pytest.mark.asyncio
async def test_delete_workspace_is_owner_only_and_audit_survives(
    app_client, session_factory
) -> None:
    """Delete is Owner-only; the audit row survives with workspace_id NULL (Req 3.6, 20.4)."""
    app, client = app_client
    async with session_factory() as sess:
        owner_id = await _make_user(sess, "Owner")
        from app.services.workspace_service import create_workspace

        ws = await create_workspace(sess, name="Doomed WS", creator_user_id=owner_id)
        member_id = await _make_user(sess, "Member")
        sess.add(
            WorkspaceMember(
                workspace_id=ws.id, user_id=member_id, role=MemberRole.MEMBER
            )
        )
        await sess.commit()
        workspace_id = ws.id

    # A non-Owner member cannot delete.
    _act_as(app, user_id=member_id, roles={workspace_id: MemberRole.MEMBER})
    resp = await client.request(
        "DELETE", f"/api/v1/workspaces/{workspace_id}"
    )
    assert resp.status_code == 403, resp.text

    # The Owner can.
    _act_as(app, user_id=owner_id, roles={workspace_id: MemberRole.OWNER})
    resp = await client.request(
        "DELETE", f"/api/v1/workspaces/{workspace_id}"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"

    # The workspace is gone.
    from app.db.models import Workspace

    async with session_factory() as sess:
        assert await sess.get(Workspace, workspace_id) is None

    # The delete audit row survives, with workspace_id nulled by ON DELETE SET NULL.
    rows = await _audit_rows(
        session_factory, action=AUDIT_ACTION_WORKSPACE_DELETED
    )
    matching = [r for r in rows if r.user_id == owner_id]
    assert len(matching) == 1
    assert matching[0].workspace_id is None
