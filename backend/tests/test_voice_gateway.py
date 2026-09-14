"""Tests for the Voice_Gateway (backend/app/services/voice_gateway.py).

Entirely DB-free and AWS-free. A fake ``agent_factory`` returns a fake
``BidiAgent`` whose ``receive()`` yields a scripted sequence of output events
and whose ``send()`` records the inputs it was given. A fake ``session_factory``
yields a sentinel "session" and :func:`voice_tools.execute_voice_tool` is
monkeypatched to record its call args and return a scripted envelope, so the
tool wrapper can be exercised with no database and no Nova Sonic call.

Coverage:

- :meth:`NovaSonicVoiceSession.send_audio` wraps raw bytes in a
  ``BidiAudioInputEvent`` (base64 pcm/16k/mono) and calls ``agent.send``.
- :meth:`NovaSonicVoiceSession.run` routes audio -> on_audio, transcript ->
  on_transcript, interruption -> on_action, error -> on_error.
- A registered voice ``@tool`` calls ``execute_voice_tool`` with the bound
  ``ctx`` / ``workspace_id`` on a FRESH session, returns the ``speak`` string,
  and forwards an ``action`` result to ``on_action``.
- No audio bytes or secrets are logged.
"""

from __future__ import annotations

import base64
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.core.tenancy import RequestContext
from app.db.models import MemberRole
from app.services import voice_gateway
from app.services.voice_gateway import NovaSonicVoiceSession


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBidiAgent:
    """A fake BidiAgent: records sent inputs, yields scripted output events."""

    def __init__(self, *, tools: list, system_prompt: str, scripted: list[dict]):
        self.tools = tools
        self.system_prompt = system_prompt
        self._scripted = scripted
        self.sent: list[Any] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def send(self, input_data: Any) -> None:
        self.sent.append(input_data)

    async def receive(self):
        for event in self._scripted:
            yield event

    async def stop(self) -> None:
        self.stopped = True


class _SentinelSession:
    """Marker object standing in for an AsyncSession (never touched)."""


def _make_session_factory(record: list[Any]):
    """A session_factory whose async context manager yields a sentinel session."""

    @asynccontextmanager
    async def _factory():
        session = _SentinelSession()
        record.append(session)
        yield session

    return _factory


def _ctx(workspace_id: uuid.UUID, *, member: bool = True) -> RequestContext:
    roles = {workspace_id: MemberRole.OWNER} if member else {}
    return RequestContext(
        user_id=uuid.uuid4(), active_workspace_id=workspace_id, roles=roles
    )


def _agent_factory_for(scripted: list[dict]):
    captured: dict[str, Any] = {}

    def factory(*, tools: list, system_prompt: str) -> FakeBidiAgent:
        agent = FakeBidiAgent(tools=tools, system_prompt=system_prompt, scripted=scripted)
        captured["agent"] = agent
        return agent

    return factory, captured


# ---------------------------------------------------------------------------
# send_audio wraps bytes into a BidiAudioInputEvent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_audio_wraps_pcm_into_audio_input_event() -> None:
    ws_id = uuid.uuid4()
    factory, captured = _agent_factory_for([])
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    pcm = b"\x01\x02\x03\x04rawpcm"
    await session.send_audio(pcm)

    agent = captured["agent"]
    assert len(agent.sent) == 1
    event = agent.sent[0]
    # It is the SDK BidiAudioInputEvent with the right fields.
    from strands.experimental.bidi import BidiAudioInputEvent

    assert isinstance(event, BidiAudioInputEvent)
    assert event.audio == base64.b64encode(pcm).decode("ascii")
    assert event.format == "pcm"
    assert event.sample_rate == 16000
    assert event.channels == 1


@pytest.mark.asyncio
async def test_send_text_wraps_into_text_input_event() -> None:
    ws_id = uuid.uuid4()
    factory, captured = _agent_factory_for([])
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()
    await session.send_text("hello")

    from strands.experimental.bidi import BidiTextInputEvent

    event = captured["agent"].sent[0]
    assert isinstance(event, BidiTextInputEvent)
    assert event.text == "hello"


# ---------------------------------------------------------------------------
# run() routes output events to the right callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_routes_audio_transcript_interruption_and_error() -> None:
    ws_id = uuid.uuid4()
    from strands.experimental.bidi import (
        BidiAudioStreamEvent,
        BidiErrorEvent,
        BidiInterruptionEvent,
        BidiTranscriptStreamEvent,
    )

    pcm24 = b"twenty-four-k-audio"
    scripted = [
        BidiTranscriptStreamEvent(
            delta={"text": "hi"}, text="hi", role="assistant", is_final=True
        ),
        BidiAudioStreamEvent(
            audio=base64.b64encode(pcm24).decode("ascii"),
            format="pcm",
            sample_rate=24000,
            channels=1,
        ),
        BidiInterruptionEvent(reason="user_speech"),
        BidiErrorEvent(RuntimeError("nova blew up")),
    ]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    audio_out: list[bytes] = []
    transcripts: list[tuple[str, str]] = []
    actions: list[dict] = []
    errors: list[str] = []

    await session.run(
        on_audio=lambda b, r: _append(audio_out, (b, r)),
        on_transcript=lambda r, t: _append(transcripts, (r, t)),
        on_action=lambda a: _append(actions, a),
        on_error=lambda m: _append(errors, m),
    )

    assert audio_out == [(pcm24, 24000)]
    assert transcripts == [("assistant", "hi")]
    assert actions == [{"type": "interrupt", "reason": "user_speech"}]
    assert errors == ["nova blew up"]


async def _append(target: list, value: Any) -> None:
    target.append(value)


