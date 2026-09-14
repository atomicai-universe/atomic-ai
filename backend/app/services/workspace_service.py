"""Workspace lifecycle service (task 7.1).

Implements the ``Workspace_Service`` operations from the design (see design.md
"Workspace_Service (lifecycle + invites)"):

- :func:`create_workspace` — persists a :class:`~app.db.models.Workspace` and
  the creator's **sole Owner** :class:`~app.db.models.WorkspaceMember` in a
  single transaction (Req 3.1). The workspace ``slug`` is derived from the name
  and made **unique** by trying successive candidates; a unique-constraint
  collision rolls the failed attempt back (via a SAVEPOINT) and retries with the
  next candidate within a bounded loop (Req 3.2, 20.4).
- :func:`delete_workspace` — **Owner-only** (Req 3.6); relies on the schema's
  ``ON DELETE CASCADE`` to remove members/invites/integrations/rules/
  agent_sessions/approval_requests, while ``system_audit_logs`` rows survive
  with ``workspace_id`` set to NULL (Req 3.5, 20.4 — enforced by the migration).
- :func:`switch_active_workspace` — validates that the caller is a member of the
  target workspace and returns it, so the session dependency can record it as
  the active workspace scope (Req 3.3, 3.7). A non-member gets 404 (existence is
  not disclosed), matching the RBAC guard's convention.

A user may belong to many workspaces (Req 3.4): nothing constrains that here,
and the ``uq_workspace_member`` unique constraint is on ``(workspace_id,
user_id)``, so the same user can own/join any number of distinct workspaces.

The slug derivation is factored into pure, DB-free helpers (:func:`slugify` and
:func:`generate_slug_candidates`) so slug uniqueness can be property-tested
without a database (Property 13, task 7.2).

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 20.4.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.db.models import (
    InviteRole,
    InviteStatus,
    MemberRole,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
)

# ---------------------------------------------------------------------------
# Slug generation (pure, DB-free)
# ---------------------------------------------------------------------------

# Maximum length of a stored slug base before a uniqueness suffix is appended.
# Kept comfortably under the model's ``String(255)`` so suffixes never overflow.
_MAX_SLUG_BASE = 200

# Fallback base used when a name slugifies to the empty string (e.g. a name made
# entirely of punctuation or non-latin characters that transliterate to nothing).
_FALLBACK_SLUG_BASE = "workspace"

# Number of numeric-suffix candidates ("-2", "-3", ...) to try before switching
# to random suffixes. Keeps common collisions readable while guaranteeing the
# generator can always produce a fresh candidate.
_NUMERIC_SUFFIX_LIMIT = 100

# Total number of insert attempts before giving up (bounded retry loop). Every
# attempt after the first uses a distinct candidate, so exhausting this bound on
# real data is astronomically unlikely; the bound exists only to guarantee
# termination (Req 3.2).
_MAX_INSERT_ATTEMPTS = 50


def slugify(name: str) -> str:
    """Return a URL-safe base slug derived from ``name`` (pure).

    Normalizes unicode to ASCII, lowercases, and collapses any run of
    non-alphanumeric characters into a single hyphen, trimming leading/trailing
    hyphens. When the input reduces to nothing (empty, whitespace, or only
    punctuation), a stable fallback base (:data:`_FALLBACK_SLUG_BASE`) is
    returned so the result is always a non-empty, valid slug base.

    This is deterministic and side-effect free, so the same name always yields
    the same base — the per-workspace uniqueness is layered on top by
    :func:`generate_slug_candidates`.
    """
    # Transliterate accented/unicode characters to their closest ASCII form and
    # drop anything that has no ASCII representation.
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    # Lowercase, then replace every maximal run of non [a-z0-9] with a hyphen.
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    base = hyphenated.strip("-")
    if not base:
        return _FALLBACK_SLUG_BASE
    return base[:_MAX_SLUG_BASE].strip("-") or _FALLBACK_SLUG_BASE


def generate_slug_candidates(name: str) -> Iterator[str]:
    """Yield an unbounded stream of distinct slug candidates for ``name`` (pure).

    The first candidate is the plain :func:`slugify` base. Subsequent candidates
    append an increasing numeric suffix (``-2``, ``-3``, ... up to
    :data:`_NUMERIC_SUFFIX_LIMIT`) so predictable collisions get readable slugs,
    then switch to short random suffixes (``-<hex>``) which make further
    collisions vanishingly unlikely. The stream never terminates, so a caller
    can pull as many fresh candidates as its retry bound allows.

    Because generation is deterministic up to the random tail, the numeric
    candidates are stable and testable; the random tail guarantees the generator
    can always produce a value not present in any finite existing set (the basis
    of Property 13).
    """
    base = slugify(name)
    yield base
    for suffix in range(2, _NUMERIC_SUFFIX_LIMIT + 1):
        yield f"{base}-{suffix}"
    # Unbounded random tail: a fresh 8-hex-char token each time.
    while True:
        yield f"{base}-{secrets.token_hex(4)}"


def unique_slug(name: str, existing: set[str]) -> str:
    """Return the first candidate for ``name`` not present in ``existing`` (pure).

    A convenience wrapper over :func:`generate_slug_candidates` used by the
    property test (Property 13) and any caller that resolves uniqueness against
    an in-memory set rather than the database. Because the candidate stream has
    an unbounded random tail, a fresh slug always exists for any finite
    ``existing`` set, so this always terminates with a value not in ``existing``.
    """
    for candidate in generate_slug_candidates(name):
        if candidate not in existing:
            return candidate
    # Unreachable: generate_slug_candidates is unbounded. Present for type
    # checkers and to fail closed rather than return a colliding slug.
    raise RuntimeError("exhausted slug candidates")  # pragma: no cover


# ---------------------------------------------------------------------------
# Workspace lifecycle operations (async, DB-backed)
# ---------------------------------------------------------------------------


async def list_workspaces_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[tuple[Workspace, MemberRole]]:
    """Return the workspaces ``user_id`` is a member of, with their role.

    Joins :class:`~app.db.models.WorkspaceMember` to
    :class:`~app.db.models.Workspace` so each row carries the workspace and the
    caller's :class:`~app.db.models.MemberRole` in it. Results are ordered by
    workspace name (case-insensitive) for a stable, user-friendly list. A user
    with no memberships gets an empty list. Read-only; the caller owns any
    surrounding transaction.

    Args:
        session: The active async session.
        user_id: The caller whose memberships to list.

    Returns:
        A list of ``(workspace, role)`` tuples, ordered by workspace name.
    """
    rows = await session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.name)
    )
    return [(ws, role) for ws, role in rows.all()]


async def create_workspace(
    session: AsyncSession,
    *,
    name: str,
    creator_user_id: uuid.UUID,
) -> Workspace:
    """Create a workspace and the creator's sole Owner membership atomically.

    Persists a :class:`~app.db.models.Workspace` with a unique ``slug`` derived
    from ``name`` and a single :class:`~app.db.models.WorkspaceMember` granting
    ``creator_user_id`` the :attr:`~app.db.models.MemberRole.OWNER` role, both in
    one transaction (Req 3.1). If a candidate slug collides with an existing
    workspace, the failed insert is rolled back to a SAVEPOINT and the next
    candidate is tried, within a bounded retry loop (Req 3.2, 20.4).

    The caller owns the surrounding transaction: this function flushes (and uses
    nested SAVEPOINTs for retry) but does not ``commit``. On success the returned
    :class:`~app.db.models.Workspace` is populated with its generated id and
    slug.

    Args:
        session: The active async session/transaction.
        name: Human-provided workspace name; the slug is derived from it.
        creator_user_id: The user who becomes the sole Owner.

    Returns:
        The persisted :class:`~app.db.models.Workspace`.

    Raises:
        APIError: 409 conflict if a unique slug cannot be established within the
            bounded number of attempts (should not occur in practice).
    """
    candidates = generate_slug_candidates(name)

    last_error: IntegrityError | None = None
    for _ in range(_MAX_INSERT_ATTEMPTS):
        slug = next(candidates)
        workspace = Workspace(
            name=name,
            slug=slug,
            created_by_user_id=creator_user_id,
        )
        # A SAVEPOINT isolates this attempt: a unique-constraint violation only
        # rolls back the failed insert, leaving the outer transaction intact so
        # we can retry with the next candidate (Req 3.2, 20.4).
        try:
            async with session.begin_nested():
                session.add(workspace)
                await session.flush()
        except IntegrityError as exc:  # slug collision -> try the next candidate
            last_error = exc
            continue

        # Slug accepted; attach the sole Owner membership in the same outer tx.
        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=creator_user_id,
            role=MemberRole.OWNER,
        )
        session.add(member)
        await session.flush()
        return workspace

    # Bounded retries exhausted without a free slug (extremely unlikely).
    raise APIError(
        status_code=409,
        code="conflict",
        message="Could not generate a unique workspace slug.",
    ) from last_error


async def delete_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_role: MemberRole,
) -> None:
    """Delete a workspace; Owner-only (Req 3.6).

    Enforces the Owner-only rule at the service boundary: a non-Owner actor is
    rejected with a 403 before any deletion is attempted (Req 3.6). On deletion,
    the database's ``ON DELETE CASCADE`` foreign keys remove the workspace's
    members, invites, integrations, rules, agent_sessions, and approval_requests,
    while ``system_audit_logs`` rows survive with ``workspace_id`` set to NULL
    (Req 3.5, 20.4 — enforced by the migration schema).

    The caller owns the surrounding transaction (no ``commit`` here).

    Args:
        session: The active async session/transaction.
        workspace_id: The workspace to delete.
        actor_role: The acting caller's role in that workspace.

    Raises:
        APIError: 403 if ``actor_role`` is not :attr:`MemberRole.OWNER`.
        APIError: 404 if the workspace does not exist.
    """
    if actor_role is not MemberRole.OWNER:
        raise APIError(
            status_code=403,
            code="forbidden",
            message="Only the workspace Owner may delete the workspace.",
        )

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise APIError(status_code=404, code="not_found", message="Workspace not found.")

    # ORM delete so cascades configured on relationships/DB FKs fire.
    await session.delete(workspace)
    await session.flush()


async def rename_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    new_name: str,
    actor_role: MemberRole,
) -> Workspace:
    """Rename a workspace; Owner-only (BUILD.md).

    Enforces the Owner-only rule at the service boundary (mirroring
    :func:`delete_workspace`). Updates the workspace ``name`` and regenerates a
    unique ``slug`` from the new name, retrying candidate slugs on collision the
    same way :func:`create_workspace` does. The caller owns the surrounding
    transaction (no ``commit`` here).

    Args:
        session: The active async session/transaction.
        workspace_id: The workspace to rename.
        new_name: The new human-provided name (must be non-empty after trim).
        actor_role: The acting caller's role in that workspace.

    Returns:
        The updated :class:`~app.db.models.Workspace`.

    Raises:
        APIError: 403 if ``actor_role`` is not :attr:`MemberRole.OWNER`.
        APIError: 404 if the workspace does not exist.
        APIError: 422 if ``new_name`` is empty after trimming.
        APIError: 409 if a unique slug cannot be established.
    """
    if actor_role is not MemberRole.OWNER:
        raise APIError(
            status_code=403,
            code="forbidden",
            message="Only the workspace Owner may rename the workspace.",
        )

    cleaned = new_name.strip()
    if not cleaned:
        raise APIError(
            status_code=422,
            code="invalid_request",
            message="Workspace name must not be empty.",
        )

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise APIError(status_code=404, code="not_found", message="Workspace not found.")

    workspace.name = cleaned

    # Regenerate a unique slug from the new name, retrying on collision within a
    # SAVEPOINT (mirrors create_workspace's approach, Req 3.2/20.4).
    candidates = generate_slug_candidates(cleaned)
    last_error: IntegrityError | None = None
    for _ in range(_MAX_INSERT_ATTEMPTS):
        workspace.slug = next(candidates)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:  # slug collision -> try next candidate
            last_error = exc
            continue
        return workspace

    raise APIError(
        status_code=409,
        code="conflict",
        message="Could not generate a unique workspace slug.",
    ) from last_error


async def switch_active_workspace(
    session: AsyncSession,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> Workspace:
    """Resolve and validate a caller's active-workspace switch (Req 3.3, 3.7).

    Confirms that ``user_id`` is a member of ``workspace_id`` before the switch
    is allowed. Only a member may make a workspace their active scope, so a
    non-member is rejected with 404 (the workspace's existence is not disclosed,
    matching the RBAC guard convention for foreign/absent workspaces).

    On success the returned :class:`~app.db.models.Workspace` is what the session
    dependency records as ``active_workspace_id`` on the
    :class:`~app.core.tenancy.RequestContext`, scoping subsequent workspace-level
    operations to it (Req 3.7).

    Args:
        session: The active async session.
        user_id: The caller switching their active workspace.
        workspace_id: The workspace the caller wants to switch to.

    Returns:
        The target :class:`~app.db.models.Workspace`.

    Raises:
        APIError: 404 if the caller is not a member of ``workspace_id`` (whether
            because the workspace does not exist or they simply are not a member).
    """
    membership = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if membership is None:
        raise APIError(status_code=404, code="not_found", message="Workspace not found.")

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        # Defensive: a membership without its workspace would be a data anomaly.
        raise APIError(status_code=404, code="not_found", message="Workspace not found.")
    return workspace


# ---------------------------------------------------------------------------
# Invite state machine + role management (task 7.3)
# ---------------------------------------------------------------------------
#
# The invite lifecycle is: ``pending`` --accept--> ``accepted`` and
# ``pending`` --time passes--> ``expired`` (a terminal state). Only a
# ``pending`` invite whose ``expires_at`` has not passed may be accepted; an
# already-``accepted`` or ``expired`` invite must be rejected on a subsequent
# acceptance (reuse rejection, Req 5.6). An invite past its ``expires_at`` is
# treated as expired even if its stored status is still ``pending`` (lazy
# expiry resolution, Req 5.5).
#
# The transition *decision* is factored into the pure, DB-free
# :func:`resolve_invite_acceptance` so it can be property-tested in isolation
# (Property 16 invite state transitions, Property 17 invite expiry — task 7.4).
# The DB-backed :func:`accept_invite` calls the resolver first, then performs
# the side effects (create member, flip status) only when the decision is
# ``"accept"``.

# Default invite validity window when the caller does not supply one.
_DEFAULT_INVITE_TTL = timedelta(days=7)

# Decision returned by :func:`resolve_invite_acceptance`.
InviteDecision = Literal["accept", "expired", "already_resolved"]

# InviteRole -> MemberRole mapping. ``owner`` is intentionally absent from
# InviteRole (Req 5.7), so every invitable role maps to a concrete membership
# role of the same name.
_INVITE_TO_MEMBER_ROLE: dict[InviteRole, MemberRole] = {
    InviteRole.ADMIN: MemberRole.ADMIN,
    InviteRole.MEMBER: MemberRole.MEMBER,
    InviteRole.VIEWER: MemberRole.VIEWER,
}


def _now() -> datetime:
    """Return the current timezone-aware UTC time (isolated for testability)."""
    return datetime.now(UTC)


def resolve_invite_acceptance(
    current_status: InviteStatus,
    expires_at: datetime | None,
    now: datetime,
) -> InviteDecision:
    """Decide how an acceptance attempt should be resolved (pure, DB-free).

    This is the whole invite state machine, expressed as a single side-effect
    free function so it can be exhaustively property-tested (Property 16, 17,
    task 7.4). It maps ``(current_status, expires_at, now)`` to one of:

    - ``"accept"`` — the invite is ``pending`` and either has no expiry or its
      ``expires_at`` is strictly in the future; the caller should create the
      member and flip the status to ``accepted`` (Req 5.2, 5.3).
    - ``"expired"`` — the invite is ``pending`` but ``now`` is at/after
      ``expires_at``; the caller must reject the acceptance and may persist the
      terminal ``expired`` status (Req 5.5).
    - ``"already_resolved"`` — the invite is already ``accepted`` or
      ``expired``; the acceptance must be rejected (reuse rejection, Req 5.6).

    Expiry is resolved lazily: an invite whose stored status is still
    ``pending`` but whose ``expires_at`` has passed is treated as expired here,
    without needing a background job to have flipped it first (Req 5.5). The
    boundary is inclusive of expiry — an invite exactly at ``expires_at`` is
    ``expired`` (it has reached its expiration time, Req 5.5).

    Args:
        current_status: The invite's persisted status.
        expires_at: The invite's expiry instant, or ``None`` for no expiry.
        now: The reference "current" time to compare against ``expires_at``.

    Returns:
        The :data:`InviteDecision` describing how to resolve the acceptance.
    """
    if current_status is not InviteStatus.PENDING:
        # accepted or expired -> terminal; any further acceptance is reuse.
        return "already_resolved"
    if expires_at is not None and now >= expires_at:
        return "expired"
    return "accept"


async def create_invite(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    email: str,
    role: InviteRole | str,
    expires_at: datetime | None = None,
    ttl: timedelta | None = None,
) -> WorkspaceInvite:
    """Create a ``pending`` workspace invite with a unique token (Req 5.1, 5.2).

    Persists a :class:`~app.db.models.WorkspaceInvite` for ``email`` with the
    requested ``role`` (which must be Admin/Member/Viewer — never Owner, Req
    5.7), a fresh cryptographically-random unique ``token`` stored as bytes, an
    ``expires_at`` (defaulting to now + 7 days), and the initial status
    ``pending`` (Req 5.1, 5.2).

    The generated ``token`` is the raw URL-safe token bytes so the accept flow
    can look the invite up directly by the token presented. The ``token`` column
    is ``UNIQUE``; the token is 32 random bytes (256 bits) so collisions are
    astronomically unlikely.

    The caller owns the surrounding transaction (this flushes but does not
    ``commit``). On success the returned invite carries its generated id and the
    raw token bytes (available to hand to the invitee).

    Args:
        session: The active async session/transaction.
        workspace_id: The workspace the invite grants membership to.
        email: The invited person's email address.
        role: The invited role; must be one of Admin/Member/Viewer. Accepts
            either an :class:`~app.db.models.InviteRole` or its string value
            (e.g. ``"member"``), which is coerced to the enum.
        expires_at: An explicit expiry instant. When omitted, computed from
            ``ttl`` (or the 7-day default).
        ttl: Validity window used to compute ``expires_at`` when it is not given.

    Returns:
        The persisted, ``pending`` :class:`~app.db.models.WorkspaceInvite`.

    Raises:
        APIError: 400 if ``role`` is not a valid invitable role (e.g. Owner,
            which is rejected as an invite role — Req 5.7).
    """
    # Coerce a string role (the wire value, e.g. "member") to the enum and
    # reject any value that is not one of the invitable roles. This defends
    # against an Owner (or otherwise non-invitable) role slipping through:
    # ``InviteRole`` excludes ``owner`` at the type level, so coercing "owner"
    # raises ValueError and is rejected here (Req 5.7).
    try:
        role = InviteRole(role)
    except ValueError:
        role = None  # type: ignore[assignment]
    if role is None or role not in _INVITE_TO_MEMBER_ROLE:
        raise APIError(
            status_code=400,
            code="bad_request",
            message="Invite role must be one of Admin, Member, or Viewer.",
        )

    resolved_expiry = expires_at
    if resolved_expiry is None:
        resolved_expiry = _now() + (ttl if ttl is not None else _DEFAULT_INVITE_TTL)

    # 32 random bytes -> a unique, unguessable token stored as raw bytes so the
    # accept flow can look the invite up by the presented token.
    token = secrets.token_bytes(32)

    invite = WorkspaceInvite(
        workspace_id=workspace_id,
        email=email,
        role=role,
        token=token,
        status=InviteStatus.PENDING,
        expires_at=resolved_expiry,
    )
    session.add(invite)
    await session.flush()
    return invite


async def accept_invite(
    session: AsyncSession,
    *,
    token: bytes,
    user_id: uuid.UUID,
) -> WorkspaceMember:
    """Accept a pending invite, creating a member with the invited role.

    Looks up the invite by the presented ``token`` and runs the pure
    :func:`resolve_invite_acceptance` state machine to decide the outcome:

    - No invite matches the token -> 404 invalid-invitation (Req 5.4).
    - Decision ``"accept"`` -> create a :class:`~app.db.models.WorkspaceMember`
      for ``user_id`` with the invited role (mapped InviteRole -> MemberRole)
      and flip the invite to ``accepted`` (Req 5.2, 5.3).
    - Decision ``"expired"`` -> persist the terminal ``expired`` status and
      reject with 410 gone (Req 5.5).
    - Decision ``"already_resolved"`` (already accepted/expired) -> reject with
      409 conflict; reuse is not allowed (Req 5.6).

    The caller owns the surrounding transaction (flushes but does not commit).

    Args:
        session: The active async session/transaction.
        token: The raw invite token bytes presented by the invitee.
        user_id: The user accepting the invite; becomes the new member.

    Returns:
        The newly created :class:`~app.db.models.WorkspaceMember`.

    Raises:
        APIError: 404 if the token matches no invite (Req 5.4); 410 if the
            invite has expired (Req 5.5); 409 if the invite was already resolved
            (Req 5.6).
    """
    invite = await session.scalar(
        select(WorkspaceInvite).where(WorkspaceInvite.token == token)
    )
    if invite is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="The invitation is invalid.",
        )

    decision = resolve_invite_acceptance(invite.status, invite.expires_at, _now())

    if decision == "expired":
        # Lazily persist the terminal expired status (Req 5.5) and reject.
        invite.status = InviteStatus.EXPIRED
        await session.flush()
        raise APIError(
            status_code=410,
            code="gone",
            message="The invitation has expired.",
        )

    if decision == "already_resolved":
        # accepted or expired already -> reuse is rejected (Req 5.6).
        raise APIError(
            status_code=409,
            code="conflict",
            message="The invitation has already been used.",
        )

    # decision == "accept": create the member with the invited role and flip the
    # invite to accepted (Req 5.2, 5.3).
    member = WorkspaceMember(
        workspace_id=invite.workspace_id,
        user_id=user_id,
        role=_INVITE_TO_MEMBER_ROLE[invite.role],
    )
    session.add(member)
    invite.status = InviteStatus.ACCEPTED
    await session.flush()
    return member


async def update_member_role(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_user_id: uuid.UUID,
    new_role: MemberRole,
) -> WorkspaceMember:
    """Change an existing member's role within a workspace (Req 4.x role mgmt).

    Applies the role state change for the workspace member identified by
    ``(workspace_id, target_user_id)``. The Owner-only capability guard for
    managing members is enforced at the router (task 7.5, Req 4.5/5.x); this
    function performs only the state transition so it can be composed and
    tested independently.

    The caller owns the surrounding transaction (flushes but does not commit).

    Args:
        session: The active async session/transaction.
        workspace_id: The workspace the membership belongs to.
        target_user_id: The user whose role is being changed.
        new_role: The role to assign (owner/admin/member/viewer).

    Returns:
        The updated :class:`~app.db.models.WorkspaceMember`.

    Raises:
        APIError: 404 if no such membership exists in the workspace.
    """
    member = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == target_user_id,
        )
    )
    if member is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Workspace member not found.",
        )

    member.role = new_role
    await session.flush()
    return member


__all__ = [
    "slugify",
    "generate_slug_candidates",
    "unique_slug",
    "list_workspaces_for_user",
    "create_workspace",
    "delete_workspace",
    "rename_workspace",
    "switch_active_workspace",
    "resolve_invite_acceptance",
    "create_invite",
    "accept_invite",
    "update_member_role",
    "InviteDecision",
]
