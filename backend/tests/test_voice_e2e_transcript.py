"""End-to-end voice harness: replay the EXACT ERROR.md transcript sequence.

The user reported (round 5) that the voice assistant could press action buttons
but still could NOT edit the body or enter the schedule calendar date/time.
Those failures happened because the user spoke the command across MULTIPLE turns
("in the body ... change hi" then "there to hello"; "approve and schedule" then
"september fourteenth" then "five pm"). Unit tests cover the pieces; this test
wires the WHOLE gateway together and drives it exactly the way the real Nova
Sonic stream does — by emitting ``BidiTranscriptStreamEvent(role="user")``
events through the real :meth:`NovaSonicVoiceSession.run` loop — with a fake
BidiAgent (no AWS) and an in-memory editable approval, then asserts the correct
server-side tool calls and frontend actions fire.

This is the headless equivalent of streaming a pre-recorded PCM WAV of the same
commands: it exercises the payload framing, the deterministic Intent_Router, the
per-session multi-turn buffers, the tool-execution loop, and the on_action /
on_transcript dispatch — end to end, offline. (A separate test drives the raw
audio-input framing via ``send_audio``.)
"""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.core.tenancy import RequestContext
from app.db.models import MemberRole
from app.services import voice_gateway
from app.services.voice_gateway import NovaSonicVoiceSession


def _ctx(ws_id: uuid.UUID) -> RequestContext:
    return RequestContext(
        user_id=uuid.uuid4(),
        active_workspace_id=ws_id,
        roles={ws_id: MemberRole.OWNER},
    )


class _EditableRequest:
    def __init__(self, workspace_id: uuid.UUID, fields: dict):
        self.workspace_id = workspace_id
        self.arguments = dict(fields)


class _EditSession:
    """Fake AsyncSession: execute() lists ids ASC; get() returns editable rows."""

    def __init__(self, workspace_id: uuid.UUID, ids: list[str], rows: dict):
        self._workspace_id = workspace_id
        self._ids = ids
        self._rows = rows

    async def execute(self, _stmt):
        class _R:
            def __init__(self, ids):
                self._ids = ids

            def scalars(self):
                return self

            def all(self):
                return list(self._ids)

        return _R(self._ids)

    async def get(self, _model, ident):
        fields = self._rows.get(str(ident))
        if fields is None:
            return None
        return _EditableRequest(self._workspace_id, fields)


class _ScriptedAgent:
    """Fake BidiAgent emitting scripted user-transcript events through receive()."""

    def __init__(self, *, tools, system_prompt, scripted):
        self.tools = tools
        self.system_prompt = system_prompt
        self._scripted = scripted
        self.sent: list[Any] = []
        self.started = False

    async def start(self):
        self.started = True

    async def send(self, data):
        self.sent.append(data)

    async def receive(self):
        for ev in self._scripted:
            yield ev

    async def stop(self):
        self.started = False


def _user_turn(text: str):
    from strands.experimental.bidi import BidiTranscriptStreamEvent

    return BidiTranscriptStreamEvent(
        delta={"text": text}, text=text, role="user", is_final=True
    )


async def _drive(session: NovaSonicVoiceSession, agent_holder: dict):
    """Run the session to completion, collecting transcripts + actions."""
    transcripts: list[tuple[str, str]] = []
    actions: list[dict] = []

    async def on_audio(_b, _r):  # pragma: no cover - no audio in this script
        pass

    async def on_transcript(role, text):
        transcripts.append((role, text))

    async def on_action(action):
        actions.append(action)

    async def on_error(_m):  # pragma: no cover
        pass

    await session.run(
        on_audio=on_audio,
        on_transcript=on_transcript,
        on_action=on_action,
        on_error=on_error,
    )
    return transcripts, actions


def _make_session(monkeypatch, ids, rows, calls, scripted):
    ws_id = uuid.uuid4()

    async def _fake_execute(name, tool_input, *, ctx, session, workspace_id):
        calls.append({"name": name, "tool_input": tool_input})
        if name == "approve_and_schedule":
            return {"speak": "I've scheduled the reply to send later."}
        if name == "edit_reply":
            return {"speak": "I've updated the reply."}
        return {"speak": "Done."}

    monkeypatch.setattr(voice_gateway.voice_tools, "execute_voice_tool", _fake_execute)
    from app.services import approval_service

    monkeypatch.setattr(
        approval_service, "decode_reply_fields", lambda args: dict(args or {})
    )

    holder: dict = {}

    def factory(*, tools, system_prompt):
        agent = _ScriptedAgent(tools=tools, system_prompt=system_prompt, scripted=scripted)
        holder["agent"] = agent
        return agent

    @asynccontextmanager
    async def _sf():
        yield _EditSession(ws_id, ids, rows)

    session = NovaSonicVoiceSession(
        ctx=_ctx(ws_id),
        workspace_id=ws_id,
        session_factory=_sf,
        agent_factory=factory,
    )
    return session, holder, ws_id


