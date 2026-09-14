"""Admin_Service — super admin directory, ban, and ownership reassignment.

Implements the super-admin control-plane operations from the design (see
design.md "Admin_Service"). Unlike every other service, these operations are
**global** rather than tenant-scoped: a super admin is a platform-wide operator,
so the directory intentionally crosses all workspaces (Req 12.3). The
``is_superadmin`` gate that authorizes these operations lives with the router
(:mod:`app.api.admin`), so this module assumes the caller has already been
authorized and focuses on the state transitions.

Operations:

- :func:`list_users` — the platform user directory across **all** workspaces
  (Req 12.3). Returns every user's basic fields and, optionally, each user's
  workspace memberships (``workspace_id`` + role). This is the one place that
  deliberately does not pass through :func:`app.core.tenancy.apply_tenant_scope`
  because a super admin's view is not bound to a single tenant.
- :func:`ban_user` — mark a user banned and cut off access (Req 12.4). Sets the
  durable ``is_banned`` flag AND revokes every one of the user's ``sessions``
  rows so any active session stops validating immediately
  (:func:`app.core.security.is_session_valid` rejects a revoked row). The
  durable flag is what blocks *future* authentication: the auth find-or-create
  seam (:mod:`app.services.auth_service`) rejects a banned existing user with a
  403 so a new session can never be issued. (A Redis session fast-path entry, if
  any, is left to self-evict at its TTL; the authoritative DB row is revoked, so
  the fast-path can never yield a false positive because ``require_session``
  always re-checks the DB row.)
- :func:`reassign_ownership` — make a target user the Owner of a workspace
  (Req 12.5). Creates the target's :class:`~app.db.models.WorkspaceMember` with
  role Owner if absent, or promotes an existing membership to Owner. Any
  *previous* Owner(s) of that workspace are demoted to Admin so ownership is not
  silently duplicated; the newly designated user is guaranteed to hold Owner.

- :func:`system_analytics` — platform-wide analytics (Req 14.2): aggregate agent
  token usage, the count of active (``running``) multi-tenant sessions, and
  database storage metrics (``pg_database_size`` plus core-table row counts).
- :func:`mcp_health` — the MCP health registry (Req 14.1): per-provider status,
  error rate, and token-usage aggregation derived from the persisted
  :class:`~app.db.models.Integration` statuses (which back the MCP tool servers)
  and the agent token metrics. Health is computed from stored status/error state
  and token usage, **not** live network pings; a live-ping hook can be layered on
  later.

The state-changing functions flush but do not commit — the caller (router) owns
the surrounding transaction, matching the convention used across the services.
The read-only inspection/analytics functions perform reads only.

Requirements: 12.3, 12.4, 12.5, 13.1, 13.2, 13.3, 13.4, 14.1, 14.2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.db.models import (
    AgentSession,
    AgentSessionStatus,
    ApprovalRequest,
    ApprovalStatus,
    Integration,
    IntegrationStatus,
    MemberRole,
    Session,
    User,
    Workspace,
    WorkspaceMember,
)


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserMembership:
    """A single workspace membership summary in the admin directory."""

    workspace_id: uuid.UUID
    role: MemberRole


@dataclass(frozen=True)
class UserDirectoryEntry:
    """A user's basic fields for the super-admin directory (Req 12.3).

    ``memberships`` is populated only when ``list_users`` is asked to include
    them; otherwise it is an empty tuple.
    """

    id: uuid.UUID
    email: str
    name: str
    is_superadmin: bool
    is_banned: bool
    memberships: tuple[UserMembership, ...] = ()


@dataclass(frozen=True)
class WorkspaceDirectoryEntry:
    """A workspace's basic fields for the super-admin directory.

    Deliberately **global** (not tenant-scoped): a super admin operates the
    whole platform, so the workspace listing crosses every tenant.

    - ``member_count`` — how many :class:`~app.db.models.WorkspaceMember` rows
      belong to the workspace.
    - ``owner_user_id`` — the user id of the current ``OWNER`` member if one
      exists, else ``None`` (a workspace with no Owner member yet).
    """

    id: uuid.UUID
    name: str
    slug: str
    created_by_user_id: uuid.UUID
    created_at: datetime
    member_count: int
    owner_user_id: uuid.UUID | None


@dataclass(frozen=True)
class ActiveSessionEntry:
    """A ``running`` agent session for the super-admin inspection view (Req 13.1).

    Carries the reasoning trace (``execution_logs``) alongside identity fields so
    a super admin can inspect what a running agent is doing across any workspace.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    triggered_by_user_id: uuid.UUID
    thread_id: str
    status: AgentSessionStatus
    execution_logs: Any
    created_at: datetime


