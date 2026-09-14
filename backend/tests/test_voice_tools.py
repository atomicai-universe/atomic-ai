"""Tests for the voice tool layer (backend/app/services/voice_tools.py).

Two layers:

1. **DB-free unit tests** for the pure handlers (navigate, type_text,
   submit_form), the tool-spec surface, and membership enforcement — no Docker.
2. **DB-backed tests** against a throwaway ``postgres:18.6-alpine`` (non-default
   port) with migrations applied, mirroring the approval-execution harness. The
   Gmail HTTP layer is monkeypatched (``agent_tools.call_provider_api``) to
   canned responses and the approval action services are patched to record
   calls, so NO real network and NO real Gmail send happen. Asserts:

   - list_unread_emails / read_email speak the count, senders, subject, body;
   - list_pending_approvals / read_approval decode a seeded gmail_reply;
   - action tools (edit/regenerate/approve+save/approve+send/approve+schedule)
     delegate to approval_service with reviewer_user_id + reviewer_role from
     ctx, and execution routes through approval_service (never a direct send);
   - schedule with a PAST time -> error envelope (no exception);
   - a non-member ctx -> forbidden envelope for any tool.

This is ONLY the tool layer: no gateway, no WebSocket, no Nova Sonic calls.
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.errors import APIError
from app.core.tenancy import RequestContext
from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    MemberRole,
    User,
    Workspace,
)
from app.services import approval_service, gmail_message, voice_tools

# ---------------------------------------------------------------------------
# Shared: build a RequestContext (member or non-member)
# ---------------------------------------------------------------------------


def _ctx(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID, role: MemberRole | None
) -> RequestContext:
    roles = {workspace_id: role} if role is not None else {}
    return RequestContext(
        user_id=user_id,
        active_workspace_id=workspace_id,
        roles=roles,
    )


# ---------------------------------------------------------------------------
# DB-free unit tests (pure handlers + specs + membership)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_valid_target_returns_action_path() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "navigate", {"target": "approvals"}, ctx=ctx, session=None, workspace_id=ws
    )
    assert out["action"] == {"type": "navigate", "path": "/dashboard/approvals"}
    assert "approvals" in out["speak"].lower()


@pytest.mark.asyncio
async def test_navigate_natural_phrases_resolve() -> None:
    """Natural spoken phrases resolve to the right route (filler words stripped)."""
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    cases = {
        "rules menu": "/dashboard/rules",
        "go to the approvals page": "/dashboard/approvals",
        "open integrations": "/dashboard/integrations",
        "team members": "/dashboard/workspace",
        "approvals tab": "/dashboard/approvals",
    }
    for phrase, path in cases.items():
        out = await voice_tools.execute_voice_tool(
            "navigate", {"target": phrase}, ctx=ctx, session=None, workspace_id=ws
        )
        assert out.get("action", {}).get("path") == path, (phrase, out)


@pytest.mark.asyncio
async def test_navigate_truly_unknown_phrase_still_errors() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "navigate", {"target": "the moon"}, ctx=ctx, session=None, workspace_id=ws
    )
    assert out["error"] == "unknown_target"


@pytest.mark.asyncio
async def test_navigate_unknown_target_returns_error_no_exception() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "navigate", {"target": "nowhere"}, ctx=ctx, session=None, workspace_id=ws
    )
    assert out["error"] == "unknown_target"
    assert "action" not in out


@pytest.mark.asyncio
async def test_type_text_allowlist_and_credential_prefix() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.MEMBER)

    ok = await voice_tools.execute_voice_tool(
        "type_text",
        {"field": "compose_body", "text": "Hello"},
        ctx=ctx, session=None, workspace_id=ws,
    )
    assert ok["action"] == {"type": "type", "field": "compose_body", "text": "Hello"}

    cred = await voice_tools.execute_voice_tool(
        "type_text",
        {"field": "credential:api_key", "text": "abc"},
        ctx=ctx, session=None, workspace_id=ws,
    )
    assert cred["action"]["field"] == "credential:api_key"

    bad = await voice_tools.execute_voice_tool(
        "type_text",
        {"field": "not_allowed", "text": "x"},
        ctx=ctx, session=None, workspace_id=ws,
    )
    assert bad["error"] == "unknown_field"
    assert "action" not in bad


@pytest.mark.asyncio
async def test_submit_form_allowlist() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.MEMBER)

    ok = await voice_tools.execute_voice_tool(
        "submit_form", {"form": "create_rule"}, ctx=ctx, session=None, workspace_id=ws
    )
    assert ok["action"] == {"type": "submit", "form": "create_rule"}

    bad = await voice_tools.execute_voice_tool(
        "submit_form", {"form": "delete_everything"}, ctx=ctx, session=None, workspace_id=ws
    )
    assert bad["error"] == "unknown_form"
    assert "action" not in bad


@pytest.mark.asyncio
async def test_unknown_tool_returns_error() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "not_a_tool", {}, ctx=ctx, session=None, workspace_id=ws
    )
    assert out["error"] == "unknown_tool"


@pytest.mark.asyncio
async def test_non_member_is_forbidden_for_any_tool() -> None:
    ws = uuid.uuid4()
    ctx = _ctx(user_id=uuid.uuid4(), workspace_id=ws, role=None)
    # Even a pure/no-DB tool must be refused for a non-member.
    out = await voice_tools.execute_voice_tool(
        "navigate", {"target": "approvals"}, ctx=ctx, session=None, workspace_id=ws
    )
    assert out["error"] == "forbidden"
    assert "action" not in out


def test_tool_specs_and_names_are_consistent() -> None:
    names = {spec["name"] for spec in voice_tools.VOICE_TOOL_SPECS}
    assert names == voice_tools.VOICE_TOOL_NAMES
    # Every spec has a description + inputSchema; names match the dispatcher.
    for spec in voice_tools.VOICE_TOOL_SPECS:
        assert spec["description"]
        assert spec["inputSchema"]["type"] == "object"
    assert voice_tools.VOICE_TOOL_NAMES == set(voice_tools._HANDLERS.keys())


# ---------------------------------------------------------------------------
# DB-backed setup (skip if Docker unavailable)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_voice_tools_test"
_HOST_PORT = 55461  # non-default so we never touch another database
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@localhost:{_HOST_PORT}/{_PG_DB}"
)

_ENC_KEY = "test-encryption-key-value-0123456789"


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
                ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB],
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
        "ENCRYPTION_KEY": _ENC_KEY,
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


@pytest.fixture(autouse=True)
def _config_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from cryptography.fernet import Fernet

    from app import config as _config

    for key, value in {
        "DATABASE_URL": _TEST_DSN,
        "REDIS_URL": "redis://localhost:6379/0",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret-value",
        "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret-value",
    }.items():
        monkeypatch.setenv(key, value)
    _config.get_settings.cache_clear()
    try:
        yield
    finally:
        _config.get_settings.cache_clear()


@pytest_asyncio.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


async def _make_user(session: AsyncSession) -> User:
    user = User(email=f"{uuid.uuid4().hex}@x.test", name="U", auth_provider="google")
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


def _sample_reply_arguments() -> dict:
    payload = gmail_message.build_draft_payload(
        to="orig@sender.com",
        subject="Question",
        body="Original reply body.",
        in_reply_to="<abc@mail>",
        thread_id="THREAD1",
    )
    return {
        "method": "POST",
        "path": "/gmail/v1/users/me/drafts",
        "body": payload,
        "source_message_id": "MSG123",
        "thread_id": "THREAD1",
    }


async def _make_pending_reply(
    session: AsyncSession, *, ws: Workspace, user: User
) -> ApprovalRequest:
    req = ApprovalRequest(
        workspace_id=ws.id,
        agent_session_id=None,
        triggered_by_user_id=user.id,
        tool_name=approval_service.GMAIL_REPLY_TOOL,
        arguments=_sample_reply_arguments(),
        status=ApprovalStatus.PENDING,
    )
    session.add(req)
    await session.flush()
    return req


def _patch_gmail_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the real vault/oauth: resolve_gmail_credentials -> fake creds."""
    async def _fake_resolve(session, *, workspace_id):
        return (uuid.uuid4(), {"access_token": "tok"}, {})

    monkeypatch.setattr(
        approval_service, "resolve_gmail_credentials", _fake_resolve
    )


