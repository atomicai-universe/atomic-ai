"""Deterministic voice intent classification and spoken guidance (pure).

This module is the single source of truth for turning a *user transcript* into a
deterministic navigation / help decision, so accessibility navigation does not
depend on the probabilistic tool-use of the speech-to-speech model. It is
deliberately **pure**: no I/O, no DB, no AWS. Everything here is unit-testable
without a network.

Two responsibilities:

- :func:`classify` inspects a spoken utterance and, when it clearly expresses a
  navigation or help intent, returns an immutable :class:`IntentResult`. It is
  conservative by design — ordinary conversation ("reply to Camilia", "read my
  unread emails", "approve and send") returns ``None`` so navigation never
  false-triggers.
- :func:`build_guidance_script` returns the natural-language guidance the
  assistant speaks on a help request, covering every destination, both read
  capabilities, and every gated action — in spoken language, with no internal
  tool names.

Destination resolution delegates to :func:`app.services.voice_tools._resolve_nav_target`
(and its ``_NAV_TARGETS`` allowlist) so the destination mapping is defined
exactly once and reused by both the model-driven tool path and this
deterministic router.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.services import voice_datetime, voice_tools

__all__ = [
    "IntentResult",
    "classify",
    "build_guidance_script",
    "parse_spoken_datetime",
    "WELCOME_DIRECTIVE",
    "NAV_DESTINATIONS_SENTENCE",
]

#: Re-exported for callers/tests: the pure spoken date/time parser lives in
#: :mod:`app.services.voice_datetime` (stdlib-only). Exposed here so the
#: gateway and tests import one intent surface.
parse_spoken_datetime = voice_datetime.parse_spoken_datetime


@dataclass(frozen=True, slots=True)
class IntentResult:
    """Immutable outcome of classifying a spoken utterance.

    ``kind`` is one of:
      - ``"navigate"`` / ``"unknown_destination"`` / ``"help"`` (navigation and
        guidance intents, unchanged from the navigation feature); or
      - a position-aware approval/reply action:
        ``"read_position"``, ``"regenerate_position"``, ``"edit_position"``,
        ``"approve_draft_position"``, ``"approve_send_position"``,
        ``"approve_schedule_position"``, ``"reschedule_position"``,
        ``"reject_position"``; or
      - an in-place field edit performed SERVER-SIDE without a further UI step:
        ``"replace_in_field_position"`` (find-and-replace text within a field)
        and ``"set_field_position"`` (set a field to a spoken value); or
      - a collection listing read aloud by the router (not the model):
        ``"list_pending"`` (pending approvals) and ``"list_unread"`` (unread
        inbox); or
      - a FRAGMENTED in-place edit spoken across two turns:
        ``"edit_search_pending"`` (the search half, awaiting a replacement) and
        ``"edit_replacement"`` (the replacement half, applied to the buffered
        search).

    Fields:
      - ``path``: the resolved dashboard route for a ``navigate`` result, else
        ``None``.
      - ``speak``: the short spoken text for the assistant to voice. Never an
        apology.
      - ``position``: the 1-based position of the pending reply the action
        targets (Nth pending approval, ``created_at`` ASC), for the
        ``*_position`` kinds; ``None`` for navigation/help.
      - ``when_iso``: an ISO-8601 send time parsed from the utterance for
        ``approve_schedule_position`` when the user spoke a time; else ``None``.

    Immutable (frozen) so a classification result can be shared safely.
    """

    kind: str
    path: str | None
    speak: str
    position: int | None = None
    when_iso: str | None = None
    field: str | None = None  # "to" | "subject" | "body" for focus_field_position
    # In-place edit payload for ``replace_in_field_position`` / ``set_field_position``:
    #   - ``search``:  the text to find in the field (case-insensitive), or None
    #     for a whole-field set.
    #   - ``replacement``: the replacement / new value.
    search: str | None = None
    replacement: str | None = None


# ---------------------------------------------------------------------------
# Navigation intent detection
# ---------------------------------------------------------------------------

#: Leading verbs that signal a navigation intent. Ordered longest-first so a
#: multi-word verb ("take me to") is matched before a shorter prefix.
_NAV_VERBS: tuple[str, ...] = (
    "take me to",
    "bring up",
    "go to",
    "navigate to",
    "navigate",
    "open",
    "show me",
    "show",
    "click",
)

#: Human-facing label for a resolved route, keyed by the route path. Derived
#: from the five Nav_Destinations (the route mapping itself lives in
#: ``voice_tools._NAV_TARGETS`` — the single source of truth).
_ROUTE_LABELS: dict[str, str] = {
    "/dashboard/integrations": "Integrations",
    "/dashboard/approvals": "Approvals",
    "/dashboard/rules": "Rules",
    "/dashboard/workspace": "Team",
    "/admin": "Admin",
}

_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")


def _strip_leading_nav_verb(text: str) -> str | None:
    """Return the destination remainder after a leading nav verb, else ``None``.

    ``text`` is expected to be lowercased and punctuation-stripped. Returns the
    (possibly empty) remaining phrase when a known verb leads the utterance;
    ``None`` when no navigation verb is present.
    """
    for verb in _NAV_VERBS:
        if text == verb:
            return ""
        prefix = verb + " "
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return None


# ---------------------------------------------------------------------------
# Help intent detection
# ---------------------------------------------------------------------------

#: Exact (normalized) utterances that request the guidance script.
_HELP_PHRASES: frozenset[str] = frozenset(
    {
        "help",
        "what can i do",
        "what can you do",
        "options",
        "commands",
    }
)

_HELP_SPEAK = "Here's what you can do."


# ---------------------------------------------------------------------------
# Bulk-clear and cancel intents (ERROR.md Task 3 / Task 1)
# ---------------------------------------------------------------------------

#: A bulk-clear must explicitly say "all"/"every"/"everything" AND a
#: clear/delete/reject/discard verb, so it never fires on "reject the first"
#: (a single-item action) or on conversational mentions of "all".
_CLEAR_VERBS: tuple[str, ...] = (
    "clear",
    "delete",
    "reject",
    "discard",
    "remove",
    "dismiss",
    "trash",
    "wipe",
)
_ALL_WORDS: frozenset[str] = frozenset({"all", "every", "everything", "them all"})
_CLEAR_SPEAK = "Clearing all pending replies."

#: Cancel / never-mind phrasings. Recognized so the deterministic router — not
#: the model — answers them, which stops Nova Sonic apologising ("cancelling is
#: not supported"). Kept tight so it doesn't swallow other verbs.
_CANCEL_PHRASES: frozenset[str] = frozenset(
    {
        "cancel",
        "never mind",
        "nevermind",
        "stop editing",
        "cancel editing",
        "cancel edit",
        "cancel that",
        "discard changes",
        "close the editor",
        "close editor",
        "forget it",
        "cancel the edit",
    }
)
_CANCEL_SPEAK = "Okay, cancelled."


def _is_clear_all(text: str) -> bool:
    """Whether a normalized utterance asks to clear ALL pending replies.

    Requires an explicit all/every word AND a clear/delete/reject verb so a
    single-item action ("reject the first") never triggers a bulk clear.
    """
    has_all = any(w in text for w in _ALL_WORDS)
    if not has_all:
        return False
    words = set(text.split())
    return any(v in words for v in _CLEAR_VERBS)


# ---------------------------------------------------------------------------
# List / read-all intents (pending approvals and unread emails)
#
# These are handled by the deterministic router (not the Nova Sonic model) so
# the reliable main-loop DB path lists them — the model's own multi-step
# list->read->act tool chain is brittle over the bidi stream (it can fail or
# loop and narrate a false apology, see ERROR.md). The router owns listing so
# the model never needs the list/read-approval tools at all.
# ---------------------------------------------------------------------------

#: Verbs that request a listing / read-aloud of a COLLECTION.
_LIST_VERBS: tuple[str, ...] = (
    "list",
    "read",
    "show",
    "tell me",
    "what",
    "whats",
    "how many",
    "do i have",
    "check",
    "any",
)

#: Words that name the pending-approvals collection.
_APPROVALS_WORDS: frozenset[str] = frozenset(
    {"approval", "approvals", "reply", "replies", "draft", "drafts"}
)
#: Words that name the unread-inbox collection.
_UNREAD_WORDS: frozenset[str] = frozenset({"email", "emails", "inbox", "mail", "messages"})


def _classify_list_request(normalized: str) -> IntentResult | None:
    """Classify a request to LIST/read-aloud pending approvals or unread email.

    Deterministic and conservative. Fires only when the utterance clearly asks
    about the COLLECTION (plural or "my"/"pending"/"awaiting"/"unread"
    qualifier) and carries NO specific position (a positional request like "read
    the first reply" is a single-item action handled elsewhere). Returns:

      - ``list_pending`` for pending approvals / replies awaiting approval; or
      - ``list_unread`` for unread emails / inbox; or
      - ``None`` when it is not clearly a list request.
    """
    words = normalized.split()
    word_set = set(words)

    # A concrete position means a single-item action, not a list.
    if _parse_position(normalized) is not None:
        return None

    # A single-item action verb ("reject the reply", "approve and send") is not
    # a list request.
    if _starts_with_action_verb(normalized):
        return None

    mentions_approvals = bool(word_set & _APPROVALS_WORDS)
    mentions_unread = bool(word_set & _UNREAD_WORDS)
    if not mentions_approvals and not mentions_unread:
        return None

    # A read/count VERB must lead the utterance — a bare navigation phrase
    # ("show approvals", "open approvals", "approvals menu") is navigation, not a
    # collection read, and must fall through to the navigation resolver.
    starts_list_verb = any(
        normalized == v or normalized.startswith(v + " ") for v in _LIST_VERBS
    )
    if not starts_list_verb:
        return None

    # Require an explicit COLLECTION qualifier so "show approvals" (a bare
    # destination -> navigation) is NOT swallowed, while "read my pending
    # approvals" / "how many replies are awaiting" (a listing) IS. The plural
    # noun alone is not enough — it must be paired with a "my"/"pending"/
    # "awaiting"/"unread"/"how many"/"any" collection cue.
    my_signal = "my" in word_set or "all" in word_set
    count_signal = normalized.startswith("how many") or "any" in word_set

    pending_qualifier = (
        "pending" in word_set
        or "awaiting" in word_set
        or "replies" in word_set
        or "drafts" in word_set
        or my_signal
        or count_signal
    )
    unread_qualifier = (
        "unread" in word_set
        or "inbox" in word_set
        or "emails" in word_set
        or "mail" in word_set
        or my_signal
        or count_signal
    )

    # Pending approvals collection.
    if mentions_approvals and pending_qualifier:
        return IntentResult(kind="list_pending", path=None, speak="")

    # Unread inbox collection.
    if mentions_unread and unread_qualifier:
        return IntentResult(kind="list_unread", path=None, speak="")

    return None


#: Action verbs whose leading presence means a single-item action, not a list.
_ACTION_LEADING_VERBS: tuple[str, ...] = (
    "reply",
    "approve",
    "send",
    "regenerate",
    "edit",
    "schedule",
    "change",
    "replace",
    "delete",
    "reject",
    "clear",
)


def _starts_with_action_verb(text: str) -> bool:
    """Whether a normalized utterance leads with a single-item action verb."""
    for verb in _ACTION_LEADING_VERBS:
        if text == verb or text.startswith(verb + " "):
            return True
    return False


def _is_cancel(text: str) -> bool:
    """Whether a normalized utterance is a cancel / never-mind request."""
    if text in _CANCEL_PHRASES:
        return True
    # Also allow a leading "cancel ..."/"never mind ..." with trailing words
    # like "cancel editing the first email" -> cancel.
    for phrase in ("cancel", "never mind", "nevermind", "stop editing"):
        if text == phrase or text.startswith(phrase + " "):
            return True
    return False

#: Leading verbs that signal a *non-navigation* intent (reading, replying,
#: acting on a draft, conversing). When an utterance begins with one of these,
#: the no-verb "bare destination" fallback (step 4) must NOT fire — a
#: conversational sentence such as "read my pending approvals" merely *mentions*
#: a destination word ("approvals") and must classify as ``None`` rather than
#: navigate. Ordered longest-first so a multi-word verb is matched before a
#: shorter prefix.
_NON_NAV_LEADING_VERBS: tuple[str, ...] = (
    "read",
    "reply",
    "approve",
    "send",
    "regenerate",
    "edit",
    "schedule",
    "summarize",
    "tell",
)


def _starts_with_non_nav_verb(text: str) -> bool:
    """Whether a normalized utterance begins with a non-navigation verb.

    ``text`` is expected to be lowercased and punctuation-stripped. Used to keep
    the bare-destination fallback conservative: a sentence that leads with a
    read/reply/act verb is conversation, not a bare destination reference, even
    if a destination word appears later in the phrase.
    """
    for verb in _NON_NAV_LEADING_VERBS:
        if text == verb or text.startswith(verb + " "):
            return True
    return False


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace (pure)."""
    lowered = _PUNCT_RE.sub(" ", text.lower())
    return " ".join(lowered.split())


# ---------------------------------------------------------------------------
# Position (ordinal) parsing and position-aware approval/reply intents
# ---------------------------------------------------------------------------

#: Spoken ordinals -> position (1-based), up to tenth. Includes the word form
#: ("first"), the numeric-ordinal form ("1st"), and the cardinal word ("one").
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "1st": 1, "one": 1,
    "second": 2, "2nd": 2, "two": 2,
    "third": 3, "3rd": 3, "three": 3,
    "fourth": 4, "4th": 4, "four": 4,
    "fifth": 5, "5th": 5, "five": 5,
    "sixth": 6, "6th": 6, "six": 6,
    "seventh": 7, "7th": 7, "seven": 7,
    "eighth": 8, "8th": 8, "eight": 8,
    "ninth": 9, "9th": 9, "nine": 9,
    "tenth": 10, "10th": 10, "ten": 10,
}

