"""Tests for the approvals router (task 13.7).

These drive :mod:`app.api.approvals` against a *real*, throwaway
``postgres:18.6-alpine`` (on a non-default host port) with the project's Alembic
migration applied, so the membership floor, the ``RESOLVE_APPROVAL`` gate, the
terminal state machine, and the resolution broadcast are exercised against a
real database.

The app is built with only the approvals router mounted and two dependencies
overridden:

- ``get_session`` -> a per-request session on a fresh ``NullPool`` engine, so
  every request runs its connection on the request's own event loop (avoiding
  asyncpg cross-loop errors under ``httpx.ASGITransport``); and
- ``app.api.deps.require_session`` -> a per-test override injecting a
  :class:`~app.core.tenancy.RequestContext` with the caller's workspace roles,
  so the membership/RBAC guards are exercised directly.

The WebSocket_Gateway broadcast is monkeypatched to record calls so we can
assert a broadcast fires exactly once on success and never on a rejected
(403) attempt.

Assertions cover (Req 10.5, 10.7, 10.8, 4.3):

- approve by Owner/Admin -> 200 ``approved`` + reviewer recorded + one
  broadcast;
- reject by Owner -> 200 ``rejected`` + one broadcast;
- approve/reject by Member/Viewer -> 403, no state change, no broadcast;
- re-resolving a terminal request -> 409;
- GET queue returns the workspace's pending requests for a member;
- a non-member gets 404 on GET and on resolve.

Requirements: 10.5.
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
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.api.approvals as approvals_module
from app.api.approvals import router as approvals_router
from app.api.deps import require_session
from app.core.errors import install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import (
    AgentSession,
    ApprovalRequest,
    ApprovalStatus,
    AuthProvider,
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

_CONTAINER_NAME = "atomic_approvals_router_test"
_HOST_PORT = 55457  # non-default so we never touch another database
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
    """Build an app with the approvals router and a per-request DB session."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(approvals_router)

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
    roles: dict[uuid.UUID, MemberRole],
) -> None:
    """Override ``require_session`` to inject a context for ``user_id``."""
    ctx = RequestContext(
        user_id=user_id,
        active_workspace_id=None,
        is_superadmin=False,
        roles=roles,
    )

    async def _require_session_override() -> RequestContext:
        return ctx

    app.dependency_overrides[require_session] = _require_session_override


class _BroadcastRecorder:
    """Records ``broadcast_to_authorized`` calls in place of the real gateway."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, dict]] = []

    async def broadcast_to_authorized(self, workspace_id, message):
        self.calls.append((workspace_id, message))
        return len(self.calls)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _BroadcastRecorder:
    """Monkeypatch the router's ws manager singleton with a recorder."""
    rec = _BroadcastRecorder()
    monkeypatch.setattr(approvals_module, "manager", rec)
    return rec


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
# Seed helpers (create users first; FKs are NOT NULL)
# ---------------------------------------------------------------------------


async def _make_user(session: AsyncSession, name: str) -> User:
    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
        name=name,
        auth_provider=AuthProvider.GOOGLE,
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


async def _add_member(
    session: AsyncSession, *, ws: Workspace, user: User, role: MemberRole
) -> None:
    session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=role)
    )
    await session.flush()


