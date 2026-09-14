"""Append-only Audit_Service (task 15.1).

Consolidates the audit-write pattern the routers use today (auth, workspaces,
integrations, rules each insert a :class:`~app.db.models.SystemAuditLog` row and
commit) into a single reusable service that records security-relevant actions:

- state-changing workspace operations (Req 15.1),
- authentication events such as login/logout (Req 15.2),
- approval resolutions (Req 15.3).

Every recorded ``metadata`` payload is passed through
:func:`app.core.scrubbing.scrub` before it is persisted, so an OAuth credential
or Session_Token can never land in the audit table (Req 15.4).

Append-only (Req 15.5) is enforced at two layers:

1. **Application layer** — this module exposes *only* :func:`record` (plus
   read/query helpers :func:`get_audit_log` and :func:`list_audit_logs`). There
   is deliberately **no** update or delete function; there is no supported code
   path through this service to mutate or remove an audit row.
2. **Database layer** — a trigger created by the Alembic migration rejects
   ``UPDATE``/``DELETE`` on ``system_audit_logs`` regardless of caller.

Transaction ownership: the caller owns the surrounding transaction. :func:`record`
adds the row and ``flush``es it (so the generated ``id``/``timestamp`` are
populated and any DB error surfaces immediately) but never ``commit``s. This
matches the service pattern used elsewhere in the codebase (``rules_service``,
``workspace_service``): the router/endpoint issues the ``commit`` once all of
its writes — including the audit row — succeed. Auth events are not
workspace-scoped, so ``workspace_id`` may be ``None``.

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scrubbing import scrub
from app.core.tenancy import RequestContext
from app.db.models import SystemAuditLog

# Audit action name conventions (dot-namespaced ``domain.event`` strings). These
# are provided for callers/tests but ``record`` accepts any action string.
ACTION_WORKSPACE_CREATED = "workspace.created"
ACTION_WORKSPACE_DELETED = "workspace.deleted"
ACTION_AUTH_LOGIN = "auth.login"
ACTION_AUTH_LOGOUT = "auth.logout"
ACTION_APPROVAL_APPROVED = "approval.approved"
ACTION_APPROVAL_REJECTED = "approval.rejected"


async def record(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    action: str,
    metadata: Mapping[str, Any] | list | None = None,
) -> SystemAuditLog:
    """Append one audit entry to ``system_audit_logs`` (Req 15.1, 15.2, 15.3).

    This is the single write path for the audit log. It records a
    state-changing workspace operation, an authentication event, or an approval
    resolution — the distinction is carried by ``action`` (see the
    ``ACTION_*`` constants) rather than by separate methods.

    ``metadata`` is scrubbed with :func:`app.core.scrubbing.scrub` before being
    persisted to the ``metadata`` column (mapped as the ``log_metadata``
    attribute on the model), so no OAuth credential or Session_Token value is
    ever stored (Req 15.4). ``None`` metadata is stored as ``NULL``.

    The caller owns the transaction: this adds the row and ``flush``es (so
    ``id``/``timestamp`` are populated) but does not ``commit`` — the caller
    commits once all of its writes succeed.

    Args:
        session: The active async session/transaction.
        workspace_id: The owning workspace, or ``None`` for non-workspace-scoped
            events (e.g. authentication).
        user_id: The acting user, or ``None`` when unknown/unattributable.
        action: The dot-namespaced action name (e.g. ``"workspace.deleted"``).
        metadata: Optional structured context. Scrubbed before persistence.

    Returns:
        The persisted (flushed) :class:`~app.db.models.SystemAuditLog` row.
    """
    entry = SystemAuditLog(
        workspace_id=workspace_id,
        user_id=user_id,
        action=action,
        log_metadata=scrub(dict(metadata) if isinstance(metadata, Mapping) else metadata)
        if metadata is not None
        else None,
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_for_context(
    session: AsyncSession,
    ctx: RequestContext,
    *,
    action: str,
    metadata: Mapping[str, Any] | list | None = None,
) -> SystemAuditLog:
    """Record an audit entry using a :class:`RequestContext` (Req 15.1).

    Convenience wrapper over :func:`record` that pulls ``workspace_id`` from the
    context's active workspace and ``user_id`` from the acting caller — the
    common shape for a workspace-scoped, state-changing operation. Delegates to
    :func:`record`, so the same scrubbing (Req 15.4) and transaction ownership
    apply.
    """
    return await record(
        session,
        workspace_id=ctx.active_workspace_id,
        user_id=ctx.user_id,
        action=action,
        metadata=metadata,
    )


async def get_audit_log(
    session: AsyncSession, audit_log_id: uuid.UUID
) -> SystemAuditLog | None:
    """Return a single audit row by id, or ``None`` if it does not exist.

    Read-only helper. There is intentionally no update/delete counterpart:
    the audit log is append-only (Req 15.5).
    """
    return await session.get(SystemAuditLog, audit_log_id)


async def list_audit_logs(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    limit: int | None = None,
) -> list[SystemAuditLog]:
    """Return audit rows, newest first, optionally filtered (read-only).

    Filters by ``workspace_id`` and/or ``user_id`` when provided. This is a
    read path only; the module exposes no way to mutate or delete rows
    (append-only, Req 15.5).
    """
    stmt = select(SystemAuditLog)
    if workspace_id is not None:
        stmt = stmt.where(SystemAuditLog.workspace_id == workspace_id)
    if user_id is not None:
        stmt = stmt.where(SystemAuditLog.user_id == user_id)
    stmt = stmt.order_by(SystemAuditLog.timestamp.desc(), SystemAuditLog.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.scalars(stmt)
    return list(result.all())


__all__ = [
    "record",
    "record_for_context",
    "get_audit_log",
    "list_audit_logs",
    "ACTION_WORKSPACE_CREATED",
    "ACTION_WORKSPACE_DELETED",
    "ACTION_AUTH_LOGIN",
    "ACTION_AUTH_LOGOUT",
    "ACTION_APPROVAL_APPROVED",
    "ACTION_APPROVAL_REJECTED",
]
