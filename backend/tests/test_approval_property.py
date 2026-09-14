"""Property-based tests for the approval resolution state machine.

Exercises :func:`app.services.approval_service.resolve_approval_transition`, the
pure, DB-free heart of the ``Approval_Hub`` state machine. It decides, for a
request in some :class:`~app.db.models.ApprovalStatus` and a proposed reviewer
:data:`~app.services.approval_service.ResolutionAction`, whether the transition
should be applied (``"apply"``) or rejected as a conflict (``"conflict"``).

Property 24: Approval state machine.
**Validates: Requirements 10.3, 10.4, 10.7**

Documented rule:

- ``PENDING`` is the only legal source state. ``PENDING`` + ``approve`` applies
  and maps to ``APPROVED`` (Req 10.3); ``PENDING`` + ``reject`` applies and maps
  to ``REJECTED`` (Req 10.4).
- Any *terminal* status (``APPROVED``/``REJECTED``) yields ``"conflict"`` for
  **any** action — re-resolution of an already-resolved request is rejected
  (Req 10.7).

The properties below also assert two universal invariants of a pure resolver:
totality (every ``(status, action)`` pair maps to exactly one defined decision
and never raises) and determinism (identical inputs yield identical decisions).

Each property runs at least 100 examples per the design's property-testing
budget (``max_examples=200`` here).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.db.models import ApprovalStatus
from app.services.approval_service import (
    _ACTION_TERMINAL_STATUS,
    resolve_approval_transition,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Every member of the closed ApprovalStatus enum (pending/approved/rejected).
_status = st.sampled_from(list(ApprovalStatus))

# The exact resolution actions the resolver accepts. Derived from the module's
# action->terminal-status mapping so the generator stays in lockstep with the
# code under test rather than hard-coding literals.
_ACTIONS = sorted(_ACTION_TERMINAL_STATUS.keys())
_action = st.sampled_from(_ACTIONS)

# The two terminal statuses (any non-PENDING status).
_terminal_status = st.sampled_from(
    [s for s in ApprovalStatus if s is not ApprovalStatus.PENDING]
)

# The only decisions the resolver may ever return.
_VALID_OUTCOMES = {"apply", "conflict"}


# ---------------------------------------------------------------------------
# Property 24: Approval state machine
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(action=_action)
def test_pending_applies_and_maps_to_terminal(action: str) -> None:
    """PENDING + any action applies and maps to the correct terminal status.

    approve -> APPROVED (Req 10.3); reject -> REJECTED (Req 10.4).

    **Validates: Requirements 10.3, 10.4**
    """
    assert resolve_approval_transition(ApprovalStatus.PENDING, action) == "apply"

    expected = {
        "approve": ApprovalStatus.APPROVED,
        "reject": ApprovalStatus.REJECTED,
    }[action]
    assert _ACTION_TERMINAL_STATUS[action] is expected


@settings(max_examples=200)
@given(status=_terminal_status, action=_action)
def test_terminal_status_always_conflicts(status: ApprovalStatus, action: str) -> None:
    """Any terminal (APPROVED/REJECTED) status + any action -> conflict.

    Re-resolution of an already-resolved request is always rejected (Req 10.7).

    **Validates: Requirements 10.7**
    """
    assert resolve_approval_transition(status, action) == "conflict"


@settings(max_examples=200)
@given(status=_status, action=_action)
def test_totality_never_raises_and_defined(
    status: ApprovalStatus, action: str
) -> None:
    """Every (status, action) pair yields exactly one defined decision, no raise.

    **Validates: Requirements 10.3, 10.4, 10.7**
    """
    outcome = resolve_approval_transition(status, action)
    assert outcome in _VALID_OUTCOMES
    # The decision is precisely "apply" iff the source state is PENDING.
    assert (outcome == "apply") is (status is ApprovalStatus.PENDING)


@settings(max_examples=200)
@given(status=_status, action=_action)
def test_determinism(status: ApprovalStatus, action: str) -> None:
    """Identical inputs always produce the identical decision.

    **Validates: Requirements 10.3, 10.4, 10.7**
    """
    first = resolve_approval_transition(status, action)
    second = resolve_approval_transition(status, action)
    assert first == second