@pytest.mark.asyncio
async def test_e2e_multiturn_body_edit_from_transcript(monkeypatch):
    """Replay: 'edit first reply' -> 'in the body of the first reply change hi'
    -> 'there to hello'. The body edit must actually execute (edit_reply)."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Hi there, team."}}
    calls: list[dict] = []
    scripted = [
        _user_turn("edit first reply"),
        _user_turn("in the body of the first reply change hi"),
        _user_turn("there to hello"),
    ]
    session, _holder, _ws = _make_session(monkeypatch, ids, rows, calls, scripted)
    await session.start()
    transcripts, actions = await _drive(session, _holder)

    edits = [c for c in calls if c["name"] == "edit_reply"]
    assert len(edits) == 1, calls
    assert edits[0]["tool_input"] == {"approval_id": id1, "body": "hello, team."}
    # A live editor update was emitted for the reviewer UI.
    assert any(a.get("type") == "edit_field" for a in actions)
    said = " ".join(t for (r, t) in transcripts if r == "assistant").lower()
    assert "sorry" not in said


@pytest.mark.asyncio
async def test_e2e_multiturn_schedule_from_transcript(monkeypatch):
    """Replay: 'approve and schedule' -> 'september fourteenth' -> 'five pm'.
    The date+time must be accumulated and approve_and_schedule must fire with a
    concrete when_iso, and open_schedule must carry the calendar value."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Body"}}
    calls: list[dict] = []
    scripted = [
        _user_turn("approve and schedule the first reply"),
        _user_turn("september fourteenth"),
        _user_turn("five pm"),
    ]
    session, _holder, _ws = _make_session(monkeypatch, ids, rows, calls, scripted)
    await session.start()
    transcripts, actions = await _drive(session, _holder)

    sched = [c for c in calls if c["name"] == "approve_and_schedule"]
    assert len(sched) == 1, calls
    ti = sched[0]["tool_input"]
    assert ti["approval_id"] == id1
    when = ti["when_iso"]
    assert when and "T17:00" in when and when[8:10] == "14", when
    # The calendar picker action carries the resolved instant.
    opens = [a for a in actions if a.get("type") == "open_schedule" and a.get("when_iso")]
    assert opens and opens[-1]["when_iso"] == when


@pytest.mark.asyncio
async def test_e2e_schedule_single_utterance(monkeypatch):
    """A one-shot 'approve and schedule the first for september 15th at 5pm'
    schedules immediately (no buffering needed)."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Body"}}
    calls: list[dict] = []
    scripted = [_user_turn("approve and schedule the first for september 15th at 5pm")]
    session, _holder, _ws = _make_session(monkeypatch, ids, rows, calls, scripted)
    await session.start()
    _t, actions = await _drive(session, _holder)

    sched = [c for c in calls if c["name"] == "approve_and_schedule"]
    assert len(sched) == 1
    assert "T17:00" in sched[0]["tool_input"]["when_iso"]


@pytest.mark.asyncio
async def test_e2e_audio_input_framing(monkeypatch):
    """Stream a synthetic 16kHz mono PCM16 buffer through send_audio and assert
    it is framed as a base64 pcm/16k/mono BidiAudioInputEvent — the same path a
    real microphone / pre-recorded WAV would take."""
    from strands.experimental.bidi import BidiAudioInputEvent

    id1 = str(uuid.uuid4())
    session, holder, _ws = _make_session(monkeypatch, [id1], {id1: {}}, [], [])
    await session.start()

    # Synthesize ~30ms of 16kHz mono PCM16 (480 samples) — a quiet sine.
    import math
    import struct

    frames = 480
    pcm = b"".join(
        struct.pack("<h", int(2000 * math.sin(2 * math.pi * 220 * n / 16000)))
        for n in range(frames)
    )
    await session.send_audio(pcm)

    sent = holder["agent"].sent
    assert len(sent) == 1
    ev = sent[0]
    assert isinstance(ev, BidiAudioInputEvent)
    assert ev.format == "pcm" and ev.sample_rate == 16000 and ev.channels == 1
    assert ev.audio == base64.b64encode(pcm).decode("ascii")


@pytest.mark.asyncio
async def test_e2e_reschedule_updates_time_from_transcript(monkeypatch):
    """After scheduling, 'change the time to seven am' reschedules (not a body
    find-replace and not reply-position-7), firing approve_and_schedule again."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Body"}}
    calls: list[dict] = []
    scripted = [
        _user_turn("approve and schedule the first for september 15th at 6am"),
        _user_turn("change the time to seven am"),
    ]
    session, _holder, _ws = _make_session(monkeypatch, ids, rows, calls, scripted)
    await session.start()
    _t, actions = await _drive(session, _holder)

    sched = [c for c in calls if c["name"] == "approve_and_schedule"]
    assert len(sched) == 2, calls  # initial + reschedule
    # No body edit fired (the time change must not be a find-replace).
    assert [c for c in calls if c["name"] == "edit_reply"] == []
    # The last schedule used 7am.
    assert "T07:00" in sched[-1]["tool_input"]["when_iso"]
    # No "couldn't find reply number 7" message.
    said = " ".join(t for (r, t) in _t if r == "assistant").lower()
    assert "number 7" not in said and "number 6" not in said


@pytest.mark.asyncio
async def test_e2e_grounding_note_sent_after_schedule(monkeypatch):
    """After the router schedules, a grounding SYSTEM note is fed to the model so
    it stops apologising / re-confirming (ERROR.md false-apology loop)."""
    id1 = str(uuid.uuid4())
    ids = [id1]
    rows = {id1: {"to": "b@x.com", "subject": "S", "body": "Body"}}
    calls: list[dict] = []
    scripted = [_user_turn("approve and schedule the first for september 15th at 5pm")]
    session, holder, _ws = _make_session(monkeypatch, ids, rows, calls, scripted)
    await session.start()
    await _drive(session, holder)

    # The fake agent recorded a text input carrying the grounding note.
    from strands.experimental.bidi import BidiTextInputEvent

    texts = [
        e.text
        for e in holder["agent"].sent
        if isinstance(e, BidiTextInputEvent)
    ]
    joined = " ".join(texts).lower()
    assert "already completed" in joined and "do not apologize" in joined
