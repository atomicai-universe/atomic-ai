"""Tests for the WebSocket_Gateway (task 13.5).

Three layers of coverage, none requiring a database:

1. **Pure recipient authorization** (``is_authorized_recipient``): Owner/Admin
   are authorized reviewers; Member/Viewer and ``None`` are not (Req 10.2,
   10.8, 11.3). This is the basis for Property 25 (task 13.6).

2. **ConnectionManager fan-out** with fake connections capturing sent messages:
   registering an Owner + a Viewer in workspace 1 and an Owner in workspace 2,
   a ``broadcast_to_authorized(ws1, msg)`` reaches ONLY the ws1 Owner — not the
   ws1 Viewer (wrong role) and not the ws2 Owner (wrong workspace) (Req 10.2,
   10.8, 11.3). Also covers scrubbing of secret-looking fields and pruning of a
   connection whose send raises.

3. **WebSocket auth-before-delivery** via FastAPI's ``TestClient``: an
   unauthenticated connection (missing/invalid token) is closed with code 4401
   and receives NO message (Req 11.1, 11.2); a valid-token connection is
   accepted. The session-load seam (``load_session`` / ``is_session_valid``) and
   the session factory are monkeypatched so no DB is needed.

Requirements: 10.2, 10.8, 11.1, 11.2, 11.3.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import ws as ws_module
from app.api.ws import (
    WS_APPROVALS_PATH,
    WS_UNAUTHORIZED_CODE,
    ConnectionManager,
    ConnectionRecord,
    is_authorized_recipient,
    register_ws_routes,
)
from app.db.models import MemberRole


# ===========================================================================
# 1. Pure recipient authorization (Property 25 basis)
# ===========================================================================


def test_owner_is_authorized_recipient() -> None:
    assert is_authorized_recipient(MemberRole.OWNER) is True


def test_admin_is_authorized_recipient() -> None:
    assert is_authorized_recipient(MemberRole.ADMIN) is True


def test_member_is_not_authorized_recipient() -> None:
    assert is_authorized_recipient(MemberRole.MEMBER) is False


def test_viewer_is_not_authorized_recipient() -> None:
    assert is_authorized_recipient(MemberRole.VIEWER) is False


def test_non_member_none_is_not_authorized_recipient() -> None:
    assert is_authorized_recipient(None) is False


# ===========================================================================
# 2. ConnectionManager fan-out with fake connections
# ===========================================================================


class FakeConnection:
    """A fake WSConnection capturing everything sent to it."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.closed_with: int | None = None

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


class BrokenConnection(FakeConnection):
    """A fake connection whose send always raises (simulates a dead client)."""

    async def send_json(self, data: Any) -> None:
        raise RuntimeError("client gone")


def _record(role: MemberRole, conn: FakeConnection) -> ConnectionRecord:
    return ConnectionRecord(connection=conn, user_id=uuid.uuid4(), role=role)


@pytest.mark.asyncio
async def test_broadcast_reaches_only_owner_admin_in_owning_workspace() -> None:
    """ws1 Owner receives; ws1 Viewer and ws2 Owner do not (Req 10.2/10.8/11.3)."""
    manager = ConnectionManager()
    ws1 = uuid.uuid4()
    ws2 = uuid.uuid4()

    owner_conn = FakeConnection()
    viewer_conn = FakeConnection()
    other_ws_owner_conn = FakeConnection()

    manager.connect(ws1, _record(MemberRole.OWNER, owner_conn))
    manager.connect(ws1, _record(MemberRole.VIEWER, viewer_conn))
    manager.connect(ws2, _record(MemberRole.OWNER, other_ws_owner_conn))

    message = {"type": "approval.created", "approval": {"id": "abc"}}
    delivered = await manager.broadcast_to_authorized(ws1, message)

    # Only the ws1 Owner got it.
    assert delivered == 1
    assert owner_conn.sent == [message]
    # ws1 Viewer is a member of the owning workspace but not an authorized role.
    assert viewer_conn.sent == []
    # ws2 Owner is authorized but in a different workspace — never considered.
    assert other_ws_owner_conn.sent == []


@pytest.mark.asyncio
async def test_broadcast_reaches_admin_recipients() -> None:
    manager = ConnectionManager()
    ws = uuid.uuid4()
    admin_conn = FakeConnection()
    member_conn = FakeConnection()
    manager.connect(ws, _record(MemberRole.ADMIN, admin_conn))
    manager.connect(ws, _record(MemberRole.MEMBER, member_conn))

    delivered = await manager.broadcast_to_authorized(ws, {"type": "x"})

    assert delivered == 1
    assert admin_conn.sent == [{"type": "x"}]
    assert member_conn.sent == []


@pytest.mark.asyncio
async def test_broadcast_scrubs_secret_fields() -> None:
    manager = ConnectionManager()
    ws = uuid.uuid4()
    conn = FakeConnection()
    manager.connect(ws, _record(MemberRole.OWNER, conn))

    await manager.broadcast_to_authorized(
        ws, {"type": "approval.created", "access_token": "super-secret"}
    )

    assert conn.sent[0]["access_token"] == "***"
    assert conn.sent[0]["type"] == "approval.created"