@dataclass(frozen=True)
class KillResult:
    """Outcome of an emergency kill-switch activation (Req 13.2, 13.3)."""

    agent_session_id: uuid.UUID
    status: AgentSessionStatus
    cancelled_approvals: int


@dataclass(frozen=True)
class SystemAnalytics:
    """Platform-wide system analytics for a super admin (Req 14.2).

    All figures are **global** (across every workspace), matching the
    super-admin operator view:

    - ``tokens_total`` — sum of ``agent_sessions.total_tokens_used`` across all
      sessions in every workspace.
    - ``active_sessions`` — number of ``running`` agent sessions platform-wide
      ("active multi-tenant sessions").
    - ``storage_bytes`` — on-disk size of the current database, from Postgres'
      ``pg_database_size(current_database())``.
    - ``table_counts`` — row counts for a handful of core tables, giving a quick
      sense of how storage is distributed.
    """

    tokens_total: int
    active_sessions: int
    storage_bytes: int
    table_counts: dict[str, int]


@dataclass(frozen=True)
class ProviderHealthEntry:
    """MCP health aggregated for one provider (Req 14.1).

    Health is derived from the persisted integration statuses backing that
    provider's MCP tool server(s), plus the provider's token usage.

    - ``total`` — integrations for this provider.
    - ``active`` / ``error`` / ``disconnected`` — counts by
      :class:`~app.db.models.IntegrationStatus`.
    - ``error_rate`` — ``error / total`` (0.0 when ``total`` is 0).
    - ``tokens_used`` — token usage attributed to this provider (see
      :func:`mcp_health` for how tokens are associated).
    """

    name: str
    total: int
    active: int
    error: int
    disconnected: int
    error_rate: float
    tokens_used: int


@dataclass(frozen=True)
class MCPHealth:
    """The MCP health registry summary for a super admin (Req 14.1).

    ``providers`` is one :class:`ProviderHealthEntry` per distinct integration
    ``provider_name``, ordered by name. ``tokens_total`` is the platform-wide
    agent token usage (the same figure surfaced by system analytics), included
    so the health view carries an at-a-glance usage total alongside the
    per-provider breakdown.

    NOTE: health here is **computed from stored integration status/error state
    and token metrics, not live network pings** to the MCP servers. Real
    server pings are out of scope for this registry; a live-ping hook can be
    layered on later without changing this shape.
    """

    providers: list[ProviderHealthEntry]
    tokens_total: int


# ---------------------------------------------------------------------------
# Directory (Req 12.3)
# ---------------------------------------------------------------------------


async def list_users(
    session: AsyncSession,
    *,
    include_memberships: bool = False,
) -> list[UserDirectoryEntry]:
    """Return the platform user directory across all workspaces (Req 12.3).

    Returns every :class:`~app.db.models.User` with their basic fields, ordered
    by creation time then id for a stable listing. This is deliberately global
    and NOT tenant-scoped: a super admin operates the whole platform, so the
    directory crosses every workspace.

    When ``include_memberships`` is true, each entry also carries the user's
    workspace memberships (``workspace_id`` + role), resolved in a single extra
    query and grouped in memory to avoid an N+1.

    The caller (router) owns the transaction; this performs reads only.
    """
    users = (
        await session.scalars(
            select(User).order_by(User.created_at, User.id)
        )
    ).all()

    memberships_by_user: dict[uuid.UUID, list[UserMembership]] = {}
    if include_memberships:
        rows = (
            await session.execute(
                select(
                    WorkspaceMember.user_id,
                    WorkspaceMember.workspace_id,
                    WorkspaceMember.role,
                )
            )
        ).all()
        for user_id, workspace_id, role in rows:
            memberships_by_user.setdefault(user_id, []).append(
                UserMembership(workspace_id=workspace_id, role=role)
            )

    return [
        UserDirectoryEntry(
            id=user.id,
            email=user.email,
            name=user.name,
            is_superadmin=bool(user.is_superadmin),
            is_banned=bool(user.is_banned),
            memberships=tuple(memberships_by_user.get(user.id, ())),
        )
        for user in users
    ]


# ---------------------------------------------------------------------------
# Ban (Req 12.4)
# ---------------------------------------------------------------------------


