"""Tests for super-admin system analytics and the MCP health registry (task 14.5).

These drive :mod:`app.api.admin`'s ``GET /api/v1/admin/analytics`` and
``GET /api/v1/admin/mcp-health`` against a *real*, throwaway
``postgres:18.6-alpine`` (on a non-default host port) with the project's Alembic
migration applied, so the super-admin gate, the global token/session aggregation,
the database storage metrics (``pg_database_size``), and the per-provider MCP
health/error-rate aggregation are exercised end to end against a real database.
The suite skips gracefully when Docker is unavailable.

The app is built with only the admin router mounted and two dependencies
overridden (mirroring :mod:`tests.test_admin_sessions`):

- ``get_session`` -> a per-request session bound to the migrated test database
  using a fresh ``NullPool`` engine, so every request runs its connection on the
  request's own event loop (avoiding asyncpg cross-loop errors under
  ``httpx.ASGITransport``); and
- ``app.api.deps.require_session`` -> a per-test override injecting a
  :class:`~app.core.tenancy.RequestContext` for either a super admin or an
  ordinary user, so the ``require_superadmin`` gate is exercised directly.

This module owns its own throwaway container (a distinct host port and container
name), so the database is otherwise empty and the aggregates can be asserted
*exactly* against the fixed seeded dataset.

Assertions cover:

- a non-super-admin is rejected **403** on both endpoints (Req 12.1, 12.2 gate);
- analytics: ``tokens_total`` equals the known SUM of seeded
  ``total_tokens_used``, ``active_sessions`` equals the known ``running`` count,
  and ``storage_bytes`` > 0 (Req 14.2);
- mcp-health: a provider with 3 integrations (2 active, 1 error) reports
  ``total=3``, ``error=1``, ``error_rate=1/3`` (Req 14.1).

Requirements: 12.1, 12.2, 14.1, 14.2.
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
    AuthProvider,
    Integration,
    IntegrationCategory,
    IntegrationStatus,
    MemberRole,
    User,
    Workspace,
    WorkspaceMember,
)
from app.db.session import get_session

# ---------------------------------------------------------------------------
# Throwaway Postgres infrastructure (skips without Docker)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_admin_analytics_test"
_HOST_PORT = 55459  # non-default so we never touch another database
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
    """Build an app with the admin router and a per-request DB session override."""
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


def _act_as(app: FastAPI, *, user_id: uuid.UUID, is_superadmin: bool) -> None:
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
# Seed helpers
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
    tokens: int,
    status: AgentSessionStatus,
) -> AgentSession:
    agent_session = AgentSession(
        workspace_id=workspace.id,
        triggered_by_user_id=user.id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        total_tokens_used=tokens,
        status=status,
    )
    session.add(agent_session)
    await session.flush()
    return agent_session


async def _make_integration(
    session: AsyncSession,
    *,
    workspace: Workspace,
    user: User,
    provider_name: str,
    category: IntegrationCategory,
    status: IntegrationStatus,
) -> Integration:
    integration = Integration(
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category=category,
        provider_name=provider_name,
        encrypted_access_token=b"ciphertext",
        status=status,
    )
    session.add(integration)
    await session.flush()
    return integration


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_superadmin_is_forbidden_on_analytics_endpoints(
    app_client, db_session
) -> None:
    """A non-super-admin gets 403 on analytics and mcp-health (Req 12.1, 12.2)."""
    app, client = app_client
    ordinary = await _make_user(db_session, "Ordinary")
    await db_session.commit()

    _act_as(app, user_id=ordinary.id, is_superadmin=False)

    resp = await client.get("/api/v1/admin/analytics")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    resp = await client.get("/api/v1/admin/mcp-health")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    # Leave the database empty for the exact-aggregate test that follows, since
    # this module owns its own container and the aggregates are asserted exactly.
    await db_session.delete(ordinary)
    await db_session.commit()


@pytest.mark.asyncio
async def test_analytics_and_mcp_health_over_fixed_dataset(
    app_client, db_session
) -> None:
    """Analytics aggregates and MCP health match a fixed seeded dataset (Req 14.1, 14.2).

    This module owns an otherwise-empty database, so the global aggregates can be
    asserted exactly against what is seeded here.
    """
    app, client = app_client
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    owner_a = await _make_user(db_session, "OwnerA")
    owner_b = await _make_user(db_session, "OwnerB", provider=AuthProvider.GITHUB)
    ws_a = await _make_workspace(db_session, "Alpha", owner=owner_a)
    ws_b = await _make_workspace(db_session, "Beta", owner=owner_b)

    # Agent sessions with KNOWN token totals across two workspaces.
    #   running:   100 + 250            -> 2 running sessions
    #   completed: 400
    #   failed:     50
    # tokens_total = 100 + 250 + 400 + 50 = 800; active (running) = 2.
    await _make_agent_session(
        db_session, workspace=ws_a, user=owner_a,
        tokens=100, status=AgentSessionStatus.RUNNING,
    )
    await _make_agent_session(
        db_session, workspace=ws_b, user=owner_b,
        tokens=250, status=AgentSessionStatus.RUNNING,
    )
    await _make_agent_session(
        db_session, workspace=ws_a, user=owner_a,
        tokens=400, status=AgentSessionStatus.COMPLETED,
    )
    await _make_agent_session(
        db_session, workspace=ws_a, user=owner_a,
        tokens=50, status=AgentSessionStatus.FAILED,
    )
    expected_tokens_total = 800
    expected_active_sessions = 2

    # Integrations with mixed statuses per provider (which back MCP servers):
    #   "github":  2 active + 1 error   -> total 3, error_rate 1/3
    #   "slack":   1 active + 1 disconnected -> total 2, error_rate 0
    for status in (
        IntegrationStatus.ACTIVE,
        IntegrationStatus.ACTIVE,
        IntegrationStatus.ERROR,
    ):
        await _make_integration(
            db_session, workspace=ws_a, user=owner_a,
            provider_name="github", category=IntegrationCategory.DEVELOPER,
            status=status,
        )
    for status in (IntegrationStatus.ACTIVE, IntegrationStatus.DISCONNECTED):
        await _make_integration(
            db_session, workspace=ws_b, user=owner_b,
            provider_name="slack", category=IntegrationCategory.COLLABORATION,
            status=status,
        )
    await db_session.commit()

    _act_as(app, user_id=admin.id, is_superadmin=True)

    # --- system analytics (Req 14.2) ---
    resp = await client.get("/api/v1/admin/analytics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tokens_total"] == expected_tokens_total
    assert body["active_sessions"] == expected_active_sessions
    assert body["storage_bytes"] > 0
    # Core-table counts reflect the seeded rows.
    counts = body["table_counts"]
    assert counts["users"] == 3
    assert counts["workspaces"] == 2
    assert counts["integrations"] == 5
    assert counts["agent_sessions"] == 4

    # --- MCP health registry (Req 14.1) ---
    resp = await client.get("/api/v1/admin/mcp-health")
    assert resp.status_code == 200, resp.text
    health = resp.json()
    assert health["tokens_total"] == expected_tokens_total
    by_name = {p["name"]: p for p in health["providers"]}

    github = by_name["github"]
    assert github["total"] == 3
    assert github["active"] == 2
    assert github["error"] == 1
    assert github["disconnected"] == 0
    assert github["error_rate"] == pytest.approx(1 / 3)

    slack = by_name["slack"]
    assert slack["total"] == 2
    assert slack["active"] == 1
    assert slack["disconnected"] == 1
    assert slack["error"] == 0
    assert slack["error_rate"] == pytest.approx(0.0)