#: Spoken position for a bare number, e.g. "number one" / "#1" / "number 1".
_NUMBER_WORD_RE = re.compile(r"\b(?:number|no|num)\s+(\d{1,2})\b")


def _parse_position(text: str) -> int | None:
    """Extract a 1-based position (ordinal / cardinal / digit) from text, or None.

    ``text`` is expected to be lowercased and punctuation-stripped (``#`` is
    already removed by :func:`_normalize`, so "#1" arrives as "1"). Resolution
    order (pure, first match wins on token scan):
      1. An ordinal / cardinal word ("first", "1st", "one", … "tenth").
      2. "number N" / "no N" (a spoken bare number).
      3. A bare digit token ("1" .. "10", also handles "#1" → "1").
    Returns ``None`` when no position token is present.
    """
    if not text:
        return None

    # 2. "number one" spelled with a digit ("number 1").
    m = _NUMBER_WORD_RE.search(text)
    if m is not None:
        try:
            val = int(m.group(1))
        except ValueError:
            val = 0
        if 1 <= val <= 99:
            return val

    tokens = text.split()

    def _is_time_context(idx: int) -> bool:
        """True when the token at ``idx`` is part of a spoken clock time.

        A number followed by am/pm/o'clock ("six pm", "seven o'clock") — or a
        colon-clock like "5:30" — is a TIME, not a reply position. Guards the
        common "change the time to six pm" case from being read as reply 6.
        """
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if nxt in ("pm", "am", "oclock", "o'clock", "p.m.", "a.m."):
            return True
        # "at six", "for seven" preceding a bare hour is still a time cue when a
        # pm/am appears anywhere after it within a short window.
        return False

    # 1 & "number <word>" & 3: scan tokens left-to-right.
    for i, tok in enumerate(tokens):
        if tok in _ORDINAL_WORDS:
            if _is_time_context(i):
                continue  # "six pm" — a time, not position six
            return _ORDINAL_WORDS[tok]
        if tok.isdigit():
            if _is_time_context(i) or ":" in tok:
                continue  # "6 pm" / "5:30" — a time, not a position
            val = int(tok)
            if 1 <= val <= 99:
                return val
    return None


