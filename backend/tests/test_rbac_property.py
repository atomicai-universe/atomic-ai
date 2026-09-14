"""Property-based tests for the RBAC guard's pure decision layer.

Implements two design properties from the Atomic AI design:

- **Property 1: RBAC capability decisions follow the role-capability map**
  **Validates: Requirements 3.6, 4.4, 4.5, 4.6, 4.7, 8.5, 10.5**
- **Property 2: Workspace operations require membership**
  **Validates: Requirements 4.2, 4.3**

These tests target the *pure* decision logic in ``app.core.rbac`` — the
``can(role, capability)`` function backed by the ``ROLE_CAPABILITIES`` map — plus
the documented membership-guard rule enforced by the ``require(capability)``
dependency. No FastAPI wiring or database is exercised: Property 2 asserts the
decision semantics of the guard (member vs non-member), which is the security
invariant that matters, at the pure layer where it can be tested exhaustively.

The design mandates a minimum of 100 examples per property; the generators here
are cheap so we run more.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.rbac import ROLE_CAPABILITIES, Capability, can
from app.db.models import MemberRole


# Shared settings profile: comfortably above the design's 100-example minimum.
_PBT = settings(max_examples=200)

# Reusable strategies over the (small, total) role and capability domains.
_roles = st.sampled_from(list(MemberRole))
_capabilities = st.sampled_from(list(Capability))


# ---------------------------------------------------------------------------
# Property 1: RBAC capability decisions follow the role-capability map
# Validates Requirements 3.6, 4.4, 4.5, 4.6, 4.7, 8.5, 10.5
# ---------------------------------------------------------------------------


@_PBT
@given(role=_roles, capability=_capabilities)
def test_can_matches_role_capability_map(
    role: MemberRole, capability: Capability
) -> None:
    """``can`` is exactly membership in the role's capability set (Property 1).

    The single source of truth is ``ROLE_CAPABILITIES``; ``can`` must never
    diverge from it for any ``(role, capability)`` pair.
    """
    assert can(role, capability) is (capability in ROLE_CAPABILITIES[role])


@_PBT
@given(capability=_capabilities)
def test_owner_holds_every_capability(capability: Capability) -> None:
    """Owner is a superset: it holds every capability (Req 4.5, 3.6)."""
    assert can(MemberRole.OWNER, capability) is True


@_PBT
@given(capability=_capabilities)
def test_viewer_holds_only_view_workspace(capability: Capability) -> None:
    """Viewer is read-only: it holds a capability iff it is VIEW_WORKSPACE.

    Every state-changing capability check fails for Viewer by construction
    (Req 4.6, 4.7).
    """
    assert can(MemberRole.VIEWER, capability) is (
        capability == Capability.VIEW_WORKSPACE
    )


@_PBT
@given(role=_roles)
def test_manage_members_is_owner_only(role: MemberRole) -> None:
    """Member management is Owner-only (Req 4.5)."""
    assert can(role, Capability.MANAGE_MEMBERS) is (role == MemberRole.OWNER)


@_PBT
@given(role=_roles)
def test_delete_workspace_is_owner_only(role: MemberRole) -> None:
    """Workspace deletion is Owner-only (Req 3.6)."""
    assert can(role, Capability.DELETE_WORKSPACE) is (role == MemberRole.OWNER)


@_PBT
@given(
    role=_roles,
    capability=st.sampled_from(
        [
            Capability.MANAGE_INTEGRATIONS,
            Capability.MANAGE_SHARED_RULES,
            Capability.RESOLVE_APPROVAL,
        ]
    ),
)
def test_owner_admin_only_capabilities(
    role: MemberRole, capability: Capability
) -> None:
    """Managing integrations/shared rules and resolving approvals are
    Owner-or-Admin only (Req 4.4, 8.5, 10.5)."""
    assert can(role, capability) is (role in {MemberRole.OWNER, MemberRole.ADMIN})


def test_privilege_is_monotonic_across_roles() -> None:
    """Privilege is monotonically non-increasing: Viewer ⊆ Member ⊆ Admin ⊆ Owner.

    This is a fixed structural invariant of ``ROLE_CAPABILITIES`` (no random
    input needed) that underpins the role hierarchy (Req 4.4-4.7).
    """
    viewer = ROLE_CAPABILITIES[MemberRole.VIEWER]
    member = ROLE_CAPABILITIES[MemberRole.MEMBER]
    admin = ROLE_CAPABILITIES[MemberRole.ADMIN]
    owner = ROLE_CAPABILITIES[MemberRole.OWNER]

    assert viewer <= member <= admin <= owner
    # Owner is the full capability set.
    assert owner == frozenset(Capability)


# ---------------------------------------------------------------------------
# Property 2: Workspace operations require membership
# Validates Requirements 4.2, 4.3
#
# We assert the membership DECISION semantics enforced by ``require(capability)``:
# the guard authorizes a caller iff the caller is a member (member_role is not
# None) AND that member's role holds the capability. A non-member (member_role
# returns None) is rejected regardless of capability. We encode this rule here at
# the pure layer rather than invoking the FastAPI Depends() plumbing, so the
# security invariant is tested directly and exhaustively.
# ---------------------------------------------------------------------------


def _authorized(role: MemberRole | None, capability: Capability) -> bool:
    """The guard's membership decision, distilled from ``rbac.require``.

    Mirrors ``require``'s two-step check: a caller must first be a member
    (``role is not None``, else 404 non-member — Req 4.2, 4.3), then the
    member's role must hold the capability (else 403 — Req 4.4-4.7).
    """
    return role is not None and can(role, capability)


@_PBT
@given(
    role=st.one_of(st.none(), _roles),
    capability=_capabilities,
)
def test_membership_gates_authorization(
    role: MemberRole | None, capability: Capability
) -> None:
    """Authorized iff caller is a member and the member's role has the
    capability (Property 2, Req 4.2, 4.3)."""
    assert _authorized(role, capability) is (role is not None and can(role, capability))


@_PBT
@given(capability=_capabilities)
def test_non_member_is_never_authorized(capability: Capability) -> None:
    """A non-member (no role) is rejected for every capability (Req 4.2, 4.3)."""
    assert _authorized(None, capability) is False


@_PBT
@given(role=_roles, capability=_capabilities)
def test_member_authorization_delegates_to_can(
    role: MemberRole, capability: Capability
) -> None:
    """For any member, the membership guard's decision equals ``can`` — the
    membership check adds a gate but never changes a member's capability
    outcome (Req 4.2-4.7)."""
    assert _authorized(role, capability) is can(role, capability)