async def ban_user(
    session: AsyncSession,
    *,
    target_user_id: uuid.UUID,
) -> User:
    """Ban a user: invalidate active sessions and block future auth (Req 12.4).

    Two effects, together satisfying "invalidate the User's active Session_Tokens
    AND prevent the User from authenticating":

    1. **Invalidate active sessions.** Every ``sessions`` row for the user is
       deleted, so any presented token no longer resolves to a live session
       (:func:`app.core.security.is_session_valid` rejects a missing/absent row).
       Deleting (rather than only flipping ``revoked``) also frees the storage;
       either way the token stops validating on the very next request.
    2. **Block future authentication.** The durable ``is_banned`` flag is set to
       ``True``. The auth find-or-create seam
       (:func:`app.services.auth_service.AuthService._find_or_create_user`)
       rejects a banned existing user with a 403 before issuing any session, so
       the user cannot log back in.

    Idempotent: banning an already-banned user simply re-affirms the flag and
    clears any (re-created) sessions.

    Args:
        session: The active async session/transaction (caller commits).
        target_user_id: The user to ban.

    Returns:
        The banned :class:`~app.db.models.User`.

    Raises:
        APIError: 404 if no user matches ``target_user_id``.
    """
    user = await session.get(User, target_user_id)
    if user is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="User not found.",
        )

    # (1) Invalidate every active session for the user (Req 12.4).
    await session.execute(
        delete(Session).where(Session.user_id == target_user_id)
    )
    # (2) Persist the durable ban so future authentication is rejected.
    user.is_banned = True
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# Role change (toggle super admin)
# ---------------------------------------------------------------------------


async def _superadmin_count(session: AsyncSession) -> int:
    """Return how many users currently hold the ``is_superadmin`` flag.

    Used by the last-admin guardrails so the platform can never be left without
    a super admin (a demote or delete that would drop the count to zero is
    refused).
    """
    count = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.is_superadmin.is_(True))
    )
    return int(count or 0)


async def set_superadmin(
    session: AsyncSession,
    *,
    target_user_id: uuid.UUID,
    is_superadmin: bool,
    acting_user_id: uuid.UUID,
) -> User:
    """Promote or demote a user's platform super-admin flag.

    Sets :attr:`~app.db.models.User.is_superadmin` to ``is_superadmin`` on the
    target user.

    Guardrail — **never remove the last super admin.** When demoting
    (``is_superadmin=False``) a user who is *currently* a super admin, the
    operation is refused with **409 conflict** if they are the only remaining
    super admin (the platform-wide count of super admins is exactly 1). This
    also covers the self-demotion case: a lone super admin cannot demote
    themselves and lock everyone out. ``acting_user_id`` is accepted for
    symmetry with the other mutating operations (and future auditing); the
    last-admin count is what actually enforces the invariant.

    Promoting (``is_superadmin=True``) is always allowed, and demoting a user
    who is not currently a super admin is a harmless no-op that simply re-affirms
    the flag.

    The caller (router) owns the transaction; this flushes but does not commit.

    Args:
        session: The active async session/transaction.
        target_user_id: The user whose role is changing.
        is_superadmin: The desired value of the super-admin flag.
        acting_user_id: The super admin performing the change.

    Returns:
        The updated :class:`~app.db.models.User`.

    Raises:
        APIError: 404 if no user matches ``target_user_id``; 409 if the change
            would remove the last remaining super admin.
    """
    user = await session.get(User, target_user_id)
    if user is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="User not found.",
        )

    # Refuse a demotion that would leave the platform with no super admin.
    if not is_superadmin and bool(user.is_superadmin):
        if await _superadmin_count(session) <= 1:
            raise APIError(
                status_code=409,
                code="conflict",
                message="Cannot remove the last super admin.",
            )

    user.is_superadmin = is_superadmin
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# Edit user (name only)
# ---------------------------------------------------------------------------