#: Position-aware action keywords -> canonical ordinal-position speak label.
_POS_LABELS: dict[int, str] = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
    6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
}


def _pos_label(position: int) -> str:
    """Spoken ordinal label for a position ("first", "second", … or "Nth")."""
    return _POS_LABELS.get(position, f"{position}th")


#: Field-name words -> canonical field. Used to (a) detect which field an edit
#: targets and (b) recognise a whole-field SET ("change the body to ...") so it
#: is not misread as a find-and-replace.
_FIELD_WORDS: dict[str, str] = {
    "body": "body",
    "message": "body",
    "content": "body",
    "text": "body",
    "subject": "subject",
    "title": "subject",
    "recipient": "to",
    "recipients": "to",
    "address": "to",
    "to": "to",
}

#: A leading verb that introduces an in-place edit of the wording.
_REPLACE_VERBS: tuple[str, ...] = ("change", "replace", "swap", "correct", "fix")

#: "change <search> to <replacement>" / "swap <search> for <replacement>" /
#: "replace <search> with <replacement>". Non-greedy search, greedy replacement
#: so the LAST connector ("to"/"with"/"for") splits the two operands. The
#: operands arrive already lowercased and punctuation-stripped by ``_normalize``.
_REPLACE_TO_RE = re.compile(
    r"\b(?:change|correct|fix|set|make|update|rename)\s+(?P<search>.+?)\s+to\s+(?P<repl>.+)$"
)
_REPLACE_WITH_RE = re.compile(
    r"\b(?:replace|change|swap)\s+(?P<search>.+?)\s+with\s+(?P<repl>.+)$"
)
_SWAP_FOR_RE = re.compile(
    r"\b(?:swap|change)\s+(?P<search>.+?)\s+for\s+(?P<repl>.+)$"
)

