"""Voice WebSocket endpoint — authenticated bridge to the Nova Sonic gateway.

Mounts a single WebSocket route at :data:`VOICE_WS_PATH`
(``/api/v1/voice/stream``) that connects a browser's microphone/speaker to a
:class:`app.services.voice_gateway.NovaSonicVoiceSession`. It reuses the exact
authentication + tenancy seam from :mod:`app.api.ws` (the approvals gateway) so
"who is this connection?" has one answer across the app.

Auth + tenancy (security-critical, mirrors :mod:`app.api.ws`)
-------------------------------------------------------------
1. Accept the handshake (required before we can send an application close code).
2. Resolve the Session_Token, preferring the HttpOnly ``session`` cookie the
   browser sends automatically, then falling back to the ``token`` query param.
   Validate it with :func:`app.api.ws.authenticate_connection` (same
   ``load_session`` / ``is_session_valid`` path the HTTP API uses). On failure,
   close with :data:`app.api.ws.WS_UNAUTHORIZED_CODE` (4401) and NO payload.
3. Require a ``workspace_id`` query param and verify the authenticated identity
   holds a role in it; otherwise close 4401 (missing) / :data:`WS_FORBIDDEN_CODE`
   (4403, not a member) with no session ever started.
4. If :data:`settings.VOICE_ENABLED` is False, close with
   :data:`WS_VOICE_DISABLED_CODE` (4404) and never start a session.

Browser <-> server message protocol
------------------------------------
Client -> server:
  - Binary frames: raw 16 kHz mono PCM16 audio chunks.
  - JSON text control messages:
      {"type": "start"}                       (optional; session auto-starts)
      {"type": "audio", "data": "<base64>"}   (16 kHz PCM16 as base64)
      {"type": "text",  "text": "..."}        (optional typed turn)
      {"type": "stop"}                        (end the conversation)

Server -> client (always JSON):
  - {"type": "ready"}                         after Nova Sonic session starts
  - {"type": "audio", "data": "<base64>"}     24 kHz PCM16 model audio (base64)
  - {"type": "transcript", "role": "...", "text": "..."}
  - {"type": "action", ...}                   navigate/type/submit/interrupt
  - {"type": "error", "message": "..."}

Lifecycle: on connect the endpoint builds the session (identity ->
RequestContext, validated workspace, ``session_scope`` factory), starts it,
sends ``{"type": "ready"}``, then runs two concurrent tasks — one draining WS
frames into the session, one draining model output into ``send_json`` — under a
single cancellation scope so the Nova Sonic stream is always stopped on
disconnect or error.

The session builder is injectable via :data:`_session_builder` (swap with
:func:`set_session_builder`) so the router test drives a fake session with NO
AWS.

Requirements: accessibility voice feature (Phase 1b); reuses Req 11.1/11.2/11.3
auth + tenancy from the approvals gateway.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Callable

import anyio
from fastapi import WebSocket

from app.api.ws import (
    SESSION_COOKIE_NAME,
    WS_TOKEN_QUERY_PARAM,
    WS_UNAUTHORIZED_CODE,
    authenticate_connection,
)
from app.config import get_settings
from app.core.tenancy import RequestContext
from app.services.voice_gateway import NovaSonicVoiceSession

logger = logging.getLogger(__name__)

# The WebSocket path the voice stream is mounted at. A constant so the frontend
# and tests share one source of truth.
VOICE_WS_PATH = "/api/v1/voice/stream"

# Query parameter naming the workspace the voice session is scoped to.
WS_WORKSPACE_QUERY_PARAM = "workspace_id"

# Application close codes (4000-4999 private range). 4401 (unauthorized) is
# reused from the approvals gateway; the others distinguish the voice-specific
# refusal reasons so the client can react precisely.
WS_FORBIDDEN_CODE = 4403  # authenticated but not a member of the workspace
WS_VOICE_DISABLED_CODE = 4404  # the voice feature is turned off server-side


# ---------------------------------------------------------------------------
# Injectable session builder (so the router test needs no AWS)
# ---------------------------------------------------------------------------


def _default_session_builder(
    *, ctx: RequestContext, workspace_id: uuid.UUID
) -> NovaSonicVoiceSession:
    """Build a real :class:`NovaSonicVoiceSession` bound to ``session_scope``.

    Imports :func:`app.db.session.session_scope` lazily so importing this module
    never constructs a DB engine (the router test swaps this builder out).
    """
    from app.db.session import session_scope

    return NovaSonicVoiceSession(
        ctx=ctx,
        workspace_id=workspace_id,
        session_factory=session_scope,
    )


# Module-level, swappable so tests can inject a fake session (no AWS / no DB).
_session_builder: Callable[..., Any] = _default_session_builder


def set_session_builder(builder: Callable[..., Any] | None) -> None:
    """Override (or reset) the voice session builder.

    Tests call this with a fake that returns a scripted session; passing
    ``None`` restores the default real builder.
    """
    global _session_builder
    _session_builder = builder or _default_session_builder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_workspace_id(raw: str | None) -> uuid.UUID | None:
    """Parse the ``workspace_id`` query param, returning ``None`` when invalid."""
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def _safe_send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send JSON, swallowing errors from an already-closed socket."""
    try:
        await websocket.send_json(payload)
    except Exception:  # noqa: BLE001 - client may have vanished mid-send
        pass


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