async def update_user(
    session: AsyncSession,
    *,
    target_user_id: uuid.UUID,
    name: str,
) -> User:
    """Update a user's display ``name`` (the only editable field).

    The email, auth provider, and role/ban flags are intentionally not editable
    here: email/provider are identity anchored to the OAuth account, and role/
    ban have dedicated operations with their own guardrails. Only the
    human-facing ``name`` is changed.

    ``name`` is validated to be non-empty after stripping surrounding
    whitespace (a blank name is rejected with **400 invalid**); the stored value
    is the stripped string.

    The caller (router) owns the transaction; this flushes but does not commit.

    Args:
        session: The active async session/transaction.
        target_user_id: The user to edit.
        name: The new display name.

    Returns:
        The updated :class:`~app.db.models.User`.

    Raises:
        APIError: 404 if no user matches ``target_user_id``; 400 if ``name`` is
            empty after stripping.
    """
    user = await session.get(User, target_user_id)
    if user is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="User not found.",
        )

    cleaned = (name or "").strip()
    if not cleaned:
        raise APIError(
            status_code=400,
            code="invalid",
            message="Name must not be empty.",
        )

    user.name = cleaned
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# Delete user (hard delete with guardrails)
# ---------------------------------------------------------------------------


async def delete_user(
    session: AsyncSession,
    *,
    target_user_id: uuid.UUID,
    acting_user_id: uuid.UUID,
) -> uuid.UUID:
    """Hard-delete a user row, subject to three safety guardrails.

    On success the ``users`` row is removed. The foreign keys that reference
    ``users.id`` clean up dependent rows automatically: ``sessions``,
    ``workspace_members``, ``integrations.created_by_user_id``,
    ``rules.created_by_user_id``, ``agent_sessions.triggered_by_user_id`` and
    ``approval_requests.triggered_by_user_id`` are ``ON DELETE CASCADE``, while
    ``approval_requests.reviewed_by_user_id`` and ``system_audit_logs.user_id``
    are ``ON DELETE SET NULL`` (so the audit trail is retained, just
    de-attributed).

    Guardrails (each refuses the delete rather than partially applying it):

    - **A — no self-delete.** A super admin cannot delete their own account
      (``target_user_id == acting_user_id``): **409 conflict**. Prevents an
      operator from accidentally locking themselves out mid-session.
    - **B — never delete the last super admin.** If the target is a super admin
      and is the only one left (count == 1): **409 conflict**. Mirrors the
      demotion guardrail so the platform always retains an operator.
    - **C — no orphaned workspaces.** ``workspaces.created_by_user_id`` is
      ``ON DELETE RESTRICT``, so a user who created any workspace cannot be
      deleted while those rows exist — the database would reject it anyway. We
      detect this up front (count of workspaces created by the user > 0) and
      raise a clear **409 conflict** telling the operator to reassign or delete
      those workspaces first, rather than letting an opaque integrity error
      surface.

    The caller (router) owns the transaction; this flushes but does not commit.

    Args:
        session: The active async session/transaction.
        target_user_id: The user to delete.
        acting_user_id: The super admin performing the delete (for guardrail A).

    Returns:
        The deleted user's id.

    Raises:
        APIError: 404 if no user matches ``target_user_id``; 409 for any of the
            three guardrails above.
    """
    user = await session.get(User, target_user_id)
    if user is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="User not found.",
        )

    # Guardrail A: an operator may not delete their own account.
    if target_user_id == acting_user_id:
        raise APIError(
            status_code=409,
            code="conflict",
            message="You cannot delete your own account.",
        )

    # Guardrail B: never delete the last remaining super admin.
    if bool(user.is_superadmin) and await _superadmin_count(session) <= 1:
        raise APIError(
            status_code=409,
            code="conflict",
            message="Cannot delete the last super admin.",
        )

    # Guardrail C: workspaces.created_by_user_id is ON DELETE RESTRICT, so a
    # workspace creator cannot be removed until those workspaces are handled.
    created_workspaces = await session.scalar(
        select(func.count())
        .select_from(Workspace)
        .where(Workspace.created_by_user_id == target_user_id)
    )
    if int(created_workspaces or 0) > 0:
        raise APIError(
            status_code=409,
            code="conflict",
            message=(
                "This user created one or more workspaces. Reassign or delete "
                "those workspaces first."
            ),
        )

    await session.delete(user)
    await session.flush()
    return target_user_id


# ---------------------------------------------------------------------------
# Workspace directory
# ---------------------------------------------------------------------------


