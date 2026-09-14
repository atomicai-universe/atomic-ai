"""Super admin control-plane router (task 14.1).

Wires the global :mod:`app.services.admin_service` operations into protected
HTTP endpoints under the ``/api/v1/admin`` prefix. Every route in this router is
guarded by :func:`require_superadmin`, so the entire control plane is reachable
only by a platform super admin; any other caller is rejected with **403**
(Req 12.1, 12.2).

Unlike the tenant-scoped feature routers, these operations are deliberately
*global*: a super admin operates the whole platform, so the user directory
crosses every workspace (Req 12.3), a ban cuts a user off everywhere (Req 12.4),
and ownership reassignment can target any workspace (Req 12.5).

Endpoints:

- ``GET  /api/v1/admin/users`` — the platform user directory across all
  workspaces (Req 12.3). Returns a safe view of each user
  (``id``, ``email``, ``name``, ``auth_provider``, ``is_superadmin``,
  ``is_banned``) — never any credential or session token.
- ``POST /api/v1/admin/users/{user_id}/ban`` — ban a user, invalidating active
  sessions and blocking future authentication (Req 12.4).
- ``POST /api/v1/admin/workspaces/{workspace_id}/reassign-owner`` — make a
  target user the Owner of a workspace (Req 12.5).
- ``GET  /api/v1/admin/sessions`` — list every ``running`` agent session across
  all workspaces, including each session's reasoning trace (Req 13.1).
- ``POST /api/v1/admin/sessions/{agent_session_id}/kill`` — emergency
  kill-switch terminating a running session, cancelling its ``pending``
  approvals, and auditing the activation (Req 13.2, 13.3, 13.4).
- ``GET  /api/v1/admin/analytics`` — platform-wide system analytics: aggregate
  token usage, active multi-tenant session count, and database storage metrics
  (Req 14.2). Read-only.
- ``GET  /api/v1/admin/mcp-health`` — MCP health registry: per-provider status,
  error rate, and token-usage aggregation derived from persisted integration
  statuses and agent token metrics, not live pings (Req 14.1). Read-only.

The mutating endpoints (ban / reassign) ``commit`` the session so their effect
persists, and each writes an append-only audit row via
:mod:`app.services.audit_service` recording the acting super admin and the
target (with scrubbed metadata, Req 15.4). The directory endpoint is read-only.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 13.1, 13.2, 13.3, 13.4, 14.1, 14.2.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_session
from app.core.errors import APIError
from app.core.tenancy import RequestContext
from app.db.models import User
from app.db.session import get_session
from app.schemas.base import BaseRequest
from app.services import admin_service, audit_service

logger = logging.getLogger("atomic_ai.admin")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# Audit action names for super-admin operations (Req 15.1).
AUDIT_ACTION_USER_BANNED = "admin.user_banned"
AUDIT_ACTION_OWNER_REASSIGNED = "admin.owner_reassigned"
AUDIT_ACTION_SESSION_KILLED = "admin.session_killed"
AUDIT_ACTION_USER_ROLE_CHANGED = "admin.user_role_changed"
AUDIT_ACTION_USER_UPDATED = "admin.user_updated"
AUDIT_ACTION_USER_DELETED = "admin.user_deleted"
AUDIT_ACTION_WORKSPACE_DELETED = "admin.workspace_deleted"


# ---------------------------------------------------------------------------
# Super admin gate (Req 12.1, 12.2)
# ---------------------------------------------------------------------------


async def require_superadmin(
    ctx: RequestContext = Depends(require_session),
) -> RequestContext:
    """Dependency: allow only platform super admins; otherwise **403**.

    Built on :func:`app.api.deps.require_session`, so a caller must first hold a
    valid Session_Token (401 otherwise). If the authenticated caller is not a
    super admin, the request is rejected with **403** — the ``/api/v1/admin/*``
    surface is invisible to ordinary users (Req 12.1, 12.2). Returns the context
    unchanged so handlers can record the acting super admin.
    """
    if not ctx.is_superadmin:
        raise APIError(
            status_code=403,
            code="forbidden",
            message="Super admin privileges are required.",
        )
    return ctx


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ReassignOwnerRequest(BaseRequest):
    """Body for reassigning workspace ownership (Req 12.5)."""

    new_owner_user_id: uuid.UUID


class SetRoleRequest(BaseRequest):
    """Body for toggling a user's platform super-admin role."""

    is_superadmin: bool


class UpdateUserRequest(BaseRequest):
    """Body for editing a user's display name (the only editable field)."""

    name: str


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_user(
    entry: admin_service.UserDirectoryEntry, auth_provider: str
) -> dict:
    """Render a directory entry as a safe, secret-free user view (Req 12.3).

    Only non-sensitive identity fields are exposed; no OAuth credential or
    session token is ever included.
    """
    return {
        "id": str(entry.id),
        "email": entry.email,
        "name": entry.name,
        "auth_provider": auth_provider,
        "is_superadmin": entry.is_superadmin,
        "is_banned": entry.is_banned,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Platform user directory across all workspaces (Req 12.3).

    Super-admin only (via :func:`require_superadmin`). Returns every user's safe
    view; the listing is deliberately global rather than tenant-scoped because a
    super admin operates the whole platform.
    """
    entries = await admin_service.list_users(session)
    # Resolve each user's auth_provider in one supplemental query so the safe
    # view can include it without leaking anything sensitive.
    provider_rows = (
        await session.execute(select(User.id, User.auth_provider))
    ).all()
    providers = {user_id: provider for user_id, provider in provider_rows}
    return {
        "users": [
            _serialize_user(entry, str(providers.get(entry.id, "")))
            for entry in entries
        ]
    }


@router.post("/users/{user_id}/ban")
async def ban_user(
    user_id: uuid.UUID,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ban a user: invalidate active sessions and block future auth (Req 12.4).

    Super-admin only. Delegates to :func:`app.services.admin_service.ban_user`,
    which deletes the target's session rows and sets the durable ``is_banned``
    flag, then writes an ``admin.user_banned`` audit row and commits so the ban
    and its audit trail are atomic.
    """
    user = await admin_service.ban_user(session, target_user_id=user_id)
    await audit_service.record(
        session,
        workspace_id=None,
        user_id=ctx.user_id,
        action=AUDIT_ACTION_USER_BANNED,
        metadata={"target_user_id": str(user_id)},
    )
    await session.commit()
    logger.info(
        "admin.user_banned actor=%s target=%s", ctx.user_id, user_id
    )
    return {"status": "banned", "user_id": str(user.id)}


@router.post("/users/{user_id}/role")
async def set_user_role(
    user_id: uuid.UUID,
    body: SetRoleRequest,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Toggle a user's platform super-admin role.

    Super-admin only. Delegates to
    :func:`app.services.admin_service.set_superadmin`, which sets the flag but
    refuses a demotion that would remove the last remaining super admin (409),
    then writes an ``admin.user_role_changed`` audit row and commits so the
    change and its audit trail are atomic.

    Returns **404** if the user does not exist and **409** if the change would
    leave the platform with no super admin (both surfaced by the service).
    """
    user = await admin_service.set_superadmin(
        session,
        target_user_id=user_id,
        is_superadmin=body.is_superadmin,
        acting_user_id=ctx.user_id,
    )
    await audit_service.record(
        session,
        workspace_id=None,
        user_id=ctx.user_id,
        action=AUDIT_ACTION_USER_ROLE_CHANGED,
        metadata={
            "target_user_id": str(user_id),
            "is_superadmin": body.is_superadmin,
        },
    )
    await session.commit()
    logger.info(
        "admin.user_role_changed actor=%s target=%s is_superadmin=%s",
        ctx.user_id,
        user_id,
        body.is_superadmin,
    )
    return {
        "status": "updated",
        "user_id": str(user.id),
        "is_superadmin": bool(user.is_superadmin),
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Edit a user's display name (Req 12.3 directory maintenance).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.update_user`, which validates the name is
    non-empty (400) and updates it, then writes an ``admin.user_updated`` audit
    row and commits. Audit metadata is kept minimal (just the target id).

    Returns the same safe, secret-free user view as the directory. Returns
    **404** if the user does not exist and **400** if the name is blank.
    """
    user = await admin_service.update_user(
        session,
        target_user_id=user_id,
        name=body.name,
    )
    await audit_service.record(
        session,
        workspace_id=None,
        user_id=ctx.user_id,
        action=AUDIT_ACTION_USER_UPDATED,
        metadata={"target_user_id": str(user_id)},
    )
    await session.commit()
    logger.info("admin.user_updated actor=%s target=%s", ctx.user_id, user_id)
    entry = admin_service.UserDirectoryEntry(
        id=user.id,
        email=user.email,
        name=user.name,
        is_superadmin=bool(user.is_superadmin),
        is_banned=bool(user.is_banned),
    )
    return _serialize_user(entry, str(user.auth_provider.value))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: uuid.UUID,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Hard-delete a user, subject to safety guardrails.

    Super-admin only. Delegates to
    :func:`app.services.admin_service.delete_user`, which refuses to delete the
    acting user's own account, the last remaining super admin, or a user who
    created any workspace (``workspaces.created_by_user_id`` is ON DELETE
    RESTRICT) — each a **409 conflict**. On success the row is removed
    (dependent rows cascade or SET NULL per the FKs), an ``admin.user_deleted``
    audit row is written, and the transaction commits.

    Returns **404** if the user does not exist and **409** for any guardrail.
    """
    deleted_id = await admin_service.delete_user(
        session,
        target_user_id=user_id,
        acting_user_id=ctx.user_id,
    )
    await audit_service.record(
        session,
        workspace_id=None,
        user_id=ctx.user_id,
        action=AUDIT_ACTION_USER_DELETED,
        metadata={"target_user_id": str(user_id)},
    )
    await session.commit()
    logger.info("admin.user_deleted actor=%s target=%s", ctx.user_id, user_id)
    return {"status": "deleted", "user_id": str(deleted_id)}


@router.get("/workspaces")
async def list_workspaces(
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Platform workspace directory across all tenants (Req 12.3, 12.5).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.list_workspaces`, returning every
    workspace's basic fields plus its ``member_count`` and current
    ``owner_user_id`` (``null`` when a workspace has no Owner member). The
    listing is deliberately global rather than tenant-scoped. Read-only; no
    commit and no secrets in the response.
    """
    entries = await admin_service.list_workspaces(session)
    return {
        "workspaces": [
            {
                "id": str(entry.id),
                "name": entry.name,
                "slug": entry.slug,
                "created_by_user_id": str(entry.created_by_user_id),
                "created_at": entry.created_at.isoformat(),
                "member_count": entry.member_count,
                "owner_user_id": (
                    str(entry.owner_user_id)
                    if entry.owner_user_id is not None
                    else None
                ),
            }
            for entry in entries
        ]
    }


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Hard-delete a workspace and cascade its dependents.

    Super-admin only. Delegates to
    :func:`app.services.admin_service.delete_workspace`, which removes the
    ``workspaces`` row. Every FK referencing ``workspaces.id`` is
    ``ON DELETE CASCADE`` (members, invites, integrations, rules, agent
    sessions, approvals) EXCEPT ``system_audit_logs.workspace_id`` which is
    ``ON DELETE SET NULL`` — so the audit trail is retained (just
    de-attributed) rather than lost. No referencing FK is ``RESTRICT``, so the
    delete has no integrity blockers. Writes an ``admin.workspace_deleted``
    audit row (``workspace_id=None`` since the workspace is being removed; the
    id is preserved in the metadata) and commits so the delete and its audit
    trail are atomic.

    Returns **404** if the workspace does not exist (surfaced by the service).
    """
    deleted_id = await admin_service.delete_workspace(
        session, workspace_id=workspace_id
    )
    await audit_service.record(
        session,
        workspace_id=None,
        user_id=ctx.user_id,
        action=AUDIT_ACTION_WORKSPACE_DELETED,
        metadata={"workspace_id": str(workspace_id)},
    )
    await session.commit()
    logger.info(
        "admin.workspace_deleted actor=%s workspace=%s", ctx.user_id, workspace_id
    )
    return {"status": "deleted", "workspace_id": str(deleted_id)}


@router.post("/workspaces/{workspace_id}/reassign-owner")
async def reassign_owner(
    workspace_id: uuid.UUID,
    body: ReassignOwnerRequest,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Make the target user the Owner of a workspace (Req 12.5).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.reassign_ownership` (which creates or
    promotes the target to Owner and demotes any previous Owner to Admin), then
    writes an ``admin.owner_reassigned`` audit row and commits.
    """
    member = await admin_service.reassign_ownership(
        session,
        workspace_id=workspace_id,
        new_owner_user_id=body.new_owner_user_id,
    )
    await audit_service.record(
        session,
        workspace_id=workspace_id,
        user_id=ctx.user_id,
        action=AUDIT_ACTION_OWNER_REASSIGNED,
        metadata={"new_owner_user_id": str(body.new_owner_user_id)},
    )
    await session.commit()
    logger.info(
        "admin.owner_reassigned actor=%s workspace=%s new_owner=%s",
        ctx.user_id,
        workspace_id,
        body.new_owner_user_id,
    )
    return {
        "status": "reassigned",
        "workspace_id": str(workspace_id),
        "new_owner_user_id": str(member.user_id),
        "role": member.role.value,
    }


@router.get("/sessions")
async def list_sessions(
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List every ``running`` agent session platform-wide, with traces (Req 13.1).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.list_active_sessions`, which returns all
    ``running`` sessions across every workspace (never the tenant-scoped view)
    including each session's ``execution_logs`` reasoning trace so a super admin
    can inspect what a live agent is doing. Read-only; no commit.
    """
    entries = await admin_service.list_active_sessions(session)
    return {
        "sessions": [
            {
                "id": str(entry.id),
                "workspace_id": str(entry.workspace_id),
                "triggered_by_user_id": str(entry.triggered_by_user_id),
                "thread_id": entry.thread_id,
                "status": entry.status.value,
                "execution_logs": entry.execution_logs,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ]
    }


@router.post("/sessions/{agent_session_id}/kill")
async def kill_session(
    agent_session_id: uuid.UUID,
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Emergency kill-switch for a running agent session (Req 13.2, 13.3, 13.4).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.kill_session`, which terminates the
    session (terminal status), cancels its ``pending`` approvals, and writes an
    ``admin.session_killed`` audit row recording the acting super admin and the
    targeted session. Commits so the termination and its audit trail are atomic.

    Returns **404** if the session does not exist and **409** if it is not
    currently running (both surfaced by the service).
    """
    result = await admin_service.kill_session(
        session,
        agent_session_id=agent_session_id,
        acting_superadmin_id=ctx.user_id,
    )
    await session.commit()
    logger.info(
        "admin.session_killed actor=%s session=%s cancelled_approvals=%d",
        ctx.user_id,
        agent_session_id,
        result.cancelled_approvals,
    )
    return {
        "agent_session_id": str(result.agent_session_id),
        "status": result.status.value,
        "cancelled_approvals": result.cancelled_approvals,
    }


@router.get("/analytics")
async def system_analytics(
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Platform-wide system analytics (Req 14.2).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.system_analytics`, returning global
    aggregate agent token usage, the count of active (``running``) multi-tenant
    sessions, and database storage metrics (on-disk size plus core-table row
    counts). Read-only; no commit and no secrets in the response.
    """
    analytics = await admin_service.system_analytics(session)
    return {
        "tokens_total": analytics.tokens_total,
        "active_sessions": analytics.active_sessions,
        "storage_bytes": analytics.storage_bytes,
        "table_counts": analytics.table_counts,
    }


@router.get("/mcp-health")
async def mcp_health(
    ctx: RequestContext = Depends(require_superadmin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """MCP health registry: per-provider status, error rate, token usage (Req 14.1).

    Super-admin only. Delegates to
    :func:`app.services.admin_service.mcp_health`, which aggregates the persisted
    integration statuses (which back the MCP tool servers) per provider into a
    status/error-rate breakdown, alongside platform-wide token usage. Health is
    derived from stored integration status/error state and token metrics, not
    live network pings. Read-only; no commit and no secrets in the response.
    """
    health = await admin_service.mcp_health(session)
    return {
        "providers": [
            {
                "name": entry.name,
                "total": entry.total,
                "active": entry.active,
                "error": entry.error,
                "disconnected": entry.disconnected,
                "error_rate": entry.error_rate,
                "tokens_used": entry.tokens_used,
            }
            for entry in health.providers
        ],
        "tokens_total": health.tokens_total,
    }


__all__ = [
    "router",
    "require_superadmin",
    "ReassignOwnerRequest",
    "SetRoleRequest",
    "UpdateUserRequest",
    "AUDIT_ACTION_USER_BANNED",
    "AUDIT_ACTION_OWNER_REASSIGNED",
    "AUDIT_ACTION_SESSION_KILLED",
    "AUDIT_ACTION_USER_ROLE_CHANGED",
    "AUDIT_ACTION_USER_UPDATED",
    "AUDIT_ACTION_USER_DELETED",
    "AUDIT_ACTION_WORKSPACE_DELETED",
]