# ---------------------------------------------------------------------------
# The registered @tool wrapper calls execute_voice_tool + forwards actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_tool_wrapper_calls_execute_and_forwards_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = uuid.uuid4()
    ctx = _ctx(ws_id)

    calls: list[dict] = []
    sessions_opened: list[Any] = []

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append(
            {
                "name": name,
                "tool_input": tool_input,
                "ctx": ctx,
                "session": session,
                "workspace_id": workspace_id,
            }
        )
        # A model-facing action tool returns a speak + an action envelope. We
        # use `submit_form` here because `navigate` is intentionally NOT exposed
        # to the model (it is deterministic-only — see _MODEL_EXCLUDED_TOOLS);
        # this test exercises the generic wrapper -> _dispatch_action path.
        return {"speak": "Submitting the form.", "action": {"type": "submit", "form": "create_rule"}}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    factory, _ = _agent_factory_for([])
    session = NovaSonicVoiceSession(
        ctx=ctx,
        workspace_id=ws_id,
        session_factory=_make_session_factory(sessions_opened),
        agent_factory=factory,
    )
    await session.start()

    # Wire an action sink the way run() would.
    forwarded: list[dict] = []
    session._on_action = lambda a: _append(forwarded, a)  # type: ignore[attr-defined]

    # Find the built (model-facing) 'submit_form' tool and invoke it.
    agent = session._agent  # type: ignore[attr-defined]
    submit_tool = _find_tool(agent.tools, "submit_form")
    spoken = await _invoke_tool(submit_tool, form="create_rule")

    # The tool spoke the envelope's `speak` and forwarded the action.
    assert spoken == "Submitting the form."
    assert forwarded == [{"type": "submit", "form": "create_rule"}]

    # execute_voice_tool was called with the bound ctx/workspace on a FRESH
    # session opened via the factory.
    assert len(calls) == 1
    assert calls[0]["name"] == "submit_form"
    assert calls[0]["ctx"] is ctx
    assert calls[0]["workspace_id"] == ws_id
    assert calls[0]["tool_input"] == {"form": "create_rule"}
    assert len(sessions_opened) == 1
    assert calls[0]["session"] is sessions_opened[0]


@pytest.mark.asyncio
async def test_voice_tool_speaks_default_when_no_speak_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = uuid.uuid4()

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        return {"data": []}  # no speak, no action

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    factory, _ = _agent_factory_for([])
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()
    # Use a tool still exposed to the model (the approval/list tools are now
    # withheld — the deterministic router owns them; see _MODEL_EXCLUDED_TOOLS).
    tool = _find_tool(session._agent.tools, "read_email")  # type: ignore[attr-defined]
    spoken = await _invoke_tool(tool)
    assert spoken == "Done."


def test_all_voice_tool_names_are_registered() -> None:
    """Every VOICE_TOOL_NAME becomes a registered Strands tool EXCEPT the
    deterministic-only tools (navigate) which are withheld from the model."""
    ws_id = uuid.uuid4()
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=lambda **_: None,
    )
    tools = session._build_tools()  # type: ignore[attr-defined]
    names = {_tool_name(t) for t in tools}
    expected = (
        set(voice_gateway.voice_tools.VOICE_TOOL_NAMES)
        - voice_gateway._MODEL_EXCLUDED_TOOLS
    )
    assert names == expected
    # `navigate` is deterministic-only: the model must NOT get it (else it
    # narrates false navigation failures — see ERROR.md).
    assert "navigate" not in names
    assert "navigate" in voice_gateway._MODEL_EXCLUDED_TOOLS


# ---------------------------------------------------------------------------
# No audio / secrets are logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_audio_bytes_logged(caplog: pytest.LogCaptureFixture) -> None:
    ws_id = uuid.uuid4()
    from strands.experimental.bidi import BidiAudioStreamEvent

    pcm = b"SUPER_SECRET_AUDIO_PAYLOAD"
    scripted = [
        BidiAudioStreamEvent(
            audio=base64.b64encode(pcm).decode("ascii"),
            format="pcm",
            sample_rate=24000,
            channels=1,
        )
    ]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )

    with caplog.at_level(logging.DEBUG):
        await session.start()
        pcm_in = b"MIC_INPUT_SECRET"
        await session.send_audio(pcm_in)
        await session.run(
            on_audio=lambda b, r: _noop(),
            on_transcript=lambda r, t: _noop(),
            on_action=lambda a: _noop(),
            on_error=lambda m: _noop(),
        )
        await session.stop()

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    # Neither raw audio nor its base64 encoding may appear in the logs.
    assert "SUPER_SECRET_AUDIO_PAYLOAD" not in log_text
    assert base64.b64encode(pcm).decode("ascii") not in log_text
    assert "MIC_INPUT_SECRET" not in log_text
    assert base64.b64encode(pcm_in).decode("ascii") not in log_text


async def _noop() -> None:
    return None


# ---------------------------------------------------------------------------
# Small helpers for driving a decorated Strands tool directly
# ---------------------------------------------------------------------------


def _tool_name(tool: Any) -> str:
    """Best-effort extraction of a decorated tool's registered name."""
    for attr in ("tool_name", "name"):
        value = getattr(tool, attr, None)
        if isinstance(value, str):
            return value
    spec = getattr(tool, "tool_spec", None)
    if isinstance(spec, dict) and isinstance(spec.get("name"), str):
        return spec["name"]
    return ""


def _find_tool(tools: list, name: str) -> Any:
    for tool in tools:
        if _tool_name(tool) == name:
            return tool
    raise AssertionError(f"tool {name!r} not registered")


async def _invoke_tool(tool: Any, **kwargs: Any) -> Any:
    """Invoke the underlying async function of a decorated Strands tool.

    A ``@tool``-decorated function remains directly callable with its original
    signature, so we call it with keyword args and await the coroutine.
    """
    result = tool(**kwargs)
    if hasattr(result, "__await__"):
        return await result
    return result

# ---------------------------------------------------------------------------
# Intent_Router: deterministic navigation, unknown destinations, help,
# de-duplication, and the once-per-session welcome (tasks 2.1 / 2.2).
#
# These exercise the gateway with scripted `bidi_transcript_stream(role=user)`
# events through the real `voice_intent` classifier (pure, no AWS) and assert
# Properties 1, 4, 5, and 6 from the design.
# ---------------------------------------------------------------------------


def _user_transcript(text: str):
    """Build a scripted final USER transcript event (Nova Sonic shape)."""
    from strands.experimental.bidi import BidiTranscriptStreamEvent

    return BidiTranscriptStreamEvent(
        delta={"text": text}, text=text, role="user", is_final=True
    )