async def voice_ws_endpoint(websocket: WebSocket) -> None:
    """Authenticated voice bridge endpoint (see module docstring)."""
    from app.db.session import get_sessionmaker

    await websocket.accept()

    settings = get_settings()
    if not settings.VOICE_ENABLED:
        await websocket.close(code=WS_VOICE_DISABLED_CODE)
        return

    # Resolve the Session_Token (cookie preferred, query param fallback) and
    # validate it via the shared auth seam — no message is delivered first.
    raw_token = websocket.cookies.get(SESSION_COOKIE_NAME) or websocket.query_params.get(
        WS_TOKEN_QUERY_PARAM
    )
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        identity = await authenticate_connection(session, raw_token)

    if identity is None:
        await websocket.close(code=WS_UNAUTHORIZED_CODE)
        return

    # A workspace is required and the caller must be a member of it.
    workspace_id = _parse_workspace_id(
        websocket.query_params.get(WS_WORKSPACE_QUERY_PARAM)
    )
    if workspace_id is None:
        await websocket.close(code=WS_UNAUTHORIZED_CODE)
        return
    role = identity.role_in(workspace_id)
    if role is None:
        await websocket.close(code=WS_FORBIDDEN_CODE)
        return

    # Build the tenant-scoped context the voice tools derive tenancy from.
    ctx = RequestContext(
        user_id=identity.user_id,
        active_workspace_id=workspace_id,
        roles=dict(identity.roles),
    )

    voice_session = _session_builder(ctx=ctx, workspace_id=workspace_id)

    # Callbacks that relay model output to the browser as JSON.
    async def on_audio(pcm: bytes, sample_rate: int) -> None:
        await _safe_send_json(
            websocket,
            {
                "type": "audio",
                "data": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": sample_rate,
            },
        )

    async def on_transcript(role_: str, text: str) -> None:
        await _safe_send_json(
            websocket, {"type": "transcript", "role": role_, "text": text}
        )

    async def on_action(action: dict[str, Any]) -> None:
        # Nest the action under an "action" key rather than spreading it into the
        # envelope. Spreading (``{"type": "action", **action}``) is a BUG: the
        # action's own ``type`` (e.g. "navigate") overwrites the envelope
        # ``type`` "action", so the client's ``switch (msg.type)`` never matches
        # "action" and silently drops the frame — navigation never fires. The
        # nested shape keeps the envelope type intact and unambiguous.
        await _safe_send_json(websocket, {"type": "action", "action": action})

    async def on_error(message: str) -> None:
        await _safe_send_json(websocket, {"type": "error", "message": message})

    try:
        await voice_session.start()
    except Exception:  # noqa: BLE001 - model connect failed; tell client, close
        logger.exception("voice session failed to start | workspace=%s", workspace_id)
        await _safe_send_json(
            websocket, {"type": "error", "message": "Voice session failed to start."}
        )
        await websocket.close()
        return

    await _safe_send_json(websocket, {"type": "ready"})

    # Speak the once-per-session welcome (Req 2.1/2.3). Best-effort internally,
    # but wrap defensively here too so an unexpected error never aborts the
    # endpoint before the pumps start.
    try:
        await voice_session.send_welcome()
    except Exception:  # noqa: BLE001 - welcome is non-critical; never abort session
        logger.exception("voice welcome failed | workspace=%s", workspace_id)

    # Run the two pumps under one anyio task group (Starlette's native
    # concurrency). Whichever side finishes first (client disconnect / stop, or
    # the model stream ending) cancels the group's scope so the other pump is
    # torn down and the Nova Sonic stream never leaks.
    try:
        async with anyio.create_task_group() as task_group:

            async def pump_client_to_session() -> None:
                """Drain client frames into the session until disconnect/stop."""
                try:
                    while True:
                        try:
                            message = await websocket.receive()
                        except Exception:  # noqa: BLE001 - client disconnected
                            return
                        if message.get("type") == "websocket.disconnect":
                            return
                        data = message.get("bytes")
                        if data is not None:
                            await voice_session.send_audio(data)
                            continue
                        text = message.get("text")
                        if text is None:
                            continue
                        control = _decode_control(text)
                        kind = control.get("type") if isinstance(control, dict) else None
                        if kind == "audio":
                            b64 = control.get("data")
                            if isinstance(b64, str) and b64:
                                await voice_session.send_audio(base64.b64decode(b64))
                        elif kind == "text":
                            turn = control.get("text")
                            if isinstance(turn, str) and turn:
                                await voice_session.send_text(turn)
                        elif kind == "stop":
                            return
                        # {"type": "start"} / unknown control messages: no-op.
                finally:
                    task_group.cancel_scope.cancel()

            async def pump_session_to_client() -> None:
                try:
                    await voice_session.run(
                        on_audio=on_audio,
                        on_transcript=on_transcript,
                        on_action=on_action,
                        on_error=on_error,
                    )
                finally:
                    task_group.cancel_scope.cancel()

            task_group.start_soon(pump_client_to_session)
            task_group.start_soon(pump_session_to_client)
    finally:
        # Teardown must never raise: a dead Bedrock/Nova Sonic stream can make
        # the agent stop sequence throw. voice_session.stop() already contains
        # its own errors, but guard here too so nothing escapes the endpoint.
        try:
            await voice_session.stop()
        except Exception:  # noqa: BLE001 - teardown must never crash the endpoint
            logger.debug("voice session stop failed during teardown | workspace=%s", workspace_id)
        # Do NOT call websocket.close() here: Starlette closes the socket when
        # the endpoint returns, and closing a socket the client already
        # disconnected raises. Only the auth-reject paths above close explicitly
        # (before any pump started).


def _decode_control(text: str) -> dict[str, Any]:
    """Parse a client JSON control frame, returning ``{}`` on malformed input."""
    import json

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def register_voice_routes(app) -> None:  # noqa: ANN001 - FastAPI app
    """Mount :func:`voice_ws_endpoint` at :data:`VOICE_WS_PATH` (called from main)."""
    app.add_api_websocket_route(VOICE_WS_PATH, voice_ws_endpoint)


__all__ = [
    "VOICE_WS_PATH",
    "WS_WORKSPACE_QUERY_PARAM",
    "WS_FORBIDDEN_CODE",
    "WS_VOICE_DISABLED_CODE",
    "voice_ws_endpoint",
    "register_voice_routes",
    "set_session_builder",
]