#: A trailing position clause that may follow a field name in a whole-field SET,
#: e.g. "change the subject OF REPLY 2 to ..." / "of the first email". Stripped
#: when deciding whether the operand names (only) a field.
_TRAILING_POSITION_CLAUSE_RE = re.compile(
    r"\s+of\s+(?:the\s+)?"
    r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"1st|2nd|3rd|\d+|reply|email|approval|message)"
    r"(?:\s+(?:reply|email|approval|message))?"
    r"(?:\s+(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"1st|2nd|3rd|\d+))?$"
)

#: Phrases we strip from the FRONT of a captured operand so incidental framing
#: ("the word hi there", "it to", leading "the") does not pollute the match.
_OPERAND_PREFIXES: tuple[str, ...] = (
    "the words ",
    "the word ",
    "the phrase ",
    "the text ",
    "the line ",
)


def _clean_operand(text: str) -> str:
    """Trim incidental framing words from a captured search/replacement operand.

    Drops a leading "the word(s)/phrase/text/line " qualifier and surrounding
    whitespace. Pure; returns "" when nothing meaningful remains.
    """
    t = text.strip()
    for prefix in _OPERAND_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    return t


def _field_prefix_len(search: str) -> int:
    """If ``search`` begins with a field reference, return how many words it spans.

    Recognises "the body", "body", "the subject", "recipient", "the to field",
    etc. at the START of the search operand — used to tell a whole-field SET
    ("change the body to hello") apart from a find-and-replace ("change hi there
    to hello"). Returns the number of leading words that name the field, or 0
    when the operand does not start with a field reference.
    """
    words = search.split()
    if not words:
        return 0
    idx = 0
    if words[idx] == "the":
        idx += 1
    if idx < len(words) and words[idx] in _FIELD_WORDS:
        consumed = idx + 1
        # Allow a trailing "field" ("the to field").
        if consumed < len(words) and words[consumed] == "field":
            consumed += 1
        return consumed
    return 0


def _classify_replace_action(normalized: str) -> IntentResult | None:
    """Classify an in-place field edit ("change X to Y" / "replace X with Y").

    Deterministic and conservative. ``normalized`` is lowercased and
    punctuation-stripped (so spoken quotes around "Hi there" are already gone).
    Distinguishes two shapes:

      - Whole-field SET — the operand names a field ("change the body to hello
        there", "set the subject to support request"): returns
        ``set_field_position`` with ``field`` + ``replacement`` (the new value).
      - Find-and-replace — the operand is arbitrary wording ("change hi there to
        hello payrogen", "replace regards with best wishes"): returns
        ``replace_in_field_position`` with ``search`` + ``replacement``. The
        concrete field is resolved at dispatch time from the LAST edit target
        (defaults to body).

    A leading position/field reference ("in the first email, change ...", "in
    the body change ...") is tolerated: the position and field are parsed and
    stripped before matching so the operands stay clean. Returns ``None`` when
    the utterance is not clearly an in-place edit.
    """
    text = normalized

    # Parse an optional position anywhere in the utterance (e.g. "in the first
    # email change ..."). Kept even if the verb clause omits it.
    position = _parse_position(text)

    # Parse an optional leading field scope: "in the body", "in the subject",
    # "on the recipient". Captured so a bare "change hi to hello" after "edit the
    # body" still lands on the body (resolved at dispatch from _last_edit_target),
    # while an explicit "in the subject change ..." pins the field here.
    scoped_field: str | None = None
    m_scope = re.match(
        r"^(?:in|on|within|inside)\s+the\s+(\w+)\b(.*)$", text
    )
    if m_scope is not None:
        candidate = m_scope.group(1)
        if candidate in _FIELD_WORDS:
            scoped_field = _FIELD_WORDS[candidate]
            # Drop the "in the <field>" scope prefix so the verb clause is clean.
            text = m_scope.group(2).strip()
            # A leading connector ("in the body, then change ...") -> drop stray
            # "and"/"then"/comma remnants.
            text = re.sub(r"^(?:and|then|,)\s+", "", text).strip()

    # Strip a leading position clause so the verb leads the remainder. Covers
    # "for the Nth reply", "in the first email", and the "of the first reply"
    # tail left after an "in the body of the first reply" scope strip.
    text = re.sub(
        r"^(?:for|in|on|of)\s+(?:the\s+)?"
        r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
        r"1st|2nd|3rd|\d+)\s+(?:email|reply|approval|message)\s*[, ]*",
        "",
        text,
    ).strip()
    # Also strip a bare leading "of the Nth reply" with the ordinal AFTER the
    # noun or a dangling "of the ... reply" remnant.
    text = re.sub(
        r"^of\s+(?:the\s+)?(?:reply|email|approval|message)\s*[, ]*",
        "",
        text,
    ).strip()

    for pattern in (_REPLACE_WITH_RE, _SWAP_FOR_RE, _REPLACE_TO_RE):
        m = pattern.match(text) or pattern.search(text)
        if m is None:
            continue
        search = _clean_operand(m.group("search"))
        replacement = _clean_operand(m.group("repl"))
        if not search or not replacement:
            continue

        # Whole-field SET: the search operand is (only) a field reference,
        # optionally followed by a position clause, e.g. "change the body to
        # hello there" or "change the subject of reply 2 to weekly update".
        search_core = _TRAILING_POSITION_CLAUSE_RE.sub("", search).strip()
        fp = _field_prefix_len(search_core)
        if fp and fp == len(search_core.split()):
            field = None
            for w in search_core.split():
                if w in _FIELD_WORDS:
                    field = _FIELD_WORDS[w]
                    break
            field = scoped_field or field or "body"
            label = {"to": "recipient", "subject": "subject", "body": "body"}[field]
            return IntentResult(
                kind="set_field_position",
                path=None,
                speak=f"Setting the {label} of the {_pos_label(position or 1)} reply.",
                position=position,
                field=field,
                search=None,
                replacement=replacement,
            )

        # Find-and-replace within a field (field resolved at dispatch time,
        # defaulting to the last-edited field / body).
        return IntentResult(
            kind="replace_in_field_position",
            path=None,
            speak=f"Changing \u201c{search}\u201d to \u201c{replacement}\u201d.",
            position=position,
            field=scoped_field,
            search=search,
            replacement=replacement,
        )

    # TRAILING CONNECTOR, no replacement yet: "change high there to" / "replace
    # regards with" / "in the body change hi there to". The user paused before
    # (or between) turns; buffer the search and prompt for the replacement. This
    # runs BEFORE the field-focus branch (via _classify_position_action calling
    # us first) so "in the body change hi there to" is an EDIT, not a focus.
    mt = _REPLACE_TRAILING_CONNECTOR_RE.match(text)
    if mt is not None:
        search = _clean_operand(mt.group("search"))
        # Refuse a bare field reference ("change the body to") — that is a
        # whole-field set/focus, not a text search.
        if search and not _is_field_or_position_operand(search):
            return IntentResult(
                kind="edit_search_pending",
                path=None,
                speak=f"What should I change \u201c{search}\u201d to?",
                position=position,
                field=scoped_field,
                search=search,
                replacement=None,
            )

    # SEARCH-ONLY, no connector at all: "change hi" (the user paused after
    # naming part of the text). Buffer the partial search; a following turn
    # either EXTENDS it and/or supplies the replacement ("there to hello").
    # Guarded so a bare field reference ("change the body") or a positional edit
    # ("change the first reply") does NOT match — those are focus/positional
    # edits handled later in _classify_position_action.
    ms = _REPLACE_SEARCH_ONLY_RE.match(text)
    if ms is not None:
        search = _clean_operand(ms.group("search"))
        if search and not _is_field_or_position_operand(search):
            return IntentResult(
                kind="edit_search_pending",
                path=None,
                speak=f"What should I change \u201c{search}\u201d to?",
                position=position,
                field=scoped_field,
                search=search,
                replacement=None,
            )

    return None


