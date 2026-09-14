"""Property-based tests for Integration_Vault sharing scope & toggle (task 9.4).

These exercise the two pure decision functions in
``app.services.integration_vault`` that are the sole authorities for
integration-use and sharing-toggle authorization. Both functions read only
``is_shared_with_workspace`` and ``created_by_user_id`` off the integration, so
the properties are checked against a lightweight ``FakeIntegration`` dataclass
(no database, no ORM row) across every ``MemberRole`` and both creator and
non-creator actors.

Property 3: **Integration sharing-scope resolution** — ``can_use_integration``
is exactly ``(user_id == created_by_user_id) OR (is_shared_with_workspace AND
role in {OWNER, ADMIN, MEMBER})``. Concretely: the creator may always use it
(any role, shared or personal); a non-creating Viewer may never use a shared
integration; and no non-creator may ever use a personal integration.
Validates: Requirements 7.3, 7.4, 7.5.

Property 4: **Integration sharing-toggle authorization** —
``can_toggle_sharing`` is exactly ``(user_id == created_by_user_id) OR (role in
{OWNER, ADMIN})``. Concretely: the creator may always toggle; a non-creating
Owner/Admin may toggle; a non-creating Member/Viewer may not.
Validates: Requirements 7.6.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from app.db.models import MemberRole
from app.services import integration_vault


@dataclass(frozen=True)
class FakeIntegration:
    """Minimal structural stand-in for an Integration (sharing-scope view).

    Exposes only the two attributes the pure resolver functions read, so the
    authorization properties can be checked without a database or ORM row.
    """

    is_shared_with_workspace: bool
    created_by_user_id: uuid.UUID


# Roles that are permitted to USE a shared integration (Req 7.3, 7.5).
_USE_ROLES = frozenset({MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MEMBER})
# Roles that are permitted to TOGGLE sharing when not the creator (Req 7.6).
_TOGGLE_ROLES = frozenset({MemberRole.OWNER, MemberRole.ADMIN})

_uuids = st.uuids()
_roles = st.sampled_from(list(MemberRole))


@settings(max_examples=200)
@given(
    is_shared=st.booleans(),
    created_by=_uuids,
    other=_uuids,
    is_creator=st.booleans(),
    role=_roles,
)
def test_can_use_integration_matches_scope_resolution(
    is_shared: bool,
    created_by: uuid.UUID,
    other: uuid.UUID,
    is_creator: bool,
    role: MemberRole,
) -> None:
    """Property 3: use authorization = creator OR (shared AND role in use-set).

    Validates: Requirements 7.3, 7.4, 7.5.
    """
    # ``is_creator`` chooses whether the acting user is the creator; when not the
    # creator we use a distinct uuid so ``other != created_by`` holds.
    user_id = created_by if is_creator else other
    actually_creator = user_id == created_by

    integration = FakeIntegration(
        is_shared_with_workspace=is_shared,
        created_by_user_id=created_by,
    )

    result = integration_vault.can_use_integration(integration, user_id, role)

    expected = actually_creator or (is_shared and role in _USE_ROLES)
    assert result is expected

    # Concrete guarantees the specification calls out explicitly.
    if actually_creator:
        # Creator may always use it — any role, shared or personal.
        assert result is True
    else:
        if not is_shared:
            # No non-creator may use a personal integration, regardless of role.
            assert result is False
        elif role is MemberRole.VIEWER:
            # A non-creating Viewer may never use a shared integration.
            assert result is False


@settings(max_examples=200)
@given(
    is_shared=st.booleans(),
    created_by=_uuids,
    other=_uuids,
    is_creator=st.booleans(),
    role=_roles,
)
def test_can_toggle_sharing_matches_authorization(
    is_shared: bool,
    created_by: uuid.UUID,
    other: uuid.UUID,
    is_creator: bool,
    role: MemberRole,
) -> None:
    """Property 4: toggle authorization = creator OR role in {OWNER, ADMIN}.

    Validates: Requirements 7.6.
    """
    user_id = created_by if is_creator else other
    actually_creator = user_id == created_by

    integration = FakeIntegration(
        is_shared_with_workspace=is_shared,
        created_by_user_id=created_by,
    )

    result = integration_vault.can_toggle_sharing(integration, user_id, role)

    expected = actually_creator or role in _TOGGLE_ROLES
    assert result is expected

    if actually_creator:
        # Creator may always toggle their own integration's sharing scope.
        assert result is True
    else:
        # Non-creating Owner/Admin may toggle; Member/Viewer may not.
        assert result is (role in _TOGGLE_ROLES)
