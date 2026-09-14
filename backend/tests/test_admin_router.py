"""Tests for the super admin control-plane router (task 14.1).

These drive :mod:`app.api.admin` against a *real*, throwaway
``postgres:18.6-alpine`` (on a non-default host port) with the project's Alembic
migration applied, so the super-admin gate, the cross-workspace directory, the
ban (session invalidation + durable flag), and ownership reassignment are all
exercised against a real database. The suite skips gracefully when Docker is
unavailable.

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

- a non-super-admin is rejected **403** on the directory, ban, and reassign
  endpoints (Req 12.1, 12.2);
- a super admin's directory returns users spanning multiple workspaces
  (Req 12.3);
- ban deletes the target's ``sessions`` rows and sets ``is_banned`` (Req 12.4);
- reassign-owner leaves the target as an Owner :class:`WorkspaceMember`
  (Req 12.5).

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5.
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
    AuthProvider,
    MemberRole,
    Session as SessionModel,
    User,
    Workspace,
    WorkspaceMember,
)
from app.db.session import get_session

# ---------------------------------------------------------------------------
# Throwaway Postgres infrastructure (skips without Docker)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_admin_router_test"
_HOST_PORT = 55453  # non-default so we never touch another database
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
    runs on. This keeps ``asyncpg`` connections from being reused across the
    loops that ``httpx.ASGITransport`` spins up, which otherwise raises
    "got Future attached to a different loop".
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


async def _add_session(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    session.add(
        SessionModel(
            user_id=user_id,
            token=uuid.uuid4().bytes,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_superadmin_is_forbidden_on_all_endpoints(
    app_client, db_session
) -> None:
    """A non-super-admin gets 403 on directory, ban, and reassign (Req 12.1, 12.2)."""
    app, client = app_client
    ordinary = await _make_user(db_session, "Ordinary")
    target = await _make_user(db_session, "Target")
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "NW", owner=owner)
    await db_session.commit()

    _act_as(app, user_id=ordinary.id, is_superadmin=False)

    resp = await client.get("/api/v1/admin/users")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    resp = await client.post(f"/api/v1/admin/users/{target.id}/ban")
    assert resp.status_code == 403, resp.text

    resp = await client.post(
        f"/api/v1/admin/workspaces/{ws.id}/reassign-owner",
        json={"new_owner_user_id": str(target.id)},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_superadmin_directory_spans_multiple_workspaces(
    app_client, db_session
) -> None:
    """The directory returns users across multiple workspaces (Req 12.3)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    owner_a = await _make_user(db_session, "OwnerA", provider=AuthProvider.GITHUB)
    owner_b = await _make_user(db_session, "OwnerB")
    ws_a = await _make_workspace(db_session, "Alpha", owner=owner_a)
    ws_b = await _make_workspace(db_session, "Beta", owner=owner_b)
    # A member of ws_a who is not an owner anywhere.
    member = await _make_user(db_session, "Member")
    db_session.add(
        WorkspaceMember(
            workspace_id=ws_a.id, user_id=member.id, role=MemberRole.MEMBER
        )
    )
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.get("/api/v1/admin/users")
    assert resp.status_code == 200, resp.text
    users = resp.json()["users"]
    by_id = {u["id"]: u for u in users}

    # All seeded users, spanning both workspaces, are present.
    for u in (admin, owner_a, owner_b, member):
        assert str(u.id) in by_id

    # Safe view: expected keys only, no secrets leaked.
    entry = by_id[str(owner_a.id)]
    assert set(entry.keys()) == {
        "id",
        "email",
        "name",
        "auth_provider",
        "is_superadmin",
        "is_banned",
    }
    assert entry["auth_provider"] == "github"
    assert by_id[str(admin.id)]["is_superadmin"] is True
    assert entry["is_banned"] is False


@pytest.mark.asyncio
async def test_ban_invalidates_sessions_and_sets_flag(app_client, db_session) -> None:
    """Ban deletes the target's sessions and sets is_banned (Req 12.4)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    victim = await _make_user(db_session, "Victim")
    await _add_session(db_session, user_id=victim.id)
    await _add_session(db_session, user_id=victim.id)
    await db_session.commit()

    # Precondition: the victim has active sessions.
    count_before = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(SessionModel)
        .where(SessionModel.user_id == victim.id)
    )
    assert count_before == 2

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.post(f"/api/v1/admin/users/{victim.id}/ban")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "banned"

    # The victim's session rows are gone and the durable flag is set. Query the
    # columns directly (rather than reading expired ORM attributes) so no
    # implicit lazy load runs outside an awaited context.
    count_after = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(SessionModel)
        .where(SessionModel.user_id == victim.id)
    )
    assert count_after == 0
    is_banned = await db_session.scalar(
        sa.select(User.is_banned).where(User.id == victim.id)
    )
    assert is_banned is True


@pytest.mark.asyncio
async def test_reassign_owner_makes_target_owner(app_client, db_session) -> None:
    """After reassignment the target is an Owner WorkspaceMember (Req 12.5)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    old_owner = await _make_user(db_session, "OldOwner")
    new_owner = await _make_user(db_session, "NewOwner")
    ws = await _make_workspace(db_session, "Gamma", owner=old_owner)
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.post(
        f"/api/v1/admin/workspaces/{ws.id}/reassign-owner",
        json={"new_owner_user_id": str(new_owner.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "reassigned"
    assert body["new_owner_user_id"] == str(new_owner.id)
    assert body["role"] == "owner"

    # Read the role columns directly to avoid lazy loads on stale ORM objects.
    target_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == new_owner.id,
        )
    )
    assert target_role is MemberRole.OWNER

    # The previous owner is demoted to Admin (single-owner invariant).
    prev_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == old_owner.id,
        )
    )
    assert prev_role is MemberRole.ADMIN


