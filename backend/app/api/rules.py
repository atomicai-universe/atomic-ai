"""Automation rules router — rule CRUD with RBAC and audit (task 10.3).

This module wires the pure/persistence pieces built in task 10.1
(:mod:`app.services.rules_service`) into protected HTTP endpoints under the
``/api/v1/rules`` prefix. Every route requires a valid Session_Token
(:func:`app.api.deps.require_session`), and every state-changing operation is
guarded by RBAC and writes an append-only audit row.

Authorization model
-------------------

The RBAC decision that matters here is :class:`~app.core.rbac.Capability`
``MANAGE_SHARED_RULES`` (held by **Owner/Admin** only — see
:data:`app.core.rbac.ROLE_CAPABILITIES`). The rule is (Req 8.5):

    If a Viewer or Member attempts to **create or modify a workspace-wide**
    Rule, the request is rejected with **403**.

This router resolves the caller's role directly from the request context —
``ctx.member_role(workspace_id)`` — and checks
``can(role, Capability.MANAGE_SHARED_RULES)`` itself, rather than using the
:func:`app.core.rbac.require` dependency factory. ``require`` authorizes against
the caller's *active* workspace, but these routes take the target
``workspace_id`` from the request body/query, so the role is resolved for that
workspace explicitly. Both paths share the same single source of truth
(:func:`app.core.rbac.can`) and the same 404-non-member / 403-insufficient
semantics.

Chosen scope policy (documented):

- **View / list** requires only workspace **membership** (any of
  Owner/Admin/Member/Viewer). Non-members get 404 so the workspace's existence
  is not disclosed (Req 4.3, 16.2).
- **Workspace-wide** rule create/modify/delete requires ``MANAGE_SHARED_RULES``
  (Owner/Admin); Viewer/Member are rejected with 403 (Req 8.5).
- A **personal-scope** (non-workspace-wide) rule may be created/modified by any
  member who can trigger workflows — i.e. **Member and above**. **Viewer** is
  read-only, so a Viewer attempting to create/modify/delete *any* rule is
  rejected with 403. (This is the ``TRIGGER_WORKFLOW`` capability, which
  Owner/Admin/Member hold and Viewer does not.)

Audit (Req 15.1). Each successful state-changing operation writes a
:class:`~app.db.models.SystemAuditLog` row recording the workspace, the acting
user, the action (``rule.created`` / ``rule.updated`` / ``rule.deleted``), and
scrubbed metadata; the operation and its audit row are committed together in one
transaction so an audit row is never orphaned from its effect. Metadata is run
through :func:`app.core.scrubbing.scrub` so no token/credential can leak
(Req 15.4). When task 15.1's Audit_Service lands, :func:`_write_rule_audit`
should delegate to it.

Requirements: 8.5, 15.1 (also uses 4.3, 15.4, 16.2).
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
from app.db.models import MemberRole, SystemAuditLog
from app.db.session import get_session
from app.schemas.base import BaseRequest
from app.services import rules_service

logger = logging.getLogger("atomic_ai.rules")

router = APIRouter(prefix="/api/v1/rules", tags=["rules"])

# Audit action names for rule operations (Req 15.1).
AUDIT_ACTION_CREATED = "rule.created"
AUDIT_ACTION_UPDATED = "rule.updated"
AUDIT_ACTION_DELETED = "rule.deleted"


# ---------------------------------------------------------------------------
# Request schemas (strict input, Req 17.1/17.2)
# ---------------------------------------------------------------------------


class CreateRuleRequest(BaseRequest):
    """Body for creating a rule.

    ``workspace_id`` names the owning workspace. ``is_workspace_wide`` (default
    ``False``) selects the shared vs personal scope that drives the RBAC check.
    """

    workspace_id: uuid.UUID
    category: str
    rule_prompt: str
    provider_name: str | None = None
    is_workspace_wide: bool = False
    is_active: bool = True


class UpdateRuleRequest(BaseRequest):
    """Body for updating a rule.

    ``workspace_id`` scopes the lookup for tenant isolation. Every mutable field
    is optional; only fields provided as non-``None`` are applied. To clear the
    provider scope (set ``provider_name`` to ``None``) pass
    ``update_provider_name=True`` together with ``provider_name=None``.
    """

    workspace_id: uuid.UUID
    category: str | None = None
    rule_prompt: str | None = None
    provider_name: str | None = None
    update_provider_name: bool = False
    is_workspace_wide: bool | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Authorization helpers (resolve role directly from ctx; Req 8.5)
# ---------------------------------------------------------------------------


def _require_member(ctx: RequestContext, workspace_id: uuid.UUID) -> MemberRole:
    """Return the caller's role in ``workspace_id`` or raise (non-member → 404).

    Membership is the floor for every rule operation. A non-member is rejected
    with **404** so the endpoint does not disclose the workspace's existence to
    someone outside it (Req 4.3, 16.2).
    """
    role = ctx.member_role(workspace_id)
    if role is None:
        raise APIError(status_code=404, code="not_found", message="Workspace not found.")
    return role


def _authorize_write(
    ctx: RequestContext, workspace_id: uuid.UUID, *, is_workspace_wide: bool
) -> MemberRole:
    """Authorize a rule create/modify/delete and return the caller's role.

    - Any workspace-wide write requires ``MANAGE_SHARED_RULES`` (Owner/Admin);
      Viewer/Member are rejected with **403** (Req 8.5).
    - A personal-scope write requires ``TRIGGER_WORKFLOW`` (Owner/Admin/Member);
      the read-only Viewer is rejected with **403**.

    Both decisions consult :func:`app.core.rbac.can` against the single
    role→capability map, so this stays consistent with the rest of the guard.
    """
    role = _require_member(ctx, workspace_id)
    capability = (
        Capability.MANAGE_SHARED_RULES
        if is_workspace_wide
        else Capability.TRIGGER_WORKFLOW
    )
    if not can(role, capability):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="Insufficient role for this operation.",
        )
    return role


async def _write_rule_audit(
    session: AsyncSession,
    *,
    action: str,
    ctx: RequestContext,
    workspace_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    """Append a rule Audit_Log row (Req 15.1).

    Records the workspace, the acting user, the action, and scrubbed metadata.
    Metadata passes through :func:`app.core.scrubbing.scrub` so no token or
    credential value is ever persisted (Req 15.4). The caller commits this
    alongside the operation so the audit row and its effect are atomic.
    """
    session.add(
        SystemAuditLog(
            workspace_id=workspace_id,
            user_id=ctx.user_id,
            action=action,
            log_metadata=scrub(metadata) if metadata is not None else None,
        )
    )


# ---------------------------------------------------------------------------
# Response serialization
# ---------------------------------------------------------------------------


def _serialize_rule(rule) -> dict:
    """Render a :class:`~app.db.models.Rule` as a plain, secret-free dict."""
    return {
        "id": str(rule.id),
        "workspace_id": str(rule.workspace_id),
        "created_by_user_id": str(rule.created_by_user_id),
        "category": rule.category,
        "provider_name": rule.provider_name,
        "rule_prompt": rule.rule_prompt,
        "is_workspace_wide": rule.is_workspace_wide,
        "is_active": rule.is_active,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_rule(
    body: CreateRuleRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a rule (Req 8.5, 15.1).

    A workspace-wide rule requires ``MANAGE_SHARED_RULES`` (Owner/Admin) — a
    Viewer/Member attempting it gets **403** (Req 8.5). A personal-scope rule is
    allowed for Member and above (Viewer is read-only → 403). On success the
    rule and a ``rule.created`` audit row are committed together (Req 15.1).
    """
    _authorize_write(ctx, body.workspace_id, is_workspace_wide=body.is_workspace_wide)

    rule = await rules_service.create_rule(
        session,
        workspace_id=body.workspace_id,
        created_by_user_id=ctx.user_id,
        category=body.category,
        rule_prompt=body.rule_prompt,
        provider_name=body.provider_name,
        is_workspace_wide=body.is_workspace_wide,
        is_active=body.is_active,
    )
    await _write_rule_audit(
        session,
        action=AUDIT_ACTION_CREATED,
        ctx=ctx,
        workspace_id=body.workspace_id,
        metadata={
            "rule_id": str(rule.id),
            "category": rule.category,
            "provider_name": rule.provider_name,
            "is_workspace_wide": rule.is_workspace_wide,
            "is_active": rule.is_active,
        },
    )
    await session.commit()
    logger.info(
        "rule.created workspace_id=%s user_id=%s rule_id=%s",
        body.workspace_id,
        ctx.user_id,
        rule.id,
    )
    return _serialize_rule(rule)


