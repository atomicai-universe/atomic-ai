"""Voice_Gateway — bridge ONE browser session to ONE Nova Sonic BidiAgent.

This is the server-side half of the accessibility voice feature. It wires the
thin voice capability surface built in Phase 1a (:mod:`app.services.voice_tools`)
into Amazon Nova Sonic through the Strands *bidirectional* agent
(:class:`strands.experimental.bidi.BidiAgent` +
:class:`strands.experimental.bidi.models.nova_sonic.BidiNovaSonicModel`). The
WebSocket router (:mod:`app.api.voice`) owns the socket; this module owns the
model conversation.

Why this shape:

- **One session : one agent.** A :class:`NovaSonicVoiceSession` holds a single
  ``BidiAgent`` for the lifetime of a browser voice session. Audio flows in via
  :meth:`send_audio`, model output (audio / transcript / interruption / errors)
  flows out via :meth:`run`, and high-impact *actions* (navigate / type / submit
  / approvals) flow out-of-band via the ``on_action`` callback the registered
  tools invoke.

- **Tools run through Strands.** Each voice tool is registered as a Strands
  ``@tool``-decorated callable (mirroring
  :func:`app.services.strands_engine._make_provider_tools`). Nova Sonic drives
  tool-use through Strands' own tool loop; the tool body calls
  :func:`app.services.voice_tools.execute_voice_tool` on a **fresh** DB session
  (opened per call via ``session_factory``) so nothing shares a connection or
  event loop unsafely. The tool's textual return is the ``speak`` string (so
  Nova Sonic voices it); if the result carries an ``action`` it is forwarded to
  the WS layer via ``on_action`` — the model never needs to know about it.

- **Tenancy is server-derived.** The tools bind the authenticated
  :class:`~app.core.tenancy.RequestContext` and a validated ``workspace_id``;
  the model never supplies a workspace. High-impact actions still route ONLY
  through :mod:`app.services.approval_service` (unchanged from Phase 1a).

- **Injectable everywhere.** ``agent_factory`` builds the ``BidiAgent`` (tests
  inject a fake that yields scripted output events and records inputs, so NO
  AWS / Nova Sonic call happens). ``session_factory`` opens DB sessions (tests
  inject a fake). This keeps the real integration wiring thin and the whole
  bridge unit-testable with no network.

Security: audio bytes and secrets are NEVER logged. Only coarse lifecycle and
event-type breadcrumbs are emitted at debug level.
"""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings
from app.core.tenancy import RequestContext
from app.services import voice_intent, voice_tools

logger = logging.getLogger(__name__)

# Nova Sonic consumes 16 kHz mono PCM16 input and emits 24 kHz mono PCM16 output.
# These constants name those rates so the audio-input event and the output
# routing read clearly and stay in one place.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT = "pcm"

# Reconnect policy for transient Bedrock/Nova Sonic stream failures. The stream
# can drop mid-session (a service ``ModelStreamErrorException`` or an awscrt
# HTTP/2 cleanup race); rather than end the whole voice session, run() rebuilds
# the model connection on the same WebSocket up to this many times, with a
# short escalating backoff between attempts.
_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_BACKOFF_S = 0.5

# The accessibility system prompt. Deliberately concise: Nova Sonic responses
# are spoken, so brevity and confirmation-before-impact matter more than prose.
VOICE_SYSTEM_PROMPT = (
    "You are Atomic AI's voice assistant for a blind user. You control the app "
    "by CALLING TOOLS — you must actually invoke the provided tools, not just "
    "describe what you would do. "
    "NAVIGATION: the application handles ALL page navigation AUTOMATICALLY the "
    "instant the user asks (open / go to / show / bring up / click a "
    "page/menu/tab/section). You have NO navigation tool and you do NOT need "
    "one — the app already opened the page. Do NOT attempt to navigate, do NOT "
    "describe navigating, and NEVER say navigation failed or that there is an "
    "issue navigating — it succeeded. At most, give a brief natural "
    "acknowledgement; otherwise stay quiet and wait for the next request. The "
    "five destinations (integrations, approvals, rules, team, admin) are opened "
    "for you by the app. "
    "READING: when the user asks to hear their pending approvals or replies "
    "awaiting approval, the app reads them aloud FOR you — do NOT try to list or "
    "read approvals yourself and NEVER say you cannot list them. For unread "
    "inbox email you may use your email tools. "
    "GUIDANCE: if the user asks what they can do, or says 'help' or 'options', "
    "tell them, in plain spoken language, that they can ask you to go to any of "
    "the five pages, have their unread emails or pending approvals read aloud, "
    "and approve and save to draft, approve and send, approve and schedule, "
    "regenerate, or edit a reply. "
    "SAFETY: always confirm out loud before high-impact actions such as sending "
    "or scheduling an email, and only proceed when the user says yes. "
    "EXECUTION: the application itself executes navigation and approval or reply "
    "actions deterministically the moment the user asks — you do NOT have to "
    "make them happen. When the user asks to read, regenerate, edit, approve, "
    "send, schedule, or save a reply by position (for example 'the first reply'), "
    "the app carries it out for you. This INCLUDES editing a reply's recipient, "
    "subject, or body BY POSITION and editing the wording in place — for example "
    "'edit the body of the first email', 'change \"Hi there\" to \"Hello "
    "PayRogen\"', or 'in the first reply set the subject to Support Request'. The "
    "app finds the reply by its number (the first, the second, number three, and "
    "so on) and applies the change itself. You NEVER need an id or reference "
    "number, and you must NEVER ask the user for an 'approval id' or any id. "
    "This ALSO covers every action button on a reply — approve and send, approve "
    "and save to draft, approve and schedule (with a spoken date and time), and "
    "reject — all BY POSITION and all carried out by the app. "
    "The user may also change wording ACROSS TWO turns — first naming the text "
    "('change the text hi there') and then, in a later turn, the replacement "
    "('to hi payroll'). The app remembers the first part and applies the change "
    "when the replacement arrives. Do NOT ask the user to repeat 'the exact "
    "text', and do NOT say the edit failed — just give a brief confirmation. "
    "SCHEDULING: the app collects the date and time ACROSS TURNS too — the user "
    "may say 'approve and schedule', then a date like 'September fifteenth', then "
    "a time like 'seven pm', and the app fills the calendar and schedules it "
    "itself. The user may also change an already-set time ('change the time to "
    "six pm', 'reschedule to seven pm') and the app updates it. When the user "
    "gives a date or a time, the app is handling it — do NOT say scheduling "
    "failed, do NOT re-ask for a date/time you already heard, and do NOT ask the "
    "user to confirm a schedule that is already done. A one-word 'correct' or "
    "'yes' after a completed schedule needs only a brief acknowledgement. "
    "Simply give a brief, natural spoken confirmation. NEVER say 'I'm sorry', "
    "'there seems to be an issue', 'due to a technical issue', or that you cannot "
    "navigate or act — those actions are handled by the app. "
    "STYLE: keep spoken responses short and natural; never read out raw ids, "
    "tokens, tool names, or long technical strings."
)

# The set of position-aware IntentResult kinds the gateway resolves server-side
# (POSITION -> approval_id) and routes through the existing approval tools.
_POSITION_INTENT_KINDS: frozenset[str] = frozenset(
    {
        "read_position",
        "regenerate_position",
        "edit_position",
        "focus_field_position",
        "regenerate_body_position",
        "replace_in_field_position",
        "set_field_position",
        "approve_draft_position",
        "approve_send_position",
        "approve_schedule_position",
        "reschedule_position",
        "reject_position",
    }
)

# Tool names the DETERMINISTIC Intent_Router fully owns, so they are NOT exposed
# to the Nova Sonic model. Rationale (ERROR.md — "Opening Integrations" then
# "persistent issue navigating"): the model hears the user's audio and, if it
# also has a `navigate` tool, tries to navigate ITSELF in parallel with the
# router. Its own attempt has no reliable success signal in the bidi loop, so it
# narrates a FALSE failure even though the router already navigated correctly.
# Navigation is 100% deterministic in _handle_user_transcript (it builds and
# dispatches the navigate action directly from voice_intent — it never calls
# this tool), so withholding `navigate` from the model removes the double-talk
# at the source WITHOUT the fragile audio-suppression that caused the earlier
# crack/twitch regression. The `_navigate` handler stays in voice_tools for any
# non-model caller; we only omit it from the MODEL's toolset here.
#
# ERROR.md (round 2) extends this to the APPROVAL tools. The transcript showed
# Nova Sonic running its OWN list->read->act tool chain in parallel with the
# deterministic router: it called `list_pending_approvals` / `read_approval` to
# discover an id it never had, those multi-step calls failed or looped over the
# bidi stream, and the model narrated a FALSE "I'm sorry, there's an issue with
# the tool" even though the router had already carried out the user's request.
# AWS's own guidance is that pushing multi-step/chained tool logic onto the
# speech-to-speech model "gets brittle" — so we withhold every tool the router
# fully owns (listing, reading a reply, and all high-impact approval actions).
# The router does all of these deterministically on the reliable main-loop DB
# session (see `_handle_user_transcript` / `_handle_position_intent`). The model
# KEEPS only the inbox read tools it may still need conversationally
# (`list_unread_emails`, `read_email`) plus the form helpers
# (`type_text`, `submit_form`); every handler remains in voice_tools for the
# router/tests — we only omit these from the MODEL's toolset here.
_MODEL_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        "navigate",
        "list_pending_approvals",
        "read_approval",
        "edit_reply",
        "regenerate_reply",
        "approve_and_save_draft",
        "approve_and_send",
        "approve_and_schedule",
        "reject_reply",
        "clear_pending",
    }
)

