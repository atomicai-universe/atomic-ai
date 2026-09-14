"""Integrations OAuth router — begin/complete per-integration authorization (task 5.1).

This module wires :mod:`app.services.integration_oauth` into two HTTP routes
under the ``/api/v1/integrations`` prefix, alongside (but separate from) the
existing connect/list/sharing/disconnect surface in :mod:`app.api.integrations`.
It is the HTTP seam for the per-integration OAuth 2.0 Authorization-Code flow:
the *producer* of the refresh token that ``oauth_refresh.ensure_access_token``
already consumes.

Two endpoints (integration-scoped, NOT login):

- ``GET /api/v1/integrations/{integration_id}/oauth/authorize`` — **session +
  workspace RBAC**. Requires a valid Session_Token via
  :func:`app.api.deps.require_session`; resolves the integration tenant-scoped
  (404 for a foreign or absent id, so existence is not disclosed — Req 2.2/2.3);
  authorizes the caller as the integration **creator** or a non-creating
  Owner/Admin holding :attr:`~app.core.rbac.Capability.MANAGE_INTEGRATIONS`
  (403 otherwise — Req 2.5), mirroring the connect/disconnect rule; rejects a
  non-OAuth-family provider with 400 (Req 2.6, via
  :func:`integration_oauth.lookup` inside ``begin_authorize``); then builds the
  provider authorization URL and responds with a **302** to it (Req 1.1). The
  response body carries no secrets or tokens (Req 7.2).
- ``GET /api/v1/integrations/oauth/callback/{provider}`` — **public** (no
  session; the request is trusted only via the Redis-validated, single-use
  ``state`` — Req 3.1). Calls
  :meth:`~app.services.integration_oauth.IntegrationOAuthService.complete_authorize`
  and, on **any** outcome, responds with a **302** back to the
  Frontend_Integrations_Page carrying an ``oauth=success|error`` status query
  (Req 3.5/3.6). A service :class:`~app.core.errors.APIError` (invalid/expired/
  mismatched state, failed exchange, missing refresh token) is caught and mapped
  to an ``?oauth=error&reason=...`` redirect rather than surfaced as a raw
  4xx — the callback always redirects the browser back to the SPA. Tokens are
  never rendered in any response (Req 7.2).

Frontend redirect base. The callback redirects the browser back into the SPA.
The frontend origin is derived from the configured ``POST_LOGIN_REDIRECT_URL``
(the same setting that sends the browser into the frontend app after login);
its scheme+host are reused and the fixed ``/dashboard/integrations`` path is
appended so the integrations page receives the ``?oauth=...`` status. No new
config key is introduced (the design reuses existing configuration).

Requirements: 1.1, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.5, 3.6, 7.2.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis, require_session
from app.config import get_settings
from app.core.errors import APIError
from app.core.rbac import Capability, can
from app.core.tenancy import RequestContext
from app.db.models import Integration, MemberRole
from app.db.session import get_session
from app.services import integration_oauth
from app.services.integration_oauth import IntegrationOAuthService

logger = logging.getLogger("atomic_ai.integrations_oauth")

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations-oauth"])

# Fixed frontend path the callback redirects the browser back to; the origin
# (scheme+host) is taken from POST_LOGIN_REDIRECT_URL at request time.
_FRONTEND_INTEGRATIONS_PATH = "/dashboard/integrations"


def _oauth_service() -> IntegrationOAuthService:
    """Provide the :class:`IntegrationOAuthService`; overridable in tests."""
    return IntegrationOAuthService()


def _frontend_integrations_url(**query: str) -> str:
    """Build the Frontend_Integrations_Page URL with an ``oauth=...`` status query.

    The frontend origin (scheme + host, e.g. ``http://localhost:3000``) is
    reused from the configured ``POST_LOGIN_REDIRECT_URL`` — the same setting
    that lands the browser in the frontend app after login — and the fixed
    ``/dashboard/integrations`` path is appended. When the setting has no
    scheme/host (e.g. a bare relative path), only the path + query is emitted so
    the browser resolves it against the current origin.
    """
    parts = urlsplit(get_settings().POST_LOGIN_REDIRECT_URL)
    return urlunsplit(
        (parts.scheme, parts.netloc, _FRONTEND_INTEGRATIONS_PATH, urlencode(query), "")
    )


@router.get("/{integration_id}/oauth/authorize")
async def begin_authorize(
    integration_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
    service: IntegrationOAuthService = Depends(_oauth_service),
) -> RedirectResponse:
    """Begin a per-integration OAuth authorization and redirect to the provider.

    Requires a valid Session_Token (Req 2.1). Resolves the integration
    tenant-scoped — a foreign or absent id returns an identical **404** so the
    integration's existence is not disclosed (Req 2.2/2.3). Authorizes the
    caller as the integration **creator**, or a non-creating Owner/Admin holding
    ``MANAGE_INTEGRATIONS`` (**403** otherwise — Req 2.5), the same rule
    connect/disconnect enforce. A non-OAuth-family provider is rejected with
    **400** by :func:`integration_oauth.lookup` inside ``begin_authorize``
    (Req 2.6). On success, responds with a **302** to the provider authorization
    URL (Req 1.1). The response body carries no secret or token material (Req 7.2).
    """
    integration = await session.get(Integration, integration_id)
    # Tenant scoping: the caller must be a member of the integration's workspace.
    # A missing integration OR one in a workspace the caller is not a member of
    # both yield the SAME 404 (existence is not disclosed — Property 4).
    role: MemberRole | None = (
        ctx.member_role(integration.workspace_id) if integration is not None else None
    )
    if integration is None or role is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Integration not found.",
        )

    # Authorization: the creator may always authorize; a non-creator needs the
    # MANAGE_INTEGRATIONS capability (Owner/Admin). Otherwise 403 (Req 2.5).
    is_creator = integration.created_by_user_id == ctx.user_id
    if not is_creator and not can(role, Capability.MANAGE_INTEGRATIONS):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="You do not have permission to perform this action.",
        )

    # begin_authorize resolves the provider registry and raises a 400
    # (unsupported_provider) for a non-OAuth-family provider (Req 2.6), mints
    # PKCE/state, stores the Redis flow record, and returns the provider URL.
    result = await service.begin_authorize(
        integration=integration,
        user_id=ctx.user_id,
        session=session,
        redis=redis,
    )
    logger.info(
        "integration_oauth.authorize integration_id=%s user_id=%s",
        integration_id,
        ctx.user_id,
    )
    # 302 to the provider; the response body carries no secrets/tokens (Req 7.2).
    return RedirectResponse(
        url=result.authorization_url, status_code=status.HTTP_302_FOUND
    )


@router.get("/oauth/callback/{provider}")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
    service: IntegrationOAuthService = Depends(_oauth_service),
) -> RedirectResponse:
    """Complete a per-integration authorization from the provider callback (public).

    This route carries **no session** (Req 3.1); the request is trusted only via
    the Redis-validated, single-use ``state`` inside
    :meth:`IntegrationOAuthService.complete_authorize`. On **any** outcome the
    browser is redirected (**302**) back to the Frontend_Integrations_Page with
    an ``oauth=success|error`` status query (Req 3.5/3.6):

    - success → ``?oauth=success&integration={integration_id}``
    - missing refresh token → ``?oauth=error&reason=no_refresh_token``
    - token-exchange failure → ``?oauth=error&reason=exchange``
    - invalid/expired/mismatched state → ``?oauth=error&reason=state``

    A service :class:`~app.core.errors.APIError` is caught and mapped to the
    corresponding error redirect rather than surfaced as a raw 4xx — the
    callback always redirects the browser back to the SPA. No token material is
    ever rendered in the response (Req 7.2).
    """
    try:
        completed = await service.complete_authorize(
            provider_slug=provider,
            code=code,
            state=state,
            session=session,
            redis=redis,
        )
    except APIError as exc:
        reason = _reason_for_error(exc)
        logger.info(
            "integration_oauth.callback_error provider=%s reason=%s", provider, reason
        )
        return RedirectResponse(
            url=_frontend_integrations_url(oauth="error", reason=reason),
            status_code=status.HTTP_302_FOUND,
        )

    logger.info(
        "integration_oauth.callback_success provider=%s integration_id=%s",
        provider,
        completed.integration_id,
    )
    return RedirectResponse(
        url=_frontend_integrations_url(
            oauth="success", integration=str(completed.integration_id)
        ),
        status_code=status.HTTP_302_FOUND,
    )


def _reason_for_error(exc: APIError) -> str:
    """Map a ``complete_authorize`` :class:`APIError` to a callback ``reason``.

    Mirrors the design's Error Handling table:

    - ``oauth_no_refresh_token`` (carries ``fields={"reason": "no_refresh_token"}``)
      → ``no_refresh_token``
    - ``oauth_exchange_failed`` → ``exchange``
    - anything else (invalid/expired/consumed/mismatched ``state``) → ``state``
    """
    fields = exc.fields or {}
    if fields.get("reason") == "no_refresh_token" or exc.code == "oauth_no_refresh_token":
        return "no_refresh_token"
    if exc.code == "oauth_exchange_failed":
        return "exchange"
    return "state"


__all__ = ["router"]
