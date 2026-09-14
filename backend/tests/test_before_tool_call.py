"""Tests for the BeforeToolCall hook and high-impact gating (task 13.1).

Two layers:

1. **Pure classifier (no DB/Docker):** :func:`app.services.approval_service.is_high_impact`
   classifies representative outbound/mutating/destructive tool names as
   high-impact and representative read-only names as not, and honours an
   overridden ruleset (Req 10.1).

2. **Hook behaviour against a real DB (skips without Docker):** exercised
   against a throwaway ``postgres:18.6-alpine`` on a non-default host port
   (55451) with the project's Alembic migration applied. A non-high-impact call
   yields ``proceed`` and creates **no** :class:`~app.db.models.ApprovalRequest`;
   a high-impact call yields *pause*, persists a ``pending`` request bound to the
   correct workspace/agent-session/tool with **scrubbed** arguments (no
   secret-looking value stored) (Req 10.1).

The container helper/fixture pattern mirrors ``tests/test_strands_engine.py``.

Requirements: 10.1.
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

from app.core.scrubbing import REDACTED
from app.db.models import (
    AgentSession,
    AgentSessionStatus,
    ApprovalRequest,
    ApprovalStatus,
    User,
    Workspace,
)
from app.services import approval_service
from app.services.approval_service import (
    ToolCallDecision,
    is_high_impact,
    make_before_tool_call,
)

# ===========================================================================
# Layer 1 — pure classifier (always runs; no DB/Docker)
# ===========================================================================


@pytest.mark.parametrize(
    "tool_name",
    [
        "send_email",
        "delete_record",
        "post_message",
        "deploy_service",
        "transfer_funds",
        # separator/case variants classify identically
        "send-email",
        "sendEmail",
        "SendEmail",
        "deleteRecord",
        "mcp.postMessage",
        "pay_invoice",
        "execute_script",
        "create_ticket",
        "update_contact",
    ],
)
def test_high_impact_names_are_gated(tool_name: str) -> None:
    """State-changing/outbound/destructive names classify as high-impact (Req 10.1)."""
    assert is_high_impact(tool_name) is True


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_email",
        "list_records",
        "read_message",
        "search_contacts",
        "fetch_report",
        "describe_service",
        "getEmail",
        "listRecords",
        "mcp.searchContacts",
        "view_dashboard",
    ],
)
def test_read_only_names_are_not_gated(tool_name: str) -> None:
    """Read-only names are not high-impact and proceed without review (Req 10.1)."""
    assert is_high_impact(tool_name) is False


def test_empty_or_unknown_verb_is_not_gated() -> None:
    """An empty or unknown-verb tool name is not high-impact by default."""
    assert is_high_impact("") is False
    assert is_high_impact("frobnicate_widget") is False


def test_generic_provider_tool_gated_by_write_method() -> None:
    """A generic ``{provider}_api`` write call is gated via its method arg (Req 10.1).

    The tool name's leading verb (``gmail``) is not a marker, so the write
    signal must come from ``arguments["method"]``: a state-changing HTTP method
    makes the call high-impact; a read-only method does not; and with no
    arguments the classifier falls back to the (non-high-impact) verb logic.
    """
    # create draft = POST /drafts -> gated.
    assert (
        is_high_impact(
            "gmail_api",
            {"method": "POST", "path": "/gmail/v1/users/me/drafts"},
        )
        is True
    )
    # read-only GET -> not gated.
    assert (
        is_high_impact(
            "gmail_api",
            {"method": "GET", "path": "/gmail/v1/users/me/messages"},
        )
        is False
    )
    # no arguments -> falls back to verb logic (gmail is not a marker).
    assert is_high_impact("gmail_api") is False
    # other state-changing methods also gate.
    for method in ("PUT", "PATCH", "DELETE"):
        assert is_high_impact("gmail_api", {"method": method, "path": "/x"}) is True


def test_override_ruleset_changes_classification() -> None:
    """An overridden ruleset tightens/widens the policy (Req 10.1)."""
    # A custom, narrow ruleset: only "frobnicate" is high-impact.
    custom = frozenset({"frobnicate"})
    assert is_high_impact("frobnicate_widget", markers=custom) is True
    # A default high-impact verb is NOT gated under this narrow ruleset.
    assert is_high_impact("send_email", markers=custom) is False
    # A normally read-only verb can be gated if the ruleset includes it.
    assert is_high_impact("get_secrets", markers=frozenset({"get"})) is True


def test_decision_helpers() -> None:
    """ToolCallDecision.allow/pause build the expected values."""
    allow = ToolCallDecision.allow()
    assert allow.proceed is True
    assert allow.approval_request_id is None

    rid = uuid.uuid4()
    pause = ToolCallDecision.pause(rid)
    assert pause.proceed is False
    assert pause.approval_request_id == rid


# ===========================================================================
# Layer 2 — hook behaviour against a real DB (skips without Docker)
# ===========================================================================

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_before_tool_call_test"
_HOST_PORT = 55451  # non-default so we never touch another database
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
    eng = create_async_engine(migrated_database, future=True, poolclass=NullPool)
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


async def _make_user(session: AsyncSession) -> User:
    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
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


async def _make_agent_session(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> AgentSession:
    agent = AgentSession(
        workspace_id=workspace_id,
        triggered_by_user_id=user_id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        status=AgentSessionStatus.RUNNING,
    )
    session.add(agent)
    await session.flush()
    return agent


@pytest.mark.asyncio
async def test_non_high_impact_proceeds_and_creates_no_request(
    session: AsyncSession,
) -> None:
    """A read-only call proceeds and persists no ApprovalRequest (Req 10.1)."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    agent = await _make_agent_session(session, workspace.id, user.id)
    await session.commit()

    hook = make_before_tool_call(
        session,
        workspace_id=workspace.id,
        agent_session_id=agent.id,
        triggered_by_user_id=user.id,
    )

    decision = await hook("get_contact", {"id": "123"})

    assert isinstance(decision, ToolCallDecision)
    assert decision.proceed is True
    assert decision.approval_request_id is None

    # No approval request row exists for this workspace.
    rows = (
        await session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == workspace.id
            )
        )
    ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_high_impact_pauses_and_persists_pending_request_scrubbed(
    session: AsyncSession,
) -> None:
    """A high-impact call pauses and persists a scrubbed pending request (Req 10.1)."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    agent = await _make_agent_session(session, workspace.id, user.id)
    await session.commit()

    hook = make_before_tool_call(
        session,
        workspace_id=workspace.id,
        agent_session_id=agent.id,
        triggered_by_user_id=user.id,
    )

    arguments = {
        "to": "person@example.com",
        "subject": "Q3 report",
        "access_token": "super-secret-token-value",
        "nested": {"api_key": "sk-live-should-not-persist", "note": "ok"},
    }
    decision = await hook("send_email", arguments)

    assert decision.proceed is False
    assert decision.approval_request_id is not None

    # Exactly one pending request persisted, bound to the right identifiers.
    row = await session.get(ApprovalRequest, decision.approval_request_id)
    assert row is not None
    assert row.status is ApprovalStatus.PENDING
    assert row.workspace_id == workspace.id
    assert row.agent_session_id == agent.id
    assert row.triggered_by_user_id == user.id
    assert row.reviewed_by_user_id is None
    assert row.tool_name == "send_email"

    # Non-secret arguments are preserved; secret-looking values are scrubbed.
    assert row.arguments["to"] == "person@example.com"
    assert row.arguments["subject"] == "Q3 report"
    assert row.arguments["access_token"] == REDACTED
    assert row.arguments["nested"]["api_key"] == REDACTED
    assert row.arguments["nested"]["note"] == "ok"

    # The raw secret values were never persisted anywhere in the JSONB.
    serialized = str(row.arguments)
    assert "super-secret-token-value" not in serialized
    assert "sk-live-should-not-persist" not in serialized


@pytest.mark.asyncio
async def test_override_classifier_controls_gating(
    session: AsyncSession,
) -> None:
    """An injected classifier decides gating; here nothing is gated (Req 10.1)."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    await session.commit()

    hook = make_before_tool_call(
        session,
        workspace_id=workspace.id,
        agent_session_id=None,
        triggered_by_user_id=user.id,
        is_high_impact=lambda name, args: False,
    )

    decision = await hook("send_email", {"to": "x@example.com"})
    assert decision.proceed is True
    assert decision.approval_request_id is None

    rows = (
        await session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == workspace.id
            )
        )
    ).all()
    assert rows == []


