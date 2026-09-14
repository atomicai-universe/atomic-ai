"""Unit + property tests for the pure voice intent module.

Covers :mod:`app.services.voice_intent` — the deterministic Intent_Router and
the spoken guidance builder. Everything under test is pure (no I/O, no DB, no
AWS), so these tests run without Docker services.

Properties verified (from the voice-navigation-guidance design):

- **Property 2** — Resolver totality over the known set: every phrase in the
  destination corpus (name / "<name> menu" / "<name> page", plus the Team
  synonyms workspace, members) resolves to exactly one of the five routes,
  deterministically.
- **Property 3** — No false navigation: conversational, non-navigation
  utterances classify to ``None`` (no navigate action).
- **Property 4** — Unknown destinations are refused, not guessed: a navigation
  verb with an out-of-set target yields ``unknown_destination`` with no path.
- **Property 7** — Guidance completeness: ``build_guidance_script()`` mentions
  all five destinations, both read capabilities, and all five gated actions,
  and contains none of the internal tool names.

Also asserts the help-intent phrases classify to ``help``.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services import voice_intent, voice_tools

# ---------------------------------------------------------------------------
# Destination corpus — derived from the single source of truth in voice_tools.
# ---------------------------------------------------------------------------

#: The five Nav_Destinations mapped to their canonical route. Team is reachable
#: via several spoken names (team / workspace / members) that all resolve to the
#: same workspace route.
_DESTINATION_ROUTES: dict[str, str] = {
    "integrations": "/dashboard/integrations",
    "approvals": "/dashboard/approvals",
    "rules": "/dashboard/rules",
    "team": "/dashboard/workspace",
    "admin": "/admin",
}

#: Spoken destination names (including Team synonyms) -> expected route. These
#: are the words a user actually says to name a place.
_SPOKEN_NAME_ROUTES: dict[str, str] = {
    "integrations": "/dashboard/integrations",
    "approvals": "/dashboard/approvals",
    "rules": "/dashboard/rules",
    "team": "/dashboard/workspace",
    "workspace": "/dashboard/workspace",
    "members": "/dashboard/workspace",
    "admin": "/admin",
}

#: Leading navigation verbs the router recognizes.
_NAV_VERBS: tuple[str, ...] = (
    "open",
    "go to",
    "show",
    "navigate to",
    "take me to",
    "click",
    "bring up",
    "show me",
)

#: Suffix variants the user may append to a destination name.
_NAME_SUFFIXES: tuple[str, ...] = ("", " menu", " page")


def _corpus_phrases() -> list[tuple[str, str]]:
    """All (phrase, expected_route) pairs across name/verb/suffix combinations."""
    pairs: list[tuple[str, str]] = []
    for name, route in _SPOKEN_NAME_ROUTES.items():
        for suffix in _NAME_SUFFIXES:
            target = f"{name}{suffix}"
            # Bare destination reference (no verb).
            pairs.append((target, route))
            # With each leading navigation verb.
            for verb in _NAV_VERBS:
                pairs.append((f"{verb} {target}", route))
                pairs.append((f"{verb} the {target}", route))
    return pairs


# ---------------------------------------------------------------------------
# Property 2 — Resolver totality over the known set (Req 1.2, 1.3)
# **Validates: Requirements 1.2, 1.3**
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase,expected_route", _corpus_phrases())
def test_classify_maps_destination_corpus_to_routes(phrase, expected_route):
    """Every corpus phrase resolves to a navigate intent for the right route."""
    result = voice_intent.classify(phrase)
    assert result is not None, f"expected a navigate intent for {phrase!r}"
    assert result.kind == "navigate", f"{phrase!r} -> {result.kind}"
    assert result.path == expected_route, f"{phrase!r} -> {result.path}"
    # A navigate result always carries a short spoken confirmation.
    assert isinstance(result.speak, str) and result.speak.strip()


@pytest.mark.parametrize("phrase,expected_route", _corpus_phrases())
def test_classify_is_deterministic(phrase, expected_route):
    """Same input yields the same output (deterministic classification)."""
    first = voice_intent.classify(phrase)
    second = voice_intent.classify(phrase)
    assert first == second


@settings(max_examples=200)
@given(
    name_route=st.sampled_from(sorted(_SPOKEN_NAME_ROUTES.items())),
    verb=st.sampled_from(_NAV_VERBS),
    suffix=st.sampled_from(_NAME_SUFFIXES),
    upper=st.booleans(),
)
def test_property2_verb_plus_destination_resolves(name_route, verb, suffix, upper):
    """Property 2: verb + destination (any casing) resolves to exactly one route.

    **Validates: Requirements 1.2, 1.3**
    """
    name, route = name_route
    phrase = f"{verb} {name}{suffix}"
    if upper:
        phrase = phrase.upper()
    result = voice_intent.classify(phrase)
    assert result is not None
    assert result.kind == "navigate"
    assert result.path == route


def test_team_synonyms_resolve_to_workspace_route():
    """Team, Workspace, and Members all resolve to the Team (workspace) route."""
    for synonym in ("team", "workspace", "members"):
        result = voice_intent.classify(f"go to {synonym}")
        assert result is not None and result.kind == "navigate"
        assert result.path == "/dashboard/workspace", synonym


# ---------------------------------------------------------------------------
# Property 3 — No false navigation (Req 1.1)
# **Validates: Requirements 1.1**
# ---------------------------------------------------------------------------

_CONVERSATIONAL_UTTERANCES: tuple[str, ...] = (
    # Genuinely conversational utterances that must NOT map to any intent.
    # NOTE: phrasings such as "approve and send", "regenerate the reply",
    # "read my pending approvals", and "schedule it for tomorrow morning" are
    # now INTENTIONALLY position-aware actions (see the position-intent tests
    # below); they are deliberately excluded from this no-intent corpus.
    "how are you today",
    "what time is it",
    "tell me a joke",
    "thanks that's all",
    "can you summarize this thread",
    "who is this message from",
    "what's the weather like",
    "good morning",
    "i had a great lunch",
)


@pytest.mark.parametrize("utterance", _CONVERSATIONAL_UTTERANCES)
def test_classify_returns_none_for_conversational_utterances(utterance):
    """Property 3: ordinary conversation never triggers a navigate action.

    **Validates: Requirements 1.1**
    """
    assert voice_intent.classify(utterance) is None


def test_classify_returns_none_for_empty_and_whitespace():
    """Empty / whitespace / punctuation-only input is not an intent."""
    for blank in ("", "   ", "\t\n", "!!!", "..."):
        assert voice_intent.classify(blank) is None


# ---------------------------------------------------------------------------
# Property 4 — Unknown destinations are refused, not guessed (Req 1.7)
# **Validates: Requirements 1.7**
# ---------------------------------------------------------------------------

_UNKNOWN_TARGETS: tuple[str, ...] = (
    "the moon",
    "mars",
    "the kitchen",
    "my bank account",
    "the settings for my dog",
    "narnia",
)


@pytest.mark.parametrize("target", _UNKNOWN_TARGETS)
@pytest.mark.parametrize("verb", ["go to", "open", "take me to", "navigate to", "show"])
def test_classify_unknown_destination_for_out_of_set_target(verb, target):
    """Property 4: a nav verb + out-of-set target yields unknown_destination.

    No path is returned and the spoken text lists the available destinations.

    **Validates: Requirements 1.7**
    """
    result = voice_intent.classify(f"{verb} {target}")
    assert result is not None
    assert result.kind == "unknown_destination"
    assert result.path is None
    assert result.speak == voice_intent.NAV_DESTINATIONS_SENTENCE


# ---------------------------------------------------------------------------
# Help intents (Req 3.1)
# ---------------------------------------------------------------------------

_HELP_PHRASES: tuple[str, ...] = (
    "help",
    "what can I do",
    "what can you do",
    "options",
    "commands",
)


@pytest.mark.parametrize("phrase", _HELP_PHRASES)
def test_classify_returns_help_for_help_phrases(phrase):
    """Help-intent phrases classify to a help intent with no path."""
    result = voice_intent.classify(phrase)
    assert result is not None
    assert result.kind == "help"
    assert result.path is None
    assert isinstance(result.speak, str) and result.speak.strip()


@pytest.mark.parametrize("phrase", _HELP_PHRASES)
def test_help_phrases_are_case_and_punctuation_insensitive(phrase):
    """Help detection is robust to casing and trailing punctuation."""
    result = voice_intent.classify(f"  {phrase.upper()}!  ")
    assert result is not None and result.kind == "help"


# ---------------------------------------------------------------------------
# Property 7 — Guidance completeness (Req 3.2, 3.3, 3.4, 3.5)
# **Validates: Requirements 3.2, 3.3, 3.4, 3.5**
# ---------------------------------------------------------------------------


def test_guidance_script_mentions_all_five_destinations():
    """Property 7: the guidance names all five Nav_Destinations.

    **Validates: Requirements 3.2**
    """
    script = voice_intent.build_guidance_script().lower()
    for destination in ("approvals", "team", "integrations", "rules", "admin"):
        assert destination in script, f"guidance missing destination {destination!r}"


def test_guidance_script_mentions_both_read_capabilities():
    """Property 7: the guidance covers reading unread emails and pending approvals.

    **Validates: Requirements 3.3**
    """
    script = voice_intent.build_guidance_script().lower()
    assert "unread email" in script
    assert "pending approval" in script


def test_guidance_script_mentions_all_five_gated_actions():
    """Property 7: the guidance covers every gated action in spoken language.

    **Validates: Requirements 3.4**
    """
    script = voice_intent.build_guidance_script().lower()
    assert "approve and save to draft" in script
    assert "approve and send" in script
    assert "approve and schedule" in script
    assert "regenerate" in script
    assert "edit" in script


def test_guidance_script_contains_no_internal_tool_names():
    """Property 7: the guidance recites no internal tool names.

    **Validates: Requirements 3.5**
    """
    script = voice_intent.build_guidance_script().lower()
    for tool_name in sorted(voice_tools.VOICE_TOOL_NAMES):
        assert tool_name.lower() not in script, f"guidance leaks tool name {tool_name!r}"


# ===========================================================================
# Position-aware approval/reply intents (position -> action classification)
# ===========================================================================

from datetime import datetime

from app.services import voice_datetime


# ---------------------------------------------------------------------------
# Ordinal / position parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("first", 1),
        ("1st", 1),
        ("one", 1),
        ("number one", 1),
        ("#1", 1),
        ("number 1", 1),
        ("second", 2),
        ("2nd", 2),
        ("third", 3),
        ("fourth", 4),
        ("fifth", 5),
        ("sixth", 6),
        ("seventh", 7),
        ("eighth", 8),
        ("ninth", 9),
        ("tenth", 10),
        ("10th", 10),
        ("7", 7),
    ],
)
def test_parse_position_recognizes_ordinals_and_digits(phrase, expected):
    assert voice_intent._parse_position(voice_intent._normalize(phrase)) == expected


def test_parse_position_none_when_absent():
    for phrase in ("read the reply", "approve and send", "the email"):
        assert voice_intent._parse_position(voice_intent._normalize(phrase)) is None


# ---------------------------------------------------------------------------
# read_position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,position",
    [
        ("read the first email", 1),
        ("read out the second reply", 2),
        ("read me the third approval", 3),
        ("read the reply", 1),  # singular target, defaults to position 1
        ("reply to the first", 1),
        ("reply to the second reply", 2),
    ],
)
def test_classify_read_position(phrase, position):
    result = voice_intent.classify(phrase)
    assert result is not None, phrase
    assert result.kind == "read_position", f"{phrase} -> {result.kind}"
    assert result.position == position
    assert result.speak and "sorry" not in result.speak.lower()


@pytest.mark.parametrize(
    "phrase",
    [
        "read my unread emails",
        "read my pending approvals",
        "reply to Camilia",
    ],
)
def test_read_list_or_named_is_not_position_intent(phrase):
    """List-all reads and named replies are NOT single-item position reads."""
    result = voice_intent.classify(phrase)
    assert result is None or result.kind != "read_position"


# ---------------------------------------------------------------------------
# regenerate_position (generate == regenerate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,position",
    [
        ("regenerate the first reply", 1),
        ("generate the first reply", 1),
        ("regenerate the second", 2),
        ("rewrite the third reply", 3),
        ("regenerate", 1),  # default position
        ("write it again", 1),
    ],
)
def test_classify_regenerate_position(phrase, position):
    result = voice_intent.classify(phrase)
    assert result is not None, phrase
    assert result.kind == "regenerate_position", f"{phrase} -> {result.kind}"
    assert result.position == position


def test_generate_equals_regenerate():
    a = voice_intent.classify("generate the first reply")
    b = voice_intent.classify("regenerate the first reply")
    assert a is not None and b is not None
    assert a.kind == b.kind == "regenerate_position"
    assert a.position == b.position == 1


# ---------------------------------------------------------------------------
# edit_position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,position",
    [
        ("edit the first reply", 1),
        ("edit the second", 2),
        ("modify the third reply", 3),
        ("change the first reply", 1),
        ("edit", 1),
    ],
)
def test_classify_edit_position(phrase, position):
    result = voice_intent.classify(phrase)
    assert result is not None, phrase
    assert result.kind == "edit_position", f"{phrase} -> {result.kind}"
    assert result.position == position


# ---------------------------------------------------------------------------
# approve_draft_position — every draft phrasing, draft wins over send
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "approve and save to draft",
        "approve and send to draft",
        "approve and save as draft",
        "save it to draft",
        "approve and draft",
        "save the first to draft",
    ],
)
def test_classify_approve_draft_position(phrase):
    result = voice_intent.classify(phrase)
    assert result is not None, phrase
    assert result.kind == "approve_draft_position", f"{phrase} -> {result.kind}"
    assert result.position >= 1


def test_draft_phrasing_wins_over_send():
    """The word 'draft' forces the draft intent even alongside 'send'."""
    result = voice_intent.classify("approve and send to draft")
    assert result is not None
    assert result.kind == "approve_draft_position"


@pytest.mark.parametrize(
    "phrase,position",
    [
        ("approve and save to draft the first", 1),
        ("save the second to draft", 2),
    ],
)
def test_approve_draft_position_with_ordinal(phrase, position):
    result = voice_intent.classify(phrase)
    assert result is not None
    assert result.kind == "approve_draft_position"
    assert result.position == position


# ---------------------------------------------------------------------------
# approve_send_position — send WITHOUT the word draft
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,position",
    [
        ("approve and send", 1),
        ("approve and send the first", 1),
        ("approve and send the second", 2),
        ("send it", 1),
        ("approve and send it", 1),
    ],
)
def test_classify_approve_send_position(phrase, position):
    result = voice_intent.classify(phrase)
    assert result is not None, phrase
    assert result.kind == "approve_send_position", f"{phrase} -> {result.kind}"
    assert result.position == position


# ---------------------------------------------------------------------------
# approve_schedule_position — with and without a spoken time
# ---------------------------------------------------------------------------


def test_classify_approve_schedule_without_time():
    result = voice_intent.classify("approve and schedule the first")
    assert result is not None
    assert result.kind == "approve_schedule_position"
    assert result.position == 1


def test_classify_approve_schedule_with_time_parses_iso():
    result = voice_intent.classify(
        "approve and schedule the first at tomorrow 3pm"
    )
    assert result is not None
    assert result.kind == "approve_schedule_position"
    assert result.position == 1
    # A time was spoken, so when_iso is populated.
    assert result.when_iso is not None


def test_schedule_wins_over_send():
    """'schedule' takes priority so it isn't swallowed by the send branch."""
    result = voice_intent.classify("approve and schedule the first to send later")
    assert result is not None
    assert result.kind == "approve_schedule_position"