async def _drive_run(session: NovaSonicVoiceSession):
    """Run the session's output loop, recording every callback invocation."""
    audio_out: list[tuple[bytes, int]] = []
    transcripts: list[tuple[str, str]] = []
    actions: list[dict] = []
    errors: list[str] = []

    await session.run(
        on_audio=lambda b, r: _append(audio_out, (b, r)),
        on_transcript=lambda r, t: _append(transcripts, (r, t)),
        on_action=lambda a: _append(actions, a),
        on_error=lambda m: _append(errors, m),
    )
    return audio_out, transcripts, actions, errors


# ---------------------------------------------------------------------------
# Property 1 — a navigation utterance dispatches exactly one navigate action
# plus a non-failure confirmation transcript.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_navigation_transcript_dispatches_one_navigate_and_confirms() -> None:
    ws_id = uuid.uuid4()
    scripted = [_user_transcript("go to approvals")]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    _, transcripts, actions, errors = await _drive_run(session)

    # Resolve the expected path from the single source of truth.
    expected_path = voice_gateway.voice_tools._resolve_nav_target("approvals")
    assert expected_path == "/dashboard/approvals"

    # Exactly one navigate action for the resolved path (Property 1).
    navigate_actions = [a for a in actions if a.get("type") == "navigate"]
    assert navigate_actions == [{"type": "navigate", "path": expected_path}]

    # An assistant confirmation transcript that does NOT claim failure.
    assistant_says = [t for (role, t) in transcripts if role == "assistant"]
    assert assistant_says, "expected an assistant confirmation transcript"
    confirmation = assistant_says[-1]
    assert "Approvals" in confirmation
    lowered = confirmation.lower()
    assert "fail" not in lowered
    assert "cannot" not in lowered
    assert "issue" not in lowered
    assert errors == []


# ---------------------------------------------------------------------------
# Property 4 — an unknown destination dispatches NO action and speaks the
# available-destinations list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_unknown_destination_dispatches_no_action_and_lists_available() -> None:
    ws_id = uuid.uuid4()
    scripted = [_user_transcript("go to the moon")]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    _, transcripts, actions, errors = await _drive_run(session)

    # No navigate action for an out-of-set destination (Property 4).
    assert [a for a in actions if a.get("type") == "navigate"] == []

    # The assistant speaks the available-destinations sentence.
    assistant_says = [t for (role, t) in transcripts if role == "assistant"]
    assert voice_gateway.voice_intent.NAV_DESTINATIONS_SENTENCE in assistant_says
    assert errors == []


# ---------------------------------------------------------------------------
# help — a help utterance injects the spoken guidance turn (send_text with the
# guidance script).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_help_transcript_injects_guidance_turn() -> None:
    ws_id = uuid.uuid4()
    scripted = [_user_transcript("help")]
    factory, captured = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    _, _, actions, errors = await _drive_run(session)

    from strands.experimental.bidi import BidiTextInputEvent

    guidance = voice_gateway.voice_intent.build_guidance_script()
    injected = [
        e.text
        for e in captured["agent"].sent
        if isinstance(e, BidiTextInputEvent)
    ]
    assert guidance in injected

    # Help dispatches no navigate action.
    assert [a for a in actions if a.get("type") == "navigate"] == []
    assert errors == []


# ---------------------------------------------------------------------------
# Property 6 — a model `navigate` tool call for the same turn/path as the
# router's own dispatch is de-duplicated (suppressed, not forwarded again).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_navigate_tool_call_deduped_within_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = uuid.uuid4()
    expected_path = voice_gateway.voice_tools._resolve_nav_target("approvals")
    assert expected_path == "/dashboard/approvals"

    # The model's navigate tool resolves to the SAME path the router dispatched.
    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        return {
            "speak": "Opening Approvals.",
            "action": {"type": "navigate", "path": expected_path},
        }

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    scripted = [_user_transcript("go to approvals")]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    # 1. Drive run(): the router deterministically dispatches ONE navigate for
    #    this turn and records _last_nav_dispatch = (turn_index, path).
    _, _, actions, _ = await _drive_run(session)
    router_navigates = [a for a in actions if a.get("type") == "navigate"]
    assert router_navigates == [{"type": "navigate", "path": expected_path}]

    # 2. In the SAME turn, a navigate action for the same path is dispatched
    #    again (this is what a model-emitted navigate WOULD do — the model no
    #    longer has the navigate tool, but the dedup guard in _dispatch_action
    #    remains as defense-in-depth). Wire an action sink (run() cleared it) and
    #    dispatch the duplicate directly.
    forwarded: list[dict] = []
    session._on_action = lambda a: _append(forwarded, a)  # type: ignore[attr-defined]

    await session._dispatch_action(  # type: ignore[attr-defined]
        {"type": "navigate", "path": expected_path}
    )

    # The duplicate navigate for the same turn/path is suppressed — NOT forwarded
    # to the client again (Property 6).
    assert forwarded == []


