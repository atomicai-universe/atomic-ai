"""WebSocket_Gateway — authenticated, authorized real-time approval delivery.

Implements the ``WebSocket_Gateway`` component from the design (see design.md
"Approval_Hub and WebSocket_Gateway"). Its job is twofold and security-critical:

1. **Authenticate before delivering anything (Req 11.1, 11.2, 10.2).** A
   connection must present a valid Session_Token *before* the gateway sends any
   message. The token is read from the ``token`` query parameter of the
   WebSocket URL (e.g. ``/api/v1/ws/approvals?token=<session-token>``) because
   browsers cannot set custom headers on a ``WebSocket`` handshake; the same
   opaque, server-tracked token used for HTTP (see :mod:`app.core.security`) is
   accepted. On a missing, unknown, revoked, or expired token the handshake is
   accepted only long enough to close it with application code **4401**
   ("unauthorized") carrying **no payload** — the client receives no approval
   data whatsoever.

2. **Broadcast only to authorized reviewers (Req 10.8, 11.3).** Approval
   creation/resolution events are delivered *only* to live connections whose
   authenticated user holds **Owner** or **Admin** in the *owning* workspace of
   the approval. The recipient decision is factored into the pure helper
   :func:`is_authorized_recipient` (backed by
   :func:`app.core.rbac.can` + :attr:`~app.core.rbac.Capability.RESOLVE_APPROVAL`)
   so task 13.6 can property-test it in isolation. Every outbound message is run
   through :func:`app.core.scrubbing.scrub` so no secret-looking field ever
   reaches a client.

Design decisions
----------------
- **In-process fan-out.** The :class:`ConnectionManager` keeps an in-memory map
  ``workspace_id -> {ConnectionRecord}`` of live connections. This matches the
  single-process gateway in the design; a multi-process deployment would add a
  Redis pub/sub bridge in front of :meth:`ConnectionManager.broadcast_to_authorized`,
  but the authorization filter stays identical.
- **Module-level singleton.** :data:`manager` is the process-wide gateway that
  tasks 13.7/13.8 publish through (``manager.broadcast_to_authorized(workspace_id,
  {"type": "approval.created", ...})``). It is also injectable for tests.
- **Auth reuses the HTTP seam.** Token validation goes through the exact same
  :func:`app.core.security.load_session` + :func:`app.core.security.is_session_valid`
  functions the HTTP :func:`app.api.deps.require_session` uses, and builds the
  same :class:`~app.core.tenancy.RequestContext`, so "who is this connection?"
  has one answer. Tests monkeypatch the two names imported here so no DB is
  needed.

Event message shapes (published by tasks 13.7/13.8)
---------------------------------------------------
- ``{"type": "approval.created", "approval": {...}}`` on gating (Req 10.2).
- ``{"type": "approval.resolved", "approval": {...}}`` on resolution (Req 10.8).

Requirements: 10.2, 10.8, 11.1, 11.2, 11.3.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import Capability, can
from app.core.scrubbing import scrub
from app.core.security import is_session_valid, load_session
from app.db.models import MemberRole, User, WorkspaceMember

# WebSocket close code used when a connection is not (or no longer) authorized.
# 4000-4999 is the application-private range; 4401 mirrors HTTP 401 so clients
# can distinguish an auth failure from a normal close (Req 11.2).
WS_UNAUTHORIZED_CODE = 4401

# The WebSocket path the approvals feed is mounted at (see :func:`register_ws_routes`
# and ``app.main.register_routers``). Documented as a constant so the frontend
# and tests share one source of truth. Clients connect with the Session_Token in
# the ``token`` query parameter: ``/api/v1/ws/approvals?token=<session-token>``.
WS_APPROVALS_PATH = "/api/v1/ws/approvals"

# Name of the query parameter carrying the Session_Token on the handshake URL.
WS_TOKEN_QUERY_PARAM = "token"

# Name of the browser cookie carrying the Session_Token. Browsers send cookies
# on the WebSocket handshake automatically, so the same HttpOnly session cookie
# used for HTTP authenticates the WS connection without JS ever reading it.
SESSION_COOKIE_NAME = "session"


# ---------------------------------------------------------------------------
# Pure recipient-authorization decision (Property 25 basis — task 13.6)
# ---------------------------------------------------------------------------


def is_authorized_recipient(role: MemberRole | None) -> bool:
    """Return whether a connection with ``role`` may receive approval broadcasts.

    Pure and DB-free so task 13.6 (Property 25) can exhaustively property-test it
    over every :class:`~app.db.models.MemberRole` (and ``None``). A recipient is
    authorized exactly when its role holds
    :attr:`~app.core.rbac.Capability.RESOLVE_APPROVAL` — i.e. **Owner** or
    **Admin** (Req 10.2, 10.8, 11.3). ``None`` (the user is not a member of the
    owning workspace) is never authorized (fail-closed).

    Delegating to :func:`app.core.rbac.can` keeps a single source of truth for
    "who may act on approvals": the same capability that guards HTTP resolution
    (task 13.7's ``RESOLVE_APPROVAL`` guard) governs who receives the events.
    """
    if role is None:
        return False
    return can(role, Capability.RESOLVE_APPROVAL)


# ---------------------------------------------------------------------------
# Connection abstraction (structural typing so tests can use fakes)
# ---------------------------------------------------------------------------


class WSConnection(Protocol):
    """The minimal surface :class:`ConnectionManager` needs from a connection.

    Both Starlette's ``WebSocket`` and the fake connections used in tests
    satisfy this structurally: an ``async send_json`` for delivering a message
    and an ``async close`` for tearing the connection down. Keeping it a
    protocol means the manager never imports Starlette and is trivially
    unit-testable.
    """

    async def send_json(self, data: Any) -> None:  # pragma: no cover - protocol
        ...

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - protocol
        ...


@dataclass(eq=False)
class ConnectionRecord:
    """A single authenticated live connection registered in a workspace.

    ``eq=False`` keeps the dataclass hashable by object identity, so each live
    connection is a distinct element in the manager's per-workspace ``set`` even
    if two records happen to share the same field values.

    Attributes:
        connection: The transport used to deliver messages / close it.
        user_id: The authenticated user behind the connection.
        role: The user's :class:`~app.db.models.MemberRole` in the workspace the
            record is filed under. This is what
            :func:`is_authorized_recipient` consults when filtering a broadcast,
            so it captures the role *for the owning workspace* rather than a
            global role.
    """

    connection: WSConnection
    user_id: uuid.UUID
    role: MemberRole


# ---------------------------------------------------------------------------
# In-process connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """In-process registry + authorized fan-out for approval events.

    Keyed by ``workspace_id`` so a broadcast for one workspace's approval can
    only ever be considered for connections registered under that workspace —
    the first layer of tenant isolation. Within a workspace, delivery is
    filtered by :func:`is_authorized_recipient` so only Owner/Admin connections
    receive the event (Req 10.2, 10.8, 11.3).

    The manager is deliberately free of any HTTP/DB dependency: the WebSocket
    endpoint authenticates and resolves the role, then hands a
    :class:`ConnectionRecord` to :meth:`connect`. This keeps the fan-out logic
    unit-testable with fake connections.
    """

    def __init__(self) -> None:
        # workspace_id -> set of live connection records.
        self._by_workspace: dict[uuid.UUID, set[ConnectionRecord]] = {}

    def connect(self, workspace_id: uuid.UUID, record: ConnectionRecord) -> None:
        """Register ``record`` as a live connection in ``workspace_id``."""
        self._by_workspace.setdefault(workspace_id, set()).add(record)

    def disconnect(self, workspace_id: uuid.UUID, record: ConnectionRecord) -> None:
        """Remove ``record`` from ``workspace_id`` (idempotent).

        Safe to call more than once (e.g. on both an error path and the
        ``finally`` cleanup); an unknown record or empty workspace is ignored.
        Empties are pruned so the map does not grow without bound.
        """
        records = self._by_workspace.get(workspace_id)
        if not records:
            return
        records.discard(record)
        if not records:
            self._by_workspace.pop(workspace_id, None)

    def connection_count(self, workspace_id: uuid.UUID) -> int:
        """Return the number of live connections registered in ``workspace_id``."""
        return len(self._by_workspace.get(workspace_id, ()))

    async def broadcast_to_authorized(
        self, workspace_id: uuid.UUID, message: Any
    ) -> int:
        """Deliver ``message`` only to authorized reviewers in ``workspace_id``.

        For every live connection registered under ``workspace_id`` whose role
        passes :func:`is_authorized_recipient` (Owner/Admin), the (scrubbed)
        message is sent. Connections whose role is not authorized — e.g. a
        Viewer or Member who connected — never receive it (Req 10.2, 10.8,
        11.3), and connections in *other* workspaces are never even considered.

        The message is scrubbed with :func:`app.core.scrubbing.scrub` before
        sending so no secret-looking field value leaves the process (Req 6.4
        aligned). A connection whose ``send_json`` raises is treated as dead and
        dropped from the registry so a broken client cannot block delivery to
        the rest.

        Returns:
            The number of connections the message was successfully delivered to.
        """
        records = self._by_workspace.get(workspace_id)
        if not records:
            return 0

        payload = scrub(message)
        delivered = 0
        # Iterate a snapshot so we can prune dead connections while sending.
        for record in list(records):
            if not is_authorized_recipient(record.role):
                continue
            try:
                await record.connection.send_json(payload)
                delivered += 1
            except Exception:  # noqa: BLE001 - a broken client must not block others
                self.disconnect(workspace_id, record)
        return delivered


# Process-wide gateway singleton. Tasks 13.7/13.8 publish approval events via
# ``manager.broadcast_to_authorized(workspace_id, {...})``; the WebSocket
# endpoint registers/deregisters connections on it. Exposed at module level so
# publishers share one instance, and injectable in tests.
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Connection authentication (reuses the HTTP session seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedConnection:
    """The resolved identity of an authenticated WebSocket connection.

    Attributes:
        user_id: The authenticated caller behind the connection.
        roles: Mapping of ``workspace_id -> MemberRole`` for every workspace the
            caller is a member of, so the endpoint can register the connection
            under each relevant workspace with the correct role.
    """

    user_id: uuid.UUID
    roles: dict[uuid.UUID, MemberRole] = field(default_factory=dict)

    def role_in(self, workspace_id: uuid.UUID) -> MemberRole | None:
        """Return the caller's role in ``workspace_id`` (``None`` if not a member)."""
        return self.roles.get(workspace_id)


async def authenticate_connection(
    session: AsyncSession, raw_token: str | None
) -> AuthenticatedConnection | None:
    """Validate a WebSocket's Session_Token and resolve its identity + roles.

    Returns an :class:`AuthenticatedConnection` for a live session, or ``None``
    when there is no token, no matching row, the row is revoked/expired, or the
    user no longer exists — in which case the caller MUST close the connection
    before delivering any message (Req 11.1, 11.2). The DB row is the authority:
    validity is checked with :func:`app.core.security.is_session_valid` against
    the current time exactly as the HTTP path does, so an expired/revoked token
    is never accepted.

    The two functions :func:`load_session` and :func:`is_session_valid` are
    imported into this module so tests can monkeypatch them here and exercise
    the auth-before-delivery contract without a database.
    """
    if not raw_token:
        return None

    record = await load_session(session, raw_token)
    now = datetime.now(timezone.utc)
    if not is_session_valid(record, now):
        return None

    user = await session.get(User, record.user_id)
    if user is None:
        return None

    roles = await _load_user_roles(session, user.id)
    return AuthenticatedConnection(user_id=user.id, roles=roles)


async def _load_user_roles(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID, MemberRole]:
    """Return ``{workspace_id: role}`` for every workspace ``user_id`` belongs to.

    Mirrors :func:`app.api.deps._load_user_roles`; kept local so the gateway has
    no import dependency on the HTTP dependency module.
    """
    result = await session.execute(
        select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(
            WorkspaceMember.user_id == user_id
        )
    )
    return {workspace_id: role for workspace_id, role in result.all()}


# ---------------------------------------------------------------------------
# WebSocket endpoint + route registration
# ---------------------------------------------------------------------------


async def approvals_ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for the collaborative approvals feed (Req 11.1-11.3).

    Handshake and auth-before-delivery:

    1. Accept the WebSocket handshake (required before we can send a close
       frame with our application code).
    2. Read the Session_Token from the ``token`` query parameter and validate it
       via :func:`authenticate_connection`. On failure, close with
       :data:`WS_UNAUTHORIZED_CODE` (4401) and **no payload** — the client
       receives no approval data (Req 11.1, 11.2).
    3. On success, register the connection with the module-level
       :data:`manager` under **every** workspace the user is a member of, each
       with that workspace's role, so later
       :meth:`ConnectionManager.broadcast_to_authorized` calls filter delivery
       to Owner/Admin recipients (Req 10.2, 10.8, 11.3). The connection then
       simply waits for events (it is a one-way server->client feed); any inbound
       frame is drained until the client disconnects, at which point the
       connection is deregistered from every workspace.

    The active DB session and the gateway manager are pulled from the app so the
    endpoint has no hidden globals beyond the documented singleton; tests drive
    it through Starlette's ``TestClient`` with the session seam monkeypatched.
    """
    from app.db.session import get_sessionmaker

    await websocket.accept()

    # Resolve the Session_Token, preferring the HttpOnly session cookie the
    # browser sends automatically on the WS handshake (same token the HTTP API
    # uses), then falling back to the ``token`` query parameter for non-browser
    # clients that cannot rely on the cookie (Req 11.1).
    raw_token = websocket.cookies.get(SESSION_COOKIE_NAME) or websocket.query_params.get(
        WS_TOKEN_QUERY_PARAM
    )

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        identity = await authenticate_connection(session, raw_token)

    if identity is None:
        # Unauthenticated: close with 4401 and NO payload before any message
        # is delivered (Req 11.1, 11.2).
        await websocket.close(code=WS_UNAUTHORIZED_CODE)
        return

    # Register under every workspace the user belongs to, with that workspace's
    # role, so broadcasts are filtered to Owner/Admin recipients (Req 11.3).
    records: list[tuple[uuid.UUID, ConnectionRecord]] = []
    for workspace_id, role in identity.roles.items():
        record = ConnectionRecord(
            connection=websocket, user_id=identity.user_id, role=role
        )
        manager.connect(workspace_id, record)
        records.append((workspace_id, record))

    try:
        # One-way server->client feed: keep the connection open, draining any
        # inbound frames until the client disconnects.
        while True:
            await websocket.receive_text()
    except Exception:  # noqa: BLE001 - normal on client disconnect
        pass
    finally:
        for workspace_id, record in records:
            manager.disconnect(workspace_id, record)


def register_ws_routes(app) -> None:  # noqa: ANN001 - FastAPI app
    """Mount the approvals WebSocket route on ``app`` (called from ``main``).

    Registers :func:`approvals_ws_endpoint` at :data:`WS_APPROVALS_PATH`
    (``/api/v1/ws/approvals``). Uses ``add_api_websocket_route`` so the endpoint
    is a plain callable rather than a router-decorated function, keeping the
    module import-light.
    """
    app.add_api_websocket_route(WS_APPROVALS_PATH, approvals_ws_endpoint)


__all__ = [
    "WS_UNAUTHORIZED_CODE",
    "WS_APPROVALS_PATH",
    "WS_TOKEN_QUERY_PARAM",
    "is_authorized_recipient",
    "WSConnection",
    "ConnectionRecord",
    "ConnectionManager",
    "manager",
    "AuthenticatedConnection",
    "authenticate_connection",
    "approvals_ws_endpoint",
    "register_ws_routes",
]
