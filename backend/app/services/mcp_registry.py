"""MCP_Registry — workspace-scoped tool-server resolution (task 11.1).

Implements the ``MCP_Registry`` component from the design (see design.md
"Strands_Engine and MCP_Registry"). Its single responsibility here is
*selection/scoping*: given a workspace and the triggering user, decide which
of the workspace's integrations back tool servers the agent may use, and
return a lightweight descriptor per selected integration. The actual MCP
client wiring/connection is left to the Strands_Engine (task 11.3); this
module never opens a real MCP connection.

Selection rule (Req 9.2, 9.3, 9.6): a workspace integration is *resolvable*
for the triggering user when it is BOTH

- scoped to THIS workspace (never another workspace's integration — tenant
  isolation, Req 9.6), AND
- either the triggering user's PERSONAL integration in this workspace
  (``created_by_user_id == user_id`` AND ``is_shared_with_workspace`` is
  ``False``) OR a SHARED integration in this workspace
  (``is_shared_with_workspace`` is ``True``, regardless of creator), AND
- currently :attr:`~app.db.models.IntegrationStatus.ACTIVE` (integrations in
  ``error`` or ``disconnected`` state are skipped — they cannot back a usable
  tool server).

The core decision is factored into the *pure*, DB-free predicate
:func:`is_resolvable_for` so task 11.2 (Property 22: agent toolset is
workspace-scoped) can property-test it without a database. :func:`resolve`
performs the workspace-scoped query and applies the predicate, returning the
:class:`ToolServer` descriptors.

The personal-or-shared selection here is deliberately narrower than the
general "may this user use this integration?" authorization in
:func:`app.services.integration_vault.can_use_integration`: an agent run's
toolset is the triggering user's own personal integrations plus the
workspace's shared integrations, which is exactly what
``can_use_integration`` also permits for the triggering member — but the
PRIMARY selection is the personal-or-shared rule stated above.

Requirements: 9.2, 9.3, 9.6.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Integration,
    IntegrationCategory,
    IntegrationStatus,
)


@runtime_checkable
class ResolvableIntegration(Protocol):
    """Structural view of an Integration for the pure selection predicate.

    Both the ORM :class:`~app.db.models.Integration` and lightweight in-memory
    fakes (used by the Property 22 test) satisfy this protocol by exposing the
    four attributes :func:`is_resolvable_for` reads.
    """

    workspace_id: uuid.UUID
    created_by_user_id: uuid.UUID
    is_shared_with_workspace: bool
    status: IntegrationStatus


@dataclass(frozen=True)
class ToolServer:
    """Lightweight descriptor of a tool server backed by an integration.

    Returned by :func:`resolve` for each integration selected into an agent's
    toolset. It carries only non-sensitive identifying metadata; it holds no
    credentials and represents no open connection. The Strands_Engine (task
    11.3) uses these descriptors to wire the actual MCP clients.

    Attributes:
        integration_id: The backing integration's id.
        category: The integration's platform category.
        provider_name: The integration's human-readable provider label.
        is_shared: Whether the backing integration is shared workspace-wide
            (``True``) or the triggering user's personal integration
            (``False``).
    """

    integration_id: uuid.UUID
    category: IntegrationCategory
    provider_name: str
    is_shared: bool


def is_resolvable_for(
    integration: ResolvableIntegration,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Decide whether ``integration`` is in ``user_id``'s toolset for ``workspace_id``.

    Pure decision function (no I/O) — the sole authority for the agent-toolset
    selection, property-tested by task 11.2 (Property 22).

    Returns ``True`` iff ALL of the following hold (Req 9.2, 9.3, 9.6):

    - the integration belongs to ``workspace_id`` (an integration from any
      other workspace is never resolvable, even when shared — tenant
      isolation, Req 9.6), AND
    - the integration is currently
      :attr:`~app.db.models.IntegrationStatus.ACTIVE` (``error`` /
      ``disconnected`` integrations cannot back a usable tool server), AND
    - the integration is either
        * SHARED with the workspace (``is_shared_with_workspace`` is ``True``),
          usable regardless of who created it, OR
        * the triggering user's PERSONAL integration
          (``created_by_user_id == user_id`` AND ``is_shared_with_workspace``
          is ``False``).

    A non-shared integration created by a DIFFERENT user is never resolvable
    (it is that other user's personal integration).

    Args:
        integration: The integration (ORM row or any object exposing
            ``workspace_id``, ``created_by_user_id``,
            ``is_shared_with_workspace``, and ``status``).
        workspace_id: The workspace the agent execution is scoped to.
        user_id: The triggering user.

    Returns:
        ``True`` if the integration should back a tool server for this
        execution, ``False`` otherwise.
    """
    if integration.workspace_id != workspace_id:
        return False
    if integration.status is not IntegrationStatus.ACTIVE:
        return False
    if integration.is_shared_with_workspace:
        return True
    return integration.created_by_user_id == user_id


def select_tool_servers(
    integrations: Iterable[ResolvableIntegration],
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[ResolvableIntegration]:
    """Return the integrations from ``integrations`` resolvable for the execution.

    Pure filter over :func:`is_resolvable_for`, preserving input order. Useful
    for property-testing the selection over an arbitrary integration set
    without a database (Property 22).
    """
    return [
        integration
        for integration in integrations
        if is_resolvable_for(integration, workspace_id, user_id)
    ]


def _to_descriptor(integration: Integration) -> ToolServer:
    """Build a :class:`ToolServer` descriptor from a selected integration row."""
    return ToolServer(
        integration_id=integration.id,
        category=integration.category,
        provider_name=integration.provider_name,
        is_shared=integration.is_shared_with_workspace,
    )


async def resolve(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[ToolServer]:
    """Resolve the tool servers available to ``user_id`` in ``workspace_id``.

    Returns a :class:`ToolServer` descriptor for each ACTIVE integration that
    is either the triggering user's personal integration in this workspace or
    a shared integration in this workspace, and never any integration from
    another workspace (Req 9.2, 9.3, 9.6).

    The query is scoped to ``workspace_id`` at the database layer, so rows from
    other workspaces are never loaded (tenant isolation). The pure
    :func:`is_resolvable_for` predicate is then applied to enforce the
    personal-or-shared + ACTIVE selection, keeping the scoping decision in one
    place shared with the property test.

    This method does NOT open MCP connections; it produces descriptors the
    Strands_Engine (task 11.3) will use to wire clients.

    Args:
        session: The active async session/transaction (read-only here).
        workspace_id: The workspace the agent execution is scoped to.
        user_id: The triggering user.

    Returns:
        The selected tool-server descriptors, ordered oldest integration first.
    """
    result = await session.scalars(
        select(Integration)
        .where(
            Integration.workspace_id == workspace_id,
            Integration.status == IntegrationStatus.ACTIVE,
        )
        .order_by(Integration.created_at, Integration.id)
    )
    integrations = list(result.all())
    return [
        _to_descriptor(integration)
        for integration in integrations
        if is_resolvable_for(integration, workspace_id, user_id)
    ]


__all__ = [
    "ResolvableIntegration",
    "ToolServer",
    "is_resolvable_for",
    "select_tool_servers",
    "resolve",
]
