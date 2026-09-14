"""Workspaces router — CRUD, invites, accept, and role updates (task 7.5).

This module wires the :mod:`app.services.workspace_service` operations (tasks
7.1/7.3) into authenticated HTTP endpoints under ``/api/v1/workspaces``, guarded
by RBAC (:mod:`app.core.rbac`) and writing a :class:`~app.db.models.SystemAuditLog`
row on every state-changing operation (Req 15.1).

Authorization model (Req 4.5, 4.3):

- Every route requires a valid Session_Token via :func:`app.api.deps.require_session`,
  which yields the resolved :class:`~app.core.tenancy.RequestContext`.
- **Creating** a workspace needs no prior membership: any authenticated user may
  create one and becomes its sole Owner (Req 3.1).
- **Accepting** an invite needs no prior membership either: possession of a valid
  invite token is the authorization (the service enforces validity, Req 5.x).
- Every other operation is *workspace-scoped*. Rather than depend on
  :func:`app.core.rbac.require` (which authorizes against the caller's *active*
  workspace), this router takes the target workspace id from the path and
  resolves the caller's role directly from the :class:`RequestContext`
  (``ctx.member_role(workspace_id)``) and authorizes
  it with the pure :func:`app.core.rbac.can` decision, translating the result to
  HTTP itself:
    * a **non-member** gets **404** (the workspace's existence is not disclosed,
      Req 4.3/16.2);
    * a member whose role **lacks** the capability gets **403** (insufficient).
  This keeps the guard's decision in the audited, property-tested ``can`` while
  keeping the guard's decision close to the workspace it resolves. These routes
  may also use ``Depends(require(...))`` for active-workspace-scoped operations
  since ``require`` is now wired to ``require_session``.

  Per Req 4.5, member management — inviting and changing member roles — is
  **Owner-only** (``Capability.MANAGE_MEMBERS``), and workspace deletion is
  Owner-only (``Capability.DELETE_WORKSPACE``); ``can`` grants both to Owner
  alone.

Audit logging (Req 15.1). Each state-changing endpoint appends a
``system_audit_logs`` row via :func:`_write_audit` with the acting ``user_id``,
the ``workspace_id`` (when applicable), a stable ``action`` name, and metadata
passed through :func:`app.core.scrubbing.scrub` so no invite token or other
secret is ever persisted. The session is committed after the operation + audit
so the two land atomically. When task 15.1 introduces an ``Audit_Service``,
:func:`_write_audit` should delegate to it.

Invite tokens. :func:`app.services.workspace_service.create_invite` returns the
raw token *bytes*; the accept flow looks an invite up by those exact bytes. Over
HTTP the token travels as a hex string: the create response returns
``token`` = ``bytes.hex()`` so the Owner can forward it to the invitee, and the
accept endpoint decodes the presented hex back to bytes. The token is never
written to logs or audit metadata.

Requirements: 4.5, 15.1 (also uses 3.1, 3.6, 4.3, 5.x, 16.2).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_session
from app.core.errors import APIError
from app.core.rbac import Capability, can
from app.core.scrubbing import scrub
from app.core.tenancy import RequestContext
from app.db.models import InviteRole, MemberRole, SystemAuditLog
from app.db.session import get_session
from app.schemas.base import BaseRequest
from app.services import workspace_service

logger = logging.getLogger("atomic_ai.workspaces")

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

# Audit action names for workspace state changes (Req 15.1).
AUDIT_ACTION_WORKSPACE_CREATED = "workspace.created"
AUDIT_ACTION_WORKSPACE_DELETED = "workspace.deleted"
AUDIT_ACTION_WORKSPACE_RENAMED = "workspace.renamed"
AUDIT_ACTION_INVITE_CREATED = "workspace.invite_created"
AUDIT_ACTION_INVITE_ACCEPTED = "workspace.invite_accepted"
AUDIT_ACTION_MEMBER_ROLE_UPDATED = "workspace.member_role_updated"


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------


class CreateWorkspaceRequest(BaseRequest):
    """Body for ``POST /api/v1/workspaces``."""

    name: str


class RenameWorkspaceRequest(BaseRequest):
    """Body for ``PATCH /api/v1/workspaces/{id}``."""

    name: str


class CreateInviteRequest(BaseRequest):
    """Body for ``POST /api/v1/workspaces/{id}/invites``.

    ``role`` is constrained to the invitable roles (Admin/Member/Viewer); Owner
    is not invitable (Req 5.7) and ``InviteRole`` excludes it at the type level.
    ``email`` is a plain string (the service does not validate address format,
    and adding an email-validator dependency is out of scope for this task).
    """

    email: str
    role: InviteRole


class AcceptInviteRequest(BaseRequest):
    """Body for ``POST /api/v1/workspaces/invites/accept``.

    ``token`` is the hex-encoded invite token the Owner forwarded to the invitee
    (see the create-invite response); it is decoded back to the raw bytes the
    service looks the invite up by.
    """

    token: str


class UpdateMemberRoleRequest(BaseRequest):
    """Body for ``PATCH /api/v1/workspaces/{id}/members/{user_id}``."""

    role: MemberRole


# ---------------------------------------------------------------------------
# Audit helper (Req 15.1)
# ---------------------------------------------------------------------------


async def _write_audit(
    session: AsyncSession,
    *,
    action: str,
    workspace_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    metadata: dict | None = None,
) -> None:
    """Append a workspace Audit_Log row for a state-changing operation (Req 15.1).

    Writes to ``system_audit_logs`` with the acting ``user_id`` and the affected
    ``workspace_id``. Any ``metadata`` is passed through
    :func:`app.core.scrubbing.scrub` first so a token or other secret can never
    be persisted (invite tokens are never passed in). This is the interim direct
    write noted in the module docstring; task 15.1's Audit_Service should replace
    it.
    """
    session.add(
        SystemAuditLog(
            workspace_id=workspace_id,
            user_id=user_id,
            action=action,
            log_metadata=scrub(metadata) if metadata is not None else None,
        )
    )


# ---------------------------------------------------------------------------
# RBAC helper — resolve the caller's role and authorize a capability
# ---------------------------------------------------------------------------


def _require_capability(
    ctx: RequestContext, workspace_id: uuid.UUID, capability: Capability
) -> MemberRole:
    """Authorize ``ctx`` for ``capability`` on ``workspace_id``; return the role.

    Resolves the caller's :class:`~app.db.models.MemberRole` from the request
    context directly (``ctx.member_role(workspace_id)``), then consults the
    pure :func:`app.core.rbac.can` decision:

    - A **non-member** is rejected with **404** so the workspace's existence is
      not disclosed to an outsider (Req 4.3/16.2).
    - A member whose role **lacks** ``capability`` is rejected with **403**
      (insufficient role) — this is what makes member management Owner-only
      (Req 4.5).

    Returns the caller's role on success so the handler can pass it to a service
    that needs the actor's role (e.g. ``delete_workspace``).
    """
    role = ctx.member_role(workspace_id)
    if role is None:
        # Non-member: hide the resource's existence (Req 4.3/16.2).
        raise APIError(
            status_code=404,
            code="not_found",
            message="Workspace not found.",
        )
    if not can(role, capability):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="Your role does not permit this operation.",
        )
    return role


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", status_code=200)
async def list_workspaces(
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List the workspaces the caller is a member of, with their role.

    Read-only and non-scoped: any authenticated user may list their own
    memberships (no ``X-Workspace-Id`` required). Returns each workspace's
    ``id``, ``name``, ``slug``, and the caller's ``role`` in it, ordered by
    workspace name. This is the authoritative source the frontend switcher and
    "Manage workspaces" dialog use instead of a local registry.
    """
    rows = await workspace_service.list_workspaces_for_user(
        session, user_id=ctx.user_id
    )
    return {
        "workspaces": [
            {
                "id": str(ws.id),
                "name": ws.name,
                "slug": ws.slug,
                "role": role.value,
            }
            for ws, role in rows
        ]
    }


