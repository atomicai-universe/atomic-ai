"""Tests for the Strands_Engine session lifecycle and rule loading (task 11.3).

The lifecycle is exercised against a *real*, throwaway ``postgres:18.6-alpine``
on a non-default host port (55449) with the project's Alembic migration applied,
so :class:`~app.db.models.AgentSession` rows are genuine. A deterministic FAKE
``run_loop`` is injected so no LLM/model is ever called.

Assertions cover:

- **Creation + completion (Req 9.1, 9.4):** triggering a run creates an
  AgentSession that ends ``completed`` with ``total_tokens_used``,
  ``execution_time_ms``, and ``execution_logs`` recorded.
- **Applicable rules (Req 8.4):** the fake loop receives exactly the active,
  applicable rules for a seeded rule set — a workspace-wide active rule is
  included; an inactive rule is excluded.
- **Workspace-scoped tools (Req 9.2/9.3):** the fake loop receives the tool
  servers resolved from ``mcp_registry`` for the seeded integrations.
- **Unrecoverable error (Req 9.5):** a loop that raises ends the session
  ``failed`` with error details in ``execution_logs``, and re-raises.

The module skips gracefully when Docker is unavailable.

Requirements: 8.4, 9.1, 9.4, 9.5.
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
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.encryption import EncryptionService
from app.db.models import (
    AgentSession,
    AgentSessionStatus,
    IntegrationCategory,
    User,
    Workspace,
)
from app.services import mcp_registry, rules_service, strands_engine
from app.services import integration_vault
from app.services.strands_engine import (
    AgentRunResult,
    RunLoopContext,
    RunLoopResult,
)

# A real Fernet key for seeding integrations without process-wide config/env.
_TEST_ENC = EncryptionService(Fernet.generate_key())


# ---------------------------------------------------------------------------
# Throwaway Postgres infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_strands_engine_test"
_HOST_PORT = 55449  # non-default so we never touch another database
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


# ---------------------------------------------------------------------------
# Fake run loop — records the context it was called with; never calls a model.
# ---------------------------------------------------------------------------


class _RecordingRunLoop:
    """A deterministic fake ``run_loop`` that records its call context."""

    def __init__(
        self,
        *,
        result: RunLoopResult | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.result = result or RunLoopResult(
            total_tokens_used=1234,
            logs={"steps": ["thought", "acted"], "final": "done"},
            output="ok",
        )
        self.raises = raises
        self.calls: list[RunLoopContext] = []

    def __call__(self, ctx: RunLoopContext) -> RunLoopResult:
        self.calls.append(ctx)
        if self.raises is not None:
            raise self.raises
        return self.result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_creates_session_and_completes_with_metrics(
    session: AsyncSession,
) -> None:
    """A run creates a session, then ends COMPLETED with metrics (Req 9.1, 9.4)."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    await session.commit()

    loop = _RecordingRunLoop(
        result=RunLoopResult(
            total_tokens_used=4242,
            logs={"trace": ["a", "b"], "final": "complete"},
            output="result-text",
        )
    )

    result = await strands_engine.run_agent(
        session,
        workspace_id=workspace.id,
        triggered_by_user_id=user.id,
        thread_id="thread-1",
        prompt="do the thing",
        category="email",
        provider_name="gmail",
        run_loop=loop,
    )

    assert isinstance(result, AgentRunResult)
    assert result.status is AgentSessionStatus.COMPLETED
    assert result.total_tokens_used == 4242
    assert result.execution_time_ms >= 0

    # The persisted row reflects the terminal state and recorded metrics.
    row = await session.get(AgentSession, result.agent_session_id)
    assert row is not None
    assert row.workspace_id == workspace.id
    assert row.triggered_by_user_id == user.id
    assert row.thread_id == "thread-1"
    assert row.status is AgentSessionStatus.COMPLETED
    assert row.total_tokens_used == 4242
    assert row.execution_time_ms >= 0
    assert row.execution_logs == {"trace": ["a", "b"], "final": "complete"}

    # The loop was invoked exactly once with the correct identifiers.
    assert len(loop.calls) == 1
    ctx = loop.calls[0]
    assert ctx.agent_session_id == result.agent_session_id
    assert ctx.workspace_id == workspace.id
    assert ctx.triggered_by_user_id == user.id
    assert ctx.prompt == "do the thing"


