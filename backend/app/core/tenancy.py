"""Request context and the single tenant-scoping choke point.

This module carries two responsibilities that together enforce tenant isolation
(Req 16) at the request and query layers:

1. :class:`RequestContext` — the immutable, per-request authorization context.
   It names the authenticated caller, the workspace the request is scoped to,
   whether the caller is a platform super admin, and how to resolve the
   caller's :class:`~app.db.models.MemberRole` in any workspace. It satisfies
   the :class:`~app.core.rbac.MembershipContext` protocol structurally, so
   :func:`app.core.rbac.require` consumes it directly (the ``require_session``
   dependency in task 5.3 will build it).

2. :func:`apply_tenant_scope` (alias :func:`scope_to_workspace`) — the *single*
   place tenant-scoped reads are constrained to the active workspace. It appends
   ``WHERE <model>.workspace_id == ctx.active_workspace_id`` to a SQLAlchemy
   :class:`~sqlalchemy.sql.Select`. Routing every tenant-scoped query through
   this one helper means "which workspace can this query see?" has exactly one
   answer, enforced in one place (Req 16.1). A request that references a
   resource in a workspace the caller has no active scope on simply matches no
   rows, so its data is never returned (Req 16.2). The same helper is re-applied
   inside queued jobs, which carry their originating ``workspace_id`` (Req 16.3).

The comparison is built with the ORM column and the context value, so the
workspace id is bound as a query parameter — never interpolated into SQL text
(Req 17.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from sqlalchemy.sql import Select

from app.db.models import MemberRole

# The tenant-scoped tables all expose a ``workspace_id`` column. Bound as a
# TypeVar so the helper returns the same Select type it was given.
_S = TypeVar("_S", bound=Select)


class TenantScopeError(Exception):
    """Raised when a tenant-scoped query is attempted without an active workspace.

    Failing closed here is deliberate: a query that reaches the scoping helper
    with no ``active_workspace_id`` has no legitimate tenant to constrain to, so
    we refuse to run it rather than risk returning cross-tenant rows (Req 16.1,
    16.2).
    """


@dataclass(frozen=True)
class RequestContext:
    """Immutable per-request authorization context.

    Attributes:
        user_id: The authenticated caller's user id.
        active_workspace_id: The workspace the request is scoped to, or ``None``
            when the caller has not selected/targeted a workspace.
        is_superadmin: Whether the caller is a platform super admin (used by the
            super-admin control plane; does not by itself grant workspace roles).
        roles: Mapping of ``workspace_id -> MemberRole`` for every workspace the
            caller is a member of. Absence of a key means "not a member", which
            :meth:`member_role` reports as ``None`` (fail-closed).

    The dataclass is frozen so a resolved context cannot be mutated mid-request.
    ``roles`` defaults to an empty mapping (a caller who is a member of nothing).
    """

    user_id: uuid.UUID
    active_workspace_id: uuid.UUID | None = None
    is_superadmin: bool = False
    roles: Mapping[uuid.UUID, MemberRole] = field(default_factory=dict)

    def member_role(self, workspace_id: uuid.UUID) -> MemberRole | None:
        """Return the caller's role in ``workspace_id``, or ``None`` if not a member.

        This is the method :class:`app.core.rbac.MembershipContext` requires, so
        an instance of this class is directly usable by :func:`app.core.rbac.require`.
        """
        return self.roles.get(workspace_id)


def apply_tenant_scope(stmt: _S, model: type, ctx: RequestContext) -> _S:
    """Constrain ``stmt`` to the caller's active workspace.

    Appends ``WHERE model.workspace_id == ctx.active_workspace_id`` to the given
    :class:`~sqlalchemy.sql.Select` and returns the new statement. This is the
    single choke point for tenant isolation at the query layer (Req 16.1): every
    read of a workspace-scoped table (integrations, rules, agent_sessions,
    approval_requests, workspace_members, workspace_invites, system_audit_logs)
    should pass through here so it can only ever see rows of the active
    workspace.

    Args:
        stmt: A SQLAlchemy ``Select`` over a workspace-scoped model.
        model: The mapped model whose ``workspace_id`` column is filtered. It
            must expose a ``workspace_id`` attribute.
        ctx: The request context supplying the active workspace.

    Returns:
        A new ``Select`` with the workspace filter applied. SQLAlchemy Selects
        are immutable builders, so the input statement is not modified.

    Raises:
        TenantScopeError: if ``ctx.active_workspace_id`` is ``None`` — there is
            no tenant to scope to, so the query is refused (fail-closed).
        AttributeError: if ``model`` has no ``workspace_id`` column (a
            programming error: the helper is only for tenant-scoped tables).
    """
    workspace_id = ctx.active_workspace_id
    if workspace_id is None:
        raise TenantScopeError(
            "cannot scope a query: request context has no active workspace"
        )

    workspace_column = model.workspace_id  # bound param, not string interpolation
    return stmt.where(workspace_column == workspace_id)


# Alias matching the name used elsewhere in the design/tasks. Both names refer
# to the same single choke point.
def scope_to_workspace(stmt: _S, model: type, ctx: RequestContext) -> _S:
    """Alias for :func:`apply_tenant_scope`."""
    return apply_tenant_scope(stmt, model, ctx)


__all__ = [
    "RequestContext",
    "TenantScopeError",
    "apply_tenant_scope",
    "scope_to_workspace",
]