@router.post("", status_code=201)
async def create_workspace(
    body: CreateWorkspaceRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a workspace owned by the caller (Req 3.1, 15.1).

    Any authenticated user may create a workspace; they become its sole Owner.
    Writes a ``workspace.created`` audit row and commits, then returns the new
    workspace's ``id``, ``name``, and ``slug``.
    """
    workspace = await workspace_service.create_workspace(
        session, name=body.name, creator_user_id=ctx.user_id
    )
    await _write_audit(
        session,
        action=AUDIT_ACTION_WORKSPACE_CREATED,
        workspace_id=workspace.id,
        user_id=ctx.user_id,
        metadata={"name": workspace.name, "slug": workspace.slug},
    )
    await session.commit()
    logger.info("workspace.created workspace_id=%s user_id=%s", workspace.id, ctx.user_id)
    return {"id": str(workspace.id), "name": workspace.name, "slug": workspace.slug}


@router.delete("/{workspace_id}", status_code=200)
async def delete_workspace(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a workspace; Owner-only (Req 3.6, 15.1).

    Resolves the caller's role and requires ``DELETE_WORKSPACE`` (Owner-only),
    then delegates to the service (which re-checks the Owner-only rule at the
    service boundary). Writes a ``workspace.deleted`` audit row and commits. The
    audit row survives the deletion because ``system_audit_logs.workspace_id``
    uses ``ON DELETE SET NULL`` (Req 20.4).
    """
    actor_role = _require_capability(ctx, workspace_id, Capability.DELETE_WORKSPACE)
    # Audit before the delete so the row is added while the workspace still
    # exists; ON DELETE SET NULL then nulls its workspace_id on commit.
    await _write_audit(
        session,
        action=AUDIT_ACTION_WORKSPACE_DELETED,
        workspace_id=workspace_id,
        user_id=ctx.user_id,
    )
    await workspace_service.delete_workspace(
        session, workspace_id=workspace_id, actor_role=actor_role
    )
    await session.commit()
    logger.info("workspace.deleted workspace_id=%s user_id=%s", workspace_id, ctx.user_id)
    return {"status": "deleted", "id": str(workspace_id)}


@router.patch("/{workspace_id}", status_code=200)
async def rename_workspace(
    workspace_id: uuid.UUID,
    body: RenameWorkspaceRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Rename a workspace; Owner-only (BUILD.md, Req 15.1).

    Resolves the caller's role and requires ``RENAME_WORKSPACE`` (Owner-only),
    then delegates to the service (which re-checks the Owner-only rule and
    regenerates a unique slug). Writes a ``workspace.renamed`` audit row and
    commits, then returns the updated ``id``, ``name``, and ``slug``.
    """
    actor_role = _require_capability(ctx, workspace_id, Capability.RENAME_WORKSPACE)
    workspace = await workspace_service.rename_workspace(
        session,
        workspace_id=workspace_id,
        new_name=body.name,
        actor_role=actor_role,
    )
    await _write_audit(
        session,
        action=AUDIT_ACTION_WORKSPACE_RENAMED,
        workspace_id=workspace_id,
        user_id=ctx.user_id,
        metadata={"name": workspace.name, "slug": workspace.slug},
    )
    await session.commit()
    logger.info("workspace.renamed workspace_id=%s user_id=%s", workspace_id, ctx.user_id)
    return {"id": str(workspace.id), "name": workspace.name, "slug": workspace.slug}


@router.post("/{workspace_id}/invites", status_code=201)
async def create_invite(
    workspace_id: uuid.UUID,
    body: CreateInviteRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Invite a person to the workspace; Owner-only (Req 4.5, 5.1, 15.1).

    Managing members is Owner-only (``MANAGE_MEMBERS``, Req 4.5): a Member,
    Viewer, or Admin caller is rejected with 403; a non-member with 404. On
    success the service creates a ``pending`` invite with a fresh unique token.

    The response includes the invite's ``id``, ``email``, ``role``, ``status``,
    and the hex-encoded ``token`` so the Owner can forward it to the invitee. The
    raw token is never written to logs or audit metadata (Req 15.4).
    """
    _require_capability(ctx, workspace_id, Capability.MANAGE_MEMBERS)
    invite = await workspace_service.create_invite(
        session, workspace_id=workspace_id, email=body.email, role=body.role
    )
    await _write_audit(
        session,
        action=AUDIT_ACTION_INVITE_CREATED,
        workspace_id=workspace_id,
        user_id=ctx.user_id,
        # Never include the token in audit metadata.
        metadata={"email": invite.email, "role": invite.role.value},
    )
    await session.commit()
    logger.info(
        "workspace.invite_created workspace_id=%s user_id=%s invite_id=%s",
        workspace_id,
        ctx.user_id,
        invite.id,
    )
    return {
        "id": str(invite.id),
        "email": invite.email,
        "role": invite.role.value,
        "status": invite.status.value,
        # Hex-encoded so the Owner can hand it to the invitee; decoded on accept.
        "token": invite.token.hex(),
    }


@router.post("/invites/accept", status_code=200)
async def accept_invite(
    body: AcceptInviteRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accept an invite by token, joining the caller to the workspace (Req 5.x, 15.1).

    Any authenticated user holding a valid invite token may accept; possession of
    the token is the authorization (the service enforces pending/unexpired/unused
    validity). The hex ``token`` is decoded back to the raw bytes the service
    looks the invite up by; a malformed hex string is a 400. On success a
    membership is created with the invited role and a ``workspace.invite_accepted``
    audit row is written.
    """
    try:
        token_bytes = bytes.fromhex(body.token)
    except ValueError:
        raise APIError(
            status_code=400,
            code="bad_request",
            message="The invitation token is malformed.",
        ) from None

    member = await workspace_service.accept_invite(
        session, token=token_bytes, user_id=ctx.user_id
    )
    await _write_audit(
        session,
        action=AUDIT_ACTION_INVITE_ACCEPTED,
        workspace_id=member.workspace_id,
        user_id=ctx.user_id,
        metadata={"role": member.role.value},
    )
    await session.commit()
    logger.info(
        "workspace.invite_accepted workspace_id=%s user_id=%s",
        member.workspace_id,
        ctx.user_id,
    )
    return {
        "workspace_id": str(member.workspace_id),
        "user_id": str(member.user_id),
        "role": member.role.value,
    }


@router.patch("/{workspace_id}/members/{user_id}", status_code=200)
async def update_member_role(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: UpdateMemberRoleRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Change a member's role; Owner-only (Req 4.5, 15.1).

    Managing members is Owner-only (``MANAGE_MEMBERS``, Req 4.5): a non-Owner
    member is rejected with 403 and a non-member with 404. Delegates the role
    change to the service, writes a ``workspace.member_role_updated`` audit row,
    and commits.
    """
    _require_capability(ctx, workspace_id, Capability.MANAGE_MEMBERS)
    member = await workspace_service.update_member_role(
        session,
        workspace_id=workspace_id,
        target_user_id=user_id,
        new_role=body.role,
    )
    await _write_audit(
        session,
        action=AUDIT_ACTION_MEMBER_ROLE_UPDATED,
        workspace_id=workspace_id,
        user_id=ctx.user_id,
        metadata={"target_user_id": str(user_id), "new_role": member.role.value},
    )
    await session.commit()
    logger.info(
        "workspace.member_role_updated workspace_id=%s user_id=%s target=%s role=%s",
        workspace_id,
        ctx.user_id,
        user_id,
        member.role.value,
    )
    return {
        "workspace_id": str(member.workspace_id),
        "user_id": str(member.user_id),
        "role": member.role.value,
    }


__all__ = [
    "router",
    "AUDIT_ACTION_WORKSPACE_CREATED",
    "AUDIT_ACTION_WORKSPACE_DELETED",
    "AUDIT_ACTION_INVITE_CREATED",
    "AUDIT_ACTION_INVITE_ACCEPTED",
    "AUDIT_ACTION_MEMBER_ROLE_UPDATED",
]
