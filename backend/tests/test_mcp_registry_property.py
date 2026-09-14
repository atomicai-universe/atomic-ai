"""Property-based tests for MCP_Registry workspace-scoped toolset (task 11.2).

These exercise the two pure selection functions in
``app.services.mcp_registry`` that are the sole authorities for agent-toolset
scoping: :func:`~app.services.mcp_registry.is_resolvable_for` (per-integration
decision) and :func:`~app.services.mcp_registry.select_tool_servers` (the
order-preserving filter over a set of integrations). Both read only
``workspace_id``, ``created_by_user_id``, ``is_shared_with_workspace`` and
``status`` off the integration, so the properties are checked against a
lightweight ``FakeIntegration`` dataclass — no database, no ORM row — across
every ``IntegrationStatus`` and both same/other workspace and creator.

Property 22: **Agent toolset is workspace-scoped** — an integration is
resolvable for the triggering user iff it belongs to THIS workspace AND is
ACTIVE AND is either shared with the workspace or the user's own personal
integration. Concretely: an integration in a different workspace is never
resolvable regardless of sharing/creator/status (tenant isolation), and a
non-shared integration created by a different user is never resolvable.
``select_tool_servers`` returns exactly the resolvable integrations, order
preserved.

Validates: Requirements 9.2, 9.3, 9.6.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from app.db.models import IntegrationStatus
from app.services import mcp_registry


@dataclass(frozen=True)
class FakeIntegration:
    """Minimal structural stand-in for an Integration (toolset-scope view).

    Exposes only the four attributes the pure selection functions read, so the
    workspace-scoping properties can be checked without a database or ORM row.
    Satisfies the ``ResolvableIntegration`` protocol structurally.
    """

    workspace_id: uuid.UUID
    created_by_user_id: uuid.UUID
    is_shared_with_workspace: bool
    status: IntegrationStatus


_uuids = st.uuids()
_statuses = st.sampled_from(list(IntegrationStatus))


def _expected_resolvable(
    integration: FakeIntegration,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Reference specification of the selection rule (Req 9.2, 9.3, 9.6)."""
    return (
        integration.workspace_id == workspace_id
        and integration.status is IntegrationStatus.ACTIVE
        and (
            integration.is_shared_with_workspace
            or integration.created_by_user_id == user_id
        )
    )


# --- integration draw --------------------------------------------------------
#
# A drawn integration's workspace is EITHER the request workspace or a distinct
# uuid (``same_ws`` toggle), and its creator is EITHER the request user or a
# distinct uuid (``created_by_self`` toggle). This concentrates draws on the
# boundary cases (same/other workspace, self/other creator) that matter.


@st.composite
def _integration(draw, request_ws: uuid.UUID, request_user: uuid.UUID):
    same_ws = draw(st.booleans())
    workspace_id = request_ws if same_ws else draw(_uuids)
    created_by_self = draw(st.booleans())
    created_by = request_user if created_by_self else draw(_uuids)
    return FakeIntegration(
        workspace_id=workspace_id,
        created_by_user_id=created_by,
        is_shared_with_workspace=draw(st.booleans()),
        status=draw(_statuses),
    )


@settings(max_examples=200)
@given(data=st.data(), request_ws=_uuids, request_user=_uuids)
def test_is_resolvable_matches_workspace_scoped_rule(
    data: st.DataObject,
    request_ws: uuid.UUID,
    request_user: uuid.UUID,
) -> None:
    """Property 22: is_resolvable_for == (in-workspace AND active AND shared-or-own).

    Validates: Requirements 9.2, 9.3, 9.6.
    """
    integration = data.draw(_integration(request_ws, request_user))

    result = mcp_registry.is_resolvable_for(integration, request_ws, request_user)

    assert result is _expected_resolvable(integration, request_ws, request_user)


@settings(max_examples=200)
@given(
    other_ws=_uuids,
    request_ws=_uuids,
    request_user=_uuids,
    created_by=_uuids,
    is_shared=st.booleans(),
    status=_statuses,
)
def test_different_workspace_is_never_resolvable(
    other_ws: uuid.UUID,
    request_ws: uuid.UUID,
    request_user: uuid.UUID,
    created_by: uuid.UUID,
    is_shared: bool,
    status: IntegrationStatus,
) -> None:
    """Property 22 (Req 9.6): a foreign-workspace integration is never resolvable.

    Holds regardless of sharing flag, creator, or status — tenant isolation.

    Validates: Requirements 9.6.
    """
    # Force a workspace distinct from the request workspace.
    foreign_ws = other_ws if other_ws != request_ws else uuid.uuid4()
    assert foreign_ws != request_ws

    integration = FakeIntegration(
        workspace_id=foreign_ws,
        created_by_user_id=created_by,
        is_shared_with_workspace=is_shared,
        status=status,
    )

    assert (
        mcp_registry.is_resolvable_for(integration, request_ws, request_user)
        is False
    )


@settings(max_examples=200)
@given(
    request_ws=_uuids,
    request_user=_uuids,
    other_user=_uuids,
    status=_statuses,
)
def test_non_shared_other_user_integration_is_never_resolvable(
    request_ws: uuid.UUID,
    request_user: uuid.UUID,
    other_user: uuid.UUID,
    status: IntegrationStatus,
) -> None:
    """Property 22: a non-shared integration owned by another user is never usable.

    Even when it lives in the request workspace and is ACTIVE, a personal
    integration belonging to a different user is not in this user's toolset.

    Validates: Requirements 9.2, 9.3.
    """
    # Force a creator distinct from the triggering user.
    creator = other_user if other_user != request_user else uuid.uuid4()
    assert creator != request_user

    integration = FakeIntegration(
        workspace_id=request_ws,
        created_by_user_id=creator,
        is_shared_with_workspace=False,
        status=status,
    )

    assert (
        mcp_registry.is_resolvable_for(integration, request_ws, request_user)
        is False
    )


@settings(max_examples=200)
@given(data=st.data(), request_ws=_uuids, request_user=_uuids)
def test_select_tool_servers_returns_exactly_resolvable_in_order(
    data: st.DataObject,
    request_ws: uuid.UUID,
    request_user: uuid.UUID,
) -> None:
    """Property 22: select_tool_servers = resolvable subset, order preserved.

    The result is exactly the resolvable integrations, is an order-preserving
    subsequence of the input, and every selected integration is in the request
    workspace and ACTIVE.

    Validates: Requirements 9.2, 9.3, 9.6.
    """
    integrations = data.draw(
        st.lists(_integration(request_ws, request_user), min_size=0, max_size=12)
    )

    selected = mcp_registry.select_tool_servers(
        integrations, request_ws, request_user
    )

    expected = [
        integration
        for integration in integrations
        if _expected_resolvable(integration, request_ws, request_user)
    ]
    # Exactly the resolvable ones, in the original relative order.
    assert selected == expected

    # Subsequence of the input (order preserved, nothing invented).
    input_iter = iter(integrations)
    assert all(item in input_iter for item in selected)

    # Every selected integration is genuinely in-workspace and ACTIVE.
    for integration in selected:
        assert integration.workspace_id == request_ws
        assert integration.status is IntegrationStatus.ACTIVE
        assert (
            integration.is_shared_with_workspace
            or integration.created_by_user_id == request_user
        )
