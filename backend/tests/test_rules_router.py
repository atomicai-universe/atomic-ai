"""Tests for the rules router: rule CRUD with RBAC + audit (task 10.3).

Coverage focuses on the two requirements the task wires:

- **Req 8.5** — a Viewer/Member attempting to create or modify a
  *workspace-wide* rule is rejected with **403**; an Owner/Admin succeeds.
- **Req 15.1** — a successful state-changing operation writes an
  ``system_audit_logs`` row recording the workspace, acting user, and action
  (``rule.created`` / ``rule.updated`` / ``rule.deleted``).

The suite prefers a throwaway ``postgres:18.6-alpine`` on a non-default port so
the whole flow (RBAC decision → rules_service persistence → audit row) runs
against a real database and the audit rows are read back. If Docker is not
available the module is skipped.

A FastAPI app is built with the rules router mounted; ``get_session`` is
overridden to the real DB sessionmaker and ``require_session`` is overridden to
return a :class:`RequestContext` for a chosen caller/role, so no OAuth/session
machinery is exercised here.

Requirements: 8.5, 15.1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
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

from app.api import deps
from app.api.rules import (
    AUDIT_ACTION_CREATED,
    AUDIT_ACTION_DELETED,
    AUDIT_ACTION_UPDATED,
    router as rules_router,
)
from app.core.errors import install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import (
    AuthProvider,
    MemberRole,
    Rule,
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

_CONTAINER_NAME = "atomic_rules_router_test"
_HOST_PORT = 55448  # non-default so we never touch another database
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
async def db_sessionmaker(engine: AsyncEngine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


# ===========================================================================
# Fixtures: seed a workspace with one member per role
# ===========================================================================


@pytest_asyncio.fixture
async def seeded(db_sessionmaker):
    """Create a workspace + one user per role; return ids for the tests."""
    slug = f"ws-{uuid.uuid4().hex[:8]}"
    async with db_sessionmaker() as sess:
        # A creator user is required for the NOT NULL workspaces.created_by_user_id.
        creator = User(
            email=f"creator-{uuid.uuid4().hex}@x.test",
            name="Creator",
            auth_provider=AuthProvider.GOOGLE,
            is_superadmin=False,
        )
        sess.add(creator)
        await sess.flush()

        workspace = Workspace(
            name="Rules WS", slug=slug, created_by_user_id=creator.id
        )
        sess.add(workspace)
        await sess.flush()

        users: dict[MemberRole, uuid.UUID] = {}
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
            sess.add(
                WorkspaceMember(
                    workspace_id=workspace.id, user_id=user.id, role=role
                )
            )
            users[role] = user.id

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
    """Build the rules app with the DB session + a fixed request context."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(rules_router)

    async def _get_session_override():
        async with db_sessionmaker() as sess:
            yield sess

    def _require_session_override() -> RequestContext:
        return ctx

    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[deps.require_session] = _require_session_override
    return app


