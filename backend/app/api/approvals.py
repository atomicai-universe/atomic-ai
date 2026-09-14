"""Approvals router — list the review queue and resolve approvals (task 13.7).

This module wires the ``Approval_Hub`` resolution service (task 13.3,
:mod:`app.services.approval_service`) and the ``WebSocket_Gateway`` broadcast
(task 13.5, :mod:`app.api.ws`) into protected HTTP endpoints under the
``/api/v1/approvals`` prefix. Every route requires a valid Session_Token
(:func:`app.api.deps.require_session`).

Authorization model
-------------------

Two distinct authorization levels apply (Req 10.5, 4.3, 16.2):

- **View the queue** requires only workspace **membership** (any of
  Owner/Admin/Member/Viewer). A non-member is rejected with **404** so the
  endpoint never discloses the workspace's existence to an outsider.
- **Resolve** (approve/reject) requires the caller's role to hold
  :attr:`~app.core.rbac.Capability.RESOLVE_APPROVAL` — i.e. **Owner** or
  **Admin** (Req 10.5). A Viewer/Member who is a member of the owning workspace
  is rejected with **403**; a non-member is rejected with **404** (existence not
  disclosed). The role is resolved from the request context against the
  *owning* workspace of the loaded request, never a client-supplied workspace.

Broadcast on resolution
------------------------

After a successful approve/reject the terminal event is fanned out to the
authorized live reviewers of the owning workspace via the module-level
:data:`app.api.ws.manager` singleton's
:meth:`~app.api.ws.ConnectionManager.broadcast_to_authorized`. The payload is a
``{"type": "approval.resolved", "approval_request_id", "status",
"workspace_id"}`` message; :meth:`broadcast_to_authorized` scrubs it before
delivery and filters to Owner/Admin recipients (Req 10.8, 11.3), so a broadcast
carries no secret and reaches only reviewers.

The service (:func:`app.services.approval_service.resolve_request`) owns the
state machine and audit row: a missing request raises **404** and an
already-terminal request raises **409** (Req 10.7); both are
:class:`~app.core.errors.APIError` and are allowed to propagate to the central
error handler.

Requirements: 10.5 (also uses 10.7, 10.8, 4.3, 16.2, 11.3).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_session
from app.api.ws import manager
from app.core.errors import APIError
from app.core.rbac import Capability, can
from app.core.tenancy import RequestContext
from app.db.models import ApprovalRequest, ApprovalStatus, MemberRole
from app.db.session import get_session
from app.services import approval_service

logger = logging.getLogger("atomic_ai.approvals")

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

# Message type published on resolution (consumed by the WebSocket_Gateway
# clients, Req 10.8).
BROADCAST_TYPE_RESOLVED = "approval.resolved"


# ---------------------------------------------------------------------------
# Authorization helpers (resolve role directly from ctx)
# ---------------------------------------------------------------------------


def _require_member(ctx: RequestContext, workspace_id: uuid.UUID) -> MemberRole:
    """Return the caller's role in ``workspace_id`` or raise (non-member → 404).

    Membership is the floor for every approvals operation. A non-member is
    rejected with **404** so the endpoint does not disclose the workspace's
    existence to someone outside it (Req 4.3, 16.2).
    """
    role = ctx.member_role(workspace_id)
    if role is None:
        raise APIError(
            status_code=404, code="not_found", message="Workspace not found."
        )
    return role


def _require_resolver(ctx: RequestContext, workspace_id: uuid.UUID) -> MemberRole:
    """Authorize a resolve (approve/reject) and return the caller's role.

    First requires membership (non-member → 404, existence not disclosed), then
    requires the role to hold ``RESOLVE_APPROVAL`` (Owner/Admin). A member whose
    role lacks the capability — a Viewer or Member — is rejected with **403**
    (Req 10.5). The decision consults :func:`app.core.rbac.can` so it stays on
    the single role→capability source of truth.
    """
    role = _require_member(ctx, workspace_id)
    if not can(role, Capability.RESOLVE_APPROVAL):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="Only workspace owners or admins may resolve approvals.",
        )
    return role


# ---------------------------------------------------------------------------
# Response serialization (secret-free safe view)
# ---------------------------------------------------------------------------


def _serialize_approval(request: ApprovalRequest) -> dict:
    """Render an :class:`~app.db.models.ApprovalRequest` as a plain safe dict.

    ``arguments`` were already scrubbed when the request was created (see
    :func:`app.services.approval_service.make_before_tool_call`), so no
    secret-looking value is present to return.
    """
    return {
        "id": str(request.id),
        "tool_name": request.tool_name,
        "arguments": request.arguments,
        "status": request.status.value,
        "triggered_by_user_id": str(request.triggered_by_user_id),
        "reviewed_by_user_id": (
            str(request.reviewed_by_user_id)
            if request.reviewed_by_user_id is not None
            else None
        ),
        "agent_session_id": (
            str(request.agent_session_id)
            if request.agent_session_id is not None
            else None
        ),
        "created_at": request.created_at.isoformat()
        if request.created_at is not None
        else None,
        # Present when this pending reply was scheduled via "Approve and
        # Schedule" (stored in arguments, NOT a status/enum). Lets the UI show
        # "Scheduled for <time>". ``None`` when not scheduled.
        "scheduled_send_at": (
            request.arguments.get("scheduled_send_at")
            if isinstance(request.arguments, dict)
            else None
        ),
    }


async def _broadcast_resolution(request: ApprovalRequest) -> None:
    """Fan out the terminal event to authorized reviewers of the workspace.

    Publishes a ``approval.resolved`` message through the WebSocket_Gateway
    singleton; delivery is scrubbed and filtered to Owner/Admin recipients of
    the owning workspace (Req 10.8, 11.3). A broadcast failure is swallowed so a
    dead subscriber can never fail the resolution the reviewer already made.
    """
    message = {
        "type": BROADCAST_TYPE_RESOLVED,
        "approval_request_id": str(request.id),
        "status": request.status.value,
        "workspace_id": str(request.workspace_id),
    }
    try:
        await manager.broadcast_to_authorized(request.workspace_id, message)
    except Exception:  # noqa: BLE001 - a broken subscriber must not fail resolution
        logger.warning(
            "approval.resolved broadcast failed workspace_id=%s approval_request_id=%s",
            request.workspace_id,
            request.id,
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_approvals(
    workspace_id: uuid.UUID,
    status: ApprovalStatus | None = None,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List a workspace's approval requests (any member; VIEW).

    Requires only membership in ``workspace_id`` (non-members get 404, Req 4.3,
    16.2). Optionally filters by ``status`` (e.g. ``?status=pending`` for the
    live review queue). Reads only, so no audit row is written and no broadcast
    is emitted.
    """
    _require_member(ctx, workspace_id)

    stmt = select(ApprovalRequest).where(
        ApprovalRequest.workspace_id == workspace_id
    )
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    # Newest-first so the most recently generated reply is position 1 / top of
    # the queue (ERROR.md Task 4). This matches the voice POSITION resolution in
    # voice_gateway/_refresh_pending_cache and voice_tools._list_pending_approvals
    # (both created_at DESC), so on-screen numbers and spoken positions agree.
    stmt = stmt.order_by(ApprovalRequest.created_at.desc())

    result = await session.execute(stmt)
    requests = result.scalars().all()
    return {"approvals": [_serialize_approval(r) for r in requests]}