# ---------------------------------------------------------------------------
# speak strings never apologize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "read the first email",
        "regenerate the first reply",
        "edit the first reply",
        "approve and save to draft",
        "approve and send",
        "approve and schedule the first",
    ],
)
def test_position_intent_speak_never_apologizes(phrase):
    result = voice_intent.classify(phrase)
    assert result is not None
    lowered = result.speak.lower()
    assert "sorry" not in lowered
    assert "cannot" not in lowered
    assert "issue" not in lowered


# ===========================================================================
# parse_spoken_datetime — anchored to a fixed `now` for determinism
# ===========================================================================

_NOW = datetime(2024, 3, 4, 10, 0, 0)  # Monday 2024-03-04, 10:00 local


def _parse(text: str) -> str | None:
    return voice_datetime.parse_spoken_datetime(text, now=_NOW)


def test_parse_tomorrow_at_3pm():
    iso = _parse("tomorrow at 3pm")
    dt = datetime.fromisoformat(iso)
    assert (dt.year, dt.month, dt.day) == (2024, 3, 5)
    assert (dt.hour, dt.minute) == (15, 0)


def test_parse_today_at_9():
    # 9 with no am/pm, today. now is 10:00 so 09:00 today is past; interpret as
    # the explicit "today" day at hour 9 (day was specified).
    iso = _parse("today at 9")
    dt = datetime.fromisoformat(iso)
    assert (dt.year, dt.month, dt.day) == (2024, 3, 4)
    assert dt.hour == 9


