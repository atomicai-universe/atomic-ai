"""End-to-end integration test for the collaborative approval flow (task 13.8).

Exercises the REAL WebSocket_Gateway (:mod:`app.api.ws`) end to end over a real
``websocket_connect`` using Starlette's ``TestClient``, together with the shared
gateway ``manager`` singleton the approvals router (:mod:`app.api.approvals`)
broadcasts through. It asserts the two security guarantees of the collaborative
approval feed:

- **Auth-before-delivery / unauthenticated close (Req 11.1).** A WebSocket that
  presents no token — or an invalid/expired token — is closed by the gateway
  with application code 4401 and receives NO message. A connection presenting a
  valid Session_Token for an Owner is accepted.
- **Authorized delivery over the wire (Req 11.2 / 10.8 / 11.3).** With a live
  Owner (authorized) and a live Viewer (unauthorized) connected for the same
  workspace, the ``intercept -> broadcast(created) -> resolve ->
  broadcast(resolved)`` sequence delivers both events to the Owner's socket and
  NEITHER to the Viewer's socket.

Design of the harness
---------------------
The WS endpoint authenticates by calling
:func:`app.api.ws.load_session` + :func:`app.api.ws.is_session_valid` and then
loading the user's workspace roles. To keep the test a *pure over-the-wire*
exercise of the gateway's auth + fan-out logic — free of the app's global,
lru-cached async engine and its event-loop coupling — those three seams
(``load_session``, ``is_session_valid``, and the role loader
``_load_user_roles``) are monkeypatched with in-memory fakes keyed by the token.
Everything else is the real code path: the real ``approvals_ws_endpoint``, the
real ``ConnectionManager``/``manager`` singleton and its authorization filter,
and the real ``broadcast_to_authorized`` fan-out driven over genuine
``websocket_connect`` sockets. No database is required, so the test is fast and
deterministic and needs no Docker.

The ``intercept`` (create a pending approval) and ``resolve`` (approve) steps are
represented by publishing the corresponding ``approval.created`` /
``approval.resolved`` events through the same ``manager.broadcast_to_authorized``
call the approvals router uses on resolution — i.e. the exact fan-out the
production resolution path invokes.

Requirements: 11.1, 11.2.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.ws as ws_module
from app.api.ws import (
    WS_APPROVALS_PATH,
    WS_UNAUTHORIZED_CODE,
    ConnectionManager,
    register_ws_routes,
)
from app.db.models import MemberRole


# ---------------------------------------------------------------------------
# In-memory session/role fakes keyed by token (no DB — see module docstring)
# ---------------------------------------------------------------------------


class _FakeSessionRow:
    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id
        self.revoked = False
        # Far-future expiry so is_session_valid accepts it.
        from datetime import datetime, timedelta, timezone

        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)


@pytest.fixture
def wired(monkeypatch):
    """Build an app with the real WS route, with the auth seams faked.

    Returns (client, ids). A fresh ConnectionManager is installed as the shared
    singleton so registrations from this test never leak across tests.
    """
    workspace_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    viewer_id = uuid.uuid4()

    # token -> (user_id, {workspace_id: role})
    directory = {
        "owner-token": (owner_id, {workspace_id: MemberRole.OWNER}),
        "viewer-token": (viewer_id, {workspace_id: MemberRole.VIEWER}),
    }

    async def _fake_load_session(session, raw_token):
        entry = directory.get(raw_token)
        if entry is None:
            return None
        return _FakeSessionRow(entry[0])

    def _fake_is_session_valid(record, now):
        return record is not None and not record.revoked and now < record.expires_at

    class _FakeUser:
        def __init__(self, uid):
            self.id = uid
            self.is_superadmin = False

    async def _fake_get(model, pk):  # AsyncSession.get(User, id) -> a user with .id
        return _FakeUser(pk)

    async def _fake_load_roles(session, user_id):
        for _tok, (uid, roles) in directory.items():
            if uid == user_id:
                return roles
        return {}

    # A no-op async session/context so the endpoint's `async with factory()`
    # works without a real database.
    class _FakeSession:
        async def get(self, model, pk):
            return await _fake_get(model, pk)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _fake_get_sessionmaker():
        # get_sessionmaker() returns a factory; calling the factory yields an
        # async-context session. Mirror that shape so `async with factory()` works.
        return lambda: _FakeSession()

    monkeypatch.setattr(ws_module, "load_session", _fake_load_session)
    monkeypatch.setattr(ws_module, "is_session_valid", _fake_is_session_valid)
    monkeypatch.setattr(ws_module, "_load_user_roles", _fake_load_roles)
    monkeypatch.setattr(
        "app.db.session.get_sessionmaker", _fake_get_sessionmaker, raising=True
    )

    # Fresh shared manager so this test's connections are isolated.
    monkeypatch.setattr(ws_module, "manager", ConnectionManager())

    app = FastAPI()
    register_ws_routes(app)
    client = TestClient(app)

    ids = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "viewer_id": viewer_id,
    }
    return client, ids


# ---------------------------------------------------------------------------
# Req 11.1 — auth before delivery / unauthenticated close
# ---------------------------------------------------------------------------


def test_unauthenticated_ws_is_closed_without_delivery(wired) -> None:
    """No-token and invalid-token connections are closed 4401 with no payload."""
    client, _ids = wired

    with pytest.raises(WebSocketDisconnect) as exc_no_token:
        with client.websocket_connect(WS_APPROVALS_PATH) as ws:
            ws.receive_json()
    assert exc_no_token.value.code == WS_UNAUTHORIZED_CODE

    with pytest.raises(WebSocketDisconnect) as exc_bad_token:
        with client.websocket_connect(f"{WS_APPROVALS_PATH}?token=not-real") as ws:
            ws.receive_json()
    assert exc_bad_token.value.code == WS_UNAUTHORIZED_CODE


def test_valid_token_connection_is_accepted(wired) -> None:
    """A valid Owner Session_Token establishes a live connection (no close)."""
    client, ids = wired
    with client.websocket_connect(f"{WS_APPROVALS_PATH}?token=owner-token") as ws:
        # The connection is registered under the owning workspace with OWNER role.
        assert ws_module.manager.connection_count(ids["workspace_id"]) == 1


# ---------------------------------------------------------------------------
# Req 11.2 / 10.8 / 11.3 — authorized delivery over the wire
# ---------------------------------------------------------------------------


def test_created_and_resolved_reach_only_authorized_reviewer(wired) -> None:
    """intercept->created and resolve->resolved reach the Owner, not the Viewer.

    Both an Owner (authorized) and a Viewer (not authorized) are connected for
    the same workspace over real WebSockets. Publishing the ``approval.created``
    event (the intercept/gating broadcast) and then the ``approval.resolved``
    event (the resolution broadcast) through the shared ``manager`` — exactly
    the fan-out the approvals router performs — delivers both to the Owner's
    socket and neither to the Viewer's. Each broadcast's delivery count is
    exactly 1 (the Owner), proving the live Viewer was excluded.
    """
    client, ids = wired
    workspace_id = ids["workspace_id"]

    owner_url = f"{WS_APPROVALS_PATH}?token=owner-token"
    viewer_url = f"{WS_APPROVALS_PATH}?token=viewer-token"

    # Enter the client so ``client.portal`` is available to drive the async
    # broadcast on the app's event loop.
    with client, \
         client.websocket_connect(owner_url) as owner_ws, \
         client.websocket_connect(viewer_url) as viewer_ws:
        # Both are live connections in the same workspace.
        assert viewer_ws is not None
        assert ws_module.manager.connection_count(workspace_id) == 2

        # intercept -> broadcast(created). Driven on the TestClient portal loop
        # so the send reaches the live sockets.
        created = {"type": "approval.created", "approval_request_id": str(uuid.uuid4())}
        delivered_created = client.portal.call(
            ws_module.manager.broadcast_to_authorized, workspace_id, created
        )
        assert delivered_created == 1  # Owner only

        got_created = owner_ws.receive_json()
        assert got_created["type"] == "approval.created"

        # resolve -> broadcast(resolved).
        resolved = {"type": "approval.resolved", "status": "approved"}
        delivered_resolved = client.portal.call(
            ws_module.manager.broadcast_to_authorized, workspace_id, resolved
        )
        assert delivered_resolved == 1  # Owner only

        got_resolved = owner_ws.receive_json()
        assert got_resolved["type"] == "approval.resolved"
        assert got_resolved["status"] == "approved"

    # The Viewer, an unauthorized recipient in the same workspace, received
    # neither event: both broadcasts delivered to exactly one connection.
