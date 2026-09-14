"""Unit/integration tests for audit recording at the operation seams (task 15.2).

Task 15.1 (``tests/test_audit_service.py``) already tests
:func:`app.services.audit_service.record` *directly* — that the write path
appends a row, scrubs metadata, and is append-only. This suite is different: it
proves the audit rows are actually *exercised by the real operations/routers*,
i.e. that a state-changing workspace operation, an authentication event, and an
approval resolution each write the expected ``system_audit_logs`` row when the
real code path runs. It deliberately avoids re-testing ``record`` in isolation.

What is asserted (each scoped to the specific workspace/user/action it creates —
never a global count, since the module DB is shared across tests):

- **State-changing workspace op writes an audit row (Req 15.1).** Driving the
  real ``app/api/workspaces.py`` router over ``httpx.AsyncClient`` +
  ``ASGITransport`` (with ``require_session`` + ``get_session`` overridden):
  ``POST /api/v1/workspaces`` writes a ``workspace.created`` row scoped to the
  new workspace + acting user, and ``POST .../invites`` writes a
  ``workspace.invite_created`` row.
- **Auth event writes an audit row (Req 15.2).** Driving the real
  ``app/api/auth.py`` logout endpoint (``POST /auth/logout``) with a real
  persisted session writes an ``auth.logout`` row for the session's user with a
  NULL ``workspace_id``.
- **Approval resolution writes an audit row (Req 15.3).** ``app/api/approvals.py``
  does not exist yet (task 13.7 lands separately), so approval-resolution
  auditing is verified at the SERVICE level via
  :func:`app.services.approval_service.approve_request`: resolving a pending
  request writes an ``approval.approved`` row scoped to the workspace + reviewer.
- **Metadata scrubbing at a recording point (Req 15.4).** The invite-create
  audit metadata written by the router excludes the raw invite token (the token
  is never passed into metadata), and a router-recorded metadata field that
  looks like a secret is redacted. ``test_audit_service.py`` covers scrubbing at
  the service level; here we assert it at an integration point.

Infrastructure mirrors the sibling suites: a throwaway ``postgres:18.6-alpine``
on a non-default host port (55461), the project's *real* ``alembic upgrade head``
migration (so the append-only trigger and full schema are present), a
``NullPool`` per-test engine (no connection outlives a test), ``docker rm -f`` in
a ``finally`` (and up-front to clear any leftover), and a graceful skip when
Docker is unavailable so the suite still passes without a Docker daemon. Users
are always created before workspaces (``workspaces.created_by_user_id`` is NOT
NULL).

Requirements: 15.1, 15.2, 15.3, 15.4.
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

import httpx
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
from sqlalchemy.pool import NullPool

from app.api import auth as auth_module
from app.api import deps
from app.api.auth import AUDIT_ACTION_LOGOUT, router as auth_router
from app.api.workspaces import (
    AUDIT_ACTION_INVITE_CREATED,
    AUDIT_ACTION_WORKSPACE_CREATED,
    router as workspaces_router,
)
from app.core.errors import install_exception_handlers
from app.core.security import persist_session
from app.core.tenancy import RequestContext
from app.db.models import (
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
from app.services.approval_service import approve_request

# --- Repo layout / container parameters --------------------------------------
# tests/ -> backend/
_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_audit_recording_test"
_HOST_PORT = 55461  # deliberately non-default so we never touch another DB
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105 - throwaway container credential
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}"
    f"@localhost:{_HOST_PORT}/{_PG_DB}"
)


# --- Docker helpers ----------------------------------------------------------


def _docker_available() -> bool:
    """Return True when a usable Docker CLI/daemon is present."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _force_remove_container() -> None:
    """Best-effort removal of the throwaway container; never raises."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _wait_until_ready(timeout_s: float = 60.0) -> None:
    """Poll ``pg_isready`` inside the container until it accepts connections."""
    deadline = time.monotonic() + timeout_s
    last_output = ""
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
    """Apply the real Alembic migration via the project's CLI against the DSN."""
    env = {
        **os.environ,
        "DATABASE_URL": _TEST_DSN,
        # Dummy values so app.config's required fields validate; unused here.
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


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Start a throwaway Postgres, apply migrations, yield the DSN, tear down.

    Skips the whole module gracefully when Docker is not available so the suite
    still passes in environments without a Docker daemon.
    """
    if not _docker_available():
        pytest.skip("Docker CLI/daemon not available")

    # Clean up any leftover container from a previous interrupted run.
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
    """A FRESH ``NullPool`` async engine bound to the test DSN.

    ``NullPool`` ensures no connection outlives a test, so the container can be
    removed cleanly at module teardown without dangling connections.
    """
    eng = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    """Async session factory bound to the throwaway engine."""
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


# --- Seed helpers ------------------------------------------------------------


async def _make_user(session: AsyncSession, email: str | None = None) -> User:
    """Insert a user (FK target for audit ``user_id`` and workspace owner)."""
    user = User(
        email=email or f"{uuid.uuid4().hex}@x.test",
        name="Test User",
        auth_provider=AuthProvider.GOOGLE,
        is_superadmin=False,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_workspace_with_owner(
    session: AsyncSession, owner: User
) -> Workspace:
    """Insert a workspace owned by ``owner`` plus the Owner membership row."""
    ws = Workspace(
        name=f"ws-{uuid.uuid4().hex[:8]}",
        slug=f"ws-{uuid.uuid4().hex}",
        created_by_user_id=owner.id,
    )
    session.add(ws)
    await session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=ws.id, user_id=owner.id, role=MemberRole.OWNER
        )
    )
    await session.flush()
    return ws


async def _audit_rows_for(
    session: AsyncSession,
    *,
    action: str,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> list[SystemAuditLog]:
    """Return audit rows matching ``action`` (+ optional user/workspace scope).

    Every assertion in this module scopes to a specific action plus the
    user/workspace it just created, so a shared module DB never causes a global
    count to interfere.
    """
    stmt = sa.select(SystemAuditLog).where(SystemAuditLog.action == action)
    if user_id is not None:
        stmt = stmt.where(SystemAuditLog.user_id == user_id)
    if workspace_id is not None:
        stmt = stmt.where(SystemAuditLog.workspace_id == workspace_id)
    result = await session.scalars(stmt)
    return list(result.all())


# --- App builders ------------------------------------------------------------


def _workspaces_app(
    db_sessionmaker: async_sessionmaker, ctx: RequestContext
) -> FastAPI:
    """Build the workspaces router app backed by the real DB session.

    ``require_session`` is overridden to yield the supplied ``ctx`` (so the
    endpoints run as that authenticated caller with those workspace roles), and
    ``get_session`` yields a real session from the throwaway database.
    """
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(workspaces_router)

    async def _get_session_override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as sess:
            yield sess

    app.dependency_overrides[deps.require_session] = lambda: ctx
    app.dependency_overrides[get_session] = _get_session_override
    return app


class _FakeRedis:
    """In-memory async Redis exposing just what the logout path touches."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None:  # pragma: no cover - cleanup no-op
        return None


def _auth_app(db_sessionmaker: async_sessionmaker) -> FastAPI:
    """Build the auth router app backed by the real DB session + fake redis."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)

    async def _get_session_override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as sess:
            yield sess

    async def _get_redis_override():
        yield _FakeRedis()

    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[deps.get_redis] = _get_redis_override
    return app


# ===========================================================================
# Req 15.1 — a state-changing workspace operation writes an audit row
# ===========================================================================


@pytest.mark.asyncio
async def test_create_workspace_route_writes_audit_row(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """POST /api/v1/workspaces (real router) writes a workspace.created row (15.1)."""
    async with db_sessionmaker() as sess:
        user = await _make_user(sess)
        await sess.commit()
        user_id = user.id

    ctx = RequestContext(user_id=user_id, active_workspace_id=None, roles={})
    app = _workspaces_app(db_sessionmaker, ctx)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    assert resp.status_code == 201
    created_workspace_id = uuid.UUID(resp.json()["id"])

    # An audit row scoped to exactly this workspace + acting user exists.
    async with db_sessionmaker() as sess:
        rows = await _audit_rows_for(
            sess,
            action=AUDIT_ACTION_WORKSPACE_CREATED,
            user_id=user_id,
            workspace_id=created_workspace_id,
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "workspace.created"
    assert row.workspace_id == created_workspace_id
    assert row.user_id == user_id
    # Metadata records name/slug and never a secret.
    assert row.log_metadata["name"] == "Acme"
    assert "slug" in row.log_metadata


@pytest.mark.asyncio
async def test_create_invite_route_writes_audit_row_without_token(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """POST .../invites (real router) writes invite_created audit; no token (15.1/15.4)."""
    async with db_sessionmaker() as sess:
        owner = await _make_user(sess)
        ws = await _make_workspace_with_owner(sess, owner)
        await sess.commit()
        owner_id = owner.id
        workspace_id = ws.id

    ctx = RequestContext(
        user_id=owner_id,
        active_workspace_id=workspace_id,
        roles={workspace_id: MemberRole.OWNER},
    )
    app = _workspaces_app(db_sessionmaker, ctx)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/invites",
            json={"email": "invitee@x.test", "role": "member"},
        )
    assert resp.status_code == 201
    token_hex = resp.json()["token"]
    assert token_hex  # sanity: a real token was issued to the caller

    async with db_sessionmaker() as sess:
        rows = await _audit_rows_for(
            sess,
            action=AUDIT_ACTION_INVITE_CREATED,
            user_id=owner_id,
            workspace_id=workspace_id,
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "workspace.invite_created"
    assert row.workspace_id == workspace_id
    assert row.user_id == owner_id
    # Metadata records the invitee email + role, and NEVER the token (Req 15.4).
    assert row.log_metadata == {"email": "invitee@x.test", "role": "member"}
    assert "token" not in row.log_metadata
    assert token_hex not in str(row.log_metadata)


# ===========================================================================
# Req 15.2 — an auth event writes an audit row
# ===========================================================================


@pytest.mark.asyncio
async def test_logout_route_writes_auth_logout_audit_row(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """POST /auth/logout (real router) writes an auth.logout row (Req 15.2)."""
    async with db_sessionmaker() as sess:
        user = await _make_user(sess)
        await sess.flush()
        user_id = user.id
        raw_token = f"logout-token-{uuid.uuid4().hex}"
        await persist_session(
            sess,
            user_id=user_id,
            raw_token=raw_token,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await sess.commit()

    app = _auth_app(db_sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.post(
            "/auth/logout", headers={"Authorization": f"Bearer {raw_token}"}
        )
    assert resp.status_code == 200

    # Exactly one auth.logout row for this user, with a NULL workspace_id.
    async with db_sessionmaker() as sess:
        rows = await _audit_rows_for(
            sess, action=AUDIT_ACTION_LOGOUT, user_id=user_id
        )
    assert len(rows) == 1
    assert rows[0].action == "auth.logout"
    assert rows[0].workspace_id is None
    assert rows[0].user_id == user_id


# ===========================================================================
# Req 15.3 — an approval resolution writes an audit row
# ===========================================================================


@pytest.mark.asyncio
async def test_approval_resolution_writes_audit_row(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """Resolving a pending approval writes an approval.approved row (Req 15.3).

    ``app/api/approvals.py`` does not exist yet (task 13.7 lands separately), so
    approval-resolution auditing is verified at the SERVICE level via
    :func:`app.services.approval_service.approve_request`.
    """
    async with db_sessionmaker() as sess:
        triggerer = await _make_user(sess)
        reviewer = await _make_user(sess)
        ws = await _make_workspace_with_owner(sess, triggerer)
        request = ApprovalRequest(
            workspace_id=ws.id,
            agent_session_id=None,
            triggered_by_user_id=triggerer.id,
            reviewed_by_user_id=None,
            tool_name="send_email",
            arguments={"to": "a@b.test"},
            status=ApprovalStatus.PENDING,
        )
        sess.add(request)
        await sess.flush()
        await sess.commit()
        workspace_id = ws.id
        reviewer_id = reviewer.id
        request_id = request.id

    async with db_sessionmaker() as sess:
        updated = await approve_request(
            sess,
            approval_request_id=request_id,
            reviewer_user_id=reviewer_id,
        )
        assert updated.status == ApprovalStatus.APPROVED

    async with db_sessionmaker() as sess:
        rows = await _audit_rows_for(
            sess,
            action="approval.approved",
            user_id=reviewer_id,
            workspace_id=workspace_id,
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "approval.approved"
    assert row.workspace_id == workspace_id
    assert row.user_id == reviewer_id
    # Metadata references the resolved request + tool, scoped to this request.
    assert row.log_metadata["approval_request_id"] == str(request_id)
    assert row.log_metadata["tool_name"] == "send_email"


# ===========================================================================
# Req 15.4 — metadata scrubbing at a recording point
# ===========================================================================


@pytest.mark.asyncio
async def test_router_recorded_metadata_is_scrubbed(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """A secret-looking field recorded via a router seam is redacted (Req 15.4).

    ``test_audit_service.py`` covers scrubbing at the service level. Here we
    prove it at an integration point: the invite-create route funnels its audit
    metadata through the same scrubbing path, so a secret-looking value never
    lands. We record via the workspaces router's ``_write_audit`` seam directly
    with a metadata payload carrying a secret-looking key, then read the raw
    JSONB back and assert the value is redacted to ``***`` and the raw secret is
    absent from the stored column.
    """
    from app.api.workspaces import _write_audit
    from app.core.scrubbing import REDACTED

    async with db_sessionmaker() as sess:
        owner = await _make_user(sess)
        ws = await _make_workspace_with_owner(sess, owner)
        await sess.commit()
        owner_id = owner.id
        workspace_id = ws.id

    secret = f"SECRET-{uuid.uuid4().hex}"
    action = f"workspace.custom_{uuid.uuid4().hex[:8]}"

    async with db_sessionmaker() as sess:
        await _write_audit(
            sess,
            action=action,
            workspace_id=workspace_id,
            user_id=owner_id,
            metadata={"access_token": secret, "note": "kept"},
        )
        await sess.commit()

    async with db_sessionmaker() as sess:
        rows = await _audit_rows_for(
            sess, action=action, user_id=owner_id, workspace_id=workspace_id
        )
        assert len(rows) == 1
        entry_id = rows[0].id
        stored_text = await sess.scalar(
            sa.text("SELECT metadata::text FROM system_audit_logs WHERE id = :id"),
            {"id": entry_id},
        )

    import json

    stored = json.loads(stored_text)
    # Secret-looking key is redacted; non-sensitive value survives.
    assert stored["access_token"] == REDACTED
    assert stored["note"] == "kept"
    # The raw secret string never appears anywhere in the persisted column.
    assert secret not in stored_text