@pytest.mark.asyncio
async def test_model_navigate_tool_call_not_deduped_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model navigate for a DIFFERENT turn than the router's dispatch is
    forwarded (de-duplication is scoped to a single turn)."""
    ws_id = uuid.uuid4()
    expected_path = voice_gateway.voice_tools._resolve_nav_target("approvals")

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        return {
            "speak": "Opening Approvals.",
            "action": {"type": "navigate", "path": expected_path},
        }

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    # Two user turns: the second is conversational (no router navigate), so the
    # model's navigate tool call belongs to a later turn than _last_nav_dispatch.
    scripted = [
        _user_transcript("go to approvals"),
        _user_transcript("read my unread emails"),
    ]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    await _drive_run(session)

    forwarded: list[dict] = []
    session._on_action = lambda a: _append(forwarded, a)  # type: ignore[attr-defined]

    # A navigate action dispatched now belongs to a LATER turn than
    # _last_nav_dispatch (the second user turn advanced _turn_index), so the
    # dedup guard does not apply and it IS forwarded.
    await session._dispatch_action(  # type: ignore[attr-defined]
        {"type": "navigate", "path": expected_path}
    )
    assert forwarded == [{"type": "navigate", "path": expected_path}]


# ---------------------------------------------------------------------------
# Property 5 — send_welcome() injects the welcome directive at most once across
# repeated calls.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_welcome_injects_directive_at_most_once() -> None:
    ws_id = uuid.uuid4()
    factory, captured = _agent_factory_for([])
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    # Call repeatedly; only the first should inject the directive.
    await session.send_welcome()
    await session.send_welcome()
    await session.send_welcome()

    from strands.experimental.bidi import BidiTextInputEvent

    welcome_events = [
        e
        for e in captured["agent"].sent
        if isinstance(e, BidiTextInputEvent)
        and e.text == voice_gateway.voice_intent.WELCOME_DIRECTIVE
    ]
    assert len(welcome_events) == 1

# ---------------------------------------------------------------------------
# Resilience: a model stream error raised MID-ITERATION out of receive() must
# be reported via on_error and end run() gracefully — never crash the endpoint.
#
# Regression: the Strands/Nova Sonic SDK surfaces transient Bedrock failures
# (e.g. ModelStreamErrorException) by RAISING out of receive() rather than
# yielding a bidi_error event. Previously that exception escaped run(), crashed
# the endpoint's anyio task group as an unhandled ExceptionGroup, and the voice
# session died silently (the client just hung).
# ---------------------------------------------------------------------------


class _RaisingBidiAgent:
    """A fake agent whose receive() yields some events, then raises."""

    def __init__(self, *, tools, system_prompt, pre_events, error):
        self.tools = tools
        self.system_prompt = system_prompt
        self._pre_events = pre_events
        self._error = error
        self.sent = []
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def send(self, input_data):
        self.sent.append(input_data)

    async def receive(self):
        for event in self._pre_events:
            yield event
        raise self._error

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_run_reconnects_then_reports_after_exhausting_attempts() -> None:
    """A persistently failing model stream is retried (reconnect) and, only
    after attempts are exhausted, reported to on_error — run() never raises so
    the endpoint task group is not crashed."""
    ws_id = uuid.uuid4()
    from strands.experimental.bidi import BidiTranscriptStreamEvent

    pre = [
        BidiTranscriptStreamEvent(
            delta={"text": "hi"}, text="hi", role="assistant", is_final=True
        )
    ]

    class _ModelStreamError(Exception):
        """Stand-in for aws ModelStreamErrorException (raised out of receive)."""

    builds = {"count": 0}

    def factory(*, tools, system_prompt):
        builds["count"] += 1
        return _RaisingBidiAgent(
            tools=tools,
            system_prompt=system_prompt,
            pre_events=pre,
            error=_ModelStreamError("transient bedrock stream failure"),
        )

    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    # Speed the test up: no backoff sleeps.
    import app.services.voice_gateway as vg
    monkeypatch_backoff = vg._RECONNECT_BACKOFF_S
    vg._RECONNECT_BACKOFF_S = 0  # type: ignore[attr-defined]
    try:
        await session.start()

        transcripts: list[tuple[str, str]] = []
        errors: list[str] = []

        # Must NOT raise, even though every connection's receive() raises.
        await session.run(
            on_audio=lambda b, r: _append([], (b, r)),
            on_transcript=lambda r, t: _append(transcripts, (r, t)),
            on_action=lambda a: _append([], a),
            on_error=lambda m: _append(errors, m),
        )
    finally:
        vg._RECONNECT_BACKOFF_S = monkeypatch_backoff  # type: ignore[attr-defined]

    # The initial build + one per reconnect attempt (bounded).
    assert builds["count"] == 1 + vg._MAX_RECONNECT_ATTEMPTS
    # The pre-error transcript was delivered on each connection attempt.
    assert transcripts == [("assistant", "hi")] * (1 + vg._MAX_RECONNECT_ATTEMPTS)
    # After exhausting reconnects, the client is told (so it can recover).
    assert len(errors) == 1
    assert errors[0]  # a non-empty, user-facing message


@pytest.mark.asyncio
async def test_run_reconnects_and_resumes_on_transient_error() -> None:
    """A transient stream error triggers a transparent reconnect; the SECOND
    (healthy) connection resumes and the session ends cleanly — the user is
    NOT shown a failure and the session is not torn down."""
    ws_id = uuid.uuid4()
    from strands.experimental.bidi import (
        BidiTranscriptStreamEvent,
        BidiAudioStreamEvent,
    )

    class _ModelStreamError(Exception):
        pass

    # First agent: yields a transcript, then raises (transient drop).
    # Second agent: yields audio, then ends cleanly (healthy reconnect).
    pre_first = [
        BidiTranscriptStreamEvent(
            delta={"text": "hi"}, text="hi", role="assistant", is_final=True
        )
    ]
    good_audio = base64.b64encode(b"reconnected-audio").decode("ascii")
    pre_second = [
        BidiAudioStreamEvent(
            audio=good_audio, format="pcm", sample_rate=16000, channels=1
        )
    ]

    agents: list = []

    def factory(*, tools, system_prompt):
        idx = len(agents)
        if idx == 0:
            agent = _RaisingBidiAgent(
                tools=tools,
                system_prompt=system_prompt,
                pre_events=pre_first,
                error=_ModelStreamError("transient drop"),
            )
        else:
            agent = FakeBidiAgent(
                tools=tools, system_prompt=system_prompt, scripted=pre_second
            )
        agents.append(agent)
        return agent

    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    import app.services.voice_gateway as vg
    saved = vg._RECONNECT_BACKOFF_S
    vg._RECONNECT_BACKOFF_S = 0  # type: ignore[attr-defined]
    try:
        await session.start()

        audio_out: list[tuple[bytes, int]] = []
        transcripts: list[tuple[str, str]] = []
        errors: list[str] = []

        await session.run(
            on_audio=lambda b, r: _append(audio_out, (b, r)),
            on_transcript=lambda r, t: _append(transcripts, (r, t)),
            on_action=lambda a: _append([], a),
            on_error=lambda m: _append(errors, m),
        )
    finally:
        vg._RECONNECT_BACKOFF_S = saved  # type: ignore[attr-defined]

    # Reconnected to a second, healthy agent.
    assert len(agents) == 2
    # The pre-drop transcript AND the post-reconnect audio were both delivered.
    assert transcripts == [("assistant", "hi")]
    assert audio_out == [(b"reconnected-audio", 16000)]
    # A successful reconnect must NOT surface a failure message to the user.
    assert errors == []


@pytest.mark.asyncio
async def test_run_does_not_swallow_cancellation() -> None:
    """CancelledError raised by the stream must propagate (task-group teardown),
    not be swallowed by the model-stream error guard."""
    import asyncio

    ws_id = uuid.uuid4()

    def factory(*, tools, system_prompt):
        return _RaisingBidiAgent(
            tools=tools,
            system_prompt=system_prompt,
            pre_events=[],
            error=asyncio.CancelledError(),
        )

    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    with pytest.raises(asyncio.CancelledError):
        await session.run(
            on_audio=lambda b, r: _append([], (b, r)),
            on_transcript=lambda r, t: _append([], (r, t)),
            on_action=lambda a: _append([], a),
            on_error=lambda m: _append([], m),
        )


# ---------------------------------------------------------------------------
# Resilience: stop() must never raise, even when the underlying agent's stop
# sequence throws (dead Bedrock/Nova Sonic stream after a mid-stream error or
# an auth/signature failure). Previously this crashed the endpoint's finally
# teardown with a RuntimeError.
# ---------------------------------------------------------------------------


class _StopRaisingBidiAgent:
    """A fake agent whose stop() raises (simulating a dead stream teardown)."""

    def __init__(self, *, tools, system_prompt):
        self.tools = tools
        self.system_prompt = system_prompt
        self.sent = []
        self.started = False
        self.stop_attempts = 0

    async def start(self):
        self.started = True

    async def send(self, input_data):
        self.sent.append(input_data)

    async def receive(self):
        if False:  # pragma: no cover - never yields
            yield {}

    async def stop(self):
        self.stop_attempts += 1
        raise RuntimeError(
            "failed stop sequence: stream already completed / InvalidSignatureException"
        )


@pytest.mark.asyncio
async def test_stop_never_raises_when_agent_stop_fails() -> None:
    """stop() swallows a failing agent stop and still marks the session stopped."""
    ws_id = uuid.uuid4()
    captured = {}

    def factory(*, tools, system_prompt):
        agent = _StopRaisingBidiAgent(tools=tools, system_prompt=system_prompt)
        captured["agent"] = agent
        return agent

    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    # Must NOT raise despite the agent.stop() RuntimeError.
    await session.stop()

    # The agent stop was attempted and the session is marked not-started so a
    # second stop() is a no-op (idempotent).
    assert captured["agent"].stop_attempts == 1
    await session.stop()  # idempotent: no second attempt, no raise
    assert captured["agent"].stop_attempts == 1


# ===========================================================================
# Position-aware approval/reply actions: POSITION -> approval_id resolution and
# per-kind action dispatch / tool routing (all DB-free, AWS-free).
# ===========================================================================


class _FakeResult:
    """Minimal stand-in for a SQLAlchemy Result: .scalars().all() -> ids."""

    def __init__(self, ids: list[str]):
        self._ids = ids

    def scalars(self):
        return self

    def all(self):
        return list(self._ids)


class _FakePendingSession:
    """A fake AsyncSession: execute() returns scripted pending ids in order.

    get() returns None by default so _describe_original degrades gracefully
    (best-effort original context is skipped and only the reply is read).
    """

    def __init__(self, ids: list[str]):
        self._ids = ids
        self.get_calls: list = []

    async def execute(self, _stmt):
        return _FakeResult(self._ids)

    async def get(self, _model, ident):
        self.get_calls.append(ident)
        return None


def _pending_session_factory(ids: list[str]):
    """A session_factory whose context manager yields a _FakePendingSession."""

    @asynccontextmanager
    async def _factory():
        yield _FakePendingSession(ids)

    return _factory


async def _run_transcript(session: NovaSonicVoiceSession, text: str):
    """Drive one user transcript turn, returning (transcripts, actions)."""
    transcripts: list[tuple[str, str]] = []
    actions: list[dict] = []
    session._on_action = lambda a: _append(actions, a)  # type: ignore[attr-defined]
    await session._handle_user_transcript(  # type: ignore[attr-defined]
        text, on_transcript=lambda r, t: _append(transcripts, (r, t))
    )
    return transcripts, actions


def _make_position_session(
    ids: list[str], monkeypatch: pytest.MonkeyPatch, calls: list[dict]
) -> NovaSonicVoiceSession:
    ws_id = uuid.uuid4()

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append({"name": name, "tool_input": tool_input, "workspace_id": workspace_id})
        if name == "read_approval":
            return {"speak": "Reply to bob@example.com, subject Re: Hi. Hello there."}
        return {"speak": "Done."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    return NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_pending_session_factory(ids),
        agent_factory=lambda **_: None,
    )


# ---------------------------------------------------------------------------
# POSITION -> approval_id resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_position_resolves_to_nth_pending_id(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = ["id-1", "id-2", "id-3"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    # "approve and send the second" -> approve_and_send on ids[1].
    await _run_transcript(session, "approve and send the second")

    send_calls = [c for c in calls if c["name"] == "approve_and_send"]
    assert len(send_calls) == 1
    assert send_calls[0]["tool_input"] == {"approval_id": "id-2"}


@pytest.mark.asyncio
async def test_position_out_of_range_no_action_and_speaks_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]  # only one pending
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    transcripts, actions = await _run_transcript(session, "approve and send the third")

    # No tool call, no action dispatched.
    assert calls == []
    assert actions == []
    # A spoken message names the missing number and the pending count.
    assistant = [t for (r, t) in transcripts if r == "assistant"]
    assert assistant, "expected a spoken count message"
    msg = assistant[-1].lower()
    assert "number 3" in msg or "reply number 3" in msg
    assert "1 pending" in msg


# ---------------------------------------------------------------------------
# Per-kind dispatch / tool routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_position_reads_reply_and_focuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1", "id-2"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    transcripts, actions = await _run_transcript(session, "read the first email")

    # read_approval tool called for the first id.
    read_calls = [c for c in calls if c["name"] == "read_approval"]
    assert read_calls == [
        {"name": "read_approval", "tool_input": {"approval_id": "id-1"},
         "workspace_id": read_calls[0]["workspace_id"]}
    ]
    # Focus action dispatched for position 1.
    assert {"type": "focus_reply", "position": 1} in actions
    # The proposed reply is spoken.
    assistant = " ".join(t for (r, t) in transcripts if r == "assistant")
    assert "proposed reply" in assistant.lower()


@pytest.mark.asyncio
async def test_regenerate_position_calls_tool_and_focuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1", "id-2"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    transcripts, actions = await _run_transcript(session, "regenerate the second reply")

    regen = [c for c in calls if c["name"] == "regenerate_reply"]
    assert regen == [{"name": "regenerate_reply", "tool_input": {"approval_id": "id-2"},
                      "workspace_id": regen[0]["workspace_id"]}]
    assert {"type": "focus_reply", "position": 2} in actions


@pytest.mark.asyncio
async def test_edit_position_dispatches_open_edit_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    transcripts, actions = await _run_transcript(session, "edit the first reply")

    # No approval tool is called for edit — the UI edit flow handles the text.
    assert [c for c in calls if c["name"].startswith("approve")] == []
    assert {"type": "open_edit", "position": 1} in actions
    assistant = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "editor" in assistant


@pytest.mark.asyncio
async def test_approve_draft_position_calls_save_draft_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    await _run_transcript(session, "approve and save to draft the first")

    draft = [c for c in calls if c["name"] == "approve_and_save_draft"]
    assert draft and draft[0]["tool_input"] == {"approval_id": "id-1"}


@pytest.mark.asyncio
async def test_approve_send_position_calls_send_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    await _run_transcript(session, "approve and send the first")

    send = [c for c in calls if c["name"] == "approve_and_send"]
    assert send and send[0]["tool_input"] == {"approval_id": "id-1"}


@pytest.mark.asyncio
async def test_approve_schedule_with_time_calls_tool_and_opens_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    _, actions = await _run_transcript(
        session, "approve and schedule the first at tomorrow 3pm"
    )

    sched = [c for c in calls if c["name"] == "approve_and_schedule"]
    assert sched, "expected approve_and_schedule tool call when a time was spoken"
    assert sched[0]["tool_input"]["approval_id"] == "id-1"
    assert sched[0]["tool_input"].get("when_iso")
    # An open_schedule action is also dispatched with the time.
    open_sched = [a for a in actions if a.get("type") == "open_schedule"]
    assert open_sched and open_sched[0]["position"] == 1
    assert open_sched[0].get("when_iso")


@pytest.mark.asyncio
async def test_approve_schedule_without_time_opens_picker_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    transcripts, actions = await _run_transcript(session, "approve and schedule the first")

    # No schedule tool call without a concrete time.
    assert [c for c in calls if c["name"] == "approve_and_schedule"] == []
    # An open_schedule action WITHOUT a when_iso is dispatched.
    open_sched = [a for a in actions if a.get("type") == "open_schedule"]
    assert open_sched and open_sched[0].get("when_iso") is None
    assistant = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "date and time" in assistant


@pytest.mark.asyncio
async def test_position_intent_uses_workspace_bound_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every position tool call is bound to the session's workspace (server-side)."""
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    await _run_transcript(session, "approve and send the first")

    assert calls, "expected a tool call"
    for c in calls:
        assert c["workspace_id"] == session._workspace_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Model audio/transcript forwarding (REGRESSION GUARD).
