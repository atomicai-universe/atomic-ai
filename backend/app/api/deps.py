"""Reusable request dependencies: session auth, cookies, and logout.

This module is the seam between the pure session logic in
:mod:`app.core.security` and the HTTP layer. It provides the pieces that
protected routers (task 5.5 wires ``api/auth.py``; other routers follow) share:

- :func:`require_session` — a FastAPI dependency that extracts the
  Session_Token from the request (the ``session`` cookie and/or an
  ``Authorization: Bearer`` header), validates it (Redis fast-path then the
  authoritative DB row), and returns a resolved
  :class:`~app.core.tenancy.RequestContext`. A missing, invalid, revoked, or
  expired token is rejected with **401** (Req 2.2, 2.4). It is applied per
  *protected* route rather than globally, so the public routes ``/auth/*`` and
  ``/health`` simply do not depend on it (Req 2.1); see
  :data:`PUBLIC_PATH_PREFIXES`.
- :func:`get_optional_session` — the same resolution but returning ``None``
  instead of raising, for routes that adapt to an optional caller.
- :func:`set_session_cookie` / :func:`clear_session_cookie` — write and clear
  the browser session cookie with ``HttpOnly``, ``Secure``, and
  ``SameSite=Lax`` and a ``max-age``/``expires`` matching the session expiry
  (Req 2.6).
- :func:`logout` — invalidate the *current* session (revoke the DB row and
  evict the Redis fast-path) and clear the cookie (Req 2.5).

Design decisions:

- The token is opaque and server-tracked (see :mod:`app.core.security`), so
  validation is a lookup + :func:`app.core.security.is_session_valid` check
  against a caller-supplied ``now`` (``datetime.now(timezone.utc)``), never a
  self-contained JWT decode. This keeps logout/ban a true invalidation.
- The Redis fast-path (:func:`app.core.security.is_cached_session_present`) is
  consulted first *only as an optimization to confirm liveness*; the DB row is
  still loaded to build the context and to re-check validity authoritatively,
  so an evicted/expired cache never yields a false positive.
- The active workspace is taken from the ``X-Workspace-Id`` header (falling
  back to a ``workspace`` cookie) when present and parseable; otherwise the
  context has no active workspace and workspace-scoped guards will require one.

Requirements: 2.1, 2.2, 2.4, 2.5, 2.6.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import APIError
from app.core.security import (
    evict_cached_session,
    is_session_valid,
    load_session,
    revoke_session,
)
from app.core.tenancy import RequestContext
from app.db.models import MemberRole, User, WorkspaceMember
from app.db.session import get_session

# Name of the browser cookie carrying the Session_Token.
SESSION_COOKIE_NAME = "session"

# Optional cookie/header naming the caller's active workspace. The header wins
# over the cookie when both are present.
ACTIVE_WORKSPACE_COOKIE_NAME = "workspace"
ACTIVE_WORKSPACE_HEADER = "X-Workspace-Id"

# Path prefixes that MUST NOT require a Session_Token (Req 2.1). These routers
# omit the ``require_session`` dependency entirely rather than being blocked by
# a global gate; the list is documentation + a helper for any middleware/router
# that wants to check "is this public?".
PUBLIC_PATH_PREFIXES: tuple[str, ...] = ("/auth", "/health", "/api/v1/webhooks")


def is_public_path(path: str) -> bool:
    """Return whether ``path`` is a public route that needs no Session_Token.

    A path is public when it equals or is nested under one of
    :data:`PUBLIC_PATH_PREFIXES` (e.g. ``/auth``, ``/auth/login``, ``/health``).
    Protected routers depend on :func:`require_session`; public routers do not.
    This helper exists so the allowlist has a single, testable definition.
    """
    for prefix in PUBLIC_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


def extract_session_token(request: Request) -> str | None:
    """Pull the raw Session_Token from a request, or ``None`` if absent.

    Looks first at the ``session`` cookie (how browsers carry it, Req 2.6) and
    then at an ``Authorization: Bearer <token>`` header (for programmatic
    clients). The cookie takes precedence when both are present. A blank or
    malformed header yields no token rather than an error, so the caller can
    apply the single 401 policy in one place.
    """
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token

    header = request.headers.get("Authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return None


def _unauthorized() -> APIError:
    """Build the standard 401 for a missing/invalid/expired session (Req 2.2/2.4)."""
    return APIError(
        status_code=401,
        code="unauthorized",
        message="Authentication is required or has failed.",
    )


def _parse_active_workspace(request: Request) -> uuid.UUID | None:
    """Resolve the caller's active workspace id from header/cookie, if valid.

    The ``X-Workspace-Id`` header is preferred; a ``workspace`` cookie is the
    fallback. A missing or unparseable value yields ``None`` (no active
    workspace), which is a valid state — workspace-scoped guards then require
    the caller to select one.
    """
    raw = request.headers.get(ACTIVE_WORKSPACE_HEADER) or request.cookies.get(
        ACTIVE_WORKSPACE_COOKIE_NAME
    )
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


async def _load_user_roles(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID, MemberRole]:
    """Return ``{workspace_id: role}`` for every workspace ``user_id`` belongs to.

    This is the membership mapping :class:`RequestContext` needs so
    :func:`app.core.rbac.require` can authorize workspace-scoped operations for
    the caller. A user who is a member of nothing yields an empty mapping.
    """
    result = await session.execute(
        select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(
            WorkspaceMember.user_id == user_id
        )
    )
    return {workspace_id: role for workspace_id, role in result.all()}


async def _build_context(
    request: Request, session: AsyncSession, user: User
) -> RequestContext:
    """Assemble the per-request :class:`RequestContext` for an authenticated user.

    Resolves the user's workspace memberships into the ``roles`` mapping, reads
    ``is_superadmin`` from the user row, and sets the active workspace from the
    request's ``X-Workspace-Id`` header / ``workspace`` cookie when present and
    the caller is actually a member of it (an unknown/foreign id is ignored so
    it can never widen access).
    """
    roles = await _load_user_roles(session, user.id)
    active = _parse_active_workspace(request)
    if active is not None and active not in roles:
        # Never trust a client-supplied workspace the caller isn't a member of.
        active = None
    return RequestContext(
        user_id=user.id,
        active_workspace_id=active,
        is_superadmin=bool(user.is_superadmin),
        roles=roles,
    )


async def _resolve_session(
    request: Request, session: AsyncSession
) -> tuple[User, RequestContext] | None:
    """Validate the request's Session_Token and resolve the user + context.

    Returns ``(user, context)`` for a live session or ``None`` when there is no
    token, no matching row, or the row is revoked/expired. The DB row is always
    the authority: even when a Redis fast-path entry exists, validity is
    re-checked against the row so an expired/revoked session is never accepted.
    """
    raw_token = extract_session_token(request)
    if raw_token is None:
        return None

    record = await load_session(session, raw_token)
    now = datetime.now(timezone.utc)
    if not is_session_valid(record, now):
        return None

    user = await session.get(User, record.user_id)
    if user is None:
        # Session points at a user that no longer exists — fail closed.
        return None

    context = await _build_context(request, session, user)
    return user, context


async def require_session(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RequestContext:
    """FastAPI dependency: require a valid Session_Token; return the context.

    Applied to protected routes (public ``/auth/*`` and ``/health`` omit it,
    Req 2.1). Rejects a request that presents no token, an unknown token, or a
    revoked/expired session with **401** (Req 2.2, 2.4); otherwise returns the
    resolved :class:`RequestContext` carrying the authenticated ``user_id``, the
    caller's workspace ``roles``, ``is_superadmin``, and the active workspace.
    """
    resolved = await _resolve_session(request, session)
    if resolved is None:
        raise _unauthorized()
    _user, context = resolved
    return context


async def get_optional_session(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RequestContext | None:
    """Like :func:`require_session` but return ``None`` instead of raising 401.

    For routes that behave differently for an authenticated vs anonymous caller
    without hard-requiring a session.
    """
    resolved = await _resolve_session(request, session)
    if resolved is None:
        return None
    _user, context = resolved
    return context


# ---------------------------------------------------------------------------
# Redis dependency
# ---------------------------------------------------------------------------


async def get_redis() -> AsyncIterator[object]:
    """FastAPI dependency yielding a short-lived async Redis client.

    Builds the client lazily from ``REDIS_URL`` (a :class:`~pydantic.SecretStr`,
    unwrapped only here so the URL never appears in logs) per request and closes
    it afterwards. The auth router uses it to stash/consume the pending OAuth
    flow (see :mod:`app.services.auth_service`) and to evict the session
    fast-path on logout. Tests override this dependency with an in-memory fake,
    so no real Redis is required to exercise the routes.
    """
    import redis.asyncio as redis_asyncio

    settings = get_settings()
    client = redis_asyncio.from_url(settings.REDIS_URL.get_secret_value())
    try:
        yield client
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


# ---------------------------------------------------------------------------
# Cookie helpers (Req 2.6)
# ---------------------------------------------------------------------------


def set_session_cookie(
    response: Response, raw_token: str, expires_at: datetime
) -> None:
    """Write the browser session cookie for ``raw_token`` (Req 2.6).

    The cookie is marked ``HttpOnly`` (not readable by JS), ``Secure`` (only
    sent over HTTPS), and ``SameSite=Lax`` (sent on top-level navigations, not
    cross-site subrequests). Its ``max-age`` is the remaining lifetime derived
    from ``expires_at`` so the cookie expires with the session; a non-positive
    remaining lifetime clamps to ``0`` (an immediately-expiring cookie).
    """
    now = datetime.now(timezone.utc)
    remaining = int((expires_at - now).total_seconds())
    max_age = remaining if remaining > 0 else 0
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Clear the browser session cookie (used on logout, Req 2.5/2.6).

    Emits a ``Set-Cookie`` that expires the cookie immediately, matching the
    attributes used when it was set so the browser reliably removes it.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# ---------------------------------------------------------------------------
# Logout (Req 2.5)
# ---------------------------------------------------------------------------


async def logout(
    request: Request,
    response: Response,
    session: AsyncSession,
    redis=None,
) -> bool:
    """Invalidate the caller's current session and clear the cookie (Req 2.5).

    Revokes the DB session row for the request's token and, when a Redis client
    is supplied, evicts the fast-path entry so the token is rejected on the very
    next request. The cookie is always cleared. Returns whether a session row
    was actually revoked (``False`` when no/unknown token was presented); the
    cookie is cleared either way so a stale cookie does not linger.
    """
    raw_token = extract_session_token(request)
    revoked = False
    if raw_token is not None:
        revoked = await revoke_session(session, raw_token)
        if redis is not None:
            await evict_cached_session(redis, raw_token)
    clear_session_cookie(response)
    return revoked


__all__ = [
    "SESSION_COOKIE_NAME",
    "ACTIVE_WORKSPACE_COOKIE_NAME",
    "ACTIVE_WORKSPACE_HEADER",
    "PUBLIC_PATH_PREFIXES",
    "is_public_path",
    "extract_session_token",
    "get_redis",
    "require_session",
    "get_optional_session",
    "set_session_cookie",
    "clear_session_cookie",
    "logout",
]
