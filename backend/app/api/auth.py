"""Auth router — OAuth login begin/callback and logout (task 5.5).

This module wires the pure/service pieces built in tasks 5.1–5.3 into public
HTTP endpoints under the ``/auth`` prefix (which the session allowlist treats as
public, so none of these routes require a prior Session_Token — Req 2.1):

- ``GET /auth/login/{provider}`` — starts an OAuth flow via
  :meth:`AuthService.begin_login` and issues a **302 redirect** to the provider's
  authorization URL. The pending flow (``state``/``code_verifier``/``nonce``) is
  stashed in Redis by the service.
- ``GET /auth/callback/{provider}`` — receives ``code`` + ``state``, calls
  :meth:`AuthService.complete_login` (which validates state, exchanges the code
  server-to-server, and finds-or-creates the user), sets the browser session
  cookie (``HttpOnly``/``Secure``/``SameSite=Lax``, Req 2.6), writes an
  ``auth.login`` audit row (Req 15.2), commits, and **302-redirects** to the
  configured post-login destination.
- ``POST /auth/logout`` — invalidates the current session and clears the cookie
  via :func:`app.api.deps.logout`, writes an ``auth.logout`` audit row for the
  session's user (Req 15.2), and returns ``200``. A missing/invalid session still
  returns ``200`` and clears the cookie (idempotent logout).

Auth audit logging (Req 15.2). Until the Audit_Service consolidation lands
(task 15.1), this router writes audit rows directly to ``system_audit_logs`` via
the request session: ``action`` is ``auth.login``/``auth.logout``, ``user_id`` is
the acting user, ``workspace_id`` is ``NULL`` (auth is not workspace-scoped), and
``metadata`` is passed through :func:`app.core.scrubbing.scrub` so no token or
credential value is ever persisted (Req 15.4). When task 15.1 introduces an
``Audit_Service``, :func:`_write_auth_audit` should delegate to it instead.

Requirements: 2.1, 15.2 (also uses 1.1, 2.6, 15.4).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    extract_session_token,
    get_redis,
    logout as logout_session,
    set_session_cookie,
)
from app.config import get_settings
from app.core.scrubbing import scrub
from app.core.security import load_session
from app.db.models import AuthProvider, SystemAuditLog
from app.db.session import get_session
from app.services.auth_service import AuthService

logger = logging.getLogger("atomic_ai.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Audit action names for authentication events (Req 15.2).
AUDIT_ACTION_LOGIN = "auth.login"
AUDIT_ACTION_LOGOUT = "auth.logout"


def _auth_service() -> AuthService:
    """Provide the :class:`AuthService`; overridable in tests."""
    return AuthService()


def _callback_redirect_uri(provider: AuthProvider) -> str:
    """Build the provider ``redirect_uri`` from the configured base URL.

    Combines ``OAUTH_REDIRECT_BASE_URL`` with this router's callback path so the
    value sent on ``begin_login`` matches the one the provider calls back to
    (``.../auth/callback/{provider}``).
    """
    base = get_settings().OAUTH_REDIRECT_BASE_URL.rstrip("/")
    return f"{base}/auth/callback/{provider.value}"


async def _write_auth_audit(
    session: AsyncSession,
    *,
    action: str,
    user_id: uuid.UUID | None,
    metadata: dict | None = None,
) -> None:
    """Append an authentication Audit_Log row (Req 15.2).

    Writes to ``system_audit_logs`` with a ``NULL`` ``workspace_id`` (auth is not
    workspace-scoped). Any ``metadata`` is scrubbed first so no Session_Token or
    OAuth credential can be persisted (Req 15.4); tokens are never passed in.
    This is the interim path noted in the module docstring — task 15.1's
    Audit_Service should replace the direct write.
    """
    session.add(
        SystemAuditLog(
            workspace_id=None,
            user_id=user_id,
            action=action,
            log_metadata=scrub(metadata) if metadata is not None else None,
        )
    )


@router.get("/login/{provider}")
async def login(
    provider: str,
    redis=Depends(get_redis),
    service: AuthService = Depends(_auth_service),
) -> RedirectResponse:
    """Begin an OAuth login and redirect to the provider (Req 1.1, 2.1).

    ``provider`` must be ``google`` or ``github`` (the service rejects anything
    else with a 400 through the central envelope). Returns a **302** to the
    provider's authorization URL; the pending flow state is stored server-side in
    Redis by the service.
    """
    resolved = _coerce_provider(provider)
    redirect_uri = _callback_redirect_uri(resolved)
    result = await service.begin_login(resolved, redirect_uri, redis=redis)
    return RedirectResponse(
        url=result.authorization_url, status_code=status.HTTP_302_FOUND
    )


@router.get("/callback/{provider}")
async def callback(
    provider: str,
    code: str,
    state: str,
    redis=Depends(get_redis),
    session: AsyncSession = Depends(get_session),
    service: AuthService = Depends(_auth_service),
) -> RedirectResponse:
    """Complete an OAuth login: set the session cookie and redirect (Req 2.1, 2.6, 15.2).

    Validates ``state`` and exchanges ``code`` via
    :meth:`AuthService.complete_login`, sets the browser session cookie, records
    an ``auth.login`` audit row, commits the transaction, and **302-redirects**
    to the configured post-login destination. Any failure (invalid state,
    provider error, account conflict) is raised as an ``APIError`` and rendered
    by the central handler; no partial session is committed.
    """
    resolved = _coerce_provider(provider)
    user, raw_token, expires_at = await service.complete_login(
        resolved, code, state, session=session, redis=redis
    )

    await _write_auth_audit(
        session,
        action=AUDIT_ACTION_LOGIN,
        user_id=user.id,
        metadata={"provider": resolved.value},
    )
    await session.commit()

    redirect = RedirectResponse(
        url=get_settings().POST_LOGIN_REDIRECT_URL,
        status_code=status.HTTP_302_FOUND,
    )
    set_session_cookie(redirect, raw_token, expires_at)
    logger.info("auth.login user_id=%s provider=%s", user.id, resolved.value)
    return redirect


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    redis=Depends(get_redis),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Invalidate the current session and clear the cookie (Req 2.5, 15.2).

    Resolves the acting user from the presented token (best-effort, before
    revocation) so the ``auth.logout`` audit row records who logged out, then
    invalidates the session + clears the cookie via :func:`app.api.deps.logout`.
    Returns ``200`` regardless of whether a live session was present, so a stale
    cookie is always cleared without leaking whether the token was valid.
    """
    # Resolve the acting user (if any) before we revoke, so the audit row can
    # attribute the logout. A missing/unknown token yields user_id=None.
    acting_user_id: uuid.UUID | None = None
    raw_token = extract_session_token(request)
    if raw_token is not None:
        record = await load_session(session, raw_token)
        if record is not None:
            acting_user_id = record.user_id

    await logout_session(request, response, session=session, redis=redis)

    await _write_auth_audit(
        session, action=AUDIT_ACTION_LOGOUT, user_id=acting_user_id
    )
    await session.commit()
    logger.info("auth.logout user_id=%s", acting_user_id)
    return {"status": "ok"}


def _coerce_provider(provider: str) -> AuthProvider:
    """Validate a path ``provider`` to :class:`AuthProvider` (Req 1.6).

    Rejects anything other than ``google``/``github`` with a 400 through the
    central error envelope, mirroring the service's own guard so a bad provider
    fails before any Redis/DB work.
    """
    from app.core.errors import APIError

    try:
        return AuthProvider(provider)
    except ValueError:
        raise APIError(
            status_code=400,
            code="unsupported_provider",
            message="Unsupported OAuth provider.",
        ) from None


__all__ = [
    "router",
    "AUDIT_ACTION_LOGIN",
    "AUDIT_ACTION_LOGOUT",
]