@pytest.mark.asyncio
async def test_non_superadmin_forbidden_on_role_edit_delete(
    app_client, db_session
) -> None:
    """A non-super-admin gets 403 on the role, edit, and delete endpoints."""
    app, client = app_client
    ordinary = await _make_user(db_session, "Ordinary")
    target = await _make_user(db_session, "Target")
    await db_session.commit()

    _act_as(app, user_id=ordinary.id, is_superadmin=False)

    resp = await client.post(
        f"/api/v1/admin/users/{target.id}/role", json={"is_superadmin": True}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    resp = await client.patch(
        f"/api/v1/admin/users/{target.id}", json={"name": "New Name"}
    )
    assert resp.status_code == 403, resp.text

    resp = await client.delete(f"/api/v1/admin/users/{target.id}")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_superadmin_can_change_role_edit_and_delete(
    app_client, db_session
) -> None:
    """A super admin can toggle a role, rename, and delete a user (happy paths)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    target = await _make_user(db_session, "Target")
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)

    # Promote to super admin.
    resp = await client.post(
        f"/api/v1/admin/users/{target.id}/role", json={"is_superadmin": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_superadmin"] is True
    assert await db_session.scalar(
        sa.select(User.is_superadmin).where(User.id == target.id)
    ) is True

    # Rename.
    resp = await client.patch(
        f"/api/v1/admin/users/{target.id}", json={"name": "Renamed"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Renamed"
    assert set(body.keys()) == {
        "id",
        "email",
        "name",
        "auth_provider",
        "is_superadmin",
        "is_banned",
    }

    # Demote back (admin remains a super admin, so this is allowed) then delete.
    resp = await client.post(
        f"/api/v1/admin/users/{target.id}/role", json={"is_superadmin": False}
    )
    assert resp.status_code == 200, resp.text

    resp = await client.delete(f"/api/v1/admin/users/{target.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"
    assert await db_session.scalar(
        sa.select(User.id).where(User.id == target.id)
    ) is None


@pytest.mark.asyncio
async def test_non_superadmin_forbidden_on_workspace_list_and_delete(
    app_client, db_session
) -> None:
    """A non-super-admin gets 403 on the workspace list and delete endpoints."""
    app, client = app_client
    ordinary = await _make_user(db_session, "Ordinary")
    owner = await _make_user(db_session, "Owner")
    ws = await _make_workspace(db_session, "WSGuard", owner=owner)
    await db_session.commit()

    _act_as(app, user_id=ordinary.id, is_superadmin=False)

    resp = await client.get("/api/v1/admin/workspaces")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    resp = await client.delete(f"/api/v1/admin/workspaces/{ws.id}")
    assert resp.status_code == 403, resp.text
    # The workspace still exists (the forbidden request had no effect).
    assert await db_session.scalar(
        sa.select(Workspace.id).where(Workspace.id == ws.id)
    ) is not None


@pytest.mark.asyncio
async def test_superadmin_lists_and_deletes_workspace(
    app_client, db_session
) -> None:
    """A super admin can list workspaces and delete one (happy path).

    The listing reports member_count and owner_user_id; the delete removes the
    workspace and cascades its members.
    """
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    owner = await _make_user(db_session, "WsOwner")
    ws = await _make_workspace(db_session, "Deletable", owner=owner)
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)

    resp = await client.get("/api/v1/admin/workspaces")
    assert resp.status_code == 200, resp.text
    by_id = {w["id"]: w for w in resp.json()["workspaces"]}
    entry = by_id[str(ws.id)]
    assert set(entry.keys()) == {
        "id",
        "name",
        "slug",
        "created_by_user_id",
        "created_at",
        "member_count",
        "owner_user_id",
    }
    assert entry["member_count"] == 1
    assert entry["owner_user_id"] == str(owner.id)

    resp = await client.delete(f"/api/v1/admin/workspaces/{ws.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"
    assert resp.json()["workspace_id"] == str(ws.id)

    # The workspace row is gone and its members cascaded away.
    assert await db_session.scalar(
        sa.select(Workspace.id).where(Workspace.id == ws.id)
    ) is None
    member_count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws.id)
    )
    assert member_count == 0


@pytest.mark.asyncio
async def test_delete_self_is_conflict(app_client, db_session) -> None:
    """A super admin deleting their own account gets 409 (guardrail A)."""
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    await _make_user(db_session, "Other", is_superadmin=True)
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)
    resp = await client.delete(f"/api/v1/admin/users/{admin.id}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "conflict"
