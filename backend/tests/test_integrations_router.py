"""Tests for the integrations router: connect / sharing toggle / disconnect (task 9.5).

Coverage focuses on the requirements the task wires:

- **Req 7.1 / 7.6** — connecting an integration requires ``MANAGE_INTEGRATIONS``
  (Owner/Admin); a Viewer/Member is rejected with **403**. Disconnecting is
  allowed for the creator or a non-creating Owner/Admin, but a non-creating
  Member/Viewer is rejected with **403**.
- **Req 7.6** — the sharing toggle is authorized by
  :func:`app.services.integration_vault.can_toggle_sharing` (creator, or
  non-creating Owner/Admin): a non-creating Member is rejected with **403**, and
  the creator (even a Member) may toggle.
- **Req 6.4** — a successful connect response contains **no** token or ciphertext
  material, and the persisted ``integration.connected`` audit row carries no
  token in its metadata.
- **Req 15.1** — a successful state-changing operation writes a
  ``system_audit_logs`` row recording the workspace, acting user, and action.
- **Req 7.7** — a disconnect actually removes the integration row.

The suite prefers a throwaway ``postgres:18.6-alpine`` on a non-default port so
the whole flow (RBAC decision → vault persistence/encryption → audit row) runs
against a real database and the audit rows are read back. If Docker is not
available the module is skipped.

A FastAPI app is built with the integrations router mounted; ``get_session`` is
overridden to the real DB sessionmaker and ``require_session`` is overridden to
return a :class:`RequestContext` for a chosen caller/role, so no OAuth/session
machinery is exercised here.

Requirements: 6.4, 7.1, 7.6, 15.1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import deps
from app.api.integrations import (
    AUDIT_ACTION_CONNECTED,
    AUDIT_ACTION_DISCONNECTED,
    AUDIT_ACTION_SHARING_TOGGLED,
    router as integrations_router,
)
import httpx
from cryptography.fernet import Fernet

from app.core.encryption import EncryptionService, set_encryption_service
from app.core.errors import install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import (
    AuthProvider,
    Integration,
    MemberRole,
    SystemAuditLog,
    User,
    Workspace,
    WorkspaceMember,
)
from app.db.session import get_session

# ===========================================================================
# Throwaway Postgres (skips without Docker)
# ===========================================================================

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_integrations_router_test"
_HOST_PORT = 55447  # non-default so we never touch another database
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105 - throwaway container credential
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}"
    f"@localhost:{_HOST_PORT}/{_PG_DB}"
)

# A valid Fernet key for the vault's encryption service. Tests strip config env,
# so the process-wide encryption service is set explicitly. Fernet requires a
# 32-byte url-safe base64 key, so generate one rather than using a plain string.
_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


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
        "ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY,
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


class _LoopSafeSessionFactory:
    """Yields :class:`AsyncSession` objects bound to the *current* event loop.

    ``TestClient`` runs the app on anyio's blocking-portal loop, which is a
    *different* loop from the pytest-asyncio loop that drives the async test
    body and seed fixtures. An asyncpg connection created under one loop cannot
    be awaited from another ("got Future attached to a different loop").

    To sidestep that, every session is served from a short-lived engine created
    lazily inside the ``async with`` (so it binds to whatever loop is running at
    that moment) and disposed when the session context exits. ``NullPool``
    guarantees the underlying asyncpg connection is opened and closed within the
    same loop, never pooled across loops. This is a test-only concession to the
    ``TestClient`` threading model.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @asynccontextmanager
    async def __call__(self):
        engine = create_async_engine(self._dsn, future=True, poolclass=NullPool)
        session = AsyncSession(bind=engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await engine.dispose()


@pytest.fixture
def db_sessionmaker(migrated_database: str) -> _LoopSafeSessionFactory:
    """A per-call session factory that binds each session to the running loop."""
    return _LoopSafeSessionFactory(migrated_database)


@pytest.fixture(autouse=True)
def _encryption_service() -> Iterator[None]:
    """Install a process-wide encryption service the vault's ``store`` uses.

    The conftest strips ``ENCRYPTION_KEY`` from the env, so ``store`` (which
    calls ``get_encryption_service()``) would otherwise fail to build one.
    """
    set_encryption_service(EncryptionService(_TEST_ENCRYPTION_KEY))
    try:
        yield
    finally:
        set_encryption_service(None)


# ===========================================================================
# Fixtures: seed a workspace with one member per role
# ===========================================================================


@pytest_asyncio.fixture
async def seeded(db_sessionmaker):
    """Create a workspace + one user per role; return ids for the tests."""
    slug = f"ws-{uuid.uuid4().hex[:8]}"
    async with db_sessionmaker() as sess:
        users: dict[MemberRole, uuid.UUID] = {}

        # Create all role users first so the workspace can reference its creator
        # (workspaces.created_by_user_id is NOT NULL).
        for role in (
            MemberRole.OWNER,
            MemberRole.ADMIN,
            MemberRole.MEMBER,
            MemberRole.VIEWER,
        ):
            user = User(
                email=f"{role.value}-{uuid.uuid4().hex}@x.test",
                name=f"{role.value} user",
                auth_provider=AuthProvider.GOOGLE,
                is_superadmin=False,
            )
            sess.add(user)
            await sess.flush()
            users[role] = user.id

        # The Owner is the workspace creator.
        workspace = Workspace(
            name="Integrations WS",
            slug=slug,
            created_by_user_id=users[MemberRole.OWNER],
        )
        sess.add(workspace)
        await sess.flush()

        for role, user_id in users.items():
            sess.add(
                WorkspaceMember(
                    workspace_id=workspace.id, user_id=user_id, role=role
                )
            )

        await sess.commit()
        return {"workspace_id": workspace.id, "users": users}


def _make_context(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID, role: MemberRole
) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        active_workspace_id=workspace_id,
        is_superadmin=False,
        roles={workspace_id: role},
    )