async def _resolve(
    approval_request_id: uuid.UUID,
    action: approval_service.ResolutionAction,
    ctx: RequestContext,
    session: AsyncSession,
) -> dict:
    """Shared approve/reject flow: authorize, resolve, broadcast, serialize.

    Loads the request to discover its owning workspace, authorizes the caller as
    a resolver of *that* workspace (non-member → 404, Viewer/Member → 403,
    Req 10.5), then delegates to the service which enforces the state machine and
    writes the audit row (missing → 404, terminal → 409, Req 10.7). On success it
    broadcasts the terminal event to authorized reviewers (Req 10.8) and returns
    the updated safe view.
    """
    request = await session.get(ApprovalRequest, approval_request_id)
    if request is None:
        # Existence not disclosed differently from the service's own 404.
        raise APIError(
            status_code=404,
            code="not_found",
            message="The approval request was not found.",
        )

    role = _require_resolver(ctx, request.workspace_id)

    updated = await approval_service.resolve_request(
        session,
        approval_request_id=approval_request_id,
        reviewer_user_id=ctx.user_id,
        action=action,
        reviewer_role=role,
    )

    await _broadcast_resolution(updated)
    logger.info(
        "approval.%s workspace_id=%s user_id=%s approval_request_id=%s",
        action,
        updated.workspace_id,
        ctx.user_id,
        updated.id,
    )
    return _serialize_approval(updated)