#
# The model's audio + transcript must ALWAYS be forwarded to the client, even
# on a turn the deterministic router handled. An earlier "suppress the model's
# competing reply" feature dropped audio per-chunk and made the voice crack /
# twitch between listening and speaking, because Nova Sonic streams audio in
# many small chunks whose response boundaries do not cleanly bracket a spoken
# response. These tests lock in the reverted, known-good behavior so per-chunk
# suppression is never silently reintroduced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_audio_forwarded_after_deterministic_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's audio + transcript are forwarded even on a router-handled
    turn (no per-chunk suppression — the crack/twitch regression fix)."""
    ws_id = uuid.uuid4()
    from strands.experimental.bidi import (
        BidiAudioStreamEvent,
        BidiTranscriptStreamEvent,
    )

    # regenerate the reply must succeed deterministically.
    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        return {"speak": "Done."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    # One pending approval id so position 1 resolves in the cache.
    pending_id = str(uuid.uuid4())

    apology = "I'm sorry, but I encountered an issue while trying to rephrase."
    scripted = [
        # User asks to regenerate the body of the first reply (router handles it).
        BidiTranscriptStreamEvent(
            delta={"text": "rephrase the body of the first email"},
            text="rephrase the body of the first email",
            role="user",
            is_final=True,
        ),
        # The model then starts its OWN competing response and apologises + audio.
        {"type": "bidi_response_start", "response_id": "r1"},
        BidiTranscriptStreamEvent(
            delta={"text": apology}, text=apology, role="assistant", is_final=True
        ),
        BidiAudioStreamEvent(
            audio=base64.b64encode(b"apology-audio").decode("ascii"),
            format="pcm",
            sample_rate=16000,
            channels=1,
        ),
        {"type": "bidi_response_complete", "response_id": "r1"},
    ]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_pending_session_factory([pending_id]),
        agent_factory=factory,
    )
    await session.start()

    audio_out: list = []
    transcripts: list[tuple[str, str]] = []

    await session.run(
        on_audio=lambda b, r: _append(audio_out, (b, r)),
        on_transcript=lambda r, t: _append(transcripts, (r, t)),
        on_action=lambda a: _append([], a),
        on_error=lambda m: _append([], m),
    )

    said = [t for (role, t) in transcripts if role == "assistant"]
    # REGRESSION GUARD: the model's audio + transcript are forwarded, NOT
    # dropped (per-chunk suppression caused the crack/twitch playback bug).
    assert audio_out == [(b"apology-audio", 16000)], (
        "model audio must be forwarded (no per-chunk suppression)"
    )
    assert apology in said, said
    # The router's own confirmation is ALSO spoken (both are heard now).
    assert any("regenerat" in t.lower() for t in said), said


@pytest.mark.asyncio
async def test_model_response_not_suppressed_for_plain_conversation() -> None:
    """A non-command turn (no deterministic intent) lets the model speak."""
    ws_id = uuid.uuid4()
    from strands.experimental.bidi import (
        BidiAudioStreamEvent,
        BidiTranscriptStreamEvent,
    )

    reply = "I'm doing well, thanks for asking!"
    scripted = [
        BidiTranscriptStreamEvent(
            delta={"text": "how are you today"},
            text="how are you today",
            role="user",
            is_final=True,
        ),
        {"type": "bidi_response_start", "response_id": "r1"},
        BidiTranscriptStreamEvent(
            delta={"text": reply}, text=reply, role="assistant", is_final=True
        ),
        BidiAudioStreamEvent(
            audio=base64.b64encode(b"friendly-audio").decode("ascii"),
            format="pcm",
            sample_rate=16000,
            channels=1,
        ),
        {"type": "bidi_response_complete", "response_id": "r1"},
    ]
    factory, _ = _agent_factory_for(scripted)
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=factory,
    )
    await session.start()

    audio_out: list = []
    transcripts: list[tuple[str, str]] = []
    await session.run(
        on_audio=lambda b, r: _append(audio_out, (b, r)),
        on_transcript=lambda r, t: _append(transcripts, (r, t)),
        on_action=lambda a: _append([], a),
        on_error=lambda m: _append([], m),
    )

    said = [t for (role, t) in transcripts if role == "assistant"]
    assert reply in said, "the model must be heard for ordinary conversation"
    assert audio_out == [(b"friendly-audio", 16000)]


# ---------------------------------------------------------------------------
# In-place field edits (ERROR.md): edit the wording / a field of the Nth reply
# by position, server-side, WITHOUT ever asking for an approval id.
# ---------------------------------------------------------------------------


class _EditableRequest:
    """A stand-in ApprovalRequest carrying decodable reply fields."""

    def __init__(self, workspace_id: uuid.UUID, fields: dict):
        self.workspace_id = workspace_id
        self.arguments = dict(fields)


class _EditPendingSession:
    """Fake session: execute() lists ids in order; get() returns editable rows.

    ``rows`` maps an id-string to a field dict so ``_read_reply_field`` can decode
    the current value of the reply being edited.
    """

    def __init__(self, workspace_id: uuid.UUID, ids: list[str], rows: dict):
        self._workspace_id = workspace_id
        self._ids = ids
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._ids)

    async def get(self, _model, ident):
        fields = self._rows.get(str(ident))
        if fields is None:
            return None
        return _EditableRequest(self._workspace_id, fields)


def _make_edit_session(
    monkeypatch: pytest.MonkeyPatch, ids: list[str], rows: dict, calls: list[dict]
) -> NovaSonicVoiceSession:
    """A session wired for in-place field edits (DB-free, AWS-free).

    ``execute_voice_tool`` is faked to record edit_reply calls; the DB session's
    ``get`` returns editable rows; ``decode_reply_fields`` returns the row's
    stored fields unchanged so the gateway can read the current value.
    """
    ws_id = uuid.uuid4()

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append({"name": name, "tool_input": tool_input, "workspace_id": workspace_id})
        return {"speak": "I've updated the reply."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)
    # _read_reply_field imports approval_service.decode_reply_fields lazily.
    from app.services import approval_service

    monkeypatch.setattr(
        approval_service, "decode_reply_fields", lambda args: dict(args or {})
    )

    @asynccontextmanager
    async def _factory():
        yield _EditPendingSession(ws_id, ids, rows)

    return NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_factory,
        agent_factory=lambda **_: None,
    )


@pytest.mark.asyncio
async def test_replace_in_body_after_focus_edits_via_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ERROR.md scenario: 'edit the body of the first email' then
    'change Hi there to Hello PayRogen' applies the edit server-side without
    ever asking for an approval id."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "bob@x.com", "subject": "Hi", "body": "Hi there, thanks."}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, ids, rows, calls)

    # 1) Focus the body of the first reply (records the last edit target).
    await _run_transcript(session, "edit the body of the first email")
    # 2) Speak the in-place wording change (no position/field/id given).
    transcripts, actions = await _run_transcript(
        session, "change hi there to hello payrogen"
    )

    edits = [c for c in calls if c["name"] == "edit_reply"]
    assert len(edits) == 1, calls
    ti = edits[0]["tool_input"]
    assert ti["approval_id"] == id1
    # The body is rewritten with the replacement applied (case preserved from
    # the replacement; the search matched case-insensitively).
    assert ti["body"] == "hello payrogen, thanks."
    # Only the body field is edited (to/subject untouched).
    assert "to" not in ti and "subject" not in ti
    assert {"type": "focus_reply", "position": 1} in actions
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "hello payrogen" in said
    assert "sorry" not in said and "approval id" not in said


@pytest.mark.asyncio
async def test_set_subject_by_position_edits_via_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
    ids = [id1, id2]
    rows = {
        id1: {"to": "a@x.com", "subject": "Old A", "body": "Body A"},
        id2: {"to": "b@x.com", "subject": "Old B", "body": "Body B"},
    }
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, ids, rows, calls)

    transcripts, _ = await _run_transcript(
        session, "change the subject of reply 2 to support request"
    )

    edits = [c for c in calls if c["name"] == "edit_reply"]
    assert len(edits) == 1
    assert edits[0]["tool_input"] == {"approval_id": id2, "subject": "support request"}
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "sorry" not in said


@pytest.mark.asyncio
async def test_replace_not_found_speaks_and_no_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the search text isn't present, no edit is made and no apology-id
    prompt is spoken — just a clear 'couldn't find' message."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "bob@x.com", "subject": "Hi", "body": "Hello team."}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, ids, rows, calls)

    await _run_transcript(session, "edit the body of the first email")
    transcripts, _ = await _run_transcript(
        session, "change goodbye to farewell"
    )

    assert [c for c in calls if c["name"] == "edit_reply"] == []
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "couldn't find" in said or "could not find" in said
    assert "approval id" not in said


# ---------------------------------------------------------------------------
# Deterministic listing (ERROR.md round 2): the router lists pending approvals /
# unread email on the reliable main loop; the model never needs those tools.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pending_routes_to_tool_and_speaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    # Override the fake execute to return a listing summary for the list tool.
    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append({"name": name, "tool_input": tool_input, "workspace_id": workspace_id})
        if name == "list_pending_approvals":
            return {"speak": "You have 1 reply awaiting approval.", "data": [{"id": "id-1"}]}
        return {"speak": "Done."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    transcripts, _ = await _run_transcript(session, "read my pending approvals")

    assert [c["name"] for c in calls] == ["list_pending_approvals"]
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "awaiting approval" in said
    assert "sorry" not in said


@pytest.mark.asyncio
async def test_list_unread_routes_to_tool_and_speaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids: list[str] = []
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append({"name": name, "tool_input": tool_input, "workspace_id": workspace_id})
        if name == "list_unread_emails":
            return {"speak": "You have 2 unread emails.", "data": []}
        return {"speak": "Done."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    transcripts, _ = await _run_transcript(session, "read my unread emails")

    assert [c["name"] for c in calls] == ["list_unread_emails"]
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "unread email" in said


def test_approval_action_tools_withheld_from_model() -> None:
    """The router owns approval listing/reading/actions — the model must NOT get
    those tools (ERROR.md: the model's competing list->read->act tool chain
    failed and narrated false apologies)."""
    ws_id = uuid.uuid4()
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=lambda **_: None,
    )
    names = {_tool_name(t) for t in session._build_tools()}  # type: ignore[attr-defined]
    for withheld in (
        "navigate",
        "list_pending_approvals",
        "read_approval",
        "edit_reply",
        "regenerate_reply",
        "approve_and_save_draft",
        "approve_and_send",
        "approve_and_schedule",
        "clear_pending",
    ):
        assert withheld not in names, withheld
    # Inbox read + form helpers remain available to the model.
    for kept in ("list_unread_emails", "read_email", "type_text", "submit_form"):
        assert kept in names, kept


# ---------------------------------------------------------------------------
# Fragmented in-place edit across two turns (ERROR.md round 3):
# "change the text hi there" then "to hi payroll" applies without an id and
# without asking the user to repeat the exact text.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fragmented_edit_applies_across_two_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "bob@x.com", "subject": "Hi", "body": "Hi there, thanks."}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, ids, rows, calls)

    # 1) Focus the body of the first reply.
    await _run_transcript(session, "edit the body of the first email")
    # 2) Name the text to change (no replacement yet).
    t1, _ = await _run_transcript(session, "change the text hi there")
    said1 = " ".join(t for (r, t) in t1 if r == "assistant").lower()
    assert "what should i change" in said1
    assert [c for c in calls if c["name"] == "edit_reply"] == []  # nothing yet
    # 3) Supply the replacement in a later turn.
    t2, actions = await _run_transcript(session, "to hi payroll")

    edits = [c for c in calls if c["name"] == "edit_reply"]
    assert len(edits) == 1, calls
    assert edits[0]["tool_input"] == {"approval_id": id1, "body": "hi payroll, thanks."}
    said2 = " ".join(t for (r, t) in t2 if r == "assistant").lower()
    assert "hi payroll" in said2
    assert "sorry" not in said2 and "exact text" not in said2
    # A live editor update is emitted.
    assert any(a.get("type") == "edit_field" for a in actions)


@pytest.mark.asyncio
async def test_stray_replacement_without_pending_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'to X' continuation with no buffered search does nothing (no crash,
    no edit)."""
    id1 = str(uuid.uuid4())
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Body"}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, [id1], rows, calls)

    transcripts, _ = await _run_transcript(session, "to hi payroll")
    assert [c for c in calls if c["name"] == "edit_reply"] == []


