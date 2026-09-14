"""Role-based access control (RBAC) guard.

RBAC is deliberately split into two layers so the security-critical *decision*
logic can be property-tested without any FastAPI or database dependency (see
design.md "RBAC_Guard" and Property 1):

- **Pure decision layer** — the :class:`Capability` enum, the
  :data:`ROLE_CAPABILITIES` map (the single source of truth), and the pure
  function :func:`can`. These have no I/O and are exhaustively testable across
  every ``(role, capability)`` pair.
- **Dependency layer** — :func:`require`, a FastAPI dependency *factory* that
  resolves the caller's workspace membership from the request context and
  raises the appropriate HTTP error. It consults :func:`can` for the decision
  but performs the membership lookup and HTTP translation itself.

Authorization model (Req 4):

- A workspace-scoped operation first requires that the caller **is a member** of
  the target workspace (Req 4.2, 4.3). A non-member is rejected.
- A member's single :class:`~app.db.models.MemberRole` is then checked against
  the required capability (Req 4.4-4.7):
    * **Owner** holds every capability (superset), including member management
      and workspace deletion (Req 4.5, 3.6).
    * **Admin** may manage integrations and shared rules and resolve approvals
      (Req 4.4, 8.5, 10.5) but may not manage members or delete the workspace.
    * **Member** may view, trigger workflows, and submit approval requests
      (glossary: Member role), but holds no management capability.
    * **Viewer** is read-only: it holds only the view capability, so every
      state-changing capability check fails for Viewer by construction
      (Req 4.6, 4.7).

Role is constrained to exactly one of the four values at the model/DB layer
(Req 4.1); this module treats :class:`~app.db.models.MemberRole` as total.

Requirements: 3.6, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 8.5, 10.5.
"""

from __future__ import annotations

import enum
import uuid
from typing import Protocol, runtime_checkable

from app.db.models import MemberRole


# ---------------------------------------------------------------------------
# Capabilities (pure decision layer)
# ---------------------------------------------------------------------------


class Capability(enum.StrEnum):
    """A discrete, checkable permission over a workspace-scoped operation.

    Capabilities are intentionally coarse-grained and named for the operation
    class they gate rather than a specific endpoint, so a single map
    (:data:`ROLE_CAPABILITIES`) is the sole source of truth for every guard.
    """

    #: Read-only workspace access (agent logs, analytics, listings). Granted to
    #: every role including Viewer (Req 4.6).
    VIEW_WORKSPACE = "view_workspace"
    #: Trigger an agent workflow / execution in the workspace (Member+).
    TRIGGER_WORKFLOW = "trigger_workflow"
    #: Submit (create) an approval request as part of a workflow (Member+).
    SUBMIT_APPROVAL = "submit_approval"
    #: Add/remove integrations or manage shared rules — Owner/Admin (Req 4.4).
    MANAGE_INTEGRATIONS = "manage_integrations"
    #: Create/modify workspace-wide (shared) rules — Owner/Admin (Req 4.4, 8.5).
    MANAGE_SHARED_RULES = "manage_shared_rules"
    #: Approve/reject an approval request — Owner/Admin (Req 10.5).
    RESOLVE_APPROVAL = "resolve_approval"
    #: Manage workspace members and invites — Owner only (Req 4.5).
    MANAGE_MEMBERS = "manage_members"
    #: Delete the entire workspace — Owner only (Req 3.6).
    DELETE_WORKSPACE = "delete_workspace"
    #: Rename the workspace — Owner only (mirrors delete; BUILD.md).
    RENAME_WORKSPACE = "rename_workspace"


# The single source of truth for authorization decisions (Property 1). Each role
# maps to the exact frozen set of capabilities it holds. Owner is the full set;
# the others are strict subsets, so privilege is monotonically non-increasing
# from Owner → Admin → Member → Viewer.
#
# ``frozenset(Capability)`` for Owner guarantees Owner automatically holds any
# capability added to the enum in future, keeping "Owner is a superset" true by
# construction.
ROLE_CAPABILITIES: dict[MemberRole, frozenset[Capability]] = {
    MemberRole.OWNER: frozenset(Capability),
    MemberRole.ADMIN: frozenset(
        {
            Capability.VIEW_WORKSPACE,
            Capability.TRIGGER_WORKFLOW,
            Capability.SUBMIT_APPROVAL,
            Capability.MANAGE_INTEGRATIONS,
            Capability.MANAGE_SHARED_RULES,
            Capability.RESOLVE_APPROVAL,
        }
    ),
    MemberRole.MEMBER: frozenset(
        {
            Capability.VIEW_WORKSPACE,
            Capability.TRIGGER_WORKFLOW,
            Capability.SUBMIT_APPROVAL,
        }
    ),
    MemberRole.VIEWER: frozenset({Capability.VIEW_WORKSPACE}),
}