async def list_workspaces(
    session: AsyncSession,
) -> list[WorkspaceDirectoryEntry]:
    """Return the platform workspace directory across all tenants.

    Returns every :class:`~app.db.models.Workspace` with its basic fields plus
    a ``member_count`` and the current ``owner_user_id``, ordered by creation
    time then id for a stable listing. Like :func:`list_users` this is
    deliberately **global** and NOT tenant-scoped: a super admin operates the
    whole platform, so the directory crosses every workspace.

    Member counts and current owners are resolved in two supplemental grouped
    queries (one ``COUNT(*) GROUP BY workspace_id`` for members, one selecting
    the ``OWNER``-role members) and joined in memory, avoiding an N+1 across
    the workspace list.

    The caller (router) owns the transaction; this performs reads only.
    """
    workspaces = (
        await session.scalars(
            select(Workspace).order_by(Workspace.created_at, Workspace.id)
        )
    ).all()

    # member_count per workspace (single grouped query).
    count_rows = (
        await session.execute(
            select(
                WorkspaceMember.workspace_id,
                func.count(),
            ).group_by(WorkspaceMember.workspace_id)
        )
    ).all()
    member_counts: dict[uuid.UUID, int] = {
        workspace_id: int(count) for workspace_id, count in count_rows
    }

    # Current owner per workspace (single query over OWNER-role members).
    owner_rows = (
        await session.execute(
            select(
                WorkspaceMember.workspace_id,
                WorkspaceMember.user_id,
            ).where(WorkspaceMember.role == MemberRole.OWNER)
        )
    ).all()
    owner_by_workspace: dict[uuid.UUID, uuid.UUID] = {
        workspace_id: user_id for workspace_id, user_id in owner_rows
    }

    return [
        WorkspaceDirectoryEntry(
            id=workspace.id,
            name=workspace.name,
            slug=workspace.slug,
            created_by_user_id=workspace.created_by_user_id,
            created_at=workspace.created_at,
            member_count=member_counts.get(workspace.id, 0),
            owner_user_id=owner_by_workspace.get(workspace.id),
        )
        for workspace in workspaces
    ]


# ---------------------------------------------------------------------------
# Delete workspace (hard delete)
# ---------------------------------------------------------------------------


async def delete_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
) -> uuid.UUID:
    """Hard-delete a workspace row and let its dependents cascade.

    On success the ``workspaces`` row is removed. Every foreign key that
    references ``workspaces.id`` is ``ON DELETE CASCADE`` — so the workspace's
    ``workspace_members``, ``workspace_invites``, ``integrations``, ``rules``,
    ``agent_sessions`` and ``approval_requests`` are removed automatically —
    with the single exception of ``system_audit_logs.workspace_id``, which is
    ``ON DELETE SET NULL``. The audit trail is therefore **retained** (just
    de-attributed from the now-deleted workspace) rather than lost. Because no
    referencing FK is ``ON DELETE RESTRICT``, the delete has no integrity
    blockers and is safe to perform directly.

    The caller (router) owns the transaction; this flushes but does not commit.

    Args:
        session: The active async session/transaction.
        workspace_id: The workspace to delete.

    Returns:
        The deleted workspace's id.

    Raises:
        APIError: 404 if no workspace matches ``workspace_id``.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Workspace not found.",
        )

    await session.delete(workspace)
    await session.flush()
    return workspace_id


# ---------------------------------------------------------------------------
# Ownership reassignment (Req 12.5)
# ---------------------------------------------------------------------------


async def reassign_ownership(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    new_owner_user_id: uuid.UUID,
) -> WorkspaceMember:
    """Make ``new_owner_user_id`` the Owner of ``workspace_id`` (Req 12.5).

    Updates the workspace's member records so the designated user holds the
    Owner role:

    - Any *existing* Owner(s) other than the target are demoted to Admin, so a
      workspace does not end up with two Owners after a reassignment (the
      demotion keeps the former owner as a privileged member rather than
      dropping them). This behavior is documented here per the task.
    - If the target already has a membership, it is promoted to Owner; otherwise
      a new Owner :class:`~app.db.models.WorkspaceMember` is created for them.

    The designated user is guaranteed to hold Owner on return.

    The caller (router) owns the transaction; this flushes but does not commit.

    Args:
        session: The active async session/transaction.
        workspace_id: The workspace whose ownership is being reassigned.
        new_owner_user_id: The user who must become Owner.

    Returns:
        The target's :class:`~app.db.models.WorkspaceMember`, now Owner.

    Raises:
        APIError: 404 if the workspace does not exist.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Workspace not found.",
        )

    members = (
        await session.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
    ).all()

    target: WorkspaceMember | None = None
    for member in members:
        if member.user_id == new_owner_user_id:
            target = member
        elif member.role is MemberRole.OWNER:
            # Demote any previous owner to Admin so ownership isn't duplicated.
            member.role = MemberRole.ADMIN

    if target is None:
        target = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=new_owner_user_id,
            role=MemberRole.OWNER,
        )
        session.add(target)
    else:
        target.role = MemberRole.OWNER

    await session.flush()
    return target


