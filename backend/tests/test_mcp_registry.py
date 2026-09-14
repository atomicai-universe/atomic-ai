"""Tests for MCP_Registry workspace-scoped tool resolution (task 11.1).

Two layers of coverage:

1. **Pure predicate tests** (no DB, always run): exercise
   :func:`app.services.mcp_registry.is_resolvable_for` directly. These form the
   basis for Property 22 (task 11.2): the agent toolset is workspace-scoped and
   personal-or-shared.

   - the triggering user's PERSONAL integration in the workspace -> resolvable.
   - ANOTHER user's personal integration in the same workspace -> NOT resolvable.
   - a SHARED integration in the workspace -> resolvable (regardless of creator).
   - an integration from ANOTHER workspace -> NOT resolvable (even if shared)
     (Req 9.6).
   - a non-ACTIVE (error/disconnected) integration -> NOT resolvable.

2. **DB-backed ``resolve()`` test** against a *real*, throwaway
   ``postgres:18.6-alpine`` on a non-default host port (55442) with the
   project's Alembic migration applied. It seeds a workspace with (a) the
   user's personal integration, (b) another user's personal integration,
   (c) a shared integration, and (d) an integration in a DIFFERENT workspace,
   plus a non-active one, then asserts ``resolve()`` returns exactly {a, c} and
   never b or d, and excludes non-active integrations. The module skips
   gracefully when Docker is unavailable.

Requirements: 9.2, 9.3, 9.6.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.encryption import EncryptionService
from app.db.models import (
    IntegrationCategory,
    IntegrationStatus,
    User,
    Workspace,
)
from app.services import integration_vault, mcp_registry
from app.services.mcp_registry import ToolServer, is_resolvable_for

# A real Fernet key for the DB tests' encryption service so seeding integrations
# does not require the process-wide config/env (cleared by the autouse fixture).
_TEST_ENC = EncryptionService(Fernet.generate_key())


# ---------------------------------------------------------------------------
# Pure predicate tests (no DB) — always run. Basis for Property 22.
# ---------------------------------------------------------------------------


@dataclass
class _FakeIntegration:
    """In-memory stand-in exposing exactly what is_resolvable_for reads."""

    workspace_id: uuid.UUID
    created_by_user_id: uuid.UUID
    is_shared_with_workspace: bool
    status: IntegrationStatus = IntegrationStatus.ACTIVE


def test_personal_integration_of_triggering_user_is_resolvable() -> None:
    """The triggering user's own personal integration is resolvable (Req 9.2)."""
    ws = uuid.uuid4()
    user = uuid.uuid4()
    integ = _FakeIntegration(
        workspace_id=ws, created_by_user_id=user, is_shared_with_workspace=False
    )
    assert is_resolvable_for(integ, ws, user) is True


def test_other_users_personal_integration_not_resolvable() -> None:
    """Another user's personal integration in the same workspace is NOT resolvable."""
    ws = uuid.uuid4()
    user = uuid.uuid4()
    other = uuid.uuid4()
    integ = _FakeIntegration(
        workspace_id=ws, created_by_user_id=other, is_shared_with_workspace=False
    )
    assert is_resolvable_for(integ, ws, user) is False


def test_shared_integration_is_resolvable_regardless_of_creator() -> None:
    """A shared integration is resolvable regardless of who created it (Req 9.2)."""
    ws = uuid.uuid4()
    user = uuid.uuid4()
    other = uuid.uuid4()
    integ = _FakeIntegration(
        workspace_id=ws, created_by_user_id=other, is_shared_with_workspace=True
    )
    assert is_resolvable_for(integ, ws, user) is True


def test_integration_from_another_workspace_not_resolvable_even_if_shared() -> None:
    """An integration in another workspace is never resolvable (Req 9.6)."""
    request_ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    user = uuid.uuid4()
    # Even a shared integration owned by the triggering user, but in a DIFFERENT
    # workspace, must never leak into this workspace's toolset (tenant isolation).
    integ = _FakeIntegration(
        workspace_id=other_ws, created_by_user_id=user, is_shared_with_workspace=True
    )
    assert is_resolvable_for(integ, request_ws, user) is False


def test_non_active_integration_not_resolvable() -> None:
    """Error/disconnected integrations cannot back a tool server."""
    ws = uuid.uuid4()
    user = uuid.uuid4()
    for bad_status in (IntegrationStatus.ERROR, IntegrationStatus.DISCONNECTED):
        # Even the user's own shared integration is skipped when not ACTIVE.
        integ = _FakeIntegration(
            workspace_id=ws,
            created_by_user_id=user,
            is_shared_with_workspace=True,
            status=bad_status,
        )
        assert is_resolvable_for(integ, ws, user) is False


