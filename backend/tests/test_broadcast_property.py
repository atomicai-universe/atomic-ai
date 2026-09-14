"""Property-based tests for authorized approval broadcasts (WebSocket_Gateway).

Implements the design property:

- **Property 25: Approval broadcasts reach only authorized reviewers**
  **Validates: Requirements 10.2, 10.8, 11.3**

These tests target the fan-out logic of :class:`app.api.ws.ConnectionManager`
(``broadcast_to_authorized``) plus the pure recipient decision
:func:`app.api.ws.is_authorized_recipient`. Two security invariants are
exercised across arbitrary populations of connections:

- **Recipient authorization (Req 10.2, 11.3):** within the targeted workspace,
  only Owner/Admin connections receive the event; Member/Viewer never do.
- **Tenant isolation (Req 10.8):** a connection registered under one workspace
  is never even considered for a broadcast targeting a different workspace.

No FastAPI wiring, database, or real WebSocket transport is used. Connections
are lightweight fakes implementing the manager's structural ``WSConnection``
protocol (``async send_json`` / ``async close``) that record every payload they
receive, so we can assert *exactly* which connections were delivered to.

The design mandates a minimum of 100 examples per property; the generators here
are cheap so we run 200.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.api.ws import (
    ConnectionManager,
    ConnectionRecord,
    is_authorized_recipient,
)
from app.db.models import MemberRole


# Shared settings profile: comfortably above the design's 100-example minimum.
_PBT = settings(max_examples=200)

# The roles that are authorized approval recipients (Owner/Admin — Req 11.3).
_AUTHORIZED_ROLES = {MemberRole.OWNER, MemberRole.ADMIN}

# Reusable strategy over the (small, total) role domain.
_roles = st.sampled_from(list(MemberRole))

# A small fixed pool of workspace ids so populations realistically overlap and
# tenant-isolation collisions are actually exercised (rather than every
# connection landing in a unique workspace).
_WORKSPACE_POOL = [uuid.uuid4() for _ in range(3)]
_workspaces = st.sampled_from(_WORKSPACE_POOL)


class _FakeConnection:
    """A recording stand-in for a live WebSocket connection.

    Satisfies the manager's structural ``WSConnection`` protocol: ``send_json``
    appends the delivered payload to :attr:`received` and ``close`` records the
    close code. Nothing is sent over a wire, so the manager's fan-out logic is
    exercised in isolation.
    """

    def __init__(self) -> None:
        self.received: list[Any] = []
        self.closed_with: int | None = None

    async def send_json(self, data: Any) -> None:
        self.received.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


# A connection spec: the workspace it is registered under, and its role there.
_conn_spec = st.tuples(_workspaces, _roles)


# ---------------------------------------------------------------------------
# Pure recipient-authorization decision
# ---------------------------------------------------------------------------


@_PBT
@given(role=_roles)
def test_is_authorized_recipient_is_owner_admin_only(role: MemberRole) -> None:
    """``is_authorized_recipient`` is True iff the role is Owner or Admin.

    The recipient filter that governs every broadcast must match the RESOLVE
    capability holders exactly (Req 10.2, 10.8, 11.3).
    """
    assert is_authorized_recipient(role) is (role in _AUTHORIZED_ROLES)


def test_is_authorized_recipient_none_is_never_authorized() -> None:
    """A non-member (``None`` role) is never an authorized recipient (fail-closed)."""
    assert is_authorized_recipient(None) is False


# ---------------------------------------------------------------------------
# Property 25: Approval broadcasts reach only authorized reviewers
# Validates Requirements 10.2, 10.8, 11.3
# ---------------------------------------------------------------------------


@_PBT
@given(
    specs=st.lists(_conn_spec, max_size=12),
    target_ws=_workspaces,
)
def test_broadcast_reaches_exactly_authorized_recipients_in_target_workspace(
    specs: list[tuple[uuid.UUID, MemberRole]], target_ws: uuid.UUID
) -> None:
    """A broadcast is delivered to exactly the Owner/Admin connections in the
    target workspace, and to no one else (Property 25).

    For an arbitrary population of connections spread across a pool of
    workspaces, ``broadcast_to_authorized(target_ws, msg)`` must:

    - deliver the message to every connection with workspace_id == target_ws
      AND role in {Owner, Admin} — exactly once (Req 10.2, 11.3);
    - never deliver to a Member/Viewer in the target workspace (Req 10.2, 11.3);
    - never deliver to any connection in another workspace, regardless of role
      (Req 10.8 tenant isolation).
    """
    manager = ConnectionManager()

    # Build fake connections and register each under its workspace/role.
    entries: list[tuple[uuid.UUID, MemberRole, _FakeConnection]] = []
    for workspace_id, role in specs:
        conn = _FakeConnection()
        record = ConnectionRecord(
            connection=conn, user_id=uuid.uuid4(), role=role
        )
        manager.connect(workspace_id, record)
        entries.append((workspace_id, role, conn))

    message = {"type": "approval.created", "seq": len(specs)}

    delivered = asyncio.run(manager.broadcast_to_authorized(target_ws, message))

    expected_recipients = [
        conn
        for (workspace_id, role, conn) in entries
        if workspace_id == target_ws and role in _AUTHORIZED_ROLES
    ]

    # The return count matches the number of authorized recipients in target_ws.
    assert delivered == len(expected_recipients)

    for workspace_id, role, conn in entries:
        should_receive = workspace_id == target_ws and role in _AUTHORIZED_ROLES
        if should_receive:
            # Delivered exactly once, with the (unchanged) message payload.
            assert conn.received == [message]
        else:
            # Member/Viewer in target_ws, or any role in another workspace:
            # never receives anything.
            assert conn.received == []


@_PBT
@given(
    other_ws_specs=st.lists(st.tuples(_workspaces, _roles), max_size=12),
    target_ws=_workspaces,
)
def test_broadcast_never_leaks_across_workspaces(
    other_ws_specs: list[tuple[uuid.UUID, MemberRole]], target_ws: uuid.UUID
) -> None:
    """No connection outside the target workspace ever receives the broadcast.

    Even Owner/Admin connections are silent when they belong to a different
    workspace than the one the approval is broadcast for (Req 10.8 tenant
    isolation), for arbitrary roles.
    """
    manager = ConnectionManager()

    foreign_conns: list[_FakeConnection] = []
    for workspace_id, role in other_ws_specs:
        if workspace_id == target_ws:
            # Only register connections in *other* workspaces for this property.
            continue
        conn = _FakeConnection()
        manager.connect(
            workspace_id,
            ConnectionRecord(connection=conn, user_id=uuid.uuid4(), role=role),
        )
        foreign_conns.append(conn)

    delivered = asyncio.run(
        manager.broadcast_to_authorized(target_ws, {"type": "approval.resolved"})
    )

    # Nothing in the target workspace was registered, so nothing is delivered,
    # and no foreign connection received anything.
    assert delivered == 0
    for conn in foreign_conns:
        assert conn.received == []