async def _make_agent_session(
    session: AsyncSession, *, ws: Workspace, user: User
) -> AgentSession:
    agent = AgentSession(
        workspace_id=ws.id,
        triggered_by_user_id=user.id,
        thread_id=uuid.uuid4().hex,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _make_pending_request(
    session: AsyncSession,
    *,
    ws: Workspace,
    triggered_by: User,
    agent_session: AgentSession,
    tool_name: str = "send_email",
) -> ApprovalRequest:
    req = ApprovalRequest(
        workspace_id=ws.id,
        agent_session_id=agent_session.id,
        triggered_by_user_id=triggered_by.id,
        tool_name=tool_name,
        arguments={"to": "a@b.test"},
        status=ApprovalStatus.PENDING,
    )
    session.add(req)
    await session.flush()
    return req


async def _status_of(
    session: AsyncSession, request_id: uuid.UUID
) -> ApprovalStatus:
    return await session.scalar(
        sa.select(ApprovalRequest.status).where(ApprovalRequest.id == request_id)
    )


async def _reviewer_of(
    session: AsyncSession, request_id: uuid.UUID
) -> uuid.UUID | None:
    return await session.scalar(
        sa.select(ApprovalRequest.reviewed_by_user_id).where(
            ApprovalRequest.id == request_id
        )
    )


# A fresh scenario per test: an owner/admin/member/viewer, a workspace with all
# four roles, an agent session, and a pending approval request.
class _Scenario:
    def __init__(self):
        self.owner: User
        self.admin: User
        self.member: User
        self.viewer: User
        self.outsider: User
        self.ws: Workspace
        self.agent: AgentSession
        self.request: ApprovalRequest

    def roles_for(self, role: MemberRole) -> dict[uuid.UUID, MemberRole]:
        """The ``{workspace_id: role}`` mapping a member with ``role`` would carry.

        :meth:`RequestContext.member_role` looks up by *workspace id*, so the
        acting user's context maps the seeded workspace to that user's role.
        """
        return {self.ws.id: role}


async def _seed(db_session: AsyncSession) -> _Scenario:
    s = _Scenario()
    s.owner = await _make_user(db_session, "Owner")
    s.admin = await _make_user(db_session, "Admin")
    s.member = await _make_user(db_session, "Member")
    s.viewer = await _make_user(db_session, "Viewer")
    s.outsider = await _make_user(db_session, "Outsider")
    s.ws = await _make_workspace(db_session, "WS", owner=s.owner)
    await _add_member(db_session, ws=s.ws, user=s.admin, role=MemberRole.ADMIN)
    await _add_member(db_session, ws=s.ws, user=s.member, role=MemberRole.MEMBER)
    await _add_member(db_session, ws=s.ws, user=s.viewer, role=MemberRole.VIEWER)
    s.agent = await _make_agent_session(db_session, ws=s.ws, user=s.member)
    s.request = await _make_pending_request(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_by_owner_records_reviewer_and_broadcasts(
    app_client, db_session, recorder
) -> None:
    """Owner approve -> 200 approved + reviewer recorded + one broadcast (Req 10.5, 10.8)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["reviewed_by_user_id"] == str(s.owner.id)
    assert body["id"] == str(s.request.id)
    # arguments are the (already scrubbed) safe view; no reviewer secret leaked.
    assert set(body.keys()) == {
        "id",
        "tool_name",
        "arguments",
        "status",
        "triggered_by_user_id",
        "reviewed_by_user_id",
        "agent_session_id",
        "created_at",
        # Added by "Approve and Schedule" (null unless scheduled) — CHANGE 2.
        "scheduled_send_at",
    }
    # Not scheduled here, so the field is present but null.
    assert body["scheduled_send_at"] is None

    assert await _status_of(db_session, s.request.id) is ApprovalStatus.APPROVED
    assert await _reviewer_of(db_session, s.request.id) == s.owner.id

    # Exactly one broadcast to the owning workspace with the resolved payload.
    assert len(recorder.calls) == 1
    ws_id, message = recorder.calls[0]
    assert ws_id == s.ws.id
    assert message == {
        "type": "approval.resolved",
        "approval_request_id": str(s.request.id),
        "status": "approved",
        "workspace_id": str(s.ws.id),
    }


@pytest.mark.asyncio
async def test_approve_by_admin_succeeds_and_broadcasts(
    app_client, db_session, recorder
) -> None:
    """Admin (holds RESOLVE_APPROVAL) may approve (Req 10.5)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.admin.id, roles=s.roles_for(MemberRole.ADMIN))
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert await _reviewer_of(db_session, s.request.id) == s.admin.id
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_reject_by_owner_succeeds_and_broadcasts(
    app_client, db_session, recorder
) -> None:
    """Owner reject -> 200 rejected + one broadcast (Req 10.4, 10.8)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/reject")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert await _status_of(db_session, s.request.id) is ApprovalStatus.REJECTED
    assert len(recorder.calls) == 1
    assert recorder.calls[0][1]["status"] == "rejected"


@pytest.mark.asyncio
async def test_member_cannot_approve(app_client, db_session, recorder) -> None:
    """A Member lacks RESOLVE_APPROVAL -> 403, no state change, no broadcast (Req 10.5)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.member.id, roles=s.roles_for(MemberRole.MEMBER))
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/approve")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    assert await _status_of(db_session, s.request.id) is ApprovalStatus.PENDING
    assert await _reviewer_of(db_session, s.request.id) is None
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_viewer_cannot_reject(app_client, db_session, recorder) -> None:
    """A Viewer lacks RESOLVE_APPROVAL -> 403, no state change, no broadcast (Req 10.5)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.viewer.id, roles=s.roles_for(MemberRole.VIEWER))
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/reject")
    assert resp.status_code == 403, resp.text

    assert await _status_of(db_session, s.request.id) is ApprovalStatus.PENDING
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_re_resolving_terminal_conflicts(
    app_client, db_session, recorder
) -> None:
    """A second resolution of a terminal request -> 409 (Req 10.7)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    first = await client.post(f"/api/v1/approvals/{s.request.id}/approve")
    assert first.status_code == 200, first.text
    assert len(recorder.calls) == 1

    # Re-approve and reject both conflict now.
    again = await client.post(f"/api/v1/approvals/{s.request.id}/approve")
    assert again.status_code == 409, again.text
    reject = await client.post(f"/api/v1/approvals/{s.request.id}/reject")
    assert reject.status_code == 409, reject.text

    # Status unchanged; no extra broadcast beyond the first success.
    assert await _status_of(db_session, s.request.id) is ApprovalStatus.APPROVED
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_get_queue_returns_workspace_pending_for_member(
    app_client, db_session
) -> None:
    """GET ?status=pending returns the workspace's pending requests for a member."""
    app, client = app_client
    s = await _seed(db_session)
    # A second pending request in the same workspace.
    second = await _make_pending_request(
        db_session,
        ws=s.ws,
        triggered_by=s.member,
        agent_session=s.agent,
        tool_name="delete_record",
    )
    await db_session.commit()

    _act_as(app, user_id=s.member.id, roles=s.roles_for(MemberRole.MEMBER))
    resp = await client.get(
        "/api/v1/approvals", params={"workspace_id": str(s.ws.id), "status": "pending"}
    )
    assert resp.status_code == 200, resp.text
    approvals = resp.json()["approvals"]
    # Scope assertions to the seeded workspace's requests (module DB is shared).
    ids = {a["id"] for a in approvals}
    assert str(s.request.id) in ids
    assert str(second.id) in ids
    for a in approvals:
        assert a["status"] == "pending"