def can(role: MemberRole, capability: Capability) -> bool:
    """Return whether ``role`` holds ``capability``.

    Pure and total: it performs a single membership test against
    :data:`ROLE_CAPABILITIES` and never touches I/O, so it can be exhaustively
    property-tested (Property 1). ``role`` is assumed to be one of the four
    valid :class:`~app.db.models.MemberRole` values (enforced at the DB layer,
    Req 4.1); an unmapped role raises :class:`KeyError` rather than silently
    granting access (fail-closed).
    """
    return capability in ROLE_CAPABILITIES[role]


# ---------------------------------------------------------------------------
# Dependency layer (membership resolution + HTTP translation)
# ---------------------------------------------------------------------------


@runtime_checkable
class MembershipContext(Protocol):
    """The minimal read surface :func:`require` needs from the request context.

    The real :class:`RequestContext` planned for ``core/tenancy.py`` (task 4.8)
    will satisfy this protocol structurally, so :func:`require` needs no change
    when that module lands. Defining it here — rather than importing a module
    that does not yet exist — keeps this task self-contained while documenting
    exactly what the dependency requires.

    Implementations expose the authenticated caller, the active workspace, and a
    way to look up the caller's role in a given workspace. ``member_role``
    returns ``None`` when the caller is not a member of that workspace.
    """

    #: The authenticated caller's user id.
    user_id: uuid.UUID
    #: The workspace the request is scoped to, or ``None`` if none is active.
    active_workspace_id: uuid.UUID | None

    def member_role(self, workspace_id: uuid.UUID) -> MemberRole | None:
        """Return the caller's role in ``workspace_id``, or ``None`` if not a member."""
        ...


def require(capability: Capability):
    """Build a FastAPI dependency enforcing ``capability`` on the active workspace.

    The returned dependency resolves the caller's membership in the request's
    active workspace and authorizes it against ``capability``:

    - If no workspace is active on the context, it raises **400** (the caller
      must select a workspace before a workspace-scoped operation).
    - If the caller is **not a member** of the active workspace, it raises
      **404** so the endpoint does not disclose the workspace's existence to a
      non-member (Req 4.3, 16.2); callers that prefer 403 for a known workspace
      may translate accordingly.
    - If the caller is a member but the member's role **lacks** ``capability``,
      it raises **403** (insufficient role) — this is what rejects Viewer/Member
      attempts at state-changing operations (Req 4.4-4.7).
    - Otherwise it returns the caller's :class:`~app.db.models.MemberRole`.

    The pure decision stays in :func:`can`; this wrapper only does the
    membership lookup and HTTP-error translation. It is intentionally thin so
    task 4.8 can wire the real ``RequestContext`` by supplying a
    :class:`MembershipContext`.

    Args:
        capability: The capability the caller must hold to proceed.

    Returns:
        A dependency callable ``dependency(ctx: MembershipContext) -> MemberRole``.
    """
    # Imported lazily so importing the pure decision layer never requires
    # FastAPI to be installed/available (keeps ``can`` trivially importable) and
    # to avoid a circular import (``app.api.deps`` imports core modules, not
    # rbac). ``require_session`` yields the authenticated
    # :class:`~app.core.tenancy.RequestContext`, which satisfies
    # :class:`MembershipContext`, so it is the real provider for this dependency.
    from fastapi import Depends, HTTPException, status

    from app.api.deps import require_session

    def dependency(
        ctx: MembershipContext = Depends(require_session),
    ) -> MemberRole:
        workspace_id = ctx.active_workspace_id
        if workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="no active workspace selected",
            )

        role = ctx.member_role(workspace_id)
        if role is None:
            # Non-member: hide the resource's existence (Req 4.3, 16.2).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="workspace not found",
            )

        if not can(role, capability):
            # Member of the workspace but role lacks the capability (Req 4.4-4.7).
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role for this operation",
            )

        return role

    return dependency


__all__ = [
    "Capability",
    "ROLE_CAPABILITIES",
    "can",
    "MembershipContext",
    "require",
]