# Type aliases for the run() callbacks (all awaitable).
OnAudio = Callable[[bytes, int], Awaitable[None]]
OnTranscript = Callable[[str, str], Awaitable[None]]
OnAction = Callable[[dict[str, Any]], Awaitable[None]]
OnError = Callable[[str], Awaitable[None]]


def _replace_ci(text: str, search: str, replacement: str) -> tuple[str, bool]:
    """Case-insensitive replace of ``search`` with ``replacement`` in ``text``.

    Pure. Replaces EVERY occurrence, matching case-insensitively (spoken
    transcripts are lowercased, but the stored draft is mixed-case), and returns
    ``(new_text, replaced)`` where ``replaced`` is True when at least one match
    was found. Whitespace within ``search`` is matched flexibly (one spoken
    space can map to any run of whitespace in the draft) so "hi there" matches
    "Hi  there" or "Hi\nthere". When nothing matches, returns the original text
    unchanged with ``replaced=False`` so the caller can tell the user.
    """
    import re as _re

    needle = (search or "").strip()
    if not needle:
        return text, False
    # Build a flexible whitespace-tolerant, case-insensitive pattern.
    pattern = _re.compile(
        r"\s+".join(_re.escape(tok) for tok in needle.split()),
        _re.IGNORECASE,
    )
    new_text, count = pattern.subn(lambda _m: replacement, text)
    return (new_text, count > 0)


def _extract_schedule_parts(text: str, *, now):  # noqa: ANN001, ANN201
    """Extract a (date, time) pair from a spoken schedule fragment.

    Pure-ish (delegates to the pure :mod:`app.services.voice_datetime`). Returns
    ``(date | None, (hour, minute) | None)``:

    - ``date`` is a :class:`datetime.date` when the text names a day (a month +
      day like "september fourteenth", or "tomorrow"/"today"/a weekday); else
      ``None``.
    - ``time`` is ``(hour, minute)`` when the text names a clock time (digits
      like "5pm"/"17:00" or a spoken hour like "five pm"); else ``None``.

    This lets the gateway accumulate a date-only turn and a time-only turn into
    one scheduled instant. Detection uses the same regexes/tables as the parser
    so it stays in agreement with :func:`voice_datetime.parse_spoken_datetime`.
    """
    import re as _re

    from app.services import voice_datetime as _vd

    norm = _vd._normalize(text)
    if not norm:
        return None, None

    # ── DATE ────────────────────────────────────────────────────────────────
    date_part = None
    md = _vd._parse_month_day(norm, now)
    if md is not None:
        date_part = md.date()
    elif "tomorrow" in norm:
        from datetime import timedelta as _td

        date_part = (now + _td(days=1)).date()
    elif "today" in norm or "tonight" in norm:
        date_part = now.date()
    else:
        for name, wd in _vd._WEEKDAYS.items():
            if _re.search(rf"\b{name}\b", norm):
                force = "next" in norm
                date_part = _vd._next_weekday(now, wd, force_next_week=force).date()
                break

    # ── TIME ──────────────────────────────────────────────────────────────── 
    # Strip an absolute month/day so its digits aren't misread as a clock hour.
    time_norm = norm
    if md is not None:
        for name in _vd._MONTHS:
            time_norm = _re.sub(rf"\b{name}\b", " ", time_norm)
        time_norm = _re.sub(r"\b\d{1,2}(?:st|nd|rd|th)?\b", " ", time_norm)
        for word in _vd._DAY_ORDINALS:
            time_norm = _re.sub(rf"\b{word}\b", " ", time_norm)
        time_norm = " ".join(time_norm.split())

    time_part = None
    cm = _vd._CLOCK_RE.search(time_norm)
    resolved = _vd._resolve_clock(cm) if cm is not None else None
    # A lone bare digit with no am/pm and no ":" is too ambiguous to be a time
    # here (e.g. leftover from a date) — require am/pm, a colon, or 24h >12.
    if resolved is not None and cm is not None:
        has_ampm = cm.group("ampm") is not None
        has_colon = cm.group("minute") is not None
        hour24 = resolved[0]
        if has_ampm or has_colon or hour24 == 0 or hour24 > 12:
            time_part = resolved
    if time_part is None:
        wm = _vd._WORD_CLOCK_RE.search(time_norm)
        if wm is not None:
            hour = _vd._HOUR_WORDS.get(wm.group("word"))
            if hour is not None:
                ampm = wm.group("ampm")
                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                time_part = (hour, 0)

    return date_part, time_part


#: Leading filler words dropped from a bare spoken replacement continuation.
_REPLACEMENT_FILLER_PREFIXES: tuple[str, ...] = (
    "um ", "uh ", "er ", "well ", "okay ", "ok ", "so ",
    "make it ", "set it to ", "change it to ", "to ", "it to ", "with ",
    "it should be ", "should be ", "the replacement is ", "use ",
)


def _split_edit_continuation(text: str) -> tuple[str, str]:
    """Split a spoken edit continuation into (search_suffix, replacement).

    Handles the mid-phrase split where the user finished naming the search AND
    gave the replacement in one follow-up turn: "there to hello" ->
    ("there", "hello"). When there is no connector, the whole (filler-stripped)
    utterance is the replacement and the suffix is empty: "hello" -> ("", "hello").
    Pure. Uses the LAST " to " / " with " / " into " as the split so a
    multi-word suffix stays with the search.
    """
    t = (text or "").strip()
    if not t:
        return "", ""
    lowered = t.lower()
    # Find the last connector token with surrounding spaces.
    best_idx = -1
    best_len = 0
    for conn in (" to ", " with ", " into "):
        idx = lowered.rfind(conn)
        if idx > best_idx:
            best_idx = idx
            best_len = len(conn)
    if best_idx >= 0:
        suffix = t[:best_idx].strip()
        replacement = t[best_idx + best_len:].strip()
        # Guard: if the "replacement" is empty (trailing "to"), treat as no split.
        if replacement:
            return suffix, replacement
    # No usable connector: the whole thing (filler-stripped) is the replacement.
    return "", _clean_spoken_replacement(t)


def _clean_spoken_replacement(text: str) -> str:
    """Extract a plausible replacement phrase from a bare spoken continuation.

    Pure. Trims surrounding whitespace and strips a leading filler / connector
    prefix ("um", "make it", "to", "it should be", …) so a user finishing a
    fragmented edit with "hello" or "make it hello" yields "hello". Returns "" for
    empty input. Only used when a pending edit is already buffered, so it never
    fires on ordinary conversation.
    """
    t = (text or "").strip()
    if not t:
        return ""
    lowered = t.lower()
    for prefix in _REPLACEMENT_FILLER_PREFIXES:
        if lowered.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    return t