@router.post("/{approval_request_id}/approve")
async def approve_approval(
    approval_request_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve a pending request (Owner/Admin only, Req 10.5).

    A Viewer/Member of the owning workspace is rejected with **403**; a
    non-member with **404**. An already-resolved request yields **409** and a
    missing one **404** (Req 10.7). On success the request is approved, the
    reviewer recorded, an audit row written, and the terminal event broadcast to
    authorized reviewers (Req 10.8).
    """
    return await _resolve(approval_request_id, "approve", ctx, session)


@router.post("/{approval_request_id}/reject")
async def reject_approval(
    approval_request_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reject a pending request (Owner/Admin only, Req 10.5).

    Same authorization and conflict semantics as
    :func:`approve_approval`; on success the request is rejected, the reviewer
    recorded, an audit row written, and the terminal event broadcast (Req 10.8).
    """
    return await _resolve(approval_request_id, "reject", ctx, session)


@router.post("/clear-pending")
async def clear_pending_approvals(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Clear ALL pending approvals in a workspace by rejecting them (bulk).

    Owner/Admin only (same resolver auth as reject): a Viewer/Member of the
    workspace is rejected with **403**, a non-member with **404**. This is a
    bulk REJECT that PRESERVES the audit trail (one ``approval.rejected`` audit
    row per cleared reply) — it is NOT a permanent delete (ERROR.md Task 3).
    Idempotent: clearing when nothing is pending returns ``{"cleared": 0}``.

    On success a single ``approval.updated`` event is broadcast so live
    reviewers refetch the (now-empty) queue.
    """
    role = _require_resolver(ctx, workspace_id)

    cleared = await approval_service.reject_all_pending(
        session,
        workspace_id=workspace_id,
        reviewer_user_id=ctx.user_id,
        reviewer_role=role,
    )

    if cleared:
        try:
            await manager.broadcast_to_authorized(
                workspace_id,
                {
                    "type": BROADCAST_TYPE_UPDATED,
                    "workspace_id": str(workspace_id),
                    "cleared": cleared,
                },
            )
        except Exception:  # noqa: BLE001 - a broken subscriber must not fail the clear
            logger.warning(
                "approval.clear broadcast failed workspace_id=%s", workspace_id
            )

    logger.info(
        "approval.clear_pending workspace_id=%s user_id=%s cleared=%s",
        workspace_id,
        ctx.user_id,
        cleared,
    )
    return {"cleared": cleared}


# ---------------------------------------------------------------------------
# Edit / Regenerate a PENDING reply (PART 3)
# ---------------------------------------------------------------------------

# Message type published when a pending approval is edited/regenerated so live
# reviewers refetch the updated draft.
BROADCAST_TYPE_UPDATED = "approval.updated"


class _EditApprovalBody(BaseModel):
    """Body for ``PATCH /{id}`` — direct edits to the AI reply (all optional)."""

    subject: str | None = None
    body: str | None = None
    to: str | None = None


async def _broadcast_update(request: ApprovalRequest) -> None:
    """Fan out an ``approval.updated`` event so live reviewers refetch (optional)."""
    message = {
        "type": BROADCAST_TYPE_UPDATED,
        "approval_request_id": str(request.id),
        "status": request.status.value,
        "workspace_id": str(request.workspace_id),
    }
    try:
        await manager.broadcast_to_authorized(request.workspace_id, message)
    except Exception:  # noqa: BLE001 - a broken subscriber must not fail the edit
        logger.warning(
            "approval.updated broadcast failed workspace_id=%s approval_request_id=%s",
            request.workspace_id,
            request.id,
        )


async def _load_and_authorize_resolver(
    approval_request_id: uuid.UUID,
    ctx: RequestContext,
    session: AsyncSession,
) -> ApprovalRequest:
    """Load a request (404 if missing) and authorize the caller as a resolver.

    Same auth as resolve: non-member -> 404 (existence not disclosed),
    Viewer/Member -> 403 (Req 10.5).
    """
    request = await session.get(ApprovalRequest, approval_request_id)
    if request is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="The approval request was not found.",
        )
    _require_resolver(ctx, request.workspace_id)
    return request


@router.post("/{approval_request_id}/regenerate")
async def regenerate_approval(
    approval_request_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Regenerate a NEW reply body for a pending approval's same source email.

    Owner/Admin only (same auth as resolve). Pending-only (409 otherwise),
    missing -> 404. Returns the updated serialized approval and broadcasts an
    ``approval.updated`` event (best-effort).
    """
    await _load_and_authorize_resolver(approval_request_id, ctx, session)
    updated = await approval_service.regenerate_request(
        session, approval_request_id=approval_request_id
    )
    await _broadcast_update(updated)
    logger.info(
        "approval.regenerate workspace_id=%s user_id=%s approval_request_id=%s",
        updated.workspace_id,
        ctx.user_id,
        updated.id,
    )
    return _serialize_approval(updated)


@router.patch("/{approval_request_id}")
async def edit_approval(
    approval_request_id: uuid.UUID,
    payload: _EditApprovalBody = Body(default_factory=_EditApprovalBody),
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Directly edit a pending reply's Subject/Body/To, rebuilding the draft.

    Owner/Admin only. Pending-only (409), missing -> 404, empty to/body -> 422.
    Returns the updated serialized approval and broadcasts ``approval.updated``.
    """
    await _load_and_authorize_resolver(approval_request_id, ctx, session)
    updated = await approval_service.edit_request(
        session,
        approval_request_id=approval_request_id,
        subject=payload.subject,
        body=payload.body,
        to=payload.to,
    )
    await _broadcast_update(updated)
    logger.info(
        "approval.edit workspace_id=%s user_id=%s approval_request_id=%s",
        updated.workspace_id,
        ctx.user_id,
        updated.id,
    )
    return _serialize_approval(updated)


# ---------------------------------------------------------------------------
# Approve + execute (create draft / send) — PART 4
# ---------------------------------------------------------------------------


async def _approve_execute(
    approval_request_id: uuid.UUID,
    execution_action: approval_service.ExecutionAction,
    ctx: RequestContext,
    session: AsyncSession,
) -> dict:
    """Shared approve-and-execute flow: authorize, execute+approve, broadcast."""
    request = await _load_and_authorize_resolver(approval_request_id, ctx, session)
    role = ctx.member_role(request.workspace_id)

    updated = await approval_service.execute_and_approve_request(
        session,
        approval_request_id=approval_request_id,
        reviewer_user_id=ctx.user_id,
        execution_action=execution_action,
        reviewer_role=role,
    )
    await _broadcast_resolution(updated)
    logger.info(
        "approval.%s workspace_id=%s user_id=%s approval_request_id=%s",
        execution_action,
        updated.workspace_id,
        ctx.user_id,
        updated.id,
    )
    return _serialize_approval(updated)


@router.post("/{approval_request_id}/approve-draft")
async def approve_draft(
    approval_request_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve a pending reply AND create the Gmail draft for real (PART 4).

    Owner/Admin only. Pending-only (409), missing -> 404. Decrypts the
    workspace's Gmail credentials, creates the draft, marks the original email
    read, and only then marks the approval approved. A Gmail failure leaves the
    request pending (502).
    """
    return await _approve_execute(approval_request_id, "save_to_draft", ctx, session)


@router.post("/{approval_request_id}/approve-send")
async def approve_send(
    approval_request_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve a pending reply AND send it immediately (PART 4).

    Owner/Admin only. Pending-only (409), missing -> 404. Sends via
    ``messages/send`` (Gmail files it in Sent), marks the original email read,
    then marks the approval approved. A Gmail failure leaves the request pending.
    """
    return await _approve_execute(approval_request_id, "send", ctx, session)


# ---------------------------------------------------------------------------
# Approve and Schedule — send automatically at a chosen future time (local cron)
# ---------------------------------------------------------------------------


class _ScheduleApprovalBody(BaseModel):
    """Body for ``POST /{id}/approve-schedule`` — when to auto-send the reply."""

    scheduled_send_at: datetime


@router.post("/{approval_request_id}/approve-schedule")
async def approve_schedule(
    approval_request_id: uuid.UUID,
    payload: _ScheduleApprovalBody = Body(...),
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Schedule a pending reply to be SENT automatically at a future time.

    Owner/Admin only. Pending-only (409), missing -> 404, non-future time ->
    422. The approval stays ``pending``; the schedule is stored in its
    ``arguments`` and a worker cron
    (:func:`app.agents.tasks.send_scheduled_replies`) sweeps due ones every
    minute (with catch-up on worker startup). Returns the updated serialized
    approval (now carrying ``scheduled_send_at``) and broadcasts
    ``approval.updated`` so live reviewers refetch.
    """
    request = await _load_and_authorize_resolver(approval_request_id, ctx, session)
    role = ctx.member_role(request.workspace_id)

    updated = await approval_service.schedule_request(
        session,
        approval_request_id=approval_request_id,
        reviewer_user_id=ctx.user_id,
        scheduled_send_at=payload.scheduled_send_at,
        reviewer_role=role,
    )
    await _broadcast_update(updated)
    logger.info(
        "approval.scheduled workspace_id=%s user_id=%s approval_request_id=%s",
        updated.workspace_id,
        ctx.user_id,
        updated.id,
    )
    return _serialize_approval(updated)


__all__ = [
    "router",
    "BROADCAST_TYPE_RESOLVED",
    "BROADCAST_TYPE_UPDATED",
]