def _client(db_sessionmaker, ctx: RequestContext) -> httpx.AsyncClient:
    """Async client running the app in-process on the current event loop.

    Using ``httpx.AsyncClient`` + ``ASGITransport`` (rather than the sync
    ``TestClient``) keeps the app's DB work on the same event loop as the test
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


# ===========================================================================
# Req 8.5 — workspace-wide create authorization
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.ADMIN])
async def test_owner_admin_can_create_workspace_wide_rule(
    db_sessionmaker, seeded, role
):
    ws = seeded["workspace_id"]
    ctx = _make_context(user_id=seeded["users"][role], workspace_id=ws, role=role)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/rules",
            json={
                "workspace_id": str(ws),
                "category": "email",
                "rule_prompt": "Always CC the manager.",
                "is_workspace_wide": True,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_workspace_wide"] is True
    assert body["category"] == "email"

    # Req 15.1 — a rule.created audit row was persisted for this workspace/user.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_CREATED)
    assert len(rows) == 1
    assert rows[0].user_id == seeded["users"][role]
    assert rows[0].log_metadata["is_workspace_wide"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER])
async def test_member_viewer_cannot_create_workspace_wide_rule(
    db_sessionmaker, seeded, role
):
    ws = seeded["workspace_id"]
    ctx = _make_context(user_id=seeded["users"][role], workspace_id=ws, role=role)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/rules",
            json={
                "workspace_id": str(ws),
                "category": "email",
                "rule_prompt": "Shared rule attempt.",
                "is_workspace_wide": True,
            },
        )
    assert resp.status_code == 403, resp.text

    # No rule.created audit row for a rejected workspace-wide create.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_CREATED)
    assert all(r.user_id != seeded["users"][role] for r in rows)


# ===========================================================================
# Req 8.5 — workspace-wide modify authorization
# ===========================================================================


async def _create_workspace_wide_rule(db_sessionmaker, workspace_id, creator_id):
    async with db_sessionmaker() as sess:
        rule = Rule(
            workspace_id=workspace_id,
            created_by_user_id=creator_id,
            category="email",
            rule_prompt="Seed shared rule.",
            is_workspace_wide=True,
            is_active=True,
        )
        sess.add(rule)
        await sess.commit()
        return rule.id


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER])
async def test_member_viewer_cannot_modify_workspace_wide_rule(
    db_sessionmaker, seeded, role
):
    ws = seeded["workspace_id"]
    rule_id = await _create_workspace_wide_rule(
        db_sessionmaker, ws, seeded["users"][MemberRole.OWNER]
    )
    ctx = _make_context(user_id=seeded["users"][role], workspace_id=ws, role=role)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.patch(
            f"/api/v1/rules/{rule_id}",
            json={"workspace_id": str(ws), "rule_prompt": "Hijacked."},
        )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_owner_can_modify_workspace_wide_rule_and_audits(
    db_sessionmaker, seeded
):
    ws = seeded["workspace_id"]
    owner_id = seeded["users"][MemberRole.OWNER]
    rule_id = await _create_workspace_wide_rule(db_sessionmaker, ws, owner_id)
    ctx = _make_context(user_id=owner_id, workspace_id=ws, role=MemberRole.OWNER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.patch(
            f"/api/v1/rules/{rule_id}",
            json={"workspace_id": str(ws), "is_active": False},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False

    # Req 15.1 — the update wrote a rule.updated audit row.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_UPDATED)
    assert len(rows) == 1
    assert rows[0].user_id == owner_id


# ===========================================================================
# list — any member (VIEW)
# ===========================================================================


@pytest.mark.asyncio
async def test_viewer_can_list_rules(db_sessionmaker, seeded):
    ws = seeded["workspace_id"]
    await _create_workspace_wide_rule(
        db_sessionmaker, ws, seeded["users"][MemberRole.OWNER]
    )
    viewer_id = seeded["users"][MemberRole.VIEWER]
    ctx = _make_context(user_id=viewer_id, workspace_id=ws, role=MemberRole.VIEWER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.get("/api/v1/rules", params={"workspace_id": str(ws)})
    assert resp.status_code == 200, resp.text
    rules = resp.json()["rules"]
    assert len(rules) >= 1
    assert all(r["workspace_id"] == str(ws) for r in rules)


# ===========================================================================
# Req 15.1 — a state-changing op writes the expected audit action
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_workspace_wide_rule_writes_audit(db_sessionmaker, seeded):
    ws = seeded["workspace_id"]
    owner_id = seeded["users"][MemberRole.OWNER]
    rule_id = await _create_workspace_wide_rule(db_sessionmaker, ws, owner_id)
    ctx = _make_context(user_id=owner_id, workspace_id=ws, role=MemberRole.OWNER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.delete(
            f"/api/v1/rules/{rule_id}", params={"workspace_id": str(ws)}
        )
    assert resp.status_code == 204, resp.text

    # Req 15.1 — a rule.deleted audit row recording the workspace + acting user.
    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_DELETED)
    assert len(rows) == 1
    assert rows[0].user_id == owner_id
    assert rows[0].log_metadata["rule_id"] == str(rule_id)

    # And the rule is actually gone.
    async with db_sessionmaker() as sess:
        gone = await sess.get(Rule, rule_id)
    assert gone is None


@pytest.mark.asyncio
async def test_member_can_create_personal_rule_and_audits(db_sessionmaker, seeded):
    """A non-workspace-wide (personal) rule is allowed for a Member (Req 15.1 audit)."""
    ws = seeded["workspace_id"]
    member_id = seeded["users"][MemberRole.MEMBER]
    ctx = _make_context(user_id=member_id, workspace_id=ws, role=MemberRole.MEMBER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/rules",
            json={
                "workspace_id": str(ws),
                "category": "calendar",
                "rule_prompt": "My personal rule.",
                "is_workspace_wide": False,
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_workspace_wide"] is False

    rows = await _audit_rows(db_sessionmaker, ws, AUDIT_ACTION_CREATED)
    assert any(r.user_id == member_id for r in rows)


@pytest.mark.asyncio
async def test_viewer_cannot_create_personal_rule(db_sessionmaker, seeded):
    """Viewer is read-only: creating even a personal rule is rejected (403)."""
    ws = seeded["workspace_id"]
    viewer_id = seeded["users"][MemberRole.VIEWER]
    ctx = _make_context(user_id=viewer_id, workspace_id=ws, role=MemberRole.VIEWER)
    async with _client(db_sessionmaker, ctx) as client:
        resp = await client.post(
            "/api/v1/rules",
            json={
                "workspace_id": str(ws),
                "category": "calendar",
                "rule_prompt": "Viewer personal rule attempt.",
                "is_workspace_wide": False,
            },
        )
    assert resp.status_code == 403, resp.text
