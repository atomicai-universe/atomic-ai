"""Tests for the voice WebSocket router (backend/app/api/voice.py).

DB-free and AWS-free. Following the approvals WS test patterns
(``tests/test_ws_gateway.py``): the session-load seam
(``load_session`` / ``is_session_valid``) and the session factory are
monkeypatched, and the voice session builder is swapped for a fake via
:func:`app.api.voice.set_session_builder` so NO Nova Sonic / AWS call happens.

Coverage:

- Connecting without a valid token closes with ``WS_UNAUTHORIZED_CODE`` and
  sends NO message first.
- A valid identity + workspace membership yields ``{"type": "ready"}`` and the
  server forwards a scripted audio / transcript / action from the fake session
  to the client; a client audio control frame reaches the fake session.
- A valid identity WITHOUT membership in the requested workspace closes 4403.
- ``VOICE_ENABLED=False`` closes with the disabled code.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import voice as voice_module
from app.api import ws as ws_module
from app.api.voice import (
    VOICE_WS_PATH,
    WS_FORBIDDEN_CODE,
    WS_VOICE_DISABLED_CODE,
    register_voice_routes,
    set_session_builder,
)
from app.api.ws import WS_UNAUTHORIZED_CODE
from app.db.models import MemberRole


# ---------------------------------------------------------------------------
# Fakes mirroring the approvals WS test
# ---------------------------------------------------------------------------


@dataclass
class _FakeSessionRow:
    user_id: uuid.UUID
    revoked: bool = False
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    )


@dataclass
class _FakeUser:
    id: uuid.UUID


class _FakeSession:
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


class FakeVoiceSession:
    """A fake NovaSonicVoiceSession: records inputs, scripts run() callbacks."""

    def __init__(self, *, ctx, workspace_id, script: list[tuple[str, Any]]):
        self.ctx = ctx
        self.workspace_id = workspace_id
        self._script = script
        self.started = False
        self.stopped = False
        self.audio_in: list[bytes] = []
        self.text_in: list[str] = []
        self.welcome_calls = 0

    async def start(self) -> None:
        self.started = True

    async def send_welcome(self) -> None:
        self.welcome_calls += 1

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_in.append(pcm)

    async def send_text(self, text: str) -> None:
        self.text_in.append(text)

    async def stop(self) -> None:
        self.stopped = True

    async def run(self, *, on_audio, on_transcript, on_action, on_error) -> None:
        for kind, payload in self._script:
            if kind == "audio":
                await on_audio(payload, 16000)
            elif kind == "transcript":
                await on_transcript(payload[0], payload[1])
            elif kind == "action":
                await on_action(payload)
            elif kind == "error":
                await on_error(payload)
        # After the script the server->client pump ends; keep the socket alive
        # long enough for the test to read all messages by never returning until
        # cancelled.
        import asyncio

        await asyncio.Event().wait()


_VALID_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@db:5432/atomic",
    "REDIS_URL": "redis://redis:6379/0",
    "ENCRYPTION_KEY": "test-encryption-key-value-0123456789",
    "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret-value",
    "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret-value",
}


def _apply_env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None) -> None:
    """Populate a valid config environment and reset the cached settings.

    The endpoint reads ``get_settings()`` (an ``lru_cache``); tests set the env
    and clear the cache so a fresh, valid ``Settings`` is built per test.
    """
    from app.config import get_settings

    for key, value in {**_VALID_ENV, **(extra or {})}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _build_app(monkeypatch: pytest.MonkeyPatch, *, session: _FakeSession) -> FastAPI:
    @asynccontextmanager
    async def _fake_session_cm():
        yield session

    def _fake_sessionmaker():
        return _fake_session_cm

    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "get_sessionmaker", _fake_sessionmaker)

    app = FastAPI()
    register_voice_routes(app)
    return app


@pytest.fixture(autouse=True)
def _reset_session_builder():
    """Ensure each test starts and ends with the default builder + fresh settings."""
    from app.config import get_settings

    set_session_builder(None)
    yield
    set_session_builder(None)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Auth-before-anything
# ---------------------------------------------------------------------------


def test_missing_token_closes_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch)

    async def _no_session(_session, _raw_token):
        return None

    monkeypatch.setattr(ws_module, "load_session", _no_session)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: False)

    app = _build_app(monkeypatch, session=_FakeSession(user=None, roles={}))
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"{VOICE_WS_PATH}?workspace_id={uuid.uuid4()}"
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == WS_UNAUTHORIZED_CODE


def test_invalid_token_closes_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch)

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=uuid.uuid4(), revoked=True)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: False)

    app = _build_app(monkeypatch, session=_FakeSession(user=None, roles={}))
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"{VOICE_WS_PATH}?workspace_id={uuid.uuid4()}&token=bogus"
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == WS_UNAUTHORIZED_CODE


def test_missing_workspace_closes_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch)
    user_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    session = _FakeSession(user=_FakeUser(id=user_id), roles={uuid.uuid4(): MemberRole.OWNER})
    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"{VOICE_WS_PATH}?token=good") as ws:
            ws.receive_text()

    assert exc_info.value.code == WS_UNAUTHORIZED_CODE


def test_non_member_workspace_closes_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch)
    user_id = uuid.uuid4()
    member_ws = uuid.uuid4()
    other_ws = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    session = _FakeSession(user=_FakeUser(id=user_id), roles={member_ws: MemberRole.OWNER})
    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"{VOICE_WS_PATH}?token=good&workspace_id={other_ws}"
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == WS_FORBIDDEN_CODE


def test_voice_disabled_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Turn the voice feature off via config env; the endpoint reads it.
    _apply_env(monkeypatch, {"VOICE_ENABLED": "false"})
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    session = _FakeSession(user=_FakeUser(id=user_id), roles={ws_id: MemberRole.OWNER})
    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"{VOICE_WS_PATH}?token=good&workspace_id={ws_id}"
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == WS_VOICE_DISABLED_CODE


# ---------------------------------------------------------------------------
# Happy path: ready + forwarded events + client audio reaches the session
# ---------------------------------------------------------------------------


def test_valid_connection_ready_and_forwards_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch)
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    pcm24 = b"model-audio-24k"
    built: dict[str, FakeVoiceSession] = {}

    def _fake_builder(*, ctx, workspace_id):
        fake = FakeVoiceSession(
            ctx=ctx,
            workspace_id=workspace_id,
            script=[
                ("transcript", ("assistant", "You have one unread email.")),
                ("audio", pcm24),
                ("action", {"type": "navigate", "path": "/dashboard/approvals"}),
            ],
        )
        built["session"] = fake
        return fake

    set_session_builder(_fake_builder)

    session = _FakeSession(user=_FakeUser(id=user_id), roles={ws_id: MemberRole.OWNER})
    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with client.websocket_connect(
        f"{VOICE_WS_PATH}?token=good&workspace_id={ws_id}"
    ) as ws:
        assert ws.receive_json() == {"type": "ready"}

        transcript = ws.receive_json()
        assert transcript == {
            "type": "transcript",
            "role": "assistant",
            "text": "You have one unread email.",
        }

        audio = ws.receive_json()
        assert audio["type"] == "audio"
        assert base64.b64decode(audio["data"]) == pcm24

        action = ws.receive_json()
        # The action is nested under an "action" key so the envelope "type"
        # stays "action" and is never clobbered by the action's own type.
        assert action == {
            "type": "action",
            "action": {"type": "navigate", "path": "/dashboard/approvals"},
        }

        # A client audio control frame reaches the fake session.
        client_pcm = b"mic-input-16k"
        ws.send_json(
            {"type": "audio", "data": base64.b64encode(client_pcm).decode("ascii")}
        )
        # Give the server loop a moment to process the frame.
        import time

        for _ in range(100):
            if built["session"].audio_in:
                break
            time.sleep(0.01)

    assert built["session"].started is True
    assert built["session"].audio_in == [client_pcm]
    # The once-per-session welcome is spoken after the ready frame (Property 5).
    assert built["session"].welcome_calls == 1
    # Tenancy: the session was built with the requested workspace.
    assert built["session"].workspace_id == ws_id
    assert built["session"].ctx.active_workspace_id == ws_id


def test_client_binary_audio_reaches_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_env(monkeypatch)
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    built: dict[str, FakeVoiceSession] = {}

    def _fake_builder(*, ctx, workspace_id):
        fake = FakeVoiceSession(ctx=ctx, workspace_id=workspace_id, script=[])
        built["session"] = fake
        return fake

    set_session_builder(_fake_builder)

    session = _FakeSession(user=_FakeUser(id=user_id), roles={ws_id: MemberRole.OWNER})
    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with client.websocket_connect(
        f"{VOICE_WS_PATH}?token=good&workspace_id={ws_id}"
    ) as ws:
        assert ws.receive_json() == {"type": "ready"}
        raw = b"\x00\x01\x02binary-pcm"
        ws.send_bytes(raw)
        import time

        for _ in range(100):
            if built["session"].audio_in:
                break
            time.sleep(0.01)

    assert built["session"].audio_in == [raw]


def test_welcome_spoken_after_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the ``ready`` frame the endpoint speaks the once-per-session welcome.

    Property 5: ``send_welcome`` is invoked exactly once on the built session,
    and the ready frame is delivered to the client (welcome happens after it).
    """
    _apply_env(monkeypatch)
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    async def _load(_session, _raw_token):
        return _FakeSessionRow(user_id=user_id)

    monkeypatch.setattr(ws_module, "load_session", _load)
    monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

    built: dict[str, FakeVoiceSession] = {}

    def _fake_builder(*, ctx, workspace_id):
        fake = FakeVoiceSession(ctx=ctx, workspace_id=workspace_id, script=[])
        built["session"] = fake
        return fake

    set_session_builder(_fake_builder)

    session = _FakeSession(user=_FakeUser(id=user_id), roles={ws_id: MemberRole.OWNER})
    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with client.websocket_connect(
        f"{VOICE_WS_PATH}?token=good&workspace_id={ws_id}"
    ) as ws:
        assert ws.receive_json() == {"type": "ready"}
        # The welcome is dispatched right after ready; poll briefly for it.
        import time

        for _ in range(100):
            if built["session"].welcome_calls:
                break
            time.sleep(0.01)

    assert built["session"].started is True
    assert built["session"].welcome_calls == 1