def test_parse_at_330pm():
    iso = _parse("at 3:30pm")
    dt = datetime.fromisoformat(iso)
    assert (dt.hour, dt.minute) == (15, 30)


def test_parse_in_2_hours():
    iso = _parse("in 2 hours")
    dt = datetime.fromisoformat(iso)
    assert dt == datetime(2024, 3, 4, 12, 0, 0)


def test_parse_next_monday_at_9am():
    iso = _parse("next monday at 9am")
    dt = datetime.fromisoformat(iso)
    # now is Monday; "next monday" forces the following week.
    assert (dt.year, dt.month, dt.day) == (2024, 3, 11)
    assert (dt.hour, dt.minute) == (9, 0)


def test_parse_tonight():
    iso = _parse("tonight")
    dt = datetime.fromisoformat(iso)
    assert (dt.year, dt.month, dt.day) == (2024, 3, 4)
    assert dt.hour == 20


def test_parse_this_afternoon():
    iso = _parse("this afternoon")
    dt = datetime.fromisoformat(iso)
    assert (dt.year, dt.month, dt.day) == (2024, 3, 4)
    assert dt.hour == 15


def test_parse_plain_3pm():
    iso = _parse("3pm")
    dt = datetime.fromisoformat(iso)
    # 3pm today (15:00) is still in the future relative to now (10:00).
    assert (dt.year, dt.month, dt.day) == (2024, 3, 4)
    assert dt.hour == 15