class NovaSonicVoiceSession:
    """Bridge a single browser voice session to a single Nova Sonic BidiAgent.

    The session is created per WebSocket connection with the authenticated
    caller's :class:`RequestContext`, the validated ``workspace_id``, and a
    ``session_factory`` (default :func:`app.db.session.session_scope`) used to
    open a *fresh* DB session for each tool call. ``agent_factory`` builds the
    underlying ``BidiAgent``; the default builds a real one against Nova Sonic,
    while tests inject a fake to avoid any AWS call.
    """

    def __init__(
        self,
        *,
        ctx: RequestContext,
        workspace_id: uuid.UUID,
        session_factory: Callable[[], Any],
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Initialize the bridge (does not start the model connection).

        Args:
            ctx: Authenticated request context (identity + roles). Tools derive
                tenancy from this; the model never supplies a workspace.
            workspace_id: The workspace this voice session is scoped to. The
                caller (router) has already verified membership.
            session_factory: Zero-arg callable returning an async context
                manager that yields a DB session (e.g.
                :func:`app.db.session.session_scope`). One fresh session per
                tool call keeps DB usage loop-safe.
            agent_factory: Optional callable that builds the ``BidiAgent`` given
                keyword ``tools`` and ``system_prompt``. Defaults to
                :meth:`_default_agent_factory` which builds a real Nova Sonic
                agent. Tests inject a fake here.
        """
        self._ctx = ctx
        self._workspace_id = workspace_id
        self._session_factory = session_factory
        self._agent_factory = agent_factory or self._default_agent_factory
        # Per-connection id grouping every turn of this voice conversation in
        # the ``voice_transcripts`` debug table. Minted once at construction so
        # the welcome, every user/assistant transcript, and every deterministic
        # confirmation of THIS session share one id (task A / ERROR.md Task 1).
        self._session_id: uuid.UUID = uuid.uuid4()
        # The on_action sink the registered tools push structured actions into.
        # Set for the lifetime of run(); before that, actions are dropped (the
        # model cannot call tools before the conversation starts anyway).
        self._on_action: OnAction | None = None
        self._agent: Any = None
        self._started = False
        # ------------------------------------------------------------------
        # Per-session transient Intent_Router state (never persisted).
        # ------------------------------------------------------------------
        # Guards the once-per-session Welcome_Message injection (task 2.2 sets
        # this; kept here so the whole session-transient surface lives together).
        self._welcome_sent: bool = False
        # (turn_index, path) of the most recently deterministically dispatched
        # navigate action, used to suppress a model-emitted `navigate` tool call
        # for the same turn/destination so the UI does not navigate twice.
        self._last_nav_dispatch: tuple[int, str] | None = None
        # Monotonic counter incremented on each user transcript turn; scopes the
        # de-duplication above to a single turn.
        self._turn_index: int = 0
        # Approval ids for the current workspace in created_at ASC order — the
        # SAME order _list_pending_approvals returns and the frontend renders,
        # so it is the single source of truth for resolving a spoken POSITION
        # ("the first reply") to a concrete approval_id SERVER-SIDE. Refreshed
        # from a fresh tenant-scoped query on each position-aware turn; the model
        # never supplies a workspace or id.
        self._pending_cache: list[str] = []
        # The (position, field) of the reply/field the user most recently
        # targeted for editing ("edit the body of the first email"). It lets a
        # follow-up in-place edit that omits the position/field ("change 'Hi
        # there' to 'Hello PayRogen'") resolve to the SAME reply + field the
        # user just opened, so editing feels conversational. 1-based position;
        # field is "to" | "subject" | "body". None until the user targets one.
        self._last_edit_target: tuple[int, str] | None = None
        # A fragmented in-place edit buffered across turns (ERROR.md): when the
        # user says "change the text hi there" WITHOUT a replacement yet, we
        # stash {"position", "field", "search"} here and apply it when the next
        # turn supplies the replacement ("to hi payroll"). Cleared once applied
        # or when another intent supersedes it.
        self._pending_replace: dict[str, Any] | None = None
        # A scheduling flow buffered across turns (ERROR.md): after "approve and
        # schedule" opens the picker WITHOUT a time, we stash {"position", "date"
        # (date|None), "time" (tuple[h,m]|None)} here and accumulate spoken date
        # and time fragments ("september fourteenth" then "five pm") until BOTH
        # are known, then approve+schedule. Cleared on completion or cancel.
        self._pending_schedule: dict[str, Any] | None = None
        # ------------------------------------------------------------------
        # Model-response suppression — RETIRED (kept as inert flags).
        # ------------------------------------------------------------------
        # These once dropped the model's parallel audio/transcript for a turn the
        # deterministic router handled (to avoid the "I'm sorry" double-talk).
        # That per-chunk suppression made the voice CRACK and TWITCH between
        # listening and speaking, because Nova Sonic streams audio in many small
        # chunks whose bidi_response_start/complete boundaries do not reliably
        # bracket a spoken response — so it dropped audio mid-phrase. The
        # suppression is now REMOVED from _consume_events; the anti-apology
        # behavior is handled by VOICE_SYSTEM_PROMPT (the known-good approach).
        # The flags are retained as inert (always False) to avoid touching any
        # other reference and to make the revert obvious in diffs. Do not re-arm
        # them without a buffer-whole-response design.
        self._suppress_next_response: bool = False
        self._suppressing_response: bool = False

    # ------------------------------------------------------------------
    # Tool construction (Strands @tool callables wrapping execute_voice_tool)
    # ------------------------------------------------------------------

    def _build_tools(self) -> list:
        """Build one Strands ``@tool`` callable per voice tool name.

        Each tool opens a FRESH DB session via ``session_factory`` and calls
        :func:`voice_tools.execute_voice_tool` with the bound ``ctx`` /
        ``workspace_id``. The tool's textual return is the ``speak`` string (so
        Nova Sonic voices it); when the result carries an ``action``, the tool
        forwards it via :meth:`_dispatch_action` so the WS layer can relay it to
        the browser. The dict is also returned in full so Strands records a
        structured tool result.
        """
        from strands import tool  # type: ignore[import-not-found]

        specs_by_name = {spec["name"]: spec for spec in voice_tools.VOICE_TOOL_SPECS}
        tools: list = []

        # Exclude deterministic-only tools (e.g. `navigate`) from the MODEL's
        # toolset — the router handles them directly, and letting the model also
        # attempt them produces false-failure narration (see
        # _MODEL_EXCLUDED_TOOLS above).
        for name in sorted(voice_tools.VOICE_TOOL_NAMES):
            if name in _MODEL_EXCLUDED_TOOLS:
                continue
            spec = specs_by_name[name]

            def _make(tool_name: str = name, tool_spec: dict = spec):
                @tool(
                    name=tool_name,
                    description=tool_spec["description"],
                    inputSchema=tool_spec.get("inputSchema", {"type": "object", "properties": {}}),
                )
                async def _voice_tool(**tool_input: Any) -> str:
                    logger.info("voice tool invoked | name=%s", tool_name)
                    result = await self._call_tool(tool_name, tool_input)
                    action = result.get("action") if isinstance(result, dict) else None
                    if action is not None:
                        await self._dispatch_action(action)
                    # The spoken text is what Nova Sonic should voice. Fall back
                    # to a neutral acknowledgement so the model always has words.
                    if isinstance(result, dict):
                        return str(result.get("speak") or "Done.")
                    return "Done."

                return _voice_tool

            tools.append(_make())

        return tools

    async def _call_tool(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Open a fresh DB session and dispatch one voice tool call.

        Isolated as a method so tests can drive a tool wrapper and assert it
        calls :func:`voice_tools.execute_voice_tool` with the bound
        ``ctx`` / ``workspace_id`` on a per-call session.
        """
        async with self._session_factory() as session:
            return await voice_tools.execute_voice_tool(
                name,
                tool_input if isinstance(tool_input, dict) else {},
                ctx=self._ctx,
                session=session,
                workspace_id=self._workspace_id,
            )

    async def _record_turn(self, role: str, text: str) -> None:
        """Persist ONE voice turn to the ``voice_transcripts`` debug table.

        Writes a single row (role / text / this session's per-connection
        ``session_id`` / server-derived workspace + user / tz-aware timestamp)
        on a FRESH DB session opened via ``session_factory`` (mirroring
        :meth:`_call_tool`). Best-effort by design: any failure — a DB error, a
        closed loop, a missing table — is swallowed so recording NEVER crashes
        the voice session. The transcript text is written ONLY to the DB, never
        to the application logger. Tenancy is server-derived: workspace and user
        come from ``ctx`` / ``workspace_id``, never from the model.

        Empty text is skipped. Audio bytes are never passed here.
        """
        if not text:
            return
        try:
            from app.db.models import VoiceTranscript

            async with self._session_factory() as session:
                session.add(
                    VoiceTranscript(
                        workspace_id=self._workspace_id,
                        user_id=self._ctx.user_id,
                        session_id=self._session_id,
                        role=str(role),
                        text=str(text),
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - transcript recording must never crash
            logger.debug("voice transcript recording failed; continuing session")

    async def _dispatch_action(self, action: dict[str, Any]) -> None:
        """Forward a structured frontend action to the current ``on_action`` sink.

        De-duplicates a model-emitted ``navigate`` tool call: if the deterministic
        Intent_Router already dispatched a navigate to the same path within the
        current user turn (tracked in ``_last_nav_dispatch``), the model's own
        ``navigate`` action for that turn/path is suppressed so the client does
        not navigate twice. Navigation via ``router.push`` to the same path is
        already idempotent, so this is belt-and-suspenders. Non-navigate actions
        are always forwarded.
        """
        if (
            isinstance(action, dict)
            and action.get("type") == "navigate"
            and self._last_nav_dispatch is not None
            and self._last_nav_dispatch == (self._turn_index, action.get("path"))
        ):
            logger.debug(
                "voice navigate action de-duplicated | turn=%s", self._turn_index
            )
            return

        sink = self._on_action
        if sink is not None:
            await sink(action)
        else:  # pragma: no cover - actions before run() have no sink
            logger.debug("voice action dropped (no active sink) | type=%s", action.get("type"))

    # ------------------------------------------------------------------
    # Deterministic Intent_Router (consumes the user transcript)
    # ------------------------------------------------------------------

    async def _handle_user_transcript(
        self, text: str, *, on_transcript: OnTranscript
    ) -> None:
        """Classify a user transcript and act deterministically on the intent.

        Increments the per-session turn counter, then classifies the utterance
        via :func:`voice_intent.classify`. Classification is best-effort and
        side-effect-free on failure: any exception is caught and treated as "no
        intent" so navigation can never crash the session (the model path still
        applies). On:

        - ``navigate``: dispatch exactly one navigate action for the resolved
          path and speak a short confirmation, recording ``_last_nav_dispatch``
          so a model-emitted ``navigate`` tool call for the same turn/path is
          de-duplicated in :meth:`_dispatch_action`.
        - ``unknown_destination``: speak the available-destinations sentence and
          dispatch NO action (Req 1.7).
        - ``help``: inject the spoken guidance script (task 2.2 owns the
          injection helper; see :meth:`_inject_guidance`).

        NEVER logs the transcript text.
        """
        # Every user turn advances the turn counter, which scopes navigate
        # de-duplication to a single turn.
        self._turn_index += 1

        try:
            intent = voice_intent.classify(text)
        except Exception:  # noqa: BLE001 - classification must never crash the session
            logger.debug("voice intent classification failed; treating as no intent")
            intent = None

        if intent is None:
            # If a scheduling flow is mid-flight (the user already said "approve
            # and schedule"), treat this next utterance as a spoken date/time
            # fragment and accumulate it ("september fourteenth" then "five pm").
            # Once both a date and a time are known, approve+schedule.
            if self._pending_schedule is not None:
                handled = await self._accumulate_schedule(
                    text, on_transcript=on_transcript
                )
                if handled:
                    return
            # If a fragmented edit is mid-flight (the user already said "change
            # the text hi there"), treat this next utterance as the REPLACEMENT
            # even when it is a bare word/phrase like "hello" that the pure
            # classifier can't recognize on its own (ERROR.md: "change high there
            # to" ... "hello"). This is the natural way users finish the edit.
            if self._pending_replace is not None:
                search_suffix, replacement = _split_edit_continuation(text)
                if replacement:
                    # Extend the buffered search with any spoken suffix that came
                    # before the connector (mid-phrase split: buffered "hi" +
                    # "there to hello" -> search "hi there", replacement "hello").
                    if search_suffix:
                        base = str(self._pending_replace.get("search") or "").strip()
                        self._pending_replace["search"] = (
                            f"{base} {search_suffix}".strip() if base else search_suffix
                        )
                    cont = voice_intent.IntentResult(
                        kind="edit_replacement",
                        path=None,
                        speak="",
                        replacement=replacement,
                    )
                    try:
                        await self._apply_pending_replace(
                            cont, on_transcript=on_transcript
                        )
                    except Exception:  # noqa: BLE001 - must never crash the session
                        logger.debug("voice bare-replacement apply failed; continuing")
                    return
            # Not a command we handle deterministically — let the model respond
            # normally (ordinary conversation like "how are you").
            return

        # NOTE (regression fix): we no longer arm model-response suppression
        # here. Per-chunk suppression in _consume_events caused the voice to
        # crack/twitch between listening and speaking. The anti-apology behavior
        # is handled by VOICE_SYSTEM_PROMPT instead (the known-good approach when
        # voice was working smoothly). Intentionally NOT setting
        # self._suppress_next_response.

        if intent.kind == "navigate" and intent.path is not None:
            # Dispatch the router's OWN navigate FIRST, then confirm out loud.
            # The dedup guard in `_dispatch_action` matches on `_last_nav_dispatch`;
            # recording it BEFORE this dispatch would make the router's own
            # navigate match the guard and be dropped, so nothing reaches the
            # client. Dispatch first (while `_last_nav_dispatch` still holds the
            # previous turn's value — or None — so this navigate always forwards),
            # then record (turn, path) so only a subsequent MODEL-emitted navigate
            # tool call for this same turn/path is suppressed as a duplicate.
            await self._dispatch_action({"type": "navigate", "path": intent.path})
            self._last_nav_dispatch = (self._turn_index, intent.path)
            if intent.speak:
                await on_transcript("assistant", intent.speak)
        elif intent.kind == "unknown_destination":
            # A navigation verb targeted something outside the five destinations:
            # tell the user what is available and dispatch NO navigate action.
            if intent.speak:
                await on_transcript("assistant", intent.speak)
        elif intent.kind == "help":
            # Speak the guidance. The actual guidance-script injection is task
            # 2.2's scope; this is the clean seam it hooks into.
            await self._inject_guidance()
        elif intent.kind == "edit_search_pending":
            # Fragmented edit, part 1: the user named WHAT to change but not yet
            # the replacement ("change the text hi there"). Buffer the search
            # (scoped to the field/position the user last opened) and ask for the
            # replacement. Applied on the next turn (edit_replacement).
            try:
                await self._buffer_pending_replace(
                    intent, on_transcript=on_transcript
                )
            except Exception:  # noqa: BLE001 - must never crash the session
                logger.debug("voice edit-search buffering failed; continuing")
        elif intent.kind == "edit_replacement":
            # Fragmented edit, part 2: the replacement arrived ("to hi payroll").
            # Apply the buffered search->replacement if one is pending; otherwise
            # this is a harmless stray continuation.
            try:
                await self._apply_pending_replace(
                    intent, on_transcript=on_transcript
                )
            except Exception:  # noqa: BLE001 - must never crash the session
                logger.debug("voice edit-replacement apply failed; continuing")
        elif intent.kind in _POSITION_INTENT_KINDS:
            # Position-aware approval/reply action: resolve the spoken POSITION
            # to a concrete approval_id SERVER-SIDE, then act via the existing
            # approval tools. Wrapped so a failure never crashes the session.
            try:
                await self._handle_position_intent(
                    intent, on_transcript=on_transcript
                )
            except Exception:  # noqa: BLE001 - position handling must never crash
                logger.debug(
                    "voice position-intent handling failed; continuing session"
                )
        elif intent.kind in ("list_pending", "list_unread"):
            # List a collection aloud on the reliable main-loop DB path. The
            # router owns listing so the model never needs the brittle
            # list->read->act tool chain (ERROR.md). Speaks the tool's summary.
            try:
                tool_name = (
                    "list_pending_approvals"
                    if intent.kind == "list_pending"
                    else "list_unread_emails"
                )
                result = await self._call_tool(tool_name, {})
                spoken = (
                    result.get("speak") if isinstance(result, dict) else None
                ) or "I couldn't read that right now."
                await on_transcript("assistant", spoken)
            except Exception:  # noqa: BLE001 - listing must never crash the session
                logger.debug("voice list handling failed; continuing session")
        elif intent.kind == "clear_all_pending":
            # Bulk-clear ALL pending replies (REJECT, keeps audit trail). Routes
            # through the tenant-scoped clear_pending tool; the tool enforces the
            # resolver (Owner/Admin) requirement and speaks a permission message
            # otherwise. Refresh the position cache afterwards since it changed.
            try:
                result = await self._call_tool("clear_pending", {})
                spoken = (
                    result.get("speak") if isinstance(result, dict) else None
                ) or intent.speak
                if spoken:
                    await on_transcript("assistant", spoken)
                action = result.get("action") if isinstance(result, dict) else None
                if action is not None:
                    await self._dispatch_action(action)
                self._pending_cache = []
            except Exception:  # noqa: BLE001 - clear must never crash the session
                logger.debug("voice clear-all handling failed; continuing session")
        elif intent.kind == "cancel":
            # Cancel / never-mind: answered deterministically so the model does
            # not apologise. Clear any buffered edit/schedule and tell the UI to
            # close any open editor, then confirm.
            self._pending_replace = None
            self._pending_schedule = None
            try:
                await self._dispatch_action({"type": "cancel_edit"})
                if intent.speak:
                    await on_transcript("assistant", intent.speak)
            except Exception:  # noqa: BLE001 - cancel must never crash the session
                logger.debug("voice cancel handling failed; continuing session")

    async def _note_action_done(self, action: str) -> None:
        """Ground the model after the router completed a high-impact action.

        The deterministic router performs schedule / edit / approve / send /
        reject itself and the model has NO tool for these — yet Nova Sonic,
        hearing only the user's audio, sometimes tries to "help" and then
        narrates a FALSE failure ("I'm sorry, there was an issue scheduling")
        even though the app already succeeded (ERROR.md). Injecting a short
        first-person-neutral SYSTEM note via ``send_text`` grounds the model in
        the truth for its next turn, so it acknowledges briefly instead of
        apologising or re-confirming. Best-effort: any failure is swallowed and
        never tears down the session. The note is intentionally terse to avoid
        adding spoken chatter. NEVER logs transcript text.
        """
        note = (
            f"(System note: the app has ALREADY completed the {action} "
            "successfully. Do not attempt it, do not apologize, and do not ask "
            "the user to confirm or repeat it. If you say anything, give a brief "
            "friendly acknowledgement only.)"
        )
        try:
            await self.send_text(note)
        except Exception:  # noqa: BLE001 - grounding note must never crash session
            logger.debug("voice action grounding note failed; continuing")

    async def _inject_guidance(self) -> None:
        """Inject the spoken guidance script as a model text turn on a help intent.

        Sends :func:`voice_intent.build_guidance_script` via :meth:`send_text`
        so Nova Sonic voices the guidance in its own voice (mirroring the welcome
        path). Injection is best-effort: any failure — including the session not
        being started — is caught, logged at debug, and does NOT tear down the
        session (a failed guidance turn must not end the conversation). The
        transcript text is never logged.
        """
        try:
            await self.send_text(voice_intent.build_guidance_script())
        except Exception:  # noqa: BLE001 - guidance injection must never tear down the session
            logger.debug("voice guidance injection failed; continuing session")

    # ------------------------------------------------------------------
    # Position -> approval_id resolution and position-aware actions
    # ------------------------------------------------------------------

    async def _refresh_pending_cache(self) -> None:
        """Refresh ``_pending_cache`` with this workspace's PENDING approval ids.

        Runs the SAME tenant-scoped query as
        :func:`voice_tools._list_pending_approvals` (``status == PENDING``,
        ``ORDER BY created_at ASC``) on a FRESH DB session opened via
        ``session_factory`` (mirroring :meth:`_call_tool`), and stores the ids as
        strings in that order. That order is the single source of truth a spoken
        POSITION resolves against, so a position always maps to the SAME reply
        the user hears listed and the frontend renders. The model never supplies
        a workspace or id — tenancy is derived from ``workspace_id`` here.
        """
        from sqlalchemy import select

        from app.db.models import ApprovalRequest, ApprovalStatus

        async with self._session_factory() as session:
            stmt = (
                select(ApprovalRequest.id)
                .where(ApprovalRequest.workspace_id == self._workspace_id)
                .where(ApprovalRequest.status == ApprovalStatus.PENDING)
                .order_by(ApprovalRequest.created_at.desc())
            )
            result = await session.execute(stmt)
            self._pending_cache = [str(row) for row in result.scalars().all()]

    async def _handle_position_intent(
        self, intent: voice_intent.IntentResult, *, on_transcript: OnTranscript
    ) -> None:
        """Resolve a spoken POSITION to an approval_id and act via the tools.

        Refreshes the pending cache, maps ``intent.position`` (1-based) to a
        concrete ``approval_id`` from the current workspace. When the position is
        out of range, speaks a short count message and dispatches NO action.
        Otherwise routes to the right existing approval tool / structured action:

        - ``read_position`` -> read the ORIGINAL email (if resolvable) then the
          PROPOSED reply aloud, and dispatch ``focus_reply``.
        - ``regenerate_position`` -> ``regenerate_reply`` tool + ``focus_reply``.
        - ``edit_position`` -> ``open_edit`` action (the edit text still flows
          through the existing edit UI).
        - ``approve_draft_position`` -> ``approve_and_save_draft`` tool.
        - ``approve_send_position`` -> ``approve_and_send`` tool.
        - ``approve_schedule_position`` -> ``approve_and_schedule`` tool when a
          time was spoken (plus ``open_schedule`` with the time); otherwise an
          ``open_schedule`` action with no time and a prompt for date/time.

        High-impact actions still route ONLY through the approval tools; no id or
        workspace is ever taken from the model. NEVER logs transcript text.
        """
        # In-place edits ("change X to Y") often omit the position/field because
        # the user just said "edit the body of the first email". Fall back to the
        # reply/field they most recently targeted so the follow-up lands there.
        if (
            intent.kind in ("replace_in_field_position", "set_field_position")
            and intent.position is None
            and self._last_edit_target is not None
        ):
            position = self._last_edit_target[0]
        elif intent.kind == "reschedule_position" and intent.position is None:
            # Reschedule targets the reply currently being scheduled (the one the
            # picker is open on), else the last edited reply, else the first.
            if self._pending_schedule is not None:
                position = int(self._pending_schedule.get("position") or 1)
            elif self._last_edit_target is not None:
                position = self._last_edit_target[0]
            else:
                position = 1
        else:
            position = intent.position or 1

        await self._refresh_pending_cache()
        total = len(self._pending_cache)
        if position < 1 or position > total:
            plural = "reply" if total == 1 else "replies"
            await on_transcript(
                "assistant",
                f"I couldn't find reply number {position}; you have "
                f"{total} pending {plural}.",
            )
            return

        approval_id = self._pending_cache[position - 1]
        kind = intent.kind

        if kind == "read_position":
            await self._read_position(approval_id, position, on_transcript=on_transcript)
            await self._dispatch_action(
                {"type": "focus_reply", "position": position}
            )
        elif kind == "regenerate_position":
            await self._call_tool("regenerate_reply", {"approval_id": approval_id})
            if intent.speak:
                await on_transcript("assistant", intent.speak)
            await self._dispatch_action(
                {"type": "focus_reply", "position": position}
            )
        elif kind == "edit_position":
            # Opening the whole-reply editor: default a follow-up in-place edit
            # to the body of this reply until the user names another field.
            self._last_edit_target = (position, "body")
            await self._dispatch_action(
                {"type": "open_edit", "position": position}
            )
            await on_transcript(
                "assistant",
                f"Opening the editor for the {voice_intent._pos_label(position)} reply.",
            )
        elif kind == "focus_field_position":
            # Focus a specific field (to/subject/body) of the reply's editor so
            # the user can dictate into it. The frontend opens the editor if
            # needed and focuses the field; dictation then flows via the model's
            # `type` action into the data-voice-field input.
            field = intent.field or "body"
            # Remember what the user opened so a follow-up "change X to Y" that
            # omits the position/field lands on this same reply + field.
            self._last_edit_target = (position, field)
            await self._dispatch_action(
                {
                    "type": "focus_field",
                    "position": position,
                    "field": field,
                }
            )
            if intent.speak:
                await on_transcript("assistant", intent.speak)
        elif kind in ("replace_in_field_position", "set_field_position"):
            await self._handle_field_edit(
                intent, approval_id, position, on_transcript=on_transcript
            )
        elif kind == "regenerate_body_position":
            # Regenerate the reply body via the existing regenerate tool, then
            # tell the UI to refocus the body of the (Nth) reply.
            await self._call_tool("regenerate_reply", {"approval_id": approval_id})
            await self._dispatch_action(
                {"type": "regenerate_body", "position": position}
            )
            if intent.speak:
                await on_transcript("assistant", intent.speak)
        elif kind == "approve_draft_position":
            await self._call_tool(
                "approve_and_save_draft", {"approval_id": approval_id}
            )
            if intent.speak:
                await on_transcript("assistant", intent.speak)
            await self._note_action_done("save to draft")
        elif kind == "approve_send_position":
            await self._call_tool("approve_and_send", {"approval_id": approval_id})
            if intent.speak:
                await on_transcript("assistant", intent.speak)
            await self._note_action_done("send")
        elif kind == "reject_position":
            result = await self._call_tool("reject_reply", {"approval_id": approval_id})
            spoken = (
                result.get("speak") if isinstance(result, dict) else None
            ) or intent.speak
            if spoken:
                await on_transcript("assistant", spoken)
            action = result.get("action") if isinstance(result, dict) else None
            if action is not None:
                await self._dispatch_action(action)
            await self._note_action_done("reject")
        elif kind == "approve_schedule_position":
            if intent.when_iso:
                await self._call_tool(
                    "approve_and_schedule",
                    {"approval_id": approval_id, "when_iso": intent.when_iso},
                )
                if intent.speak:
                    await on_transcript("assistant", intent.speak)
                await self._dispatch_action(
                    {
                        "type": "open_schedule",
                        "position": position,
                        "when_iso": intent.when_iso,
                    }
                )
                await self._note_action_done("schedule")
            else:
                # No spoken time: do NOT schedule without a time — open the
                # picker, buffer a pending-schedule so the user can supply the
                # date/time across the NEXT turns, and ask for one.
                self._pending_schedule = {
                    "position": position,
                    "date": None,
                    "time": None,
                }
                await self._dispatch_action(
                    {"type": "open_schedule", "position": position}
                )
                await on_transcript(
                    "assistant",
                    f"Opening the scheduler for the "
                    f"{voice_intent._pos_label(position)} reply. "
                    "What date and time?",
                )
        elif kind == "reschedule_position":
            # Change an already-chosen send time ("change the time to 6pm"). When
            # a concrete time parsed, re-schedule now + move the picker; else
            # re-open the picker and buffer so the next turn(s) supply it.
            if intent.when_iso:
                await self._call_tool(
                    "approve_and_schedule",
                    {"approval_id": approval_id, "when_iso": intent.when_iso},
                )
                self._pending_schedule = None
                await self._dispatch_action(
                    {"type": "open_schedule", "position": position,
                     "when_iso": intent.when_iso}
                )
                await on_transcript(
                    "assistant",
                    f"I've updated the send time for the "
                    f"{voice_intent._pos_label(position)} reply.",
                )
                await self._note_action_done("reschedule")
            else:
                self._pending_schedule = {
                    "position": position, "date": None, "time": None,
                }
                await self._dispatch_action(
                    {"type": "open_schedule", "position": position}
                )
                await on_transcript("assistant", "What date and time?")

    async def _handle_field_edit(
        self,
        intent: voice_intent.IntentResult,
        approval_id: str,
        position: int,
        *,
        on_transcript: OnTranscript,
    ) -> None:
        """Apply an in-place edit to a reply field SERVER-SIDE (ERROR.md).

        Handles the two in-place edit intents so the user never has to supply an
        approval id and the edit happens without a further dictation step:

        - ``set_field_position`` — set the whole field to a spoken value
          ("change the body to hello there").
        - ``replace_in_field_position`` — case-insensitive find-and-replace of a
          phrase within the field ("change 'Hi there' to 'Hello PayRogen'").

        The target FIELD is resolved from (in order): the intent's own field, the
        most recent edit target the user opened (``_last_edit_target``), else
        ``body``. The current field value is decoded from the stored approval,
        the edit is applied in memory, and the result is written back through the
        existing tenant-scoped ``edit_reply`` tool — so the reply stays PENDING
        and every security invariant (server-derived tenancy, approval-only
        writes) is preserved. Speaks a natural confirmation and dispatches a
        ``focus_reply`` so a live reviewer sees the change. Never logs the text.
        """
        # Resolve which field this edit targets.
        field = intent.field
        if field is None and self._last_edit_target is not None:
            field = self._last_edit_target[1]
        field = field or "body"
        # Remember for the next follow-up edit.
        self._last_edit_target = (position, field)
        # A completed in-place edit supersedes any half-finished fragmented one.
        self._pending_replace = None

        if intent.kind == "replace_in_field_position":
            await self._do_replace_in_field(
                approval_id,
                position,
                field,
                intent.search or "",
                intent.replacement or "",
                on_transcript=on_transcript,
            )
            return

        # set_field_position: set the whole field to the spoken value.
        current = await self._read_reply_field(approval_id, field)
        if current is None:
            await on_transcript(
                "assistant", "I couldn't open that reply to edit it."
            )
            return
        new_value = intent.replacement or ""
        label = {"to": "recipient", "subject": "subject", "body": "body"}[field]
        result = await self._call_tool(
            "edit_reply", {"approval_id": approval_id, field: new_value}
        )
        if isinstance(result, dict) and result.get("error"):
            await on_transcript(
                "assistant",
                str(result.get("speak") or "I couldn't apply that edit."),
            )
            return
        await on_transcript("assistant", f"I've updated the {label}.")
        await self._dispatch_action(
            {"type": "edit_field", "position": position, "field": field,
             "value": new_value}
        )
        await self._dispatch_action({"type": "focus_reply", "position": position})

    async def _buffer_pending_replace(
        self, intent: voice_intent.IntentResult, *, on_transcript: OnTranscript
    ) -> None:
        """Buffer the search half of a fragmented edit and ask for the replacement.

        Records ``{position, field, search}`` (resolving position/field from the
        intent or the last edit target) so a following ``edit_replacement`` turn
        can complete the change. Speaks the intent's prompt ("What should I
        change X to?"). NEVER logs transcript text.
        """
        if intent.position is not None:
            position = intent.position
        elif self._last_edit_target is not None:
            position = self._last_edit_target[0]
        else:
            position = 1
        field = intent.field
        if field is None and self._last_edit_target is not None:
            field = self._last_edit_target[1]
        field = field or "body"

        self._pending_replace = {
            "position": position,
            "field": field,
            "search": intent.search or "",
        }
        self._last_edit_target = (position, field)
        if intent.speak:
            await on_transcript("assistant", intent.speak)

    async def _apply_pending_replace(
        self, intent: voice_intent.IntentResult, *, on_transcript: OnTranscript
    ) -> None:
        """Apply a buffered fragmented edit once the replacement arrives.

        Uses the buffered ``{position, field, search}`` from a prior
        ``edit_search_pending`` turn and the replacement from this
        ``edit_replacement`` turn. If nothing is buffered, this is a harmless
        no-op (a stray "to X" with no pending edit). Clears the buffer after.
        """
        pending = self._pending_replace
        replacement = intent.replacement or ""
        if not pending or not replacement:
            return
        self._pending_replace = None

        await self._refresh_pending_cache()
        position = int(pending.get("position") or 1)
        total = len(self._pending_cache)
        if position < 1 or position > total:
            plural = "reply" if total == 1 else "replies"
            await on_transcript(
                "assistant",
                f"I couldn't find reply number {position}; you have "
                f"{total} pending {plural}.",
            )
            return
        approval_id = self._pending_cache[position - 1]
        await self._do_replace_in_field(
            approval_id,
            position,
            str(pending.get("field") or "body"),
            str(pending.get("search") or ""),
            replacement,
            on_transcript=on_transcript,
        )

    async def _accumulate_schedule(
        self, text: str, *, on_transcript: OnTranscript
    ) -> bool:
        """Accumulate spoken date/time fragments for a pending schedule.

        Called only when ``_pending_schedule`` is buffered. Parses ``text`` for a
        DATE part (month/day, "tomorrow", weekday) and/or a TIME part (clock or
        spoken-hour) using :mod:`app.services.voice_datetime`, merges whichever
        it found into the buffer, and — once BOTH a date and a time are known —
        approves and schedules the reply (via the ``approve_and_schedule`` tool)
        and emits ``open_schedule`` with the resolved ISO so the calendar picker
        fills in. Returns ``True`` when it consumed the utterance as a schedule
        fragment (so the caller stops), ``False`` when the utterance had no
        date/time at all (let the model handle it). NEVER logs transcript text.
        """
        from datetime import datetime as _dt

        pending = self._pending_schedule
        if pending is None:
            return False

        now = _dt.now()
        date_part, time_part = _extract_schedule_parts(text, now=now)
        if date_part is None and time_part is None:
            return False  # nothing schedule-like; let the model respond

        if date_part is not None:
            pending["date"] = date_part
        if time_part is not None:
            pending["time"] = time_part

        # Not complete yet — ask for the missing half.
        if pending.get("date") is None:
            await on_transcript("assistant", "What date?")
            return True
        if pending.get("time") is None:
            await on_transcript("assistant", "What time?")
            return True

        # Both known: build the ISO and schedule.
        d = pending["date"]
        h, m = pending["time"]
        when = _dt(d.year, d.month, d.day, h, m)
        position = int(pending.get("position") or 1)
        when_iso = when.isoformat()
        self._pending_schedule = None

        await self._refresh_pending_cache()
        total = len(self._pending_cache)
        if position < 1 or position > total:
            plural = "reply" if total == 1 else "replies"
            await on_transcript(
                "assistant",
                f"I couldn't find reply number {position}; you have "
                f"{total} pending {plural}.",
            )
            return True
        approval_id = self._pending_cache[position - 1]
        result = await self._call_tool(
            "approve_and_schedule",
            {"approval_id": approval_id, "when_iso": when_iso},
        )
        if isinstance(result, dict) and result.get("error"):
            await on_transcript(
                "assistant", str(result.get("speak") or "I couldn't schedule that.")
            )
            return True
        await self._dispatch_action(
            {"type": "open_schedule", "position": position, "when_iso": when_iso}
        )
        await on_transcript(
            "assistant",
            f"I've scheduled the {voice_intent._pos_label(position)} reply.",
        )
        await self._note_action_done("schedule")
        return True

    async def _do_replace_in_field(
        self,
        approval_id: str,
        position: int,
        field: str,
        search: str,
        replacement: str,
        *,
        on_transcript: OnTranscript,
    ) -> None:
        """Read a field, apply a case-insensitive find-and-replace, write it back.

        Shared by the single-turn ``replace_in_field_position`` path and the
        fragmented (buffered) edit path. Reads the current field via
        ``_read_reply_field``, applies :func:`_replace_ci`, writes through the
        tenant-scoped ``edit_reply`` tool, speaks a confirmation, and dispatches
        ``edit_field`` (live editor update) + ``focus_reply``. NEVER logs text.
        """
        if not search:
            await on_transcript("assistant", "I didn't catch what to change.")
            return
        current = await self._read_reply_field(approval_id, field)
        if current is None:
            await on_transcript("assistant", "I couldn't open that reply to edit it.")
            return
        new_value, replaced = _replace_ci(current, search, replacement)
        if not replaced:
            await on_transcript(
                "assistant",
                f"I couldn't find \u201c{search}\u201d in the "
                f"{voice_intent._pos_label(position)} reply's "
                f"{'recipient' if field == 'to' else field}.",
            )
            return
        result = await self._call_tool(
            "edit_reply", {"approval_id": approval_id, field: new_value}
        )
        if isinstance(result, dict) and result.get("error"):
            await on_transcript(
                "assistant", str(result.get("speak") or "I couldn't apply that edit.")
            )
            return
        self._last_edit_target = (position, field)
        await on_transcript(
            "assistant",
            f"I've changed \u201c{search}\u201d to \u201c{replacement}\u201d.",
        )
        await self._dispatch_action(
            {"type": "edit_field", "position": position, "field": field,
             "search": search, "replacement": replacement}
        )
        await self._dispatch_action({"type": "focus_reply", "position": position})
        await self._note_action_done("edit")

    async def _read_reply_field(self, approval_id: str, field: str) -> str | None:
        """Return the current value of ``field`` for a pending reply, or ``None``.

        Loads the tenant-scoped approval, decodes its stored reply fields via
        ``approval_service.decode_reply_fields``, and returns the requested
        field's current text ("to" | "subject" | "body"). Returns ``None`` when
        the approval is missing, in another workspace, or the id is malformed —
        so an edit never touches another tenant's reply. Never raises.
        """
        import uuid as _uuid

        from app.db.models import ApprovalRequest
        from app.services import approval_service

        try:
            parsed = _uuid.UUID(approval_id)
        except ValueError:
            return None
        try:
            async with self._session_factory() as session:
                request = await session.get(ApprovalRequest, parsed)
                if request is None or request.workspace_id != self._workspace_id:
                    return None
                fields = approval_service.decode_reply_fields(request.arguments)
        except Exception:  # noqa: BLE001 - a read failure must not crash the session
            return None
        value = fields.get(field)
        return str(value) if value is not None else ""

    async def _read_position(
        self, approval_id: str, position: int, *, on_transcript: OnTranscript
    ) -> None:
        """Read the original email (if available) then the proposed reply aloud.

        Reads the PROPOSED reply via the existing ``read_approval`` tool (which
        returns the recipient/subject/body of the pending draft). When the stored
        approval also lets us describe the ORIGINAL email being replied to
        (decoded via ``approval_service.decode_reply_fields``), that context is
        spoken first. Each spoken line is emitted as an assistant transcript.
        NEVER logs transcript text.
        """
        # Read the PROPOSED reply through the existing tenant-scoped tool.
        result = await self._call_tool("read_approval", {"approval_id": approval_id})
        reply_speak = ""
        if isinstance(result, dict):
            if result.get("error"):
                # Tool already produced a friendly spoken message.
                await on_transcript("assistant", str(result.get("speak") or "I couldn't read that reply."))
                return
            reply_speak = str(result.get("speak") or "")

        # Best-effort ORIGINAL-email context: the stored reply's recipient is the
        # original sender and its subject is the original subject line. Read that
        # first so the user has context before the proposed reply. Any failure is
        # non-fatal — we still read the reply.
        original_line = ""
        try:
            original_line = await self._describe_original(approval_id)
        except Exception:  # noqa: BLE001 - original context is best-effort
            original_line = ""

        if original_line:
            await on_transcript("assistant", original_line)
        if reply_speak:
            await on_transcript("assistant", f"The proposed reply is: {reply_speak}")

    async def _describe_original(self, approval_id: str) -> str:
        """Return a spoken one-liner describing the original email, or "".

        Opens a fresh DB session, loads the approval, and decodes its stored
        fields via ``approval_service.decode_reply_fields``. The reply's ``to``
        is the original sender and its ``subject`` the original subject, so we
        can describe the original without a Gmail fetch. Tenant-scoped: only an
        approval in this workspace is described. Returns "" when unavailable.
        """
        import uuid as _uuid

        from app.db.models import ApprovalRequest
        from app.services import approval_service

        try:
            parsed = _uuid.UUID(approval_id)
        except ValueError:
            return ""
        async with self._session_factory() as session:
            request = await session.get(ApprovalRequest, parsed)
            if request is None or request.workspace_id != self._workspace_id:
                return ""
            fields = approval_service.decode_reply_fields(request.arguments)
        sender = (fields.get("to") or "").strip()
        subject = (fields.get("subject") or "").strip()
        if not sender and not subject:
            return ""
        who = sender or "an unknown sender"
        subj = subject or "no subject"
        return f"The original email is from {who}, subject {subj}."

    # ------------------------------------------------------------------
    # Default (real) agent factory
    # ------------------------------------------------------------------

    def _default_agent_factory(self, *, tools: list, system_prompt: str) -> Any:
        """Build a real ``BidiAgent`` backed by Nova Sonic (no AWS call here).

        Instantiates :class:`BidiNovaSonicModel` with the configured model id and
        AWS region, then a :class:`BidiAgent` with the voice tools and the
        accessibility system prompt. Constructing the model does NOT open a
        stream — that happens in :meth:`start`.
        """
        from strands.experimental.bidi import BidiAgent  # type: ignore[import-not-found]
        from strands.experimental.bidi.models.nova_sonic import (  # type: ignore[import-not-found]
            BidiNovaSonicModel,
        )

        settings = get_settings()
        model = BidiNovaSonicModel(
            model_id=settings.NOVA_SONIC_MODEL_ID,
            client_config={"region": settings.AWS_REGION},
        )
        return BidiAgent(model=model, tools=tools, system_prompt=system_prompt)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the agent (with tools) and open the Nova Sonic connection."""
        if self._started:
            raise RuntimeError("voice session already started")
        tools = self._build_tools()
        self._agent = self._agent_factory(tools=tools, system_prompt=VOICE_SYSTEM_PROMPT)
        await self._agent.start()
        self._started = True
        logger.debug("voice session started | workspace=%s", self._workspace_id)

    async def send_audio(self, pcm16k_bytes: bytes) -> None:
        """Send a chunk of 16 kHz mono PCM16 audio to the model.

        Wraps the raw bytes in the SDK's :class:`BidiAudioInputEvent` (base64,
        pcm, 16 kHz, mono) and hands it to ``BidiAgent.send``. Never logs the
        audio.
        """
        if not self._started or self._agent is None:
            raise RuntimeError("voice session not started")
        from strands.experimental.bidi import BidiAudioInputEvent  # type: ignore[import-not-found]

        encoded = base64.b64encode(pcm16k_bytes).decode("ascii")
        event = BidiAudioInputEvent(
            audio=encoded,
            format=AUDIO_FORMAT,
            sample_rate=INPUT_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
        )
        await self._agent.send(event)

    async def send_text(self, text: str) -> None:
        """Send a text turn to the model (optional; e.g. typed control input)."""
        if not self._started or self._agent is None:
            raise RuntimeError("voice session not started")
        from strands.experimental.bidi import BidiTextInputEvent  # type: ignore[import-not-found]

        await self._agent.send(BidiTextInputEvent(text=text))

    async def send_welcome(self) -> None:
        """Inject the once-per-session spoken Welcome_Message (Req 2.1, 2.3).

        Injects :data:`voice_intent.WELCOME_DIRECTIVE` via :meth:`send_text` so
        Nova Sonic greets the user in its own voice when the session becomes
        ready. Guarded by ``_welcome_sent``: the first call injects the directive
        and sets the flag; every subsequent call is a no-op (Property 5). The
        flag is set only after a successful injection so that a transient failure
        on the first attempt does not permanently suppress the greeting.

        The router calls this after :meth:`start` (following the ``ready``
        frame), but injection is best-effort regardless: any failure — including
        the session not being started — is caught, logged at debug, and does NOT
        tear down the session.
        """
        if self._welcome_sent:
            return
        try:
            await self.send_text(voice_intent.WELCOME_DIRECTIVE)
        except Exception:  # noqa: BLE001 - welcome injection must never tear down the session
            logger.debug("voice welcome injection failed; continuing session")
            return
        self._welcome_sent = True
        # Record the welcome as the first turn of this session's transcript, so
        # the debug table captures the conversation "from the welcome message
        # onward" (ERROR.md Task 1). Best-effort; never crashes the session.
        await self._record_turn("system", voice_intent.WELCOME_DIRECTIVE)
        logger.debug("voice welcome injected | workspace=%s", self._workspace_id)

    async def stop(self) -> None:
        """End the model conversation and release resources (idempotent).

        Teardown MUST NOT raise. The underlying Bedrock/Nova Sonic stream may
        already be dead (e.g. after a mid-stream ``ModelStreamErrorException`` or
        an auth/signature failure), in which case ``agent.stop()`` raises while
        trying to close an already-completed HTTP/2 stream. Failing to close a
        stream that is already gone is not actionable and must never propagate —
        it would crash the WebSocket endpoint's ``finally`` teardown. So any
        error from the agent stop sequence is caught and logged (type only, no
        secrets) and the session is still marked stopped.
        """
        self._on_action = None
        if self._agent is not None and self._started:
            try:
                await self._agent.stop()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                logger.debug(
                    "voice agent stop failed (stream likely already closed) | "
                    "workspace=%s | error=%s",
                    self._workspace_id,
                    type(exc).__name__,
                )
            finally:
                self._started = False
                logger.debug("voice session stopped | workspace=%s", self._workspace_id)

    async def run(
        self,
        *,
        on_audio: OnAudio,
        on_transcript: OnTranscript,
        on_action: OnAction,
        on_error: OnError,
    ) -> None:
        """Consume model output events and dispatch them to the callbacks.

        Routing:

        - :class:`BidiAudioStreamEvent` -> ``on_audio(pcm24k_bytes)`` (decoded
          from the event's base64 audio).
        - :class:`BidiTranscriptStreamEvent` -> ``on_transcript(role, text)``.
        - :class:`BidiInterruptionEvent` -> ``on_action({"type": "interrupt", ...})``
          so the frontend can stop playback immediately.
        - :class:`BidiErrorEvent` -> ``on_error(message)``.

        Tool execution is handled by Strands (the registered ``@tool`` callables
        run automatically); the structured ``action`` side-channel flows through
        the ``on_action`` sink those tools call, wired here for the duration of
        the run. Other event types (connection lifecycle, usage, response
        start/complete, tool-use notifications) are ignored — they carry no
        client-facing payload for this feature.

        NEVER logs audio or transcript text.
        """
        if not self._started or self._agent is None:
            raise RuntimeError("voice session not started")

        # Wrap the caller's transcript sink so EVERY spoken turn that reaches the
        # client — every user transcript, every assistant transcript, and every
        # deterministic confirmation the gateway speaks — is also recorded to the
        # ``voice_transcripts`` debug table (task A). Recording is best-effort and
        # runs before forwarding so a record failure never blocks the client.
        caller_on_transcript = on_transcript

        async def _recording_on_transcript(role: str, text: str) -> None:
            await self._record_turn(role, text)
            await caller_on_transcript(role, text)

        on_transcript = _recording_on_transcript

        # Wire the tool action side-channel to the caller's sink for this run.
        self._on_action = on_action
        try:
            # Reconnect loop. The Strands/Nova Sonic SDK surfaces transient
            # Bedrock stream failures (e.g. ``ModelStreamErrorException``, or an
            # awscrt HTTP/2 ``HTTP-stream has completed`` cleanup race) by
            # RAISING out of ``receive()`` — and it only auto-restarts on its own
            # idle-``BidiModelTimeoutError``, not on these. Rather than kill the
            # whole voice session (leaving the user to click "Start" again), we
            # transparently rebuild the model connection and resume on the SAME
            # WebSocket. The connection itself is healthy; a fresh agent works.
            #
            # ``CancelledError`` (a ``BaseException`` on 3.11+) is intentionally
            # NOT caught, so normal task-group cancellation still tears us down.
            attempt = 0
            while True:
                try:
                    await self._consume_events(
                        on_audio=on_audio,
                        on_transcript=on_transcript,
                        on_action=on_action,
                        on_error=on_error,
                    )
                    return  # stream ended cleanly (user stop / graceful close)
                except Exception as exc:  # noqa: BLE001 - contain stream failures
                    cause = exc.__cause__ or exc.__context__
                    logger.warning(
                        "voice model stream error | workspace=%s | error=%s | "
                        "detail=%s | cause=%s | attempt=%s",
                        self._workspace_id,
                        type(exc).__name__,
                        str(exc)[:500],
                        (f"{type(cause).__name__}: {str(cause)[:300]}"
                         if cause else "none"),
                        attempt,
                    )
                    attempt += 1
                    if attempt > _MAX_RECONNECT_ATTEMPTS:
                        # Out of retries: tell the client so it can recover
                        # (show the retry prompt) instead of hanging.
                        try:
                            await on_error(
                                "The voice service keeps dropping. "
                                "Please try again."
                            )
                        except Exception:  # noqa: BLE001 - client may be gone
                            logger.debug(
                                "voice on_error delivery failed after stream error"
                            )
                        return
                    # Rebuild the model connection and resume. A short backoff
                    # lets any half-closed HTTP/2 stream finish tearing down.
                    reconnected = await self._reconnect(delay=_RECONNECT_BACKOFF_S * attempt)
                    if not reconnected:
                        try:
                            await on_error(
                                "The voice service hit a temporary problem. "
                                "Please try again."
                            )
                        except Exception:  # noqa: BLE001 - client may be gone
                            logger.debug(
                                "voice on_error delivery failed after reconnect failure"
                            )
                        return
        finally:
            self._on_action = None

    async def _consume_events(
        self,
        *,
        on_audio: OnAudio,
        on_transcript: OnTranscript,
        on_action: OnAction,
        on_error: OnError,
    ) -> None:
        """Consume one connection's worth of model output events.

        Raises whatever ``self._agent.receive()`` raises so :meth:`run` can
        decide whether to reconnect. Returns normally when the stream ends
        cleanly (user stop / graceful close). NEVER logs audio or transcript.
        """
        async for event in self._agent.receive():
            event_type = event.get("type") if isinstance(event, dict) else None
            # NOTE (regression fix): the model-response SUPPRESSION that used to
            # live here (dropping the model's audio/transcript for a turn the
            # deterministic router handled, gated on bidi_response_start /
            # bidi_response_complete) caused the voice to "crack and twitch"
            # between listening and speaking — Nova Sonic streams audio in many
            # small chunks and those response boundaries do not reliably bracket
            # each spoken response, so suppression tore audio apart mid-phrase.
            # We reverted to the KNOWN-GOOD behavior: forward ALL model audio and
            # ALL assistant transcripts unconditionally. The "I'm sorry"
            # double-talk is handled by the system prompt (VOICE_SYSTEM_PROMPT),
            # exactly as it was when voice was working smoothly. Do NOT re-add
            # per-chunk suppression here without a response-complete-buffered
            # design (buffer a whole response, then decide) — the streaming
            # drop-logic is what broke playback.
            if event_type == "bidi_audio_stream":
                audio_b64 = event.get("audio")
                if isinstance(audio_b64, str) and audio_b64:
                    # Nova Sonic (via Strands) outputs 16 kHz mono PCM16 by
                    # default; forward the event's declared rate so the client
                    # plays at the correct speed/pitch (playing 16k as 24k made
                    # the voice fast + robotic).
                    rate = event.get("sample_rate")
                    try:
                        rate = int(rate)
                    except (TypeError, ValueError):
                        rate = OUTPUT_SAMPLE_RATE
                    await on_audio(base64.b64decode(audio_b64), rate)
            elif event_type == "bidi_transcript_stream":
                role = event.get("role", "assistant")
                text = event.get("text", "")
                if text:
                    # Forward the transcript (user or assistant) to the client —
                    # unconditional, as in the working version.
                    await on_transcript(str(role), str(text))
                # Deterministic Intent_Router: inspect the USER transcript and
                # act on a clear navigation / approval intent ourselves, so
                # accessibility actions do not depend on the model's
                # probabilistic tool-use.
                if str(role) == "user" and text:
                    await self._handle_user_transcript(
                        str(text), on_transcript=on_transcript
                    )
            elif event_type == "bidi_interruption":
                await on_action(
                    {"type": "interrupt", "reason": event.get("reason", "user_speech")}
                )
            elif event_type == "bidi_error":
                await on_error(str(event.get("message", "voice error")))
            # All other event types are intentionally ignored.

    async def _reconnect(self, *, delay: float) -> bool:
        """Tear down the dead agent and build a fresh one on the same session.

        Returns ``True`` if a new model connection was opened, ``False`` if the
        rebuild failed (caller then surfaces a retry prompt). Session-transient
        state (welcome-sent flag, turn counter) is intentionally PRESERVED so a
        reconnect is seamless and the welcome is not repeated. Never raises.
        """
        import asyncio

        logger.info(
            "voice session reconnecting | workspace=%s | delay=%.2fs",
            self._workspace_id,
            delay,
        )
        # Best-effort teardown of the dead agent (never raises — see stop()).
        old_agent = self._agent
        if old_agent is not None:
            try:
                await old_agent.stop()
            except Exception as exc:  # noqa: BLE001 - dead stream teardown
                logger.debug(
                    "voice old-agent stop failed during reconnect | error=%s",
                    type(exc).__name__,
                )
        self._agent = None
        self._started = False

        if delay > 0:
            await asyncio.sleep(delay)

        try:
            tools = self._build_tools()
            self._agent = self._agent_factory(
                tools=tools, system_prompt=VOICE_SYSTEM_PROMPT
            )
            await self._agent.start()
            self._started = True
            logger.info(
                "voice session reconnected | workspace=%s", self._workspace_id
            )
            return True
        except Exception as exc:  # noqa: BLE001 - reconnect must never raise
            logger.warning(
                "voice session reconnect failed | workspace=%s | error=%s",
                self._workspace_id,
                type(exc).__name__,
            )
            return False



__all__ = [
    "NovaSonicVoiceSession",
    "VOICE_SYSTEM_PROMPT",
    "INPUT_SAMPLE_RATE",
    "OUTPUT_SAMPLE_RATE",
]