def _is_field_or_position_operand(operand: str) -> bool:
    """Whether a replace operand actually names a FIELD or a POSITION.

    Such operands ("the body", "the subject of reply 2", "the first reply") are
    field-focus / positional edits, NOT literal text to search for — so the
    fragmented-search buffering must decline them. Pure.
    """
    words = operand.split()
    word_set = set(words)
    if word_set & set(_FIELD_WORDS):
        return True
    if word_set & {
        "reply", "replies", "email", "emails", "approval", "approvals", "message",
    }:
        return True
    if _parse_position(operand) is not None:
        return True
    return False


def _classify_fragmented_search(normalized: str) -> IntentResult | None:
    """Classify the SEARCH half of a fragmented edit ("change the text hi there").

    Fires only when the utterance is a replace verb ("change"/"replace"/…) plus
    real wording to find, with NO "to/with" connector yet (the replacement comes
    in a later turn). Deliberately checked LAST — after the field-scoped and
    positional edit classifiers — so "change the subject of reply 2" (a field
    focus) and "change the first reply" (a positional edit) win first. It refuses
    when the operand is a field reference or contains a position/field word, so
    it only captures a genuine text search. Returns ``edit_search_pending`` or
    ``None``.
    """
    m = _REPLACE_SEARCH_ONLY_RE.match(normalized)
    if m is None:
        return None
    search = _clean_operand(m.group("search"))
    if not search:
        return None
    words = search.split()
    word_set = set(words)
    # Refuse when the operand references a FIELD or a POSITION — those are
    # focus/positional edits handled by the position classifier, not a text
    # search. (e.g. "the recipient of the first reply", "the subject of reply 2",
    # "the first reply".)
    if word_set & set(_FIELD_WORDS):
        return None
    if word_set & {
        "reply", "replies", "email", "emails", "approval", "approvals", "message",
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
        "eighth", "ninth", "tenth",
    }:
        return None
    if _parse_position(search) is not None:
        return None
    return IntentResult(
        kind="edit_search_pending",
        path=None,
        speak=f"What should I change \u201c{search}\u201d to?",
        search=search,
        replacement=None,
    )


#: A replace verb + search operand ending in a DANGLING connector with nothing
#: after it: "change high there to" / "replace regards with" / "swap x for".
#: The user named the search and paused; the replacement arrives next turn. The
#: trailing connector is consumed by the regex so it never leaks into `search`.
_REPLACE_TRAILING_CONNECTOR_RE = re.compile(
    r"^(?:change|correct|fix|set|make|update|rename|replace|swap)\s+"
    r"(?P<search>.+?)\s+(?:to|with|into|for)\s*$"
)

#: A replace verb + search operand with NO "to/with/for <replacement>" yet, e.g.
#: "change the text hi there" / "change hi there" / "replace regards". Captures
#: just the search; the replacement arrives in a later turn (see
#: ``edit_replacement`` continuation). Anchored to a leading replace verb so it
#: never fires on arbitrary speech.
_REPLACE_SEARCH_ONLY_RE = re.compile(
    r"^(?:change|correct|fix|replace|swap)\s+(?P<search>.+)$"
)

#: A standalone replacement continuation the user speaks AFTER a pending search.
#: Two shapes:
#:   * a bare connector: "to hi payroll" / "with best wishes" / "into cheers";
#:   * an "it" pronoun form: "change it to hi payroll" / "make it hi payroll" /
#:     "set it to hi payroll" (the pronoun refers to the buffered search).
#: Captures the replacement text. Anchored so it never swallows ordinary speech.
_REPLACEMENT_CONT_RE = re.compile(
    r"^(?:change|make|set|replace)\s+it\s+(?:to|with|into|for)?\s*(?P<repl>.+)$"
    r"|^(?:to|with|into)\s+(?P<repl2>.+)$"
)


def _classify_replacement_continuation(normalized: str) -> IntentResult | None:
    """Classify a standalone replacement continuation ("to hi payroll"), or None.

    Fires only on an explicit connector form ("to Y", "with Y", "change it to
    Y", "make it Y") so it never swallows ordinary speech. The gateway only
    honours it when a pending search from a prior ``edit_search_pending`` turn is
    buffered; otherwise it is a harmless no-op there. Returns an
    ``edit_replacement`` result carrying the replacement text.
    """
    m = _REPLACEMENT_CONT_RE.match(normalized)
    if m is None:
        return None
    replacement = _clean_operand(m.group("repl") or m.group("repl2") or "")
    if not replacement:
        return None
    return IntentResult(
        kind="edit_replacement",
        path=None,
        speak="",  # the gateway speaks the applied-change confirmation
        replacement=replacement,
    )