@pytest.mark.asyncio
async def test_run_agent_loads_only_applicable_active_rules(
    session: AsyncSession,
) -> None:
    """Only active, applicable rules are loaded and passed to the loop (Req 8.4)."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)

    # (a) workspace-wide + active -> included regardless of category.
    ws_wide = await rules_service.create_rule(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category="crm",
        rule_prompt="always be polite",
        is_workspace_wide=True,
        is_active=True,
    )
    # (b) category-matching + active -> included for category "email".
    email_rule = await rules_service.create_rule(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category="email",
        rule_prompt="cc the manager",
        is_workspace_wide=False,
        is_active=True,
    )
    # (c) workspace-wide but INACTIVE -> excluded.
    inactive = await rules_service.create_rule(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category="calendar",
        rule_prompt="never schedule fridays",
        is_workspace_wide=True,
        is_active=False,
    )
    # (d) active but different category (not workspace-wide) -> excluded.
    other_cat = await rules_service.create_rule(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category="calendar",
        rule_prompt="only weekdays",
        is_workspace_wide=False,
        is_active=True,
    )
    await session.commit()

    loop = _RecordingRunLoop()

    await strands_engine.run_agent(
        session,
        workspace_id=workspace.id,
        triggered_by_user_id=user.id,
        thread_id="thread-rules",
        prompt="draft an email",
        category="email",
        provider_name="gmail",
        run_loop=loop,
    )

    assert len(loop.calls) == 1
    passed_ids = {r.id for r in loop.calls[0].rules}
    # Workspace-wide active + matching-category active are present.
    assert ws_wide.id in passed_ids
    assert email_rule.id in passed_ids
    # Inactive and non-matching-category rules are absent.
    assert inactive.id not in passed_ids
    assert other_cat.id not in passed_ids


@pytest.mark.asyncio
async def test_run_agent_passes_workspace_scoped_tools(
    session: AsyncSession,
) -> None:
    """Workspace-scoped tool servers are resolved and passed to the loop (Req 9.2/9.3)."""
    user = await _make_user(session)
    other = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    other_workspace = await _make_workspace(session, user.id)

    # Personal integration of the triggering user in the workspace -> included.
    personal = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        access_token="tok-personal",
        is_shared_with_workspace=False,
        encryption_service=_TEST_ENC,
    )
    # Shared integration in the workspace (created by other) -> included.
    shared = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=other.id,
        category=IntegrationCategory.CALENDAR,
        provider_name="gcal",
        access_token="tok-shared",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    # Another user's personal integration in the workspace -> excluded.
    others_personal = await integration_vault.store(
        session,
        workspace_id=workspace.id,
        created_by_user_id=other.id,
        category=IntegrationCategory.CRM,
        provider_name="salesforce",
        access_token="tok-other",
        is_shared_with_workspace=False,
        encryption_service=_TEST_ENC,
    )
    # Integration in a DIFFERENT workspace -> excluded (tenant isolation).
    foreign = await integration_vault.store(
        session,
        workspace_id=other_workspace.id,
        created_by_user_id=user.id,
        category=IntegrationCategory.DEVELOPER,
        provider_name="github",
        access_token="tok-foreign",
        is_shared_with_workspace=True,
        encryption_service=_TEST_ENC,
    )
    await session.commit()

    # Expected set from the registry for cross-check.
    expected = await mcp_registry.resolve(
        session, workspace_id=workspace.id, user_id=user.id
    )
    expected_ids = {s.integration_id for s in expected}

    loop = _RecordingRunLoop()
    await strands_engine.run_agent(
        session,
        workspace_id=workspace.id,
        triggered_by_user_id=user.id,
        thread_id="thread-tools",
        prompt="use the tools",
        category="email",
        provider_name="gmail",
        run_loop=loop,
    )

    assert len(loop.calls) == 1
    passed_ids = {s.integration_id for s in loop.calls[0].tool_servers}
    assert passed_ids == expected_ids
    assert passed_ids == {personal.id, shared.id}
    assert others_personal.id not in passed_ids
    assert foreign.id not in passed_ids


@pytest.mark.asyncio
async def test_run_agent_marks_failed_on_unrecoverable_error(
    session: AsyncSession,
) -> None:
    """A raising loop ends the session FAILED with error details (Req 9.5)."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    await session.commit()

    boom = RuntimeError("model exploded")
    loop = _RecordingRunLoop(raises=boom)

    with pytest.raises(RuntimeError, match="model exploded"):
        await strands_engine.run_agent(
            session,
            workspace_id=workspace.id,
            triggered_by_user_id=user.id,
            thread_id="thread-fail",
            prompt="cause an error",
            category="email",
            provider_name="gmail",
            run_loop=loop,
        )

    # Find the failed session for this workspace and assert its terminal state.
    from sqlalchemy import select

    row = await session.scalar(
        select(AgentSession).where(AgentSession.workspace_id == workspace.id)
    )
    assert row is not None
    assert row.status is AgentSessionStatus.FAILED
    assert row.execution_time_ms >= 0
    assert isinstance(row.execution_logs, dict)
    error = row.execution_logs["error"]
    assert error["type"] == "RuntimeError"
    assert error["message"] == "model exploded"
    assert "traceback" in error
