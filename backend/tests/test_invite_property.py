"""Property-based tests for the pure invite-acceptance state machine.

Implements two design properties from the Atomic AI design (task 7.4):

- **Property 16: Invite state transitions**
  **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6**
- **Property 17: Invite expiry**
  **Validates: Requirements 5.5**

These tests target the *pure*, DB-free
:func:`app.services.workspace_service.resolve_invite_acceptance` — the entire
invite acceptance decision expressed as a side-effect-free function mapping
``(current_status, expires_at, now)`` to one of ``"accept"``, ``"expired"``, or
``"already_resolved"``. Because it takes no session and performs no I/O, the
whole state machine can be exercised exhaustively at the pure layer, which is
where the transition/expiry invariants that matter actually live.

Semantics under test:

- ``PENDING`` + (no expiry OR ``now < expires_at``) -> ``"accept"`` (Req 5.2/5.3).
- ``PENDING`` + ``now >= expires_at`` -> ``"expired"`` — the expiry boundary is
  inclusive, so exactly-at-expiry resolves to expired (Req 5.5).
- ``ACCEPTED`` or ``EXPIRED`` (any non-``PENDING`` status) -> always
  ``"already_resolved"``, regardless of expiry (reuse rejection, Req 5.6).

The design mandates a minimum of 100 examples per property; the generators are
cheap so we run 200.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.db.models import InviteStatus
from app.services.workspace_service import (
    InviteDecision,
    resolve_invite_acceptance,
)


# Shared settings profile: comfortably above the design's 100-example minimum.
_PBT = settings(max_examples=200)

# The three literal decisions the function is allowed to return; used to assert
# totality (the function always returns exactly one of these).
_DECISIONS: frozenset[InviteDecision] = frozenset(
    {"accept", "expired", "already_resolved"}
)

# All invite statuses, and just the terminal (non-PENDING) ones.
_statuses = st.sampled_from(list(InviteStatus))
_terminal_statuses = st.sampled_from(
    [InviteStatus.ACCEPTED, InviteStatus.EXPIRED]
)


# Bound the datetime range so that adding/subtracting the ~10-year deltas used
# below can never overflow ``datetime.min``/``datetime.max``.
_MIN_DT = datetime(1900, 1, 1)
_MAX_DT = datetime(2200, 1, 1)


def _aware_datetimes() -> st.SearchStrategy[datetime]:
    """Timezone-aware (UTC) datetimes within a comfortably-bounded range.

    Hypothesis' ``st.datetimes`` yields naive datetimes by default; comparing a
    naive and an aware datetime raises ``TypeError``, so every instant fed to
    the resolver is normalised to UTC here. The range is bounded away from
    ``datetime.min``/``datetime.max`` so offsetting by the future/past deltas
    used in the tests cannot overflow.
    """
    return st.datetimes(min_value=_MIN_DT, max_value=_MAX_DT).map(
        lambda dt: dt.replace(tzinfo=UTC)
    )


# ---------------------------------------------------------------------------
# Property 16: Invite state transitions
# Validates Requirements 5.1, 5.2, 5.3, 5.4, 5.6
# ---------------------------------------------------------------------------


@_PBT
@given(
    status=_statuses,
    now=_aware_datetimes(),
    # A strictly-future expiry: PENDING with this expiry must accept, so the
    # transition is governed purely by status (not by expiry) for this family.
    future_delta=st.timedeltas(
        min_value=timedelta(seconds=1), max_value=timedelta(days=3650)
    ),
)
def test_decision_matches_predicate_with_future_expiry(
    status: InviteStatus, now: datetime, future_delta: timedelta
) -> None:
    """The decision matches the state-machine predicate (Property 16).

    With a strictly-future expiry, a ``PENDING`` invite accepts and any
    non-``PENDING`` invite is already resolved (Req 5.2, 5.3, 5.6).
    """
    expires_at = now + future_delta
    decision = resolve_invite_acceptance(status, expires_at, now)

    if status is InviteStatus.PENDING:
        assert decision == "accept"
    else:
        assert decision == "already_resolved"


@_PBT
@given(
    status=_terminal_statuses,
    now=_aware_datetimes(),
    # Any expiry at all — past, present, future, or absent — must not matter.
    expires_at=st.none() | _aware_datetimes(),
)
def test_terminal_status_is_always_already_resolved(
    status: InviteStatus, now: datetime, expires_at: datetime | None
) -> None:
    """A non-``PENDING`` invite is ALWAYS ``"already_resolved"`` (Req 5.6).

    Expiry is irrelevant once an invite has left ``pending``: an accepted or
    expired invite can never be re-accepted or re-expired (reuse rejection).
    """
    assert (
        resolve_invite_acceptance(status, expires_at, now) == "already_resolved"
    )


@_PBT
@given(
    now=_aware_datetimes(),
    future_delta=st.timedeltas(
        min_value=timedelta(seconds=1), max_value=timedelta(days=3650)
    ),
)
def test_pending_with_future_or_absent_expiry_accepts(
    now: datetime, future_delta: timedelta
) -> None:
    """``PENDING`` with a future OR absent expiry -> ``"accept"`` (Req 5.2/5.3)."""
    # Absent expiry: nothing to expire against, so accept.
    assert resolve_invite_acceptance(InviteStatus.PENDING, None, now) == "accept"
    # Strictly-future expiry: not yet reached, so accept.
    assert (
        resolve_invite_acceptance(
            InviteStatus.PENDING, now + future_delta, now
        )
        == "accept"
    )


@_PBT
@given(
    status=_statuses,
    now=_aware_datetimes(),
    expires_at=st.none() | _aware_datetimes(),
)
def test_function_is_total(
    status: InviteStatus, now: datetime, expires_at: datetime | None
) -> None:
    """The resolver is total: it always returns one of the three literals.

    Across arbitrary status, expiry (present or absent), and reference time it
    never raises and never returns anything outside the decision set.
    """
    assert resolve_invite_acceptance(status, expires_at, now) in _DECISIONS


# ---------------------------------------------------------------------------
# Property 17: Invite expiry
# Validates Requirements 5.5
# ---------------------------------------------------------------------------


@_PBT
@given(
    now=_aware_datetimes(),
    # Generated INDEPENDENTLY of ``now`` so both now < expiry and now >= expiry
    # arise across examples, exercising both branches of the expiry predicate.
    expires_at=_aware_datetimes(),
)
def test_pending_expiry_predicate(now: datetime, expires_at: datetime) -> None:
    """``PENDING`` expires iff ``now >= expires_at`` (Property 17, Req 5.5).

    ``now`` and ``expires_at`` are drawn independently, so the whole space of
    orderings (before, at, after expiry) is covered.
    """
    decision = resolve_invite_acceptance(
        InviteStatus.PENDING, expires_at, now
    )
    if now >= expires_at:
        assert decision == "expired"
    else:
        assert decision == "accept"


@_PBT
@given(instant=_aware_datetimes())
def test_pending_inclusive_boundary_is_expired(instant: datetime) -> None:
    """The expiry boundary is inclusive: ``now == expires_at`` -> ``"expired"``.

    An invite exactly at its expiration time has reached it and is expired
    (Req 5.5).
    """
    assert (
        resolve_invite_acceptance(InviteStatus.PENDING, instant, instant)
        == "expired"
    )


@_PBT
@given(
    status=_terminal_statuses,
    now=_aware_datetimes(),
    # A past expiry — the invite IS expired by time — yet a terminal status
    # must still short-circuit to reuse rejection rather than re-expiring.
    past_delta=st.timedeltas(
        min_value=timedelta(seconds=1), max_value=timedelta(days=3650)
    ),
)
def test_terminal_status_stays_resolved_even_when_expired(
    status: InviteStatus, now: datetime, past_delta: timedelta
) -> None:
    """A terminal invite stays ``"already_resolved"`` even past expiry (Req 5.5/5.6).

    Lazy expiry only applies to ``pending`` invites; an already accepted/expired
    invite is never re-classified as freshly ``"expired"``.
    """
    expires_at = now - past_delta
    assert (
        resolve_invite_acceptance(status, expires_at, now)
        == "already_resolved"
    )