#: Reschedule phrasings: the user changes an ALREADY-CHOSEN send time.
#:   "change the schedule to 6pm", "change the time to seven am",
#:   "reschedule to tomorrow at 5", "set the time to five pm", "make the
#:   schedule september 16th".
_RESCHEDULE_RE = re.compile(
    r"^(?:(?:change|set|update|make|move|push)\s+(?:the\s+)?"
    r"(?:schedule|scheduled\s+time|send\s+time|time|date)\s+"
    r"(?:to|for|back\s+to)\s+(?P<when1>.+))$"
    r"|^(?:reschedule|re-schedule)\s+(?:it\s+|the\s+\w+\s+reply\s+)?"
    r"(?:to|for)?\s*(?P<when2>.+)$"
)


def _classify_reschedule(normalized: str) -> IntentResult | None:
    """Classify a change to an already-chosen SEND TIME ("change the time to 6pm").

    Returns ``reschedule_position`` carrying the parsed ``when_iso`` (via
    :func:`voice_datetime.parse_spoken_datetime`) when a new time resolves, or a
    ``reschedule_position`` with ``when_iso=None`` (the gateway will re-open the
    picker and ask) when the verb is present but no time parses. Position is left
    to the gateway (defaults to the reply currently being scheduled). ``None``
    when this is not a reschedule request. Pure.
    """
    m = _RESCHEDULE_RE.match(normalized)
    if m is None:
        return None
    when_text = (m.group("when1") or m.group("when2") or "").strip()
    if not when_text:
        return None
    when_iso = voice_datetime.parse_spoken_datetime(when_text, now=_now())
    return IntentResult(
        kind="reschedule_position",
        path=None,
        speak="Updating the send time.",
        position=None,
        when_iso=when_iso,
    )