@pytest.mark.asyncio
async def test_fragmented_edit_via_change_it_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    id1 = str(uuid.uuid4())
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Hello there team."}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, [id1], rows, calls)

    await _run_transcript(session, "edit the body of the first email")
    await _run_transcript(session, "change hello there")
    t2, _ = await _run_transcript(session, "change it to good morning")

    edits = [c for c in calls if c["name"] == "edit_reply"]
    assert len(edits) == 1
    assert edits[0]["tool_input"] == {"approval_id": id1, "body": "good morning team."}


# ---------------------------------------------------------------------------
# Single-item REJECT via voice + trailing-connector / bare-word fragmented edit
# (ERROR.md round 4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_position_calls_reject_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["id-1", "id-2"]
    calls: list[dict] = []
    session = _make_position_session(ids, monkeypatch, calls)

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append({"name": name, "tool_input": tool_input, "workspace_id": workspace_id})
        if name == "reject_reply":
            return {"speak": "I've rejected that reply.",
                    "action": {"type": "approvals_cleared", "cleared": 1}}
        return {"speak": "Done."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)

    transcripts, actions = await _run_transcript(session, "reject the second reply")

    rej = [c for c in calls if c["name"] == "reject_reply"]
    assert rej and rej[0]["tool_input"] == {"approval_id": "id-2"}
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "rejected" in said
    assert any(a.get("type") == "approvals_cleared" for a in actions)