# ---------------------------------------------------------------------------
# Live session inspection (Req 13.1)
# ---------------------------------------------------------------------------


async def list_active_sessions(
    session: AsyncSession,
) -> list[ActiveSessionEntry]:
    """Return every ``running`` agent session across all workspaces (Req 13.1).

    A super admin inspects live executions platform-wide, so this listing is
    deliberately **global** rather than tenant-scoped — it crosses every
    workspace, mirroring the directory in :func:`list_users`. Each returned
    entry includes the session's reasoning trace (``execution_logs``) so a super
    admin can see what the agent is doing before deciding whether to intervene.

    Only sessions with status ``RUNNING`` are returned; ``completed``,
    ``failed``, and ``terminated`` sessions are excluded because they are no
    longer live and cannot be killed.

    The caller (router) owns the transaction; this performs reads only.
    """
    rows = (
        await session.scalars(
            select(AgentSession)
            .where(AgentSession.status == AgentSessionStatus.RUNNING)
            .order_by(AgentSession.created_at, AgentSession.id)
        )
    ).all()
    return [
        ActiveSessionEntry(
            id=row.id,
            workspace_id=row.workspace_id,
            triggered_by_user_id=row.triggered_by_user_id,
            thread_id=row.thread_id,
            status=row.status,
            execution_logs=row.execution_logs,
            created_at=row.created_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Emergency kill-switch (Req 13.2, 13.3, 13.4)
# ---------------------------------------------------------------------------


async def kill_session(
    session: AsyncSession,
    *,
    agent_session_id: uuid.UUID,
    acting_superadmin_id: uuid.UUID,
) -> KillResult:
    """Emergency-terminate a running agent session (Req 13.2, 13.3, 13.4).

    Effects, performed atomically within the caller's transaction:

    1. **Terminate the session (Req 13.2).** The target
       :class:`~app.db.models.AgentSession` status is set to
       :attr:`AgentSessionStatus.TERMINATED`, the terminal value reserved for
       operator intervention (distinct from the ``completed``/``failed`` values
       the engine sets on its own).
    2. **Cancel pending approvals (Req 13.3).** Every
       :class:`~app.db.models.ApprovalRequest` for this session still in
       :attr:`ApprovalStatus.PENDING` is moved to
       :attr:`ApprovalStatus.REJECTED` — the cancelled/terminal outcome, since a
       killed session must never proceed with a gated tool call. Approvals that
       are already resolved (``approved``/``rejected``) are left untouched; a
       kill only affects still-open requests.
    3. **Audit the activation (Req 13.4).** An ``admin.session_killed`` audit row
       is written via :func:`app.services.audit_service.record` recording the
       acting super admin (``user_id``) and the targeted session, scoped to the
       session's workspace.

    Choice of the not-running case: if the target session exists but is not
    ``RUNNING`` (already ``completed``/``failed``/``terminated``), this raises a
    **409 conflict** rather than silently succeeding. Killing is a live-only
    operation; a terminal session has nothing to terminate, and reporting a
    conflict makes the no-op explicit to the operator (and keeps the audit trail
    honest — no ``session_killed`` row is written for a session that was not
    actually running). No approvals are cancelled and no audit row is written in
    that case.

    The caller (router) owns the transaction; this flushes but does not commit.

    Args:
        session: The active async session/transaction.
        agent_session_id: The agent session to terminate.
        acting_superadmin_id: The super admin activating the kill-switch, for the
            audit record.

    Returns:
        A :class:`KillResult` with the terminated session id, its new terminal
        status, and the number of pending approvals that were cancelled.

    Raises:
        APIError: 404 if no agent session matches ``agent_session_id``; 409 if
            the session exists but is not ``RUNNING``.
    """
    # Imported here to avoid a circular import at module load (audit_service and
    # admin_service are both wired through the API layer).
    from app.services import audit_service

    agent_session = await session.get(AgentSession, agent_session_id)
    if agent_session is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Agent session not found.",
        )

    if agent_session.status is not AgentSessionStatus.RUNNING:
        raise APIError(
            status_code=409,
            code="conflict",
            message="Agent session is not running.",
        )

    # (1) Terminate the session (Req 13.2).
    agent_session.status = AgentSessionStatus.TERMINATED

    # (2) Cancel any pending approvals for this session (Req 13.3). Already
    # resolved requests are excluded by the status predicate and left untouched.
    pending = (
        await session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.agent_session_id == agent_session_id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
        )
    ).all()
    for request in pending:
        request.status = ApprovalStatus.REJECTED

    # (3) Record the kill-switch activation (Req 13.4).
    await audit_service.record(
        session,
        workspace_id=agent_session.workspace_id,
        user_id=acting_superadmin_id,
        action="admin.session_killed",
        metadata={"agent_session_id": str(agent_session_id)},
    )

    await session.flush()
    return KillResult(
        agent_session_id=agent_session_id,
        status=agent_session.status,
        cancelled_approvals=len(pending),
    )