def test_default_markers_are_exposed() -> None:
    """The default ruleset is importable/inspectable for callers/tests."""
    assert "send" in approval_service.DEFAULT_HIGH_IMPACT_MARKERS
    assert "delete" in approval_service.DEFAULT_HIGH_IMPACT_MARKERS
    assert "get" not in approval_service.DEFAULT_HIGH_IMPACT_MARKERS


@pytest.mark.asyncio
async def test_gmail_reply_deduplicated_by_source_message_id(
    session: AsyncSession,
) -> None:
    """A gmail_reply is created AT MOST ONCE per source email (runaway-reply fix).

    The first gmail_reply for a given source_message_id gates + persists a
    pending request. A SECOND gmail_reply for the SAME source_message_id (as the
    5-minute poll would attempt while the first is still unapproved/unread) is
    SKIPPED: no new row, and the decision is "do not proceed" with no request id
    (so the tool reports already-handled and does not draft again).
    """
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    agent = await _make_agent_session(session, workspace.id, user.id)
    await session.commit()

    # Use a session_factory so the guard + persist run in fresh sessions like
    # production (the poll hook is invoked from a separate worker loop).
    factory = async_sessionmaker(
        bind=session.bind, class_=AsyncSession, expire_on_commit=False
    )

    hook = make_before_tool_call(
        session,
        workspace_id=workspace.id,
        agent_session_id=agent.id,
        triggered_by_user_id=user.id,
        session_factory=factory,
    )

    args = {
        "method": "POST",
        "path": "/gmail/v1/users/me/drafts",
        "body": {"message": {"raw": "cmF3", "threadId": "T1"}},
        "source_message_id": "MSG-123",
        "thread_id": "T1",
    }

    # First call: gated + persisted.
    first = await hook("gmail_reply", args)
    assert first.proceed is False
    assert first.approval_request_id is not None

    # Second call, SAME source_message_id: skipped (no row, no request id).
    second = await hook("gmail_reply", dict(args))
    assert second.proceed is False
    assert second.approval_request_id is None

    # A DIFFERENT source email still creates a reply.
    other = dict(args)
    other["source_message_id"] = "MSG-999"
    other["thread_id"] = "T9"
    third = await hook("gmail_reply", other)
    assert third.proceed is False
    assert third.approval_request_id is not None

    # Exactly TWO gmail_reply rows exist (MSG-123 once, MSG-999 once) — the
    # duplicate for MSG-123 was never persisted.
    rows = (
        await session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == workspace.id,
                ApprovalRequest.tool_name == "gmail_reply",
            )
        )
    ).all()
    assert len(rows) == 2, [r.arguments.get("source_message_id") for r in rows]


@pytest.mark.asyncio
async def test_gmail_reply_dedup_ignores_status(session: AsyncSession) -> None:
    """Dedup holds even after the first reply is REJECTED/APPROVED — a handled
    email is never re-drafted regardless of the approval's status or the email's
    read/unread state."""
    user = await _make_user(session)
    workspace = await _make_workspace(session, user.id)
    await session.commit()

    factory = async_sessionmaker(
        bind=session.bind, class_=AsyncSession, expire_on_commit=False
    )
    hook = make_before_tool_call(
        session,
        workspace_id=workspace.id,
        agent_session_id=None,
        triggered_by_user_id=user.id,
        session_factory=factory,
    )
    args = {
        "method": "POST",
        "path": "/gmail/v1/users/me/drafts",
        "body": {"message": {"raw": "cmF3"}},
        "source_message_id": "MSG-STATUS",
    }
    first = await hook("gmail_reply", args)
    assert first.approval_request_id is not None

    # Mark the first as REJECTED, then try again — still deduped.
    row = await session.get(ApprovalRequest, first.approval_request_id)
    row.status = ApprovalStatus.REJECTED
    await session.commit()

    again = await hook("gmail_reply", dict(args))
    assert again.proceed is False
    assert again.approval_request_id is None