def _classify_position_action(normalized: str) -> IntentResult | None:
    """Classify a position-aware approval/reply action, or ``None``.

    Deterministic and conservative. ``normalized`` is lowercased and
    punctuation-stripped. Recognizes (with an optional Nth position, defaulting
    to position 1 when a clear single-item action omits it):

      - read_position:           read / read out / read me (the) (Nth)
                                  email|reply|approval, and "reply to the Nth".
      - regenerate_position:     regenerate|generate|rewrite|write again (Nth).
      - edit_position:           edit|change|modify (Nth).
      - approve_draft_position:  any draft phrasing (approve and save/send to
                                 draft, save as draft, approve and draft, save
                                 it to draft) — the word "draft" forces draft.
      - approve_send_position:   approve and send / send it (WITHOUT "draft").
      - approve_schedule_position: approve and schedule / schedule it, plus an
                                 optional spoken time parsed to ISO.

    Returns ``None`` for anything that is not clearly one of these.
    """
    words = normalized.split()
    word_set = set(words)
    position = _parse_position(normalized)

    # --- Reschedule ("change the schedule/time to 6pm", "reschedule to 7pm") —
    #     checked BEFORE the generic edit so "change the time to six pm" updates
    #     the SEND TIME rather than being read as a body find-and-replace (and so
    #     "six pm" is never mistaken for reply position 6). ---
    resched = _classify_reschedule(normalized)
    if resched is not None:
        return resched

    # --- In-place field edit ("change X to Y" / "replace X with Y") — checked
    #     FIRST so a spoken wording edit is performed server-side instead of
    #     falling through to a focus/whole-reply edit (ERROR.md). ---
    replace = _classify_replace_action(normalized)
    if replace is not None:
        return replace

    has_draft = "draft" in word_set or "drafts" in word_set
    has_send = "send" in word_set
    has_schedule = "schedule" in word_set
    has_approve = "approve" in word_set

    # --- Reject / decline / discard a SINGLE reply (the Reject button). Bulk
    #     "reject all" is caught earlier by _is_clear_all, so anything reaching
    #     here is a single-item reject. Fires on a leading reject verb (with or
    #     without an explicit position; defaults to the first). ---
    _reject_verb = (
        "reject" in word_set or "decline" in word_set or "discard" in word_set
        or "dismiss" in word_set or "trash" in word_set
    )
    if _reject_verb and normalized.split()[0] in (
        "reject", "decline", "discard", "dismiss", "trash"
    ):
        pos = position or 1
        return IntentResult(
            kind="reject_position",
            path=None,
            speak=f"Rejecting the {_pos_label(pos)} reply.",
            position=pos,
        )

    # --- Scheduling: highest-priority among the approve family so "approve and
    #     schedule ... send later" doesn't fall into the send branch. ---
    if has_schedule and (has_approve or "it" in word_set or "reply" in word_set
                         or "this" in word_set or normalized.startswith("schedule")):
        pos = position or 1
        when_iso = voice_datetime.parse_spoken_datetime(normalized, now=_now())
        return IntentResult(
            kind="approve_schedule_position",
            path=None,
            speak=f"Scheduling the {_pos_label(pos)} reply.",
            position=pos,
            when_iso=when_iso,
        )

    # --- Draft: the word "draft" ALWAYS wins over send. Covers "approve and
    #     save to draft", "approve and send to draft", "save as draft",
    #     "approve and draft", "save it to draft". ---
    if has_draft and (has_approve or has_send or "save" in word_set):
        pos = position or 1
        return IntentResult(
            kind="approve_draft_position",
            path=None,
            speak=f"Saving the {_pos_label(pos)} reply to drafts.",
            position=pos,
        )

    # --- Send (without the word "draft"). ---
    if has_send and (has_approve or "it" in word_set or "reply" in word_set
                    or normalized.startswith("send")):
        pos = position or 1
        return IntentResult(
            kind="approve_send_position",
            path=None,
            speak=f"Sending the {_pos_label(pos)} reply.",
            position=pos,
        )

    # --- Field-specific editing: "edit/change the To/subject/body of reply N",
    #     "change the recipient", "edit the subject". Emits focus_field_position
    #     so the UI opens the editor and focuses that field for dictation. The
    #     BODY, additionally, supports regeneration below (regenerate the body).
    #     Field detection: recipient/to -> to; subject -> subject; body/message
    #     /content -> body. ---
    _edit_verb = ("edit" in word_set or "change" in word_set
                  or "modify" in word_set or "update" in word_set
                  or "focus" in word_set)
    _field = None
    if "subject" in word_set:
        _field = "subject"
    elif "body" in word_set or "message" in word_set or "content" in word_set:
        _field = "body"
    elif "recipient" in word_set or "recipients" in word_set or "address" in word_set:
        _field = "to"
    elif "to" in word_set and _edit_verb and ("field" in word_set or "the to" in normalized):
        _field = "to"

    # Body + a generate/rephrase/rewrite verb => regenerate the body.
    _regen_verb = ("regenerate" in word_set or "generate" in word_set
                   or "rephrase" in word_set or "rewrite" in word_set
                   or ("write" in word_set and ("again" in word_set or "new" in word_set)))
    if _field == "body" and _regen_verb:
        pos = position or 1
        return IntentResult(
            kind="regenerate_body_position",
            path=None,
            speak=f"Regenerating the body of the {_pos_label(pos)} reply.",
            position=pos,
            field="body",
        )

    # Edit/change/focus a specific field (To / Subject / Body) for dictation.
    if _field is not None and _edit_verb:
        pos = position or 1
        label = {"to": "recipient", "subject": "subject", "body": "body"}[_field]
        return IntentResult(
            kind="focus_field_position",
            path=None,
            speak=f"Editing the {label} of the {_pos_label(pos)} reply. Go ahead.",
            position=pos,
            field=_field,
        )

    # --- Regenerate / generate / rewrite (treated identically). ---
    if ("regenerate" in word_set or "generate" in word_set
            or "rewrite" in word_set
            or (("write" in word_set) and ("again" in word_set or "new" in word_set))):
        pos = position or 1
        return IntentResult(
            kind="regenerate_position",
            path=None,
            speak=f"Regenerating the {_pos_label(pos)} reply.",
            position=pos,
        )

    # --- Edit / change / modify a reply. ---
    if ("edit" in word_set or "modify" in word_set
            or ("change" in word_set and ("reply" in word_set or "email" in word_set
                or position is not None))):
        pos = position or 1
        return IntentResult(
            kind="edit_position",
            path=None,
            speak=f"Editing the {_pos_label(pos)} reply.",
            position=pos,
        )

    # --- Read: "read (the) (Nth) email|reply|approval" and "reply to the Nth".
    #     Reading the original + proposed reply is the read action.
    #
    #     Disambiguation from a LIST-ALL request (which the model handles):
    #       * An explicit position ("read the first reply") -> read_position.
    #       * A singular "the reply" / "the email" / "the approval" with NO
    #         plural/"my" list qualifier ("read the reply") -> read_position=1.
    #       * "read my unread emails" / "read my pending approvals" (plural,
    #         "my", no position) is a LIST request -> None (fall through). ---
    starts_read = normalized.startswith("read")
    reply_to = normalized.startswith("reply to") or normalized == "reply"

    if reply_to:
        # "reply to the first" / "reply to the second reply" — reading the
        # original + proposed reply is the read action. Only fires when the
        # phrase clearly targets a POSITION (an ordinal/number is present) so a
        # named "reply to Camilia" stays conversation (None).
        if position is not None:
            return IntentResult(
                kind="read_position",
                path=None,
                speak=f"Reading the {_pos_label(position)} reply.",
                position=position,
            )
        return None

    if starts_read:
        singular_target = (
            (" the reply" in f" {normalized}")
            or (" the email" in f" {normalized}")
            or (" the approval" in f" {normalized}")
            or (" the message" in f" {normalized}")
        )
        list_qualifier = (
            "my" in word_set
            or "unread" in word_set
            or "pending" in word_set
            or "emails" in word_set
            or "approvals" in word_set
            or "replies" in word_set
        )
        if position is not None or (singular_target and not list_qualifier):
            pos = position or 1
            return IntentResult(
                kind="read_position",
                path=None,
                speak=f"Reading the {_pos_label(pos)} reply.",
                position=pos,
            )

    # --- Fragmented in-place edit, SEARCH half ("change the text hi there").
    #     Checked LAST so a field focus ("change the subject") or a positional
    #     edit ("change the first reply") wins first; only a genuine text search
    #     with no field/position words reaches here. ---
    frag = _classify_fragmented_search(normalized)
    if frag is not None:
        return frag

    return None


def _now() -> datetime:
    """Local wall-clock now (naive). Isolated so schedule parsing has an anchor.

    Kept tiny and separate so tests can monkeypatch it if needed; the gateway
    does not rely on this — it re-parses the time server-side with an injected
    ``now`` — but a classifier default is convenient for a spoken-time preview.
    """
    return datetime.now()