@pytest.mark.parametrize(
    "extra_env,build_session,query,expected_code",
    [
        # Missing/invalid token -> 4401, no membership lookup needed.
        (None, "no_user", f"workspace_id={uuid.uuid4()}", WS_UNAUTHORIZED_CODE),
        # Missing workspace -> 4401.
        (None, "member", "token=good", WS_UNAUTHORIZED_CODE),
        # Non-member of requested workspace -> 4403.
        (None, "other_ws", f"token=good&workspace_id={uuid.uuid4()}", WS_FORBIDDEN_CODE),
        # Voice disabled -> disabled code.
        ({"VOICE_ENABLED": "false"}, "member", f"token=good&workspace_id={uuid.uuid4()}", WS_VOICE_DISABLED_CODE),
    ],
)
def test_reject_paths_never_build_session(
    monkeypatch: pytest.MonkeyPatch,
    extra_env,
    build_session,
    query,
    expected_code,
) -> None:
    """Every auth/gating reject path closes with the expected code and NEVER
    builds, starts, or welcomes a voice session (Requirements 5.1, 5.4)."""
    _apply_env(monkeypatch, extra_env)
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    if build_session == "no_user":
        async def _load(_session, _raw_token):
            return None

        monkeypatch.setattr(ws_module, "load_session", _load)
        monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: False)
        session = _FakeSession(user=None, roles={})
        connect_query = query
    else:
        async def _load(_session, _raw_token):
            return _FakeSessionRow(user_id=user_id)

        monkeypatch.setattr(ws_module, "load_session", _load)
        monkeypatch.setattr(ws_module, "is_session_valid", lambda record, now: True)

        if build_session == "member":
            roles = {ws_id: MemberRole.OWNER}
            # Missing-workspace case has no workspace_id (close is 4401); the
            # disabled case targets the member workspace so gating (not
            # membership) is the reason for the close.
            if "workspace_id=" in query:
                connect_query = f"token=good&workspace_id={ws_id}"
            else:
                connect_query = query
        else:  # other_ws -> user is a member of a DIFFERENT workspace
            roles = {uuid.uuid4(): MemberRole.OWNER}
            connect_query = query
        session = _FakeSession(user=_FakeUser(id=user_id), roles=roles)

    built: dict[str, FakeVoiceSession] = {}

    def _fake_builder(*, ctx, workspace_id):
        fake = FakeVoiceSession(ctx=ctx, workspace_id=workspace_id, script=[])
        built["session"] = fake
        return fake

    set_session_builder(_fake_builder)

    app = _build_app(monkeypatch, session=session)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"{VOICE_WS_PATH}?{connect_query}") as ws:
            ws.receive_text()

    assert exc_info.value.code == expected_code
    # No session was ever constructed on a reject path, so none was started or
    # welcomed.
    assert "session" not in built


def test_route_registered_at_expected_path() -> None:
    """The voice stream is mounted at /api/v1/voice/stream."""
    app = FastAPI()
    register_voice_routes(app)
    ws_paths = [
        route.path
        for route in app.routes
        if getattr(route, "path", None) == VOICE_WS_PATH
    ]
    assert VOICE_WS_PATH == "/api/v1/voice/stream"
    assert ws_paths == [VOICE_WS_PATH]