# ---------------------------------------------------------------------------
# Stale-session cleanup (orphaned "running" sessions)
# ---------------------------------------------------------------------------

#: How old a still-``running`` AgentSession must be before the sweep treats it
#: as orphaned. A real agent run completes in seconds; anything running for many
#: minutes did not reach its terminal commit (the worker was killed/restarted
#: mid-run — common during redeploys or a crash), so its ``running`` status is
#: stale, not live. 30 minutes is comfortably beyond any legitimate run.
STALE_SESSION_AFTER_MINUTES = 30


async def sweep_stale_running_sessions(
    session: AsyncSession,
    *,
    older_than_minutes: int = STALE_SESSION_AFTER_MINUTES,
    now: datetime | None = None,
) -> int:
    """Mark orphaned ``running`` AgentSessions as ``terminated`` (cleanup).

    A session is only flipped to ``completed``/``failed`` at the END of
    :func:`app.services.strands_engine._execute_agent_run`. If the worker
    process dies or is restarted mid-run (redeploy, crash, the event-loop errors
    we fixed), the row is left at ``running`` forever — inflating the admin
    "Active sessions" count with rows that are NOT live (they consume no AWS /
    Bedrock resources; they are just stale DB status).

    This marks every ``running`` session older than ``older_than_minutes`` as
    ``TERMINATED`` and tags ``execution_logs`` with a ``stale_swept`` marker so
    they are distinguishable from an operator kill_session. It intentionally
    does NOT touch recent ``running`` rows (a genuinely in-flight run). Returns
    the number of sessions swept. Flushes but does not commit — the caller owns
    the transaction (matching this module's convention).

    NOTE: this is a status-only cleanup. It does not stop any process (there is
    nothing live to stop) and has no effect on AWS usage; it only makes the
    admin session view truthful.
    """
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(minutes=max(0, older_than_minutes))

    stale = (
        await session.scalars(
            select(AgentSession)
            .where(AgentSession.status == AgentSessionStatus.RUNNING)
            .where(AgentSession.created_at < cutoff)
        )
    ).all()

    for agent_session in stale:
        agent_session.status = AgentSessionStatus.TERMINATED
        # Preserve any existing logs; add a marker explaining the sweep.
        logs = agent_session.execution_logs
        marker = {
            "stale_swept": True,
            "reason": "orphaned running session (worker restart/crash mid-run)",
            "swept_at": current.isoformat(),
        }
        if isinstance(logs, dict):
            merged = dict(logs)
            merged.setdefault("cleanup", marker)
            agent_session.execution_logs = merged
        else:
            agent_session.execution_logs = {"cleanup": marker}

    await session.flush()
    return len(stale)


# ---------------------------------------------------------------------------
# System analytics (Req 14.2)
# ---------------------------------------------------------------------------


# Core tables whose row counts are surfaced in the analytics view. Kept small
# and explicit (rather than reflecting every table) so the metric stays a quick
# operator signal rather than an exhaustive dump.
_ANALYTICS_TABLES: tuple[tuple[str, type], ...] = (
    ("users", User),
    ("workspaces", Workspace),
    ("integrations", Integration),
    ("agent_sessions", AgentSession),
    ("approval_requests", ApprovalRequest),
)


