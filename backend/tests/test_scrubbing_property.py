"""Property-based tests for secret scrubbing.

Exercises :func:`app.core.scrubbing.scrub`, which returns a deep-copied
structure with any dict value redacted whenever its key matches a sensitive
pattern.

Property 19: Secret scrubbing excludes credentials and tokens.
**Validates: Requirements 6.4, 15.4**

Each property runs at least 100 examples per the design's property-testing
budget.
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.scrubbing import (
    REDACTED,
    SENSITIVE_KEY_PATTERNS,
    is_sensitive_key,
    scrub,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Leaf/scalar values that may appear as dict values or list items.
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)

# Sensitive keys: guaranteed to match ``is_sensitive_key`` because they embed a
# fragment from ``SENSITIVE_KEY_PATTERNS`` somewhere within them.
_sensitive_keys = st.builds(
    lambda prefix, fragment, suffix: f"{prefix}{fragment}{suffix}",
    st.text(alphabet="abcABC_", max_size=6),
    st.sampled_from(SENSITIVE_KEY_PATTERNS),
    st.text(alphabet="abcABC_", max_size=6),
)

# Benign keys: arbitrary text that provably does NOT match ``is_sensitive_key``.
_benign_keys = st.text(max_size=15).filter(lambda k: not is_sensitive_key(k))


def _nested(children: st.SearchStrategy) -> st.SearchStrategy:
    """Build one level of containers over ``children``.

    Dicts mix sensitive and benign keys so scrubbing has something to redact
    at every level; lists and tuples carry the nested values through.
    """
    dicts = st.dictionaries(
        keys=st.one_of(_sensitive_keys, _benign_keys),
        values=children,
        max_size=5,
    )
    lists = st.lists(children, max_size=5)
    tuples = st.lists(children, max_size=5).map(tuple)
    return st.one_of(dicts, lists, tuples)


# Arbitrary nested structures: scalars at the leaves, containers built up via
# ``st.recursive``.
_structures = st.recursive(_scalars, _nested, max_leaves=25)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_no_sensitive_value_survives(obj) -> None:
    """Walk ``obj`` asserting every sensitive key maps to ``REDACTED``."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if is_sensitive_key(key):
                assert value == REDACTED, (
                    f"sensitive key {key!r} not redacted: {value!r}"
                )
            else:
                _assert_no_sensitive_value_survives(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _assert_no_sensitive_value_survives(item)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(obj=_structures)
def test_sensitive_keys_are_redacted(obj) -> None:
    """No value under any sensitive key survives scrubbing.

    Every key matching ``is_sensitive_key`` maps to ``REDACTED`` in the
    scrubbed structure. Validates Requirements 6.4, 15.4.
    """
    scrubbed = scrub(obj)
    _assert_no_sensitive_value_survives(scrubbed)


@settings(max_examples=200)
@given(
    benign_key=_benign_keys,
    value=_structures,
)
def test_benign_keys_retain_scrubbed_values(benign_key, value) -> None:
    """Benign keys retain their recursively scrubbed value.

    A key that is not sensitive keeps ``scrub(value)`` rather than being
    redacted. Validates Requirements 6.4, 15.4.
    """
    result = scrub({benign_key: value})
    assert result[benign_key] == scrub(value)


@settings(max_examples=200)
@given(obj=_structures)
def test_input_is_not_mutated(obj) -> None:
    """Scrubbing never mutates its input.

    A deep-compared snapshot of the input is unchanged after ``scrub``.
    Validates Requirements 6.4, 15.4.
    """
    before = copy.deepcopy(obj)
    scrub(obj)
    assert obj == before


@settings(max_examples=200)
@given(obj=_structures)
def test_scrubbing_is_idempotent(obj) -> None:
    """Scrubbing twice equals scrubbing once.

    ``scrub(scrub(x)) == scrub(x)``. Validates Requirements 6.4, 15.4.
    """
    once = scrub(obj)
    twice = scrub(once)
    assert twice == once
