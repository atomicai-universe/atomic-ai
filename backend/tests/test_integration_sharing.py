"""Pure unit tests for integration sharing-scope resolution and toggle auth (task 9.3).

Exercises the two PURE decision functions of the Integration_Vault — no DB, no
encryption, no I/O:

- :func:`~app.services.integration_vault.can_use_integration` (Req 7.3, 7.4, 7.5)
- :func:`~app.services.integration_vault.can_toggle_sharing` (Req 7.6)

A tiny :class:`_FakeIntegration` dataclass stands in for the ORM row by exposing
just the two attributes the resolvers read (``is_shared_with_workspace`` and
``created_by_user_id``), keeping these tests fast and dependency-free.

Requirements: 7.3, 7.4, 7.5, 7.6.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.db.models import MemberRole
from app.services.integration_vault import (
    can_toggle_sharing,
    can_use_integration,
)


@dataclass
class _FakeIntegration:
    """Minimal stand-in exposing the fields the pure resolvers read."""

    is_shared_with_workspace: bool
    created_by_user_id: uuid.UUID


CREATOR_ID = uuid.uuid4()
OTHER_USER_ID = uuid.uuid4()

NON_VIEWER_ROLES = [MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MEMBER]
ALL_ROLES = [*NON_VIEWER_ROLES, MemberRole.VIEWER]


# --------------------------------------------------------------------------- #
# can_use_integration                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("role", ALL_ROLES)
def test_creator_can_always_use(shared: bool, role: MemberRole) -> None:
    """Creator may use their integration whether shared or personal, any role."""
    integration = _FakeIntegration(
        is_shared_with_workspace=shared,
        created_by_user_id=CREATOR_ID,
    )
    assert can_use_integration(integration, CREATOR_ID, role) is True


@pytest.mark.parametrize("role", NON_VIEWER_ROLES)
def test_shared_usable_by_owner_admin_member_noncreator(role: MemberRole) -> None:
    """Shared integration is usable by non-creating Owner/Admin/Member (Req 7.3, 7.5)."""
    integration = _FakeIntegration(
        is_shared_with_workspace=True,
        created_by_user_id=CREATOR_ID,
    )
    assert can_use_integration(integration, OTHER_USER_ID, role) is True


def test_shared_not_usable_by_viewer_noncreator() -> None:
    """Shared integration is NOT usable by a non-creating Viewer (Req 7.5)."""
    integration = _FakeIntegration(
        is_shared_with_workspace=True,
        created_by_user_id=CREATOR_ID,
    )
    assert can_use_integration(integration, OTHER_USER_ID, MemberRole.VIEWER) is False


@pytest.mark.parametrize("role", ALL_ROLES)
def test_personal_not_usable_by_any_noncreator(role: MemberRole) -> None:
    """Personal integration is usable ONLY by its creator (Req 7.4, 7.5)."""
    integration = _FakeIntegration(
        is_shared_with_workspace=False,
        created_by_user_id=CREATOR_ID,
    )
    assert can_use_integration(integration, OTHER_USER_ID, role) is False


# --------------------------------------------------------------------------- #
# can_toggle_sharing                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("role", ALL_ROLES)
def test_creator_can_toggle(shared: bool, role: MemberRole) -> None:
    """Creator may always toggle sharing scope of their integration (Req 7.6)."""
    integration = _FakeIntegration(
        is_shared_with_workspace=shared,
        created_by_user_id=CREATOR_ID,
    )
    assert can_toggle_sharing(integration, CREATOR_ID, role) is True


@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.ADMIN])
def test_owner_admin_noncreator_can_toggle(role: MemberRole) -> None:
    """Non-creating Owner/Admin may toggle sharing scope (Req 7.6)."""
    integration = _FakeIntegration(
        is_shared_with_workspace=False,
        created_by_user_id=CREATOR_ID,
    )
    assert can_toggle_sharing(integration, OTHER_USER_ID, role) is True


@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER])
def test_member_viewer_noncreator_cannot_toggle(role: MemberRole) -> None:
    """Non-creating Member/Viewer may NOT toggle sharing scope (Req 7.6)."""
    integration = _FakeIntegration(
        is_shared_with_workspace=True,
        created_by_user_id=CREATOR_ID,
    )
    assert can_toggle_sharing(integration, OTHER_USER_ID, role) is False