async def system_analytics(session: AsyncSession) -> SystemAnalytics:
    """Return platform-wide system analytics for a super admin (Req 14.2).

    Aggregates three families of metric, all **global** (not tenant-scoped),
    because a super admin monitors the whole platform:

    1. **Aggregate token usage** — ``SUM(agent_sessions.total_tokens_used)``
       across every workspace. ``NULL`` (no sessions) is coalesced to ``0``.
    2. **Active multi-tenant session count** —
       ``COUNT(*) WHERE status = 'running'`` across every workspace.
    3. **Database storage metrics** — the current database's on-disk size via
       Postgres ``pg_database_size(current_database())`` (bytes), plus row
       counts for a handful of core tables so storage can be reasoned about.

    The caller (router) owns the transaction; this performs reads only.
    """
    tokens_total = await session.scalar(
        select(func.coalesce(func.sum(AgentSession.total_tokens_used), 0))
    )

    active_sessions = await session.scalar(
        select(func.count())
        .select_from(AgentSession)
        .where(AgentSession.status == AgentSessionStatus.RUNNING)
    )

    # Database on-disk size in bytes. Uses sa.text since pg_database_size is a
    # Postgres-specific function with no ORM construct.
    storage_bytes = await session.scalar(
        sa.text("SELECT pg_database_size(current_database())")
    )

    table_counts: dict[str, int] = {}
    for name, model in _ANALYTICS_TABLES:
        count = await session.scalar(select(func.count()).select_from(model))
        table_counts[name] = int(count or 0)

    return SystemAnalytics(
        tokens_total=int(tokens_total or 0),
        active_sessions=int(active_sessions or 0),
        storage_bytes=int(storage_bytes or 0),
        table_counts=table_counts,
    )


# ---------------------------------------------------------------------------
# MCP health registry (Req 14.1)
# ---------------------------------------------------------------------------


async def mcp_health(session: AsyncSession) -> MCPHealth:
    """Return the MCP health registry summary for a super admin (Req 14.1).

    The platform's MCP tool servers are backed by :class:`~app.db.models.Integration`
    records, so this aggregates the persisted integration state per distinct
    ``provider_name`` into a health entry reporting:

    - ``total`` integrations for the provider,
    - counts by :class:`~app.db.models.IntegrationStatus`
      (``active`` / ``error`` / ``disconnected``), and
    - an ``error_rate`` of ``error / total`` (0.0 when the provider has no
      integrations).

    Token usage is reported at the platform level via ``tokens_total`` (the sum
    of ``agent_sessions.total_tokens_used``). There is no persisted association
    between an agent session's tokens and a specific integration/provider, so a
    truthful per-provider token attribution is not available; each provider
    entry therefore carries ``tokens_used = 0`` and the real figure lives in the
    global ``tokens_total``. If such an association is added later, the
    per-provider field can be populated without changing this shape.

    IMPORTANT: health is computed from **stored integration status/error state
    and token metrics, not live network pings** of the MCP servers. Live pinging
    is out of scope for this registry and can be layered on later.

    The caller (router) owns the transaction; this performs reads only.
    """
    rows = (
        await session.execute(
            select(
                Integration.provider_name,
                Integration.status,
                func.count(),
            ).group_by(Integration.provider_name, Integration.status)
        )
    ).all()

    # provider_name -> {status: count}
    by_provider: dict[str, dict[IntegrationStatus, int]] = {}
    for provider_name, status, count in rows:
        by_provider.setdefault(provider_name, {})[status] = int(count)

    providers: list[ProviderHealthEntry] = []
    for provider_name in sorted(by_provider):
        counts = by_provider[provider_name]
        active = counts.get(IntegrationStatus.ACTIVE, 0)
        error = counts.get(IntegrationStatus.ERROR, 0)
        disconnected = counts.get(IntegrationStatus.DISCONNECTED, 0)
        total = active + error + disconnected
        error_rate = (error / total) if total else 0.0
        providers.append(
            ProviderHealthEntry(
                name=provider_name,
                total=total,
                active=active,
                error=error,
                disconnected=disconnected,
                error_rate=error_rate,
                tokens_used=0,
            )
        )

    tokens_total = await session.scalar(
        select(func.coalesce(func.sum(AgentSession.total_tokens_used), 0))
    )

    return MCPHealth(providers=providers, tokens_total=int(tokens_total or 0))


__all__ = [
    "UserMembership",
    "UserDirectoryEntry",
    "WorkspaceDirectoryEntry",
    "ActiveSessionEntry",
    "KillResult",
    "SystemAnalytics",
    "ProviderHealthEntry",
    "MCPHealth",
    "list_users",
    "ban_user",
    "set_superadmin",
    "update_user",
    "delete_user",
    "list_workspaces",
    "delete_workspace",
    "reassign_ownership",
    "list_active_sessions",
    "kill_session",
    "sweep_stale_running_sessions",
    "system_analytics",
    "mcp_health",
]