def _make_app(db_sessionmaker, ctx: RequestContext) -> FastAPI:
    """Build the integrations app with the DB session + a fixed request context."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(integrations_router)

    async def _get_session_override():
        async with db_sessionmaker() as sess:
            yield sess

    def _require_session_override() -> RequestContext:
        return ctx

    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[deps.require_session] = _require_session_override
    return app


def _client(db_sessionmaker, ctx: RequestContext) -> httpx.AsyncClient:
    """Async in-process client running the app on the current event loop.

    Using ``httpx.AsyncClient`` + ``ASGITransport`` (instead of the sync
    ``TestClient``) keeps the app's DB work on the SAME event loop as the test
    and the ``db_sessionmaker`` engine, avoiding cross-loop asyncpg errors.
    """
    app = _make_app(db_sessionmaker, ctx)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    )


async def _audit_rows(db_sessionmaker, workspace_id, action):
    async with db_sessionmaker() as sess:
        return (
            await sess.scalars(
                sa.select(SystemAuditLog).where(
                    SystemAuditLog.workspace_id == workspace_id,
                    SystemAuditLog.action == action,
                )
            )
        ).all()


async def _create_integration(
    db_sessionmaker,
    *,
    workspace_id,
    creator_id,
    shared=False,
):
    """Persist an integration directly (bypassing the router) for setup."""
    from app.services import integration_vault

    async with db_sessionmaker() as sess:
        integration = await integration_vault.store(
            sess,
            workspace_id=workspace_id,
            created_by_user_id=creator_id,
            category="email",
            provider_name="gmail",
            access_token="secret-access-token",
            refresh_token="secret-refresh-token",
            is_shared_with_workspace=shared,
        )
        await sess.commit()
        return integration.id


# A recognizable plaintext token used to assert it never leaks (Req 6.4).
_SECRET_ACCESS = "super-secret-access-token-VALUE"
_SECRET_REFRESH = "super-secret-refresh-token-VALUE"
_SECRET_CLIENT_ID = "gmail-client-id-VALUE"
_SECRET_CLIENT_SECRET = "gmail-client-secret-VALUE"


# ===========================================================================
# connect — Req 7.1 / 7.6 authorization + Req 6.4 no-leak + Req 15.1 audit
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.ADMIN])
async def test_owner_admin_can_connect_and_response_has_no_token(
    db_sessionmaker, seeded, role
):
    ws = seeded["workspace_id"]
    user_id = seeded["users"][role]
    ctx = _make_context(user_id=user_id, workspace_id=ws, role=role)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/integrations",
            json={
                "workspace_id": str(ws),
                "category": "email",
                "provider_name": "gmail",
                "credentials": {
                    "client_id": _SECRET_CLIENT_ID,
                    "client_secret": _SECRET_CLIENT_SECRET,
                    "refresh_token": _SECRET_REFRESH,
                    "access_token": _SECRET_ACCESS,
                },
                "is_shared_with_workspace": False,
            },
        )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    # The safe view exposes only non-sensitive fields.
    assert set(body.keys()) == {
        "id",
        "workspace_id",
        "category",
        "provider_name",
        "is_shared_with_workspace",
        "status",
        "webhook",
    }
    assert body["provider_name"] == "gmail"
    assert body["status"] == "active"

    # Req 6.4 — no token / ciphertext material anywhere in the response body.
    raw = resp.text
    assert _SECRET_ACCESS not in raw
    assert _SECRET_REFRESH not in raw
    assert "access_token" not in raw
    assert "refresh_token" not in raw
    assert "encrypted" not in raw

    # Req 15.1 — an integration.connected audit row was persisted, and Req 6.4 —
    # its metadata carries no token value.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_CONNECTED)
    assert len(rows) == 1
    assert rows[0].user_id == user_id
    meta = rows[0].log_metadata
    assert meta["provider_name"] == "gmail"
    assert _SECRET_ACCESS not in str(meta)
    assert _SECRET_REFRESH not in str(meta)
    assert "access_token" not in meta and "refresh_token" not in meta

    # The stored row holds ciphertext, not the plaintext secrets (Req 6.1/6.4).
    # Flexible-credential providers (Gmail) store the whole secret map in the
    # encrypted_credentials blob; assert the plaintext never appears there.
    async with db_sessionmaker() as sess:
        stored = await sess.get(Integration, uuid.UUID(body["id"]))
    assert stored is not None
    assert stored.encrypted_credentials is not None
    blob = stored.encrypted_credentials
    assert _SECRET_ACCESS.encode() not in blob
    assert _SECRET_REFRESH.encode() not in blob
    assert _SECRET_CLIENT_SECRET.encode() not in blob


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER])
async def test_member_viewer_cannot_connect(db_sessionmaker, seeded, role):
    ws = seeded["workspace_id"]
    user_id = seeded["users"][role]
    ctx = _make_context(user_id=user_id, workspace_id=ws, role=role)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/integrations",
            json={
                "workspace_id": str(ws),
                "category": "email",
                "provider_name": "gmail",
                "credentials": {
                    "client_id": _SECRET_CLIENT_ID,
                    "client_secret": _SECRET_CLIENT_SECRET,
                    "access_token": _SECRET_ACCESS,
                },
            },
        )
    assert resp.status_code == 403, resp.text

    # No integration.connected audit row for a rejected connect.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_CONNECTED)
    assert all(r.user_id != user_id for r in rows)


@pytest.mark.asyncio
async def test_connect_by_non_member_is_404(db_sessionmaker, seeded):
    """A caller with no membership in the workspace gets 404 (existence hidden)."""
    ws = seeded["workspace_id"]
    stranger = uuid.uuid4()
    # Context reports the stranger as Owner of a *different* workspace only.
    ctx = RequestContext(
        user_id=stranger,
        active_workspace_id=ws,
        is_superadmin=False,
        roles={uuid.uuid4(): MemberRole.OWNER},
    )
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/integrations",
            json={
                "workspace_id": str(ws),
                "category": "email",
                "provider_name": "gmail",
                "credentials": {
                    "client_id": _SECRET_CLIENT_ID,
                    "client_secret": _SECRET_CLIENT_SECRET,
                    "access_token": _SECRET_ACCESS,
                },
            },
        )
    assert resp.status_code == 404, resp.text


# ===========================================================================
# sharing toggle — Req 7.6 (can_toggle_sharing)
# ===========================================================================


@pytest.mark.asyncio
async def test_creator_member_can_toggle_sharing(db_sessionmaker, seeded):
    """A Member who created the integration may toggle its sharing (Req 7.6)."""
    ws = seeded["workspace_id"]
    member_id = seeded["users"][MemberRole.MEMBER]
    integration_id = await _create_integration(
        db_sessionmaker, workspace_id=ws, creator_id=member_id, shared=False
    )
    ctx = _make_context(user_id=member_id, workspace_id=ws, role=MemberRole.MEMBER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.patch(
            f"/api/v1/integrations/{integration_id}/sharing",
            json={"workspace_id": str(ws), "shared": True},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_shared_with_workspace"] is True

    # Req 15.1 — a sharing_toggled audit row was persisted.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_SHARING_TOGGLED)
    assert len(rows) == 1
    assert rows[0].user_id == member_id

    # The change is durable.
    async with db_sessionmaker() as sess:
        stored = await sess.get(Integration, integration_id)
    assert stored.is_shared_with_workspace is True


@pytest.mark.asyncio
async def test_non_creator_member_cannot_toggle_sharing(db_sessionmaker, seeded):
    """A non-creating Member cannot toggle sharing (Req 7.6): 403."""
    ws = seeded["workspace_id"]
    # Created by the ADMIN; the acting caller is a different MEMBER.
    admin_id = seeded["users"][MemberRole.ADMIN]
    integration_id = await _create_integration(
        db_sessionmaker, workspace_id=ws, creator_id=admin_id, shared=False
    )
    member_id = seeded["users"][MemberRole.MEMBER]
    ctx = _make_context(user_id=member_id, workspace_id=ws, role=MemberRole.MEMBER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.patch(
            f"/api/v1/integrations/{integration_id}/sharing",
            json={"workspace_id": str(ws), "shared": True},
        )
    assert resp.status_code == 403, resp.text

    # Unchanged.
    async with db_sessionmaker() as sess:
        stored = await sess.get(Integration, integration_id)
    assert stored.is_shared_with_workspace is False


@pytest.mark.asyncio
async def test_non_creator_owner_can_toggle_sharing(db_sessionmaker, seeded):
    """A non-creating Owner may toggle sharing (Req 7.6)."""
    ws = seeded["workspace_id"]
    member_id = seeded["users"][MemberRole.MEMBER]
    integration_id = await _create_integration(
        db_sessionmaker, workspace_id=ws, creator_id=member_id, shared=False
    )
    owner_id = seeded["users"][MemberRole.OWNER]
    ctx = _make_context(user_id=owner_id, workspace_id=ws, role=MemberRole.OWNER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.patch(
            f"/api/v1/integrations/{integration_id}/sharing",
            json={"workspace_id": str(ws), "shared": True},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_shared_with_workspace"] is True


# ===========================================================================
# disconnect — Req 7.6 authorization + Req 7.7 removal + Req 15.1 audit
# ===========================================================================


@pytest.mark.asyncio
async def test_owner_can_disconnect_and_row_removed(db_sessionmaker, seeded):
    ws = seeded["workspace_id"]
    owner_id = seeded["users"][MemberRole.OWNER]
    # Created by a member; disconnected by a non-creating Owner (allowed).
    member_id = seeded["users"][MemberRole.MEMBER]
    integration_id = await _create_integration(
        db_sessionmaker, workspace_id=ws, creator_id=member_id
    )
    ctx = _make_context(user_id=owner_id, workspace_id=ws, role=MemberRole.OWNER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.request(
            "DELETE",
            f"/api/v1/integrations/{integration_id}",
            json={"workspace_id": str(ws)},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disconnected"

    # Req 7.7 — the integration row is gone.
    async with db_sessionmaker() as sess:
        gone = await sess.get(Integration, integration_id)
    assert gone is None

    # Req 15.1 — a disconnected audit row recording the workspace + acting user.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_DISCONNECTED)
    assert len(rows) == 1
    assert rows[0].user_id == owner_id
    assert rows[0].log_metadata["integration_id"] == str(integration_id)


@pytest.mark.asyncio
async def test_creator_member_can_disconnect(db_sessionmaker, seeded):
    """The creating Member may disconnect their own integration (Req 7.6)."""
    ws = seeded["workspace_id"]
    member_id = seeded["users"][MemberRole.MEMBER]
    integration_id = await _create_integration(
        db_sessionmaker, workspace_id=ws, creator_id=member_id
    )
    ctx = _make_context(user_id=member_id, workspace_id=ws, role=MemberRole.MEMBER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.request(
            "DELETE",
            f"/api/v1/integrations/{integration_id}",
            json={"workspace_id": str(ws)},
        )
    assert resp.status_code == 200, resp.text

    async with db_sessionmaker() as sess:
        gone = await sess.get(Integration, integration_id)
    assert gone is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER])
async def test_non_creator_member_viewer_cannot_disconnect(
    db_sessionmaker, seeded, role
):
    """A non-creating Member/Viewer cannot disconnect (Req 7.6): 403."""
    ws = seeded["workspace_id"]
    # Created by the ADMIN; the acting caller is a different non-privileged user.
    admin_id = seeded["users"][MemberRole.ADMIN]
    integration_id = await _create_integration(
        db_sessionmaker, workspace_id=ws, creator_id=admin_id
    )
    actor_id = seeded["users"][role]
    ctx = _make_context(user_id=actor_id, workspace_id=ws, role=role)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.request(
            "DELETE",
            f"/api/v1/integrations/{integration_id}",
            json={"workspace_id": str(ws)},
        )
    assert resp.status_code == 403, resp.text

    # The integration still exists (Req 7.7 not triggered on a denied call).
    async with db_sessionmaker() as sess:
        still = await sess.get(Integration, integration_id)
    assert still is not None