def test_parse_bare_time_in_past_rolls_to_tomorrow():
    # 8am today is before now (10:00) and no day was specified -> next day.
    iso = _parse("8am")
    dt = datetime.fromisoformat(iso)
    assert (dt.year, dt.month, dt.day) == (2024, 3, 5)
    assert dt.hour == 8


def test_parse_returns_none_for_unparseable():
    for text in ("", "hello there", "someday", "whenever you like"):
        assert _parse(text) is None


# ---------------------------------------------------------------------------
# Cancel + bulk-clear intents (ERROR.md Task 1 / Task 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "cancel",
        "never mind",
        "nevermind",
        "stop editing",
        "cancel editing",
        "cancel editing the first email",
        "cancel that",
        "forget it",
        "discard changes",
    ],
)
def test_classify_cancel_intent(utterance):
    """Cancel / never-mind phrasings resolve to a deterministic cancel intent so
    the model does not apologise that cancelling is unsupported."""
    result = voice_intent.classify(utterance)
    assert result is not None, utterance
    assert result.kind == "cancel", (utterance, result.kind)
    assert result.speak and "sorry" not in result.speak.lower()


@pytest.mark.parametrize(
    "utterance",
    [
        "clear all replies",
        "delete all drafts",
        "clear everything",
        "reject all",
        "reject all pending",
        "clear all pending approvals",
        "discard all replies",
        "clear all my drafts",
    ],
)
def test_classify_clear_all_pending_intent(utterance):
    """Bulk-clear phrasings (with an all/every word + a clear verb) resolve to
    clear_all_pending."""
    result = voice_intent.classify(utterance)
    assert result is not None, utterance
    assert result.kind == "clear_all_pending", (utterance, result.kind)