@pytest.mark.asyncio
async def test_non_member_gets_404_on_queue(app_client, db_session) -> None:
    """A non-member gets 404 on GET (existence not disclosed, Req 4.3, 16.2)."""
    app, client = app_client
    s = await _seed(db_session)

    # Outsider has no role in the workspace.
    _act_as(app, user_id=s.outsider.id, roles={})
    resp = await client.get(
        "/api/v1/approvals", params={"workspace_id": str(s.ws.id)}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_non_member_gets_404_on_resolve(
    app_client, db_session, recorder
) -> None:
    """A non-member gets 404 on resolve, no state change, no broadcast (Req 4.3)."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.outsider.id, roles={})
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/approve")
    assert resp.status_code == 404, resp.text

    assert await _status_of(db_session, s.request.id) is ApprovalStatus.PENDING
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_resolve_missing_request_404(app_client, db_session, recorder) -> None:
    """Resolving a non-existent request -> 404, no broadcast."""
    app, client = app_client
    s = await _seed(db_session)

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(f"/api/v1/approvals/{s.request.id}/approve")  # baseline
    assert resp.status_code == 200
    resp = await client.post(f"/api/v1/approvals/{uuid.uuid4()}/reject")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# New endpoints: regenerate / edit / approve-draft / approve-send (PART 3 + 4)
# ---------------------------------------------------------------------------


async def _make_pending_gmail_reply(
    session: AsyncSession, *, ws: Workspace, triggered_by: User, agent_session: AgentSession
) -> ApprovalRequest:
    """Seed a pending gmail_reply approval with a real base64url draft + source id."""
    from app.services import gmail_message

    payload = gmail_message.build_draft_payload(
        to="orig@sender.com",
        subject="Question",
        body="Original body.",
        in_reply_to="<abc@mail>",
        thread_id="THREAD1",
    )
    req = ApprovalRequest(
        workspace_id=ws.id,
        agent_session_id=agent_session.id,
        triggered_by_user_id=triggered_by.id,
        tool_name="gmail_reply",
        arguments={
            "method": "POST",
            "path": "/gmail/v1/users/me/drafts",
            "body": payload,
            "source_message_id": "MSG123",
            "thread_id": "THREAD1",
        },
        status=ApprovalStatus.PENDING,
    )
    session.add(req)
    await session.flush()
    return req


@pytest.mark.asyncio
async def test_edit_by_owner_rebuilds_and_stays_pending(app_client, db_session) -> None:
    """PATCH edits the reply, rebuilds the draft, stays pending (Owner)."""
    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.patch(
        f"/api/v1/approvals/{req.id}",
        json={"subject": "Edited", "body": "A new edited body."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert await _status_of(db_session, req.id) is ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_edit_empty_body_422(app_client, db_session) -> None:
    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.patch(f"/api/v1/approvals/{req.id}", json={"body": "   "})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_edit_by_member_forbidden(app_client, db_session) -> None:
    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    _act_as(app, user_id=s.member.id, roles=s.roles_for(MemberRole.MEMBER))
    resp = await client.patch(f"/api/v1/approvals/{req.id}", json={"body": "x"})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_regenerate_by_member_forbidden(app_client, db_session) -> None:
    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    _act_as(app, user_id=s.viewer.id, roles=s.roles_for(MemberRole.VIEWER))
    resp = await client.post(f"/api/v1/approvals/{req.id}/regenerate")
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_approve_draft_by_member_forbidden(app_client, db_session, recorder) -> None:
    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    _act_as(app, user_id=s.member.id, roles=s.roles_for(MemberRole.MEMBER))
    resp = await client.post(f"/api/v1/approvals/{req.id}/approve-draft")
    assert resp.status_code == 403, resp.text
    assert await _status_of(db_session, req.id) is ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_approve_send_missing_404(app_client, db_session) -> None:
    app, client = app_client
    s = await _seed(db_session)
    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(f"/api/v1/approvals/{uuid.uuid4()}/approve-send")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_approve_draft_no_gmail_integration_404_leaves_pending(
    app_client, db_session
) -> None:
    """Owner approve-draft with no Gmail integration -> 404, stays pending."""
    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(f"/api/v1/approvals/{req.id}/approve-draft")
    # No active gmail Integration seeded -> resolve_gmail_credentials raises 404,
    # and crucially the approval is NOT flipped.
    assert resp.status_code == 404, resp.text
    assert await _status_of(db_session, req.id) is ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# approve-schedule (CHANGE 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_schedule_by_owner_stays_pending_and_returns_time(
    app_client, db_session
) -> None:
    """POST /approve-schedule with a future time stores the schedule, stays pending."""
    from datetime import datetime, timedelta, timezone

    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(
        f"/api/v1/approvals/{req.id}/approve-schedule",
        json={"scheduled_send_at": when},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "pending"
    assert payload["scheduled_send_at"] is not None
    assert await _status_of(db_session, req.id) is ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_approve_schedule_past_time_422(app_client, db_session) -> None:
    from datetime import datetime, timedelta, timezone

    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _act_as(app, user_id=s.owner.id, roles=s.roles_for(MemberRole.OWNER))
    resp = await client.post(
        f"/api/v1/approvals/{req.id}/approve-schedule",
        json={"scheduled_send_at": past},
    )
    assert resp.status_code == 422, resp.text
    assert await _status_of(db_session, req.id) is ApprovalStatus.PENDING


@pytest.mark.asyncio
async def test_approve_schedule_by_member_forbidden(app_client, db_session) -> None:
    from datetime import datetime, timedelta, timezone

    app, client = app_client
    s = await _seed(db_session)
    req = await _make_pending_gmail_reply(
        db_session, ws=s.ws, triggered_by=s.member, agent_session=s.agent
    )
    await db_session.commit()

    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _act_as(app, user_id=s.member.id, roles=s.roles_for(MemberRole.MEMBER))
    resp = await client.post(
        f"/api/v1/approvals/{req.id}/approve-schedule",
        json={"scheduled_send_at": when},
    )
    assert resp.status_code == 403, resp.text