@router.get("")
async def list_rules(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List a workspace's rules (any member; VIEW).

    Requires only membership in ``workspace_id`` (non-members get 404). Reads
    only, so no audit row is written.
    """
    _require_member(ctx, workspace_id)
    rules = await rules_service.list_rules(session, workspace_id=workspace_id)
    return {"rules": [_serialize_rule(rule) for rule in rules]}


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    body: UpdateRuleRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update a rule (Req 8.5, 15.1).

    The write is authorized as workspace-wide when the rule *is* workspace-wide
    or is *being set* workspace-wide — in either case ``MANAGE_SHARED_RULES``
    (Owner/Admin) is required and a Viewer/Member is rejected with **403**
    (Req 8.5). A purely personal-scope edit needs ``TRIGGER_WORKFLOW``
    (Member+). On success a ``rule.updated`` audit row is committed with the
    change (Req 15.1).
    """
    # Determine current scope so a Viewer/Member cannot modify a workspace-wide
    # rule, and cannot promote a personal rule to workspace-wide either.
    existing = await rules_service.list_rules(session, workspace_id=body.workspace_id)
    current = next((r for r in existing if r.id == rule_id), None)
    if current is None:
        raise APIError(status_code=404, code="not_found", message="Rule not found.")

    becoming_wide = (
        body.is_workspace_wide
        if body.is_workspace_wide is not None
        else current.is_workspace_wide
    )
    treat_as_wide = bool(current.is_workspace_wide or becoming_wide)
    _authorize_write(ctx, body.workspace_id, is_workspace_wide=treat_as_wide)

    rule = await rules_service.update_rule(
        session,
        rule_id=rule_id,
        workspace_id=body.workspace_id,
        category=body.category,
        provider_name=body.provider_name,
        rule_prompt=body.rule_prompt,
        is_workspace_wide=body.is_workspace_wide,
        is_active=body.is_active,
        update_provider_name=body.update_provider_name,
    )
    await _write_rule_audit(
        session,
        action=AUDIT_ACTION_UPDATED,
        ctx=ctx,
        workspace_id=body.workspace_id,
        metadata={
            "rule_id": str(rule.id),
            "is_workspace_wide": rule.is_workspace_wide,
            "is_active": rule.is_active,
        },
    )
    await session.commit()
    logger.info(
        "rule.updated workspace_id=%s user_id=%s rule_id=%s",
        body.workspace_id,
        ctx.user_id,
        rule.id,
    )
    return _serialize_rule(rule)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a rule (Req 8.5, 15.1).

    Deleting a workspace-wide rule requires ``MANAGE_SHARED_RULES``
    (Owner/Admin); a Viewer/Member is rejected with **403** (Req 8.5). A
    personal-scope rule may be deleted by Member and above (Viewer → 403). On
    success a ``rule.deleted`` audit row is committed with the deletion
    (Req 15.1).
    """
    existing = await rules_service.list_rules(session, workspace_id=workspace_id)
    current = next((r for r in existing if r.id == rule_id), None)
    if current is None:
        raise APIError(status_code=404, code="not_found", message="Rule not found.")

    _authorize_write(
        ctx, workspace_id, is_workspace_wide=bool(current.is_workspace_wide)
    )

    await rules_service.delete_rule(
        session, rule_id=rule_id, workspace_id=workspace_id
    )
    await _write_rule_audit(
        session,
        action=AUDIT_ACTION_DELETED,
        ctx=ctx,
        workspace_id=workspace_id,
        metadata={
            "rule_id": str(rule_id),
            "is_workspace_wide": bool(current.is_workspace_wide),
        },
    )
    await session.commit()
    logger.info(
        "rule.deleted workspace_id=%s user_id=%s rule_id=%s",
        workspace_id,
        ctx.user_id,
        rule_id,
    )
    return None


__all__ = [
    "router",
    "AUDIT_ACTION_CREATED",
    "AUDIT_ACTION_UPDATED",
    "AUDIT_ACTION_DELETED",
    "CreateRuleRequest",
    "UpdateRuleRequest",
]