@pytest.mark.parametrize(
    "utterance",
    [
        "reject the first",
        "reject the first reply",
        "reject reply two",
        "delete the second reply",
    ],
)
def test_single_reject_does_not_trigger_clear_all(utterance):
    """A single-item reject/delete must NOT be misread as a bulk clear."""
    result = voice_intent.classify(utterance)
    if result is not None:
        assert result.kind != "clear_all_pending", (utterance, result.kind)


# ---------------------------------------------------------------------------
# Field-specific editing + body regeneration (ERROR.md Task 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,field,pos",
    [
        ("edit the to of reply one", "to", 1),
        ("change the recipient of the first reply", "to", 1),
        ("edit the subject", "subject", 1),
        ("change the subject of reply 2", "subject", 2),
        ("edit the body of the first email", "body", 1),
    ],
)
def test_classify_focus_field(utterance, field, pos):
    """Field-targeted edits resolve to focus_field_position with the field."""
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "focus_field_position", (utterance, r.kind)
    assert r.field == field, (utterance, r.field)
    assert r.position == pos


# ---------------------------------------------------------------------------
# In-place field edits: set_field_position + replace_in_field_position
# (ERROR.md — edit the wording by position without an approval id)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,field,value,pos",
    [
        ("change the body to hello there", "body", "hello there", None),
        ("set the subject to support request", "subject", "support request", None),
        (
            "change the subject of reply 2 to weekly update",
            "subject",
            "weekly update",
            2,
        ),
    ],
)
def test_classify_set_field(utterance, field, value, pos):
    """Naming a field then 'to <value>' sets the whole field server-side."""
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "set_field_position", (utterance, r.kind)
    assert r.field == field, (utterance, r.field)
    assert r.replacement == value, (utterance, r.replacement)
    assert r.search is None
    assert r.position == pos