def test_reject_reply_withheld_from_model() -> None:
    """Reject is router-owned; the model must not get the reject_reply tool."""
    ws_id = uuid.uuid4()
    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_make_session_factory([]),
        agent_factory=lambda **_: None,
    )
    names = {_tool_name(t) for t in session._build_tools()}  # type: ignore[attr-defined]
    assert "reject_reply" not in names
    assert "reject_reply" in voice_gateway._MODEL_EXCLUDED_TOOLS


@pytest.mark.asyncio
async def test_trailing_connector_then_bare_word_completes_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact ERROR.md flow: 'in the body change high there to' then a bare
    'hello' applies the edit without an id and without an apology loop."""
    id1 = str(uuid.uuid4())
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "High there, team."}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, [id1], rows, calls)

    # 1) Trailing-connector search (no replacement yet).
    t1, _ = await _run_transcript(session, "in the body change high there to")
    said1 = " ".join(t for (r, t) in t1 if r == "assistant").lower()
    assert "what should i change" in said1
    assert [c for c in calls if c["name"] == "edit_reply"] == []
    # 2) Bare replacement word.
    t2, actions = await _run_transcript(session, "hello")

    edits = [c for c in calls if c["name"] == "edit_reply"]
    assert len(edits) == 1, calls
    # "High there" matched case-insensitively; replaced with "hello".
    assert edits[0]["tool_input"] == {"approval_id": id1, "body": "hello, team."}
    said2 = " ".join(t for (r, t) in t2 if r == "assistant").lower()
    assert "sorry" not in said2 and "exact text" not in said2


@pytest.mark.asyncio
async def test_bare_word_without_pending_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare word with NO buffered edit is left to the model (no edit_reply)."""
    id1 = str(uuid.uuid4())
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Body"}}
    calls: list[dict] = []
    session = _make_edit_session(monkeypatch, [id1], rows, calls)

    await _run_transcript(session, "hello")
    assert [c for c in calls if c["name"] == "edit_reply"] == []
