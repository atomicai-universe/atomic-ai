"""Property-based tests for high-impact tool-call gating.

Exercises :func:`app.services.approval_service.is_high_impact`, the pure,
DB-free classifier that decides whether a proposed tool call is high-impact
(state-changing / outbound / destructive) and must be gated for human review.

Property 23: High-impact tool calls are gated.
**Validates: Requirements 10.1**

The documented policy: a tool name's *leading verb token* (namespace-stripped,
separator- and camelCase-aware, lowercased) is high-impact iff it is a member
of the active marker set (:data:`DEFAULT_HIGH_IMPACT_MARKERS` by default). The
properties below tie the classifier to that rule across many generated names.

Each property runs at least 100 examples per the design's property-testing
budget (``max_examples=200`` here).
"""

from __future__ import annotations

from string import ascii_letters

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.approval_service import (
    DEFAULT_HIGH_IMPACT_MARKERS,
    _leading_verb,
    is_high_impact,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A high-impact verb drawn from the default marker set.
_marker_verb = st.sampled_from(sorted(DEFAULT_HIGH_IMPACT_MARKERS))

# Read-only / safe verbs that are deliberately NOT in the marker set. Combined
# with arbitrary non-marker tokens (filtered below) to exercise the negative
# case broadly.
_SAFE_VERBS = ["get", "list", "read", "search", "fetch", "describe", "view", "count", "exists"]

# An alphabetic suffix used to build realistic multi-token tool names.
_suffix = st.text(alphabet=ascii_letters, min_size=1, max_size=12)

# How to combine a leading verb with a suffix into a concrete tool name.
_STYLES = ["under", "dash", "camel", "namespaced", "bare"]
_style = st.sampled_from(_STYLES)


def _compose(verb: str, suffix: str, style: str) -> str:
    """Build a tool name from ``verb`` + ``suffix`` in the requested ``style``.

    The verb is always the leading token so ``_leading_verb`` recovers it
    regardless of the surrounding separator/namespacing style.
    """
    if style == "under":
        return f"{verb}_{suffix}"
    if style == "dash":
        return f"{verb}-{suffix}"
    if style == "camel":
        return f"{verb}{suffix.capitalize()}"
    if style == "namespaced":
        return f"ns.{verb}_{suffix}"
    # bare
    return verb


# A token that is guaranteed NOT to be a default marker: an arbitrary
# alphabetic token whose derived leading verb is not in the marker set.
_non_marker_token = st.text(alphabet=ascii_letters, min_size=1, max_size=15).filter(
    lambda t: _leading_verb(t) not in DEFAULT_HIGH_IMPACT_MARKERS
)

# Arbitrary tool names of any shape (may or may not be high-impact), to check
# consistency with the leading-verb rule across the whole input space.
_arbitrary_name = st.text(
    alphabet=ascii_letters + "._-0123456789", min_size=0, max_size=25
)

# A small alphabet of verbs to draw custom marker rulesets from (mix of default
# markers and safe verbs so both membership outcomes are exercised).
_CUSTOM_ALPHABET = ["send", "delete", "deploy", "get", "list", "read", "ping", "sync"]
_custom_markers = st.sets(st.sampled_from(_CUSTOM_ALPHABET), max_size=len(_CUSTOM_ALPHABET))

# A JSON-ish arguments dict to confirm arguments are irrelevant to the policy.
_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_json_dict = st.dictionaries(
    st.text(alphabet=ascii_letters, min_size=1, max_size=10),
    _json_scalar,
    max_size=6,
)


# ---------------------------------------------------------------------------
# Property 23: High-impact tool calls are gated
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(verb=_marker_verb, suffix=_suffix, style=_style)
def test_marker_verbs_are_high_impact(verb: str, suffix: str, style: str) -> None:
    """Any name whose leading token is a default marker classifies high-impact.

    **Validates: Requirements 10.1**
    """
    name = _compose(verb, suffix, style)
    assert is_high_impact(name) is True


@settings(max_examples=200)
@given(
    verb=st.sampled_from(_SAFE_VERBS),
    token=_non_marker_token,
    suffix=_suffix,
    style=_style,
)
def test_non_marker_verbs_are_not_high_impact(
    verb: str, token: str, suffix: str, style: str
) -> None:
    """Read-only / non-marker leading verbs are NOT high-impact.

    Covers both a fixed set of safe verbs and arbitrary non-marker tokens.

    **Validates: Requirements 10.1**
    """
    safe_name = _compose(verb, suffix, style)
    assert is_high_impact(safe_name) is False

    token_name = _compose(token, suffix, style)
    assert is_high_impact(token_name) is False


@settings(max_examples=200)
@given(name=_arbitrary_name)
def test_consistent_with_leading_verb_rule(name: str) -> None:
    """For any name, classification equals leading-verb membership (Req 10.1).

    This ties the classifier to its documented rule across the whole input
    space, case-insensitively.

    **Validates: Requirements 10.1**
    """
    expected = _leading_verb(name).lower() in {
        m.lower() for m in DEFAULT_HIGH_IMPACT_MARKERS
    }
    assert is_high_impact(name) is expected


@settings(max_examples=200)
@given(name=_arbitrary_name, custom=_custom_markers)
def test_override_ruleset(name: str, custom: set[str]) -> None:
    """With a custom marker set, classification follows that set (Req 10.1).

    **Validates: Requirements 10.1**
    """
    expected = _leading_verb(name).lower() in {m.lower() for m in custom}
    assert is_high_impact(name, markers=custom) is expected


@settings(max_examples=200)
@given(name=_arbitrary_name, arguments=_json_dict)
def test_argument_aware_method_signal(name: str, arguments: dict) -> None:
    """Classification is the verb signal OR the argument ``method`` signal (Req 10.1).

    Generic provider tools (``{provider}_api``) carry their write intent in
    ``arguments["method"]``, so a state-changing HTTP method makes a call
    high-impact even when the tool name's leading verb is not a marker.
    Read-only methods contribute nothing beyond the verb signal.

    **Validates: Requirements 10.1**
    """
    verb_signal = _leading_verb(name).lower() in {
        m.lower() for m in DEFAULT_HIGH_IMPACT_MARKERS
    }
    method = arguments.get("method")
    method_signal = (
        isinstance(method, str)
        and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    )
    assert is_high_impact(name, arguments=arguments) is (verb_signal or method_signal)


@settings(max_examples=200)
@given(
    name=_arbitrary_name,
    method=st.sampled_from(["POST", "PUT", "PATCH", "DELETE", "post", "Patch"]),
    path=_suffix,
)
def test_state_changing_method_always_gates(name: str, method: str, path: str) -> None:
    """Any call with a state-changing HTTP method is high-impact (Req 10.1).

    Covers the generic ``{provider}_api`` write path (create draft = ``POST``,
    send = ``POST``) regardless of the tool name's leading verb.

    **Validates: Requirements 10.1**
    """
    assert is_high_impact(name, {"method": method, "path": f"/{path}"}) is True


@settings(max_examples=200)
@given(
    name=_arbitrary_name.filter(
        lambda n: _leading_verb(n).lower() not in DEFAULT_HIGH_IMPACT_MARKERS
    ),
    method=st.sampled_from(["GET", "HEAD", "OPTIONS", "get"]),
    path=_suffix,
)
def test_read_only_method_does_not_gate_non_marker(
    name: str, method: str, path: str
) -> None:
    """A read-only HTTP method does not gate a non-marker tool (Req 10.1).

    **Validates: Requirements 10.1**
    """
    assert is_high_impact(name, {"method": method, "path": f"/{path}"}) is False