# ---------------------------------------------------------------------------
# DB-backed: READ handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_unread_emails_speaks_count_and_senders(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await session.commit()
    _patch_gmail_creds(monkeypatch)

    async def _fake_call(*, provider_name, credentials, config, method, path, query=None, body=None):
        if path == "/gmail/v1/users/me/messages":
            return {"status_code": 200, "ok": True, "body": {"messages": [{"id": "m1"}, {"id": "m2"}]}}
        return {
            "status_code": 200, "ok": True,
            "body": {
                "snippet": "hi there",
                "payload": {"headers": [
                    {"name": "From", "value": f"sender-{path[-1]}@x.com"},
                    {"name": "Subject", "value": f"Subject {path[-1]}"},
                ]},
            },
        }

    monkeypatch.setattr(voice_tools.agent_tools, "call_provider_api", _fake_call)

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "list_unread_emails", {"max_results": 5}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert "2 unread" in out["speak"]
    assert "sender-1@x.com" in out["speak"]
    assert len(out["data"]) == 2


@pytest.mark.asyncio
async def test_read_email_speaks_subject_from_body(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64 as _b64

    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await session.commit()
    _patch_gmail_creds(monkeypatch)

    body_text = "This is the full message body."
    encoded = _b64.urlsafe_b64encode(body_text.encode()).decode()

    async def _fake_call(*, provider_name, credentials, config, method, path, query=None, body=None):
        return {
            "status_code": 200, "ok": True,
            "body": {
                "snippet": "snippet",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alice@x.com"},
                        {"name": "Subject", "value": "Hello there"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": encoded},
                },
            },
        }

    monkeypatch.setattr(voice_tools.agent_tools, "call_provider_api", _fake_call)

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "read_email", {"message_id": "m1"}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert "Hello there" in out["speak"]
    assert "alice@x.com" in out["speak"]
    assert body_text in out["speak"]
    assert out["data"]["body"] == body_text


@pytest.mark.asyncio
async def test_list_unread_emails_no_gmail_integration_is_friendly(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await session.commit()

    async def _no_integration(session, *, workspace_id):
        raise APIError(status_code=404, code="integration_not_found", message="none")

    monkeypatch.setattr(approval_service, "resolve_gmail_credentials", _no_integration)

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "list_unread_emails", {}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert out["error"] == "integration_not_found"
    assert "Gmail" in out["speak"]


# ---------------------------------------------------------------------------
# DB-backed: approvals READ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pending_approvals_decodes_reply(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "list_pending_approvals", {}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert "1 reply awaiting approval" in out["speak"]
    assert "orig@sender.com" in out["speak"]
    assert len(out["data"]) == 1


@pytest.mark.asyncio
async def test_read_approval_decodes_body(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "read_approval", {"approval_id": str(req.id)}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert "orig@sender.com" in out["speak"]
    assert "Original reply body." in out["speak"]


# ---------------------------------------------------------------------------
# DB-backed: ACTION handlers delegate to approval_service ONLY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_reply_delegates_to_approval_service(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    calls: list[dict] = []

    async def _fake_edit(session, *, approval_request_id, subject=None, body=None, to=None):
        calls.append({"id": approval_request_id, "subject": subject, "body": body, "to": to})
        return req

    monkeypatch.setattr(approval_service, "edit_request", _fake_edit)

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "edit_reply",
        {"approval_id": str(req.id), "body": "new body"},
        ctx=ctx, session=session, workspace_id=ws.id,
    )
    assert "error" not in out
    assert calls == [{"id": req.id, "subject": None, "body": "new body", "to": None}]


@pytest.mark.asyncio
async def test_regenerate_reply_delegates(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    calls: list[uuid.UUID] = []

    async def _fake_regen(session, *, approval_request_id, generate_body=None):
        calls.append(approval_request_id)
        return req

    monkeypatch.setattr(approval_service, "regenerate_request", _fake_regen)

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "regenerate_reply", {"approval_id": str(req.id)}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert "error" not in out
    assert calls == [req.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected_action"),
    [("approve_and_save_draft", "save_to_draft"), ("approve_and_send", "send")],
)
async def test_approve_actions_route_through_approval_service(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tool: str, expected_action: str
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    calls: list[dict] = []

    async def _fake_execute(
        session, *, approval_request_id, reviewer_user_id, execution_action,
        reviewer_role=None, call_provider_api=None,
    ):
        calls.append({
            "id": approval_request_id,
            "reviewer_user_id": reviewer_user_id,
            "execution_action": execution_action,
            "reviewer_role": reviewer_role,
        })
        return req

    monkeypatch.setattr(approval_service, "execute_and_approve_request", _fake_execute)
    # Ensure there is NO direct Gmail path: fail loudly if anyone calls the API.
    async def _boom(*a, **k):
        raise AssertionError("voice must not call Gmail directly for actions")

    monkeypatch.setattr(voice_tools.agent_tools, "call_provider_api", _boom)

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        tool, {"approval_id": str(req.id)}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert "error" not in out
    assert len(calls) == 1
    call = calls[0]
    assert call["execution_action"] == expected_action
    assert call["reviewer_user_id"] == user.id
    assert call["reviewer_role"] is MemberRole.OWNER


@pytest.mark.asyncio
async def test_approve_and_schedule_delegates_with_future_time(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    calls: list[dict] = []

    async def _fake_schedule(
        session, *, approval_request_id, reviewer_user_id, scheduled_send_at,
        reviewer_role=None,
    ):
        calls.append({
            "id": approval_request_id,
            "reviewer_user_id": reviewer_user_id,
            "when": scheduled_send_at,
            "reviewer_role": reviewer_role,
        })
        return req

    monkeypatch.setattr(approval_service, "schedule_request", _fake_schedule)

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "approve_and_schedule",
        {"approval_id": str(req.id), "when_iso": future},
        ctx=ctx, session=session, workspace_id=ws.id,
    )
    assert "error" not in out
    assert len(calls) == 1
    assert calls[0]["reviewer_user_id"] == user.id
    assert calls[0]["reviewer_role"] is MemberRole.OWNER


@pytest.mark.asyncio
async def test_approve_and_schedule_past_time_returns_error(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    req = await _make_pending_reply(session, ws=ws, user=user)
    await session.commit()

    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "approve_and_schedule",
        {"approval_id": str(req.id), "when_iso": past},
        ctx=ctx, session=session, workspace_id=ws.id,
    )
    # The real schedule_request raises APIError(422); the dispatcher converts it.
    assert "error" in out
    assert "action" not in out


@pytest.mark.asyncio
async def test_action_on_other_workspace_is_not_found(session: AsyncSession) -> None:
    owner = await _make_user(session)
    ws = await _make_workspace(session, owner.id)
    req = await _make_pending_reply(session, ws=ws, user=owner)

    other_user = await _make_user(session)
    other_ws = await _make_workspace(session, other_user.id)
    await session.commit()

    # Caller is a member of other_ws but references ws's approval id.
    ctx = _ctx(user_id=other_user.id, workspace_id=other_ws.id, role=MemberRole.OWNER)
    out = await voice_tools.execute_voice_tool(
        "read_approval", {"approval_id": str(req.id)}, ctx=ctx, session=session, workspace_id=other_ws.id
    )
    assert out["error"] == "not_found"


@pytest.mark.asyncio
async def test_non_member_forbidden_db_tool(session: AsyncSession) -> None:
    user = await _make_user(session)
    ws = await _make_workspace(session, user.id)
    await session.commit()

    ctx = _ctx(user_id=user.id, workspace_id=ws.id, role=None)
    out = await voice_tools.execute_voice_tool(
        "list_pending_approvals", {}, ctx=ctx, session=session, workspace_id=ws.id
    )
    assert out["error"] == "forbidden"
