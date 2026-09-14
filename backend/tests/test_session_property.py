"""Property-based tests for session validity and expiry bounds.

These tests exercise the pure decision functions in ``app.core.security``:

- :func:`issue_session_token` — expiry must be bounded to 24 hours (Req 2.3).
- :func:`is_session_valid` — the validity decision for present/revoked/expired
  and missing sessions (Req 2.2, 2.4).

All datetimes are kept timezone-aware so aware-to-aware comparisons never raise
``TypeError``. Each property runs at least 100 examples per the design's
property-testing budget.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.security import SESSION_TTL, is_session_valid, issue_session_token


# ---------------------------------------------------------------------------
# Test doubles / strategies
# ---------------------------------------------------------------------------


@dataclass
class SessionRecord:
    """Minimal session record satisfying the ``SessionRecord`` protocol.

    Exposes exactly the two attributes :func:`is_session_valid` reads, keeping
    the property test free of any ORM dependency.
    """

    revoked: bool
    expires_at: datetime


# Timezone-aware datetimes: generate naive datetimes over a wide but valid range
# and attach UTC so every comparison in the tests is aware-to-aware.
_aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Property 9: Session expiry is bounded to 24 hours (Req 2.3)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(now=_aware_datetimes)
def test_session_expiry_bounded_to_24h(now: datetime) -> None:
    """issue_session_token returns an expiry within (now, now + 24h].

    **Validates: Requirements 2.3**

    The implementation sets ``expires_at = now + SESSION_TTL`` (exactly 24h), so
    we assert both the exact equality and the ``now < expires_at <= now + 24h``
    bound that Req 2.3 demands.
    """
    _raw_token, expires_at = issue_session_token(uuid.uuid4(), now)

    upper_bound = now + timedelta(hours=24)
    assert SESSION_TTL == timedelta(hours=24)
    assert expires_at == upper_bound
    assert now < expires_at
    assert expires_at <= upper_bound


# ---------------------------------------------------------------------------
# Property 8: Session validity decision (Req 2.2, 2.4)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(now=_aware_datetimes, expires_at=_aware_datetimes, revoked=st.booleans())
def test_session_validity_decision(
    now: datetime, expires_at: datetime, revoked: bool
) -> None:
    """A present session is valid iff not revoked and now < expires_at.

    **Validates: Requirements 2.2, 2.4**
    """
    record = SessionRecord(revoked=revoked, expires_at=expires_at)
    expected = (not revoked) and (now < expires_at)
    assert is_session_valid(record, now) == expected


@settings(max_examples=200)
@given(now=_aware_datetimes)
def test_missing_session_is_never_valid(now: datetime) -> None:
    """A missing (``None``) session is never valid at any instant.

    **Validates: Requirements 2.2, 2.4**
    """
    assert is_session_valid(None, now) is False