def test_select_tool_servers_filters_and_preserves_order() -> None:
    """select_tool_servers returns only resolvable integrations, in order."""
    ws = uuid.uuid4()
    user = uuid.uuid4()
    other = uuid.uuid4()
    other_ws = uuid.uuid4()

    personal = _FakeIntegration(ws, user, False)  # resolvable (a)
    others_personal = _FakeIntegration(ws, other, False)  # not resolvable (b)
    shared = _FakeIntegration(ws, other, True)  # resolvable (c)
    foreign = _FakeIntegration(other_ws, user, True)  # not resolvable (d)

    selected = mcp_registry.select_tool_servers(
        [personal, others_personal, shared, foreign], ws, user
    )
    assert selected == [personal, shared]


# ---------------------------------------------------------------------------
# DB-backed integration test infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_mcp_registry_test"
_HOST_PORT = 55442  # non-default so we never touch another database
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


async def _make_workspace(session: AsyncSession, creator_id: uuid.UUID) -> Workspace:
    ws = Workspace(
        name=f"ws-{uuid.uuid4().hex[:8]}",
        slug=f"ws-{uuid.uuid4().hex}",
        created_by_user_id=creator_id,
    )
    session.add(ws)
    await session.flush()
    return ws


# ---------------------------------------------------------------------------
# DB-backed resolve() test (Req 9.2, 9.3, 9.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_only_personal_and_shared_of_this_workspace(
    session: AsyncSession,
) -> None:
    """resolve() returns exactly {personal-of-user, shared}, never b/d/non-active."""
    user = await _make_user(session)
    other = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    other_workspace = await _make_workspace(session, user.id)

    # (a) the triggering user's PERSONAL integration in the workspace -> included.
    a = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        access_token="tok-a",
        is_shared_with_workspace=False,
        encryption_service=_TEST_ENC,
    )
    # (b) ANOTHER user's personal integration in the same workspace -> excluded.
    b = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=other.id,
        category=IntegrationCategory.CRM,
        provider_name="salesforce",
        access_token="tok-b",
        is_shared_with_workspace=False,
        encryption_service=_TEST_ENC,
    )
    # (c) a SHARED integration in the workspace (created by other) -> included.
    c = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=other.id,
        category=IntegrationCategory.CALENDAR,
        provider_name="gcal",
        access_token="tok-c",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    # (d) a shared integration in a DIFFERENT workspace -> excluded (Req 9.6).
    d = await integration_vault.store(
        session,
        workspace_id=other_workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.DEVELOPER,
        provider_name="github",
        access_token="tok-d",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    # (e) a shared but NON-ACTIVE integration in the workspace -> excluded.
    e = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.SUPPORT,
        provider_name="zendesk",
        access_token="tok-e",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    e.status = IntegrationStatus.DISCONNECTED
    await session.flush()
    await session.commit()

    servers = await mcp_registry.resolve(
        session, workspace_id=workspace.id, user_id=user.id
    )

    assert all(isinstance(s, ToolServer) for s in servers)
    resolved_ids = {s.integration_id for s in servers}
    assert resolved_ids == {a.id, c.id}
    assert b.id not in resolved_ids
    assert d.id not in resolved_ids
    assert e.id not in resolved_ids

    # Descriptor fields carry the backing integration's metadata.
    by_id = {s.integration_id: s for s in servers}
    assert by_id[a.id].is_shared is False
    assert by_id[a.id].provider_name == "gmail"
    assert by_id[c.id].is_shared is True
    assert by_id[c.id].category is IntegrationCategory.CALENDAR


@pytest.mark.asyncio
async def test_resolve_scopes_to_requesting_workspace_only(
    session: AsyncSession,
) -> None:
    """resolve() for the other workspace only sees that workspace's integrations."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    other_workspace = await _make_workspace(session, user.id)

    here = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        access_token="tok-here",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    there = await integration_vault.store(
        session,
        workspace_id=other_workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        access_token="tok-there",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    await session.commit()

    servers = await mcp_registry.resolve(
        session, workspace_id=other_workspace.id, user_id=user.id
    )
    resolved_ids = {s.integration_id for s in servers}
    assert resolved_ids == {there.id}
    assert here.id not in resolved_ids