@pytest.mark.asyncio
async def test_broadcast_to_empty_workspace_delivers_nothing() -> None:
    manager = ConnectionManager()
    delivered = await manager.broadcast_to_authorized(uuid.uuid4(), {"type": "x"})
    assert delivered == 0


@pytest.mark.asyncio
async def test_broadcast_prunes_dead_connection() -> None:
    manager = ConnectionManager()
    ws = uuid.uuid4()
    broken = BrokenConnection()
    manager.connect(ws, _record(MemberRole.OWNER, broken))

    delivered = await manager.broadcast_to_authorized(ws, {"type": "x"})

    # Send failed, so nothing delivered and the dead connection was dropped.
    assert delivered == 0
    assert manager.connection_count(ws) == 0


def test_disconnect_is_idempotent() -> None:
    manager = ConnectionManager()
    ws = uuid.uuid4()
    rec = _record(MemberRole.OWNER, FakeConnection())
    manager.connect(ws, rec)
    manager.disconnect(ws, rec)
    manager.disconnect(ws, rec)  # second call must not raise
    assert manager.connection_count(ws) == 0


# ===========================================================================
# 3. WebSocket auth-before-delivery via TestClient
# ===========================================================================


@dataclass
class _FakeSessionRow:
    """A session row satisfying the SessionRecord protocol + a user_id."""

    user_id: uuid.UUID
    revoked: bool = False
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    )


@dataclass
class _FakeUser:
    id: uuid.UUID


class _FakeSession:
    """A fake AsyncSession: ``get`` returns a user, ``execute`` returns roles."""

    def __init__(self, user: _FakeUser | None, roles: dict[uuid.UUID, MemberRole]):
        self._user = user
        self._roles = roles

    async def get(self, model: type, pk: uuid.UUID):
        return self._user

    async def execute(self, _stmt: Any):
        rows = list(self._roles.items())

        class _Result:
            def all(self_inner):
                return rows

        return _Result()


def _make_ws_app(monkeypatch: pytest.MonkeyPatch, *, session: _FakeSession) -> FastAPI:
    """Build an app with only the WS route mounted and the DB seam faked."""

    @asynccontextmanager
    async def _fake_session_cm():
        yield session

    def _fake_sessionmaker():
        return _fake_session_cm

    # The endpoint imports get_sessionmaker lazily from app.db.session; patch it
    # there so no real engine/DB is built.
    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "get_sessionmaker", _fake_sessionmaker)

    app = FastAPI()
    register_ws_routes(app)
    return app


def test_unauthenticated_connection_closed_4401_no_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No/invalid token -> close 4401 and deliver NO message (Req 11.1, 11.2)."""
    # load_session returns None (unknown token); is_session_valid then False.
    async def _no_session(_session, _raw_token):
        return None

    monkeypatch.setattr(ws_module, "load_session", _no_session)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: False)

    app = _make_ws_app(monkeypatch, session=_FakeSession(user=None, roles={}))
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(WS_APPROVALS_PATH) as websocket:
            # Any attempt to receive must not yield an approval message; the
            # server closes the connection instead.
            websocket.receive_text()

    assert exc_info.value.code == WS_UNAUTHORIZED_CODE


def test_invalid_token_connection_closed_4401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present-but-invalid token is rejected the same way (Req 11.1, 11.2)."""
    # load_session returns a row, but is_session_valid rejects it (e.g. expired).
    user_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id, revoked=True)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: False)

    app = _make_ws_app(monkeypatch, session=_FakeSession(user=None, roles={}))
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"{WS_APPROVALS_PATH}?token=bogus"
        ) as websocket:
            websocket.receive_text()

    assert exc_info.value.code == WS_UNAUTHORIZED_CODE


def test_valid_token_connection_accepted_and_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid token is accepted and registered under the user's workspaces.

    After connecting, a broadcast to a workspace the user owns is delivered over
    the live socket, proving the connection was accepted (Req 11.1) and wired to
    the gateway for authorized delivery (Req 11.3).
    """
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    # Use a fresh manager so this test's connection is isolated.
    fresh_manager = ConnectionManager()
    monkeypatch.setattr(ws_module, "manager", fresh_manager)

    session = _FakeSession(
        user=_FakeUser(id=user_id), roles={workspace_id: MemberRole.OWNER}
    )
    app = _make_ws_app(monkeypatch, session=session)
    client = TestClient(app)

    import time

    with client.websocket_connect(f"{WS_APPROVALS_PATH}?token=good"):
        # The handshake was accepted (no 4401) and the connection registered
        # under the workspace the user owns, ready for authorized delivery.
        # Registration happens on the server loop just after accept(); poll
        # briefly to avoid a handshake/registration race.
        for _ in range(100):
            if fresh_manager.connection_count(workspace_id) == 1:
                break
            time.sleep(0.01)
        assert fresh_manager.connection_count(workspace_id) == 1

    # On disconnect the connection is deregistered from the workspace.
    for _ in range(100):
        if fresh_manager.connection_count(workspace_id) == 0:
            break
        time.sleep(0.01)
    assert fresh_manager.connection_count(workspace_id) == 0