@pytest.mark.parametrize(
    "utterance,search,replacement",
    [
        ("change hi there to hello payrogen", "hi there", "hello payrogen"),
        ("replace regards with best wishes", "regards", "best wishes"),
        ("swap thanks for cheers", "thanks", "cheers"),
        (
            "change the word urgent to important",
            "urgent",
            "important",
        ),
    ],
)
def test_classify_replace_in_field(utterance, search, replacement):
    """A wording edit resolves to replace_in_field_position with search/replacement."""
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "replace_in_field_position", (utterance, r.kind)
    assert r.search == search, (utterance, r.search)
    assert r.replacement == replacement, (utterance, r.replacement)


def test_replace_with_explicit_field_scope():
    """'in the subject change X to Y' pins the field to subject."""
    r = voice_intent.classify("in the subject change draft to final")
    assert r is not None and r.kind == "replace_in_field_position"
    assert r.field == "subject"
    assert r.search == "draft"
    assert r.replacement == "final"


def test_replace_with_leading_position_clause():
    """'in the first email change X to Y' parses the position and the operands."""
    r = voice_intent.classify("in the first email change hi there to hello payrogen")
    assert r is not None and r.kind == "replace_in_field_position"
    assert r.position == 1
    assert r.search == "hi there"
    assert r.replacement == "hello payrogen"


@pytest.mark.parametrize(
    "utterance",
    ["regenerate the body", "rephrase the body of reply one", "rewrite the body"],
)
def test_classify_regenerate_body(utterance):
    """Body + a regenerate/rephrase/rewrite verb resolves to regenerate_body_position."""
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "regenerate_body_position", (utterance, r.kind)
    assert r.field == "body"


def test_generic_edit_without_field_still_edit_position():
    """An edit with no named field falls back to the whole-reply edit_position."""
    r = voice_intent.classify("edit the first")
    assert r is not None and r.kind == "edit_position"
    assert r.field is None


# ---------------------------------------------------------------------------
# Collection listing intents: list_pending / list_unread (ERROR.md round 2)
# The router owns listing so the model never needs the brittle list/read tools.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "read my pending approvals",
        "list my pending approvals",
        "list pending approvals",
        "what is awaiting approval",
        "how many pending approvals do i have",
        "do i have any pending replies",
        "show me my pending approvals",
        "read my replies awaiting approval",
    ],
)
def test_classify_list_pending(utterance):
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "list_pending", (utterance, r.kind)


@pytest.mark.parametrize(
    "utterance",
    [
        "read my unread emails",
        "list my unread emails",
        "check my inbox",
        "any unread emails",
        "how many unread emails do i have",
    ],
)
def test_classify_list_unread(utterance):
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "list_unread", (utterance, r.kind)