def classify(text: str) -> IntentResult | None:
    """Classify a spoken utterance into a navigation / help intent, or ``None``.

    Resolution (pure, deterministic):
      1. Help intents (exact normalized match) → ``help``.
      2. A leading navigation verb whose remainder resolves via
         ``voice_tools._resolve_nav_target`` → ``navigate`` (with the route and a
         short "Opening <Destination>." spoken confirmation).
      3. A leading navigation verb whose remainder does *not* resolve →
         ``unknown_destination`` (speaks the available-destinations sentence).
      4. No leading navigation verb, but the utterance is itself a bare
         destination reference that resolves ("approvals", "team", "workspace",
         "members", "approvals menu") → ``navigate``. This covers a user who
         just names a place. Utterances that lead with a *non-navigation* verb
         (read / reply / approve / send / …) are treated as conversation even
         when a destination word appears later, so they fall through to step 5.
      5. Everything else (ordinary conversation) → ``None``.

    Being conservative — only firing on a clear leading verb or a bare utterance
    that resolves cleanly to one of the five destinations — is intentional so
    that conversational utterances never trigger a false navigation.
    """
    if not isinstance(text, str):
        return None

    normalized = _normalize(text)
    if not normalized:
        return None

    # 1. Help intents.
    if normalized in _HELP_PHRASES:
        return IntentResult(kind="help", path=None, speak=_HELP_SPEAK)

    # 1a. Bulk-clear ALL pending replies ("clear all replies", "delete all
    #     drafts", "reject all", "clear everything"). Requires an all/every word
    #     + a clear verb, so a single "reject the first" never triggers it.
    if _is_clear_all(normalized):
        return IntentResult(kind="clear_all_pending", path=None, speak=_CLEAR_SPEAK)

    # 1b. Cancel / never-mind. Handled deterministically so the model does not
    #     apologise that "cancelling is not supported" (ERROR.md Task 1).
    if _is_cancel(normalized):
        return IntentResult(kind="cancel", path=None, speak=_CANCEL_SPEAK)

    # 1c. List / read-aloud a collection (pending approvals, unread inbox).
    #     Checked BEFORE navigation so "list my pending approvals" does not fall
    #     into the bare-destination navigate fallback (a mention of "approvals").
    #     The router owns listing so the brittle model list->read->act tool chain
    #     is never needed (ERROR.md).
    listing = _classify_list_request(normalized)
    if listing is not None:
        return listing

    # 2/3. Navigation with an explicit leading verb.
    remainder = _strip_leading_nav_verb(normalized)
    if remainder is not None:
        path = voice_tools._resolve_nav_target(remainder) if remainder else None
        if path is not None:
            return _navigate_result(path, remainder)
        # A navigation verb was present but the target does not resolve to one of
        # the five destinations: refuse rather than guess (Req 1.7).
        return IntentResult(
            kind="unknown_destination", path=None, speak=NAV_DESTINATIONS_SENTENCE
        )

    # 3a. Replacement CONTINUATION of a fragmented edit ("to hi payroll" /
    #     "change it to hi payroll") spoken after a "change the text X" turn.
    #     Checked BEFORE the position-action step so "change it to Y" resolves as
    #     a continuation (the pronoun "it" refers to the buffered search) rather
    #     than a literal find-and-replace of the word "it". Only an explicit
    #     connector / "it"-pronoun form matches; the gateway applies it when a
    #     pending search is buffered, else it is a harmless no-op.
    continuation = _classify_replacement_continuation(normalized)
    if continuation is not None:
        return continuation

    # 3b. Position-aware approval/reply actions (read / regenerate / edit /
    #     approve+draft / approve+send / approve+schedule the Nth reply). These
    #     lead with a non-navigation verb, so they are checked before the bare
    #     destination fallback and never trigger a false navigation.
    action = _classify_position_action(normalized)
    if action is not None:
        return action

    # 4. No navigation verb — accept only a bare utterance that itself names a
    #    destination (e.g. "approvals", "team members", "approvals menu").
    #    The resolver matches a destination word appearing ANYWHERE in the
    #    phrase (correct for the model-driven tool path), so we must guard
    #    against conversational sentences that merely mention a destination word:
    #    an utterance that leads with a non-navigation verb ("read my pending
    #    approvals", "reply to Camilia", "send this email") is conversation, not
    #    a bare destination reference, and must classify as None.
    if not _starts_with_non_nav_verb(normalized):
        path = voice_tools._resolve_nav_target(normalized)
        if path is not None:
            return _navigate_result(path, normalized)

    # 5. Everything else: not an intent we act on.
    return None


def _navigate_result(path: str, remainder: str) -> IntentResult:
    """Build a ``navigate`` :class:`IntentResult` for a resolved route (pure)."""
    label = _ROUTE_LABELS.get(path, remainder)
    return IntentResult(kind="navigate", path=path, speak=f"Opening {label}.")


# ---------------------------------------------------------------------------
# Spoken guidance and directives
# ---------------------------------------------------------------------------

#: Spoken sentence listing the available navigation destinations, used when a
#: navigation verb targets something outside the known five.
NAV_DESTINATIONS_SENTENCE: str = (
    "I can take you to Approvals, Team, Integrations, Rules, or Admin. "
    "Which one would you like?"
)

#: Directive injected once when a session becomes ready. Instructs the model to
#: greet the user in its own voice and state what it can do. Authored for
#: text-to-speech: short, natural, no ids or tool names.
WELCOME_DIRECTIVE: str = (
    "The voice session is now ready. Begin your greeting with the exact phrase "
    "\"Welcome to Atomic AI.\" and then greet the user warmly in one or two "
    "short sentences. Tell them you can navigate the application, read their "
    "emails and approvals aloud, and type on their behalf. Invite them to say "
    "'help' at any time to hear everything they can do. Speak this greeting now."
)


def build_guidance_script() -> str:
    """Return the spoken guidance script (pure).

    Covers, in natural spoken language: every one of the five destinations with
    an example phrase, how to have unread emails and pending approvals read
    aloud, and how to trigger each of the five gated actions. Contains no
    internal tool names.
    """
    return (
        "Here's what you can do. "
        "To move around, just tell me where to go. "
        "Say 'go to Approvals' to open your approvals. "
        "Say 'go to Team' to open your team. "
        "Say 'go to Integrations' to open your integrations. "
        "Say 'go to Rules' to open your rules. "
        "Say 'go to Admin' to open the admin area. "
        "To hear your messages, say 'read my unread emails' and I'll read them "
        "aloud. To hear what's waiting on you, say 'read my pending approvals' "
        "and I'll read each one. "
        "When you're reviewing a reply, you can tell me what to do with it. "
        "Say 'approve and save to draft' to keep it as a draft. "
        "Say 'approve and send' to send it right away. "
        "Say 'approve and schedule' to send it at a later time. "
        "Say 'reject' to decline a reply. "
        "Say 'regenerate' and I'll write a fresh version for you to review. "
        "Say 'edit' to change the recipient, subject, or wording. To change a "
        "phrase, say for example 'change hi there to hello'. "
        "And any time you're not sure, just say 'help' and I'll go through this "
        "again."
    )