@pytest.mark.parametrize(
    "utterance",
    [
        # Bare navigation phrases must stay navigation, NOT a list read.
        "show approvals",
        "show the approvals",
        "show me approvals",
        "show approvals menu",
        "open approvals",
        "go to approvals",
        "approvals",
    ],
)
def test_bare_destination_stays_navigation_not_list(utterance):
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "navigate", (utterance, r.kind)
    assert r.path == "/dashboard/approvals"


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("read the first reply", "read_position"),
        ("approve and send the first", "approve_send_position"),
        ("edit the body of the first email", "focus_field_position"),
    ],
)
def test_positional_requests_are_not_lists(utterance, expected):
    """A request naming a position is a single-item action, never a list."""
    r = voice_intent.classify(utterance)
    assert r is not None and r.kind == expected, (utterance, r.kind if r else None)


# ---------------------------------------------------------------------------
# Single-item reject + trailing-connector fragmented edit (ERROR.md round 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,position",
    [
        ("reject", 1),
        ("reject the first", 1),
        ("reject the first reply", 1),
        ("reject reply two", 2),
        ("discard the first", 1),
        ("decline the second reply", 2),
        ("dismiss the first reply", 1),
    ],
)
def test_classify_reject_position(utterance, position):
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "reject_position", (utterance, r.kind)
    assert r.position == position


@pytest.mark.parametrize(
    "utterance",
    ["reject all", "clear all pending", "delete all drafts", "reject everything"],
)
def test_reject_all_is_still_bulk_clear(utterance):
    """'reject all' must remain a bulk clear, not a single reject."""
    r = voice_intent.classify(utterance)
    assert r is not None and r.kind == "clear_all_pending", (utterance, r.kind if r else None)


@pytest.mark.parametrize(
    "utterance,search,field",
    [
        ("change high there to", "high there", None),
        ("in the body change high there to", "high there", "body"),
        ("replace regards with", "regards", None),
        ("in the subject change draft to", "draft", "subject"),
    ],
)
def test_classify_trailing_connector_edit(utterance, search, field):
    """A 'change X to' with a dangling connector buffers the search."""
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "edit_search_pending", (utterance, r.kind)
    assert r.search == search, (utterance, r.search)
    assert r.field == field, (utterance, r.field)


@pytest.mark.parametrize(
    "utterance,field",
    [
        ("change the body to", "body"),
        ("change the subject to", "subject"),
    ],
)
def test_bare_field_with_trailing_to_is_focus_not_search(utterance, field):
    """'change the body to' (bare field ref) is a field focus, not a text search."""
    r = voice_intent.classify(utterance)
    assert r is not None and r.kind == "focus_field_position", (utterance, r.kind if r else None)
    assert r.field == field


# ---------------------------------------------------------------------------
# Reschedule + time-word-is-not-a-position (ERROR.md round 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "change the schedule to six pm",
        "change the time to seven am",
        "change the time to 7am",
        "reschedule to seven pm",
        "set the time to five pm",
        "change the schedule to september 16th at 5pm",
        "move the schedule to tomorrow at 3pm",
    ],
)
def test_classify_reschedule(utterance):
    r = voice_intent.classify(utterance)
    assert r is not None, utterance
    assert r.kind == "reschedule_position", (utterance, r.kind)
    # A concrete time resolved (not None) for all of these.
    assert r.when_iso, (utterance, r.when_iso)


@pytest.mark.parametrize(
    "utterance",
    [
        "change the schedule to six pm",
        "change the time to seven am",
        "set the time to five pm",
    ],
)
def test_time_word_is_not_a_reply_position(utterance):
    """'six pm' / 'seven am' must NOT be read as reply position 6 / 7."""
    r = voice_intent.classify(utterance)
    assert r is not None
    # reschedule carries no position (defaults to the reply being scheduled).
    assert r.position is None, (utterance, r.position)


def test_parse_position_ignores_clock_times():
    from app.services.voice_intent import _parse_position

    assert _parse_position("six pm") is None
    assert _parse_position("seven am") is None
    assert _parse_position("5:30") is None
    # But a real position still parses.
    assert _parse_position("the second reply") == 2
    assert _parse_position("reply 3") == 3


def test_reschedule_does_not_shadow_body_edits():
    """'change hi there to hello' is still a body edit, not a reschedule."""
    r = voice_intent.classify("change hi there to hello")
    assert r is not None and r.kind == "replace_in_field_position"
