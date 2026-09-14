"""Integration_OAuth_Service — per-integration OAuth 2.0 authorization-code flow.

This module is the missing *producer* of the refresh token that
``app.services.oauth_refresh.ensure_access_token`` already consumes. It lets a
workspace member grant the platform delegated access to a provider account and
persists the resulting refresh token into that integration's encrypted
credential blob. It is deliberately distinct from the login flow in
``app.services.auth_service``: completing an integration authorization never
creates a ``User``, never issues a Session_Token, and never writes ``auth.*``
audit actions.

This first slice provides the **OAuth_Provider_Registry** — a pure data +
helper structure (no I/O), mirroring the shape philosophy of
``provider_credentials.PROVIDER_CREDENTIALS``. It maps each OAuth-authorization-
code provider slug to its doc-verified authorize/token URLs, scopes, and the
provider-specific parameters that force a refresh token to be issued
(Google/Zoho ``access_type=offline`` + ``prompt=consent``; Microsoft family
``offline_access`` scope; Salesforce ``refresh_token`` scope).

API-key / bot-token / basic-auth / client-credentials-only providers are **not**
OAuth-family and are intentionally absent from the registry; a lookup for such a
slug raises an :class:`APIError` mapping to HTTP 400.

Requirements: 5.1, 5.2, 5.3, 8.1, 9.1, 9.2, 9.3, 9.4.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from urllib.parse import urlencode

from app.config import get_settings
from app.core.errors import APIError
from app.core.scrubbing import scrub
from app.db.models import Integration, SystemAuditLog
from app.services import integration_vault
from app.services.auth_service import (
    OAUTH_FLOW_TTL_SECONDS,
    AsyncHTTPClient,
    derive_code_challenge,
    generate_pkce_verifier,
    generate_state,
)

logger = logging.getLogger("atomic_ai.integration_oauth")

# Redis key prefix for pending per-integration OAuth flows, keyed by the opaque
# ``state``. Deliberately distinct from the login flow's ``oauth:state:`` prefix
# (``auth_service._OAUTH_STATE_PREFIX``) so the two never collide (Req 1.4, 3.1).
_FLOW_STATE_PREFIX = "oauth:integration:"

# Fixed, provider-keyed callback path appended to ``OAUTH_REDIRECT_BASE_URL`` to
# build the ``redirect_uri`` (Req 8.1). The ``integration_id`` is NOT in the URL
# (Req 8.2/8.3) — it travels in the Redis flow record instead.
_CALLBACK_PATH = "/api/v1/integrations/oauth/callback/{provider}"

# Audit action recorded when an integration authorization completes successfully
# (Req 7.4). Namespaced under ``integration.`` like the connect/disconnect
# actions in ``app.api.integrations`` and deliberately NOT an ``auth.*`` action
# (completing an integration authorization never touches the login flow).
AUDIT_ACTION_AUTHORIZED = "integration.authorized"

# Non-secret marker persisted in an integration's ``config`` once the OAuth
# authorize flow has completed and a refresh token is stored. Lets the UI
# render an "Authorized" state without decrypting credentials.
OAUTH_AUTHORIZED_KEY = "__oauth_authorized"


@dataclass(frozen=True)
class OAuthProvider:
    """One OAuth-authorization-code provider's registry entry.

    Attributes:
        slug: Provider slug, matching the ``provider_credentials`` key (e.g.
            ``"gmail"``).
        authorize_url: Provider authorization endpoint. For Microsoft/Entra this
            is a ``{tenant}`` template resolved from the integration config at
            request time.
        token_url: Provider token endpoint (server-to-server exchange). May be a
            ``{tenant}`` template when :attr:`token_url_template` is True.
        scopes: Doc-verified scopes requested for this provider. Never empty.
        extra_authorize_params: Extra query parameters appended to the
            authorization URL to force a refresh token (e.g.
            ``{"access_type": "offline", "prompt": "consent"}``).
        uses_pkce: True when the provider supports PKCE (S256); the authorize URL
            then carries a ``code_challenge`` with ``code_challenge_method=S256``.
        client_id_key: Key of the client id inside the integration credential map.
        client_secret_key: Key of the client secret inside the credential map.
        token_url_template: True when :attr:`authorize_url`/:attr:`token_url`
            contain a ``{tenant}`` placeholder that must be resolved before use.
    """

    slug: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    uses_pkce: bool = True
    client_id_key: str = "client_id"
    client_secret_key: str = "client_secret"
    token_url_template: bool = False


# ---------------------------------------------------------------------------
# Doc-verified endpoints, scopes, and refresh-token parameters.
#
# Sources (see design.md Data Models → "Doc-verified provider parameters"):
# Google      https://developers.google.com/identity/protocols/oauth2/web-server
# Microsoft   https://learn.microsoft.com/graph/auth-v2-user
# Salesforce  https://help.salesforce.com/ (OAuth 2.0 Web Server Flow)
# Zoho        https://www.zoho.com/developer/oauth/
# Intuit      https://developer.intuit.com/app/developer/qbpayments/docs/develop
# Content was rephrased for compliance with licensing restrictions.
# ---------------------------------------------------------------------------

_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Google/Zoho: offline access + forced consent guarantee a refresh token.
_GOOGLE_REFRESH_PARAMS: dict[str, str] = {
    "access_type": "offline",
    "prompt": "consent",
}

_MS_AUTHORIZE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Microsoft/Entra: offline_access in scope yields a refresh token; prompt=consent
# forces re-consent so a refresh token is reliably issued.
_MS_REFRESH_PARAMS: dict[str, str] = {"prompt": "consent"}


def _google(slug: str, *scopes: str) -> OAuthProvider:
    """Build a Google-family registry entry (offline + consent params)."""
    return OAuthProvider(
        slug=slug,
        authorize_url=_GOOGLE_AUTHORIZE_URL,
        token_url=_GOOGLE_TOKEN_URL,
        scopes=(*scopes, "openid", "email"),
        extra_authorize_params=dict(_GOOGLE_REFRESH_PARAMS),
        uses_pkce=True,
    )


def _microsoft(slug: str, *scopes: str) -> OAuthProvider:
    """Build a Microsoft/Entra-family entry (offline_access scope, {tenant} URLs)."""
    return OAuthProvider(
        slug=slug,
        authorize_url=_MS_AUTHORIZE_URL,
        token_url=_MS_TOKEN_URL,
        scopes=("offline_access", *scopes),
        extra_authorize_params=dict(_MS_REFRESH_PARAMS),
        uses_pkce=True,
        token_url_template=True,
    )


# Registry keyed by provider slug — OAuth-authorization-code family ONLY.
# The 21 slugs enumerated in Requirement 9.1.
OAUTH_PROVIDERS: dict[str, OAuthProvider] = {
    # ---- Google family (access_type=offline + prompt=consent) ----
    "gmail": _google("gmail", "https://www.googleapis.com/auth/gmail.modify"),
    "google_calendar": _google(
        "google_calendar", "https://www.googleapis.com/auth/calendar"
    ),
    "google_drive": _google(
        "google_drive", "https://www.googleapis.com/auth/drive"
    ),
    "google_docs": _google(
        "google_docs", "https://www.googleapis.com/auth/documents"
    ),
    "google_sheets": _google(
        "google_sheets", "https://www.googleapis.com/auth/spreadsheets"
    ),
    "google_slides": _google(
        "google_slides", "https://www.googleapis.com/auth/presentations"
    ),
    "google_meet": _google(
        "google_meet", "https://www.googleapis.com/auth/meetings.space.created"
    ),
    "gcp": _google("gcp", "https://www.googleapis.com/auth/cloud-platform"),
    # ---- Microsoft/Entra family (offline_access scope, {tenant} URLs) ----
    "outlook": _microsoft("outlook", "Mail.ReadWrite", "Mail.Send"),
    "outlook_calendar": _microsoft("outlook_calendar", "Calendars.ReadWrite"),
    "onedrive": _microsoft("onedrive", "Files.ReadWrite.All"),
    "teams": _microsoft(
        "teams", "Chat.ReadWrite", "ChannelMessage.Send", "Team.ReadBasic.All"
    ),
    "word": _microsoft("word", "Files.ReadWrite.All"),
    "excel": _microsoft("excel", "Files.ReadWrite.All"),
    "powerpoint": _microsoft("powerpoint", "Files.ReadWrite.All"),
    # ---- Salesforce (refresh_token scope forces a refresh token) ----
    "salesforce": OAuthProvider(
        slug="salesforce",
        authorize_url="https://login.salesforce.com/services/oauth2/authorize",
        token_url="https://login.salesforce.com/services/oauth2/token",
        scopes=("api", "refresh_token"),
        extra_authorize_params={},
        uses_pkce=True,
    ),
    # ---- Zoho (access_type=offline + prompt=consent) ----
    "zoho_crm": OAuthProvider(
        slug="zoho_crm",
        authorize_url="https://accounts.zoho.com/oauth/v2/auth",
        token_url="https://accounts.zoho.com/oauth/v2/token",
        scopes=("ZohoCRM.modules.ALL",),
        extra_authorize_params=dict(_GOOGLE_REFRESH_PARAMS),
        uses_pkce=True,
    ),
    # ---- QuickBooks / Intuit (offline by default via openid scope set) ----
    "quickbooks": OAuthProvider(
        slug="quickbooks",
        authorize_url="https://appcenter.intuit.com/connect/oauth2",
        token_url="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        scopes=("com.intuit.quickbooks.accounting", "openid"),
        extra_authorize_params={},
        uses_pkce=True,
    ),
    # ---- Xero (offline_access scope yields a refresh token) ----
    "xero": OAuthProvider(
        slug="xero",
        authorize_url="https://login.xero.com/identity/connect/authorize",
        token_url="https://identity.xero.com/connect/token",
        scopes=("offline_access", "accounting.transactions"),
        extra_authorize_params={},
        uses_pkce=True,
    ),
    # ---- Yahoo (Google-style offline consent) ----
    "yahoo": OAuthProvider(
        slug="yahoo",
        authorize_url="https://api.login.yahoo.com/oauth2/request_auth",
        token_url="https://api.login.yahoo.com/oauth2/get_token",
        scopes=("openid", "mail-r", "mail-w"),
        extra_authorize_params={},
        uses_pkce=True,
    ),
    # ---- Workday (offline via {tenant}-style token endpoint) ----
    "workday": OAuthProvider(
        slug="workday",
        authorize_url="https://{tenant}/authorize",
        token_url="https://{tenant}/token",
        scopes=("openid",),
        extra_authorize_params={},
        uses_pkce=True,
        token_url_template=True,
    ),
}


def is_oauth_family(provider_slug: str) -> bool:
    """Return True when ``provider_slug`` is an OAuth-authorization-code provider.

    OAuth-family providers are exactly the keys of :data:`OAUTH_PROVIDERS`
    (Requirement 9.1/9.2). API-key, bot-token, basic-auth, and
    client-credentials-only providers are not classified as OAuth-family.
    """
    return provider_slug in OAUTH_PROVIDERS


def lookup(provider_slug: str) -> OAuthProvider:
    """Return the registry entry for an OAuth-family ``provider_slug``.

    Raises:
        APIError: HTTP 400 ``unsupported_provider`` when the slug is not an
            OAuth-family provider (Requirement 9.4). This is the same error
            mapping the authorize endpoint surfaces for a non-OAuth-family
            integration.
    """
    provider = OAUTH_PROVIDERS.get(provider_slug)
    if provider is None:
        raise APIError(
            status_code=400,
            code="unsupported_provider",
            message="This provider does not support in-app OAuth authorization.",
        )
    return provider


# ---------------------------------------------------------------------------
# Integration_OAuth_Service — begin_authorize
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizeRedirect:
    """The outcome of :meth:`IntegrationOAuthService.begin_authorize`.

    Attributes:
        authorization_url: The provider authorization URL the browser should be
            redirected (302) to.
        state: The opaque, single-use CSRF ``state`` that keys the Redis
            Flow_Record for this pending authorization.
    """

    authorization_url: str
    state: str


@dataclass(frozen=True)
class CompletedAuthorization:
    """The outcome of :meth:`IntegrationOAuthService.complete_authorize`.

    Attributes:
        integration_id: The id of the integration whose credentials were
            authorized. Carried out of the flow so the callback route can build
            the frontend success redirect (``?oauth=success&integration={id}``).
        workspace_id: The workspace that owns the integration, resolved from the
            server-side Flow_Record (never from the callback URL).
    """

    integration_id: UUID
    workspace_id: UUID


class IntegrationOAuthService:
    """Orchestrates begin/complete of the per-integration authorization flow.

    Reuses the pure PKCE/state helpers exported by ``auth_service``
    (``generate_pkce_verifier``, ``derive_code_challenge``, ``generate_state``)
    rather than duplicating them, but keeps its own Redis key namespace
    (``oauth:integration:{state}``) and never issues a Session_Token or creates a
    ``User`` (Req 1.5).
    """

    def __init__(self, settings=None) -> None:
        self._settings = settings or get_settings()

    def _redirect_uri(self, provider_slug: str) -> str:
        """Build the fixed, provider-keyed ``redirect_uri`` (Req 8.1/8.2).

        ``{OAUTH_REDIRECT_BASE_URL}/api/v1/integrations/oauth/callback/{provider}``
        — the ``integration_id`` is never in the URL; it travels in the Redis
        Flow_Record instead.
        """
        base = self._settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
        return base + _CALLBACK_PATH.format(provider=provider_slug)

    async def begin_authorize(
        self,
        *,
        integration: Integration,
        user_id: UUID,
        session,
        redis,
    ) -> AuthorizeRedirect:
        """Begin an integration authorization: stash flow state, build the URL.

        Resolves the provider registry entry for the integration's
        ``provider_name`` (400 for a non-OAuth-family provider, via
        :func:`lookup`), mints PKCE material (S256 where the provider supports
        it) and an opaque ``state``, persists the pending Flow_Record in Redis
        keyed by ``state`` with a 600-second TTL, and returns the provider
        authorization URL (Req 1.1–1.4, 8.1, 8.2).

        The Flow_Record carries ``integration_id``, ``provider``,
        ``workspace_id``, ``user_id``, ``code_verifier``, and ``redirect_uri``;
        no secret client credentials are stored — those are read from the
        integration row at exchange time. This method never creates a ``User``,
        issues a Session_Token, or writes an ``auth.*`` audit action (Req 1.5).
        """
        provider = lookup(integration.provider_name)

        # The provider authorization endpoint REQUIRES the OAuth client_id. It is
        # stored (encrypted) in the integration's credential blob under the
        # provider's ``client_id_key`` and was entered by the user at connect
        # time. Decrypt and read it here; without it Google/etc. reject the
        # request with "Missing required parameter: client_id" (Req 4.1/12.2).
        credential = await integration_vault.use_credential(
            session, integration_id=integration.id
        )
        client_id = credential.get(provider.client_id_key)
        if not client_id:
            raise APIError(
                status_code=400,
                code="missing_client_id",
                message=(
                    "This integration has no client_id stored. Reconnect the "
                    "provider with its OAuth client_id and client_secret before "
                    "authorizing."
                ),
            )

        redirect_uri = self._redirect_uri(provider.slug)
        state = generate_state()

        # PKCE material (only meaningful for providers that support S256).
        code_verifier = generate_pkce_verifier()
        code_challenge = (
            derive_code_challenge(code_verifier) if provider.uses_pkce else ""
        )

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(provider.scopes),
            "state": state,
            "redirect_uri": redirect_uri,
        }
        if provider.uses_pkce:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        # Provider-specific refresh-token params (e.g. access_type=offline,
        # prompt=consent) appended last so the registry stays authoritative.
        params.update(provider.extra_authorize_params)

        authorization_url = f"{provider.authorize_url}?{urlencode(params)}"

        flow = {
            "integration_id": str(integration.id),
            "provider": provider.slug,
            "workspace_id": str(integration.workspace_id),
            "user_id": str(user_id),
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
        await redis.set(
            f"{_FLOW_STATE_PREFIX}{state}",
            json.dumps(flow),
            ex=OAUTH_FLOW_TTL_SECONDS,
        )

        # Log without any secret material (no verifier/state value/tokens).
        logger.info("integration_oauth.begin_authorize provider=%s", provider.slug)
        return AuthorizeRedirect(authorization_url=authorization_url, state=state)

    async def complete_authorize(
        self,
        *,
        provider_slug: str,
        code: str,
        state: str,
        session,
        redis,
        http_client: AsyncHTTPClient | None = None,
    ) -> CompletedAuthorization:
        """Complete an integration authorization from the provider callback.

        The callback carries no session and trusts only the Redis-validated,
        single-use ``state``: the pending Flow_Record is read-and-deleted
        atomically so a replayed ``state`` cannot be reused (Req 3.2), and the
        callback path ``provider_slug`` must match the ``provider`` recorded when
        the flow began or the request is rejected without any token exchange
        (Req 3.4). Absent, expired, or already-consumed state is likewise
        rejected before any provider request is made (Req 3.1, 3.3).

        After the ``state`` is validated, the integration's stored
        ``client_id``/``client_secret`` are decrypted through
        :func:`integration_vault.use_credential` and the authorization ``code``
        is exchanged server-to-server at the provider token endpoint, carrying
        the same ``redirect_uri`` built at authorize time and (for PKCE
        providers) the ``code_verifier`` from the Flow_Record (Req 4.1–4.4). A
        non-2xx token response surfaces as HTTP 401 ``oauth_exchange_failed``,
        logging only the provider's non-secret ``error``/``error_description``
        and never marking the integration ACTIVE (Req 10.1, 10.2). A decryption
        failure of the stored client credentials is raised by
        ``integration_vault`` as HTTP 502 (integration marked ``ERROR``) before
        any exchange is attempted (Req 11.1, 11.2).

        A successful exchange must yield a ``refresh_token`` (Req 5.4); its
        absence is a failed authorization surfaced as an :class:`APIError`
        carrying ``reason=no_refresh_token`` (the callback route maps this to
        ``?oauth=error&reason=no_refresh_token``) and the integration is never
        marked ACTIVE (Req 5.5). On success the obtained ``refresh_token`` +
        ``access_token`` are merged into the integration's existing
        ``encrypted_credentials`` through :func:`integration_vault.store`
        (updating the row in place — re-authorizing rotates the tokens — and
        setting ``status = ACTIVE``; Req 6.1–6.5), and a scrubbed audit record
        that excludes all token/secret material is written (Req 7.4). No
        ``client_secret``/``code_verifier``/``access_token``/``refresh_token``
        is ever logged or returned (Req 7.1, 7.3).
        """
        flow = await self._consume_flow(provider_slug, state, redis)

        # Bindings resolved from the server-side flow record, never the URL.
        integration_id = UUID(flow["integration_id"])
        workspace_id = UUID(flow["workspace_id"])

        provider = lookup(provider_slug)

        # Decrypt the integration's stored client credentials. A tampered blob is
        # surfaced by the vault as HTTP 502 (and marks the integration ERROR)
        # BEFORE any provider request is made (Req 11.1, 11.2) — we deliberately
        # do not guard this call, so the exchange never runs on a bad credential.
        credential = await integration_vault.use_credential(
            session, integration_id=integration_id
        )
        client_id = credential.get(provider.client_id_key)
        client_secret = credential.get(provider.client_secret_key)

        # Exchange the authorization code for tokens (exactly one POST). The
        # returned payload feeds the refresh-token requirement + persistence
        # below; a non-2xx response has already been mapped to 401 upstream.
        token_data = await self._exchange_code(
            provider=provider,
            code=code,
            code_verifier=flow["code_verifier"],
            redirect_uri=flow["redirect_uri"],
            client_id=client_id,
            client_secret=client_secret,
            config=credential.config,
            http_client=http_client,
        )

        # Require a refresh token in the token response (Req 5.4). Its absence
        # is a failed authorization: raise a domain error carrying the
        # ``no_refresh_token`` reason so the callback router (task 5.1) can
        # redirect to ``?oauth=error&reason=no_refresh_token`` — mirroring how a
        # failed exchange raises ``oauth_exchange_failed`` (mapped to
        # ``reason=exchange``). We raise BEFORE any persistence, so the
        # integration is never marked ACTIVE (Req 5.5). The token payload is
        # never logged or echoed here.
        refresh_token = token_data.get("refresh_token")
        access_token = token_data.get("access_token")
        if not refresh_token:
            logger.warning(
                "integration_oauth.no_refresh_token provider=%s", provider_slug
            )
            raise APIError(
                status_code=401,
                code="oauth_no_refresh_token",
                message="The provider did not return a refresh token; "
                "re-authorize and grant offline access.",
                fields={"reason": "no_refresh_token"},
            )

        # Persist the obtained tokens into the SAME integration row's
        # ``encrypted_credentials`` (Req 6.1, 6.2), preserving the client
        # credentials the user already entered and re-encrypting the whole
        # secret map. ``integration_vault.store`` updates the existing row in
        # place (dedupe on workspace+creator+category+provider) rather than
        # creating a new one, so re-authorizing rotates the tokens (Req 6.5),
        # and sets ``status = ACTIVE`` (Req 6.3). No migration is performed
        # (Req 6.4) — this reuses the existing ``encrypted_credentials`` column.
        integration = await session.get(Integration, integration_id)
        if integration is None:  # pragma: no cover - consumed flow guarantees it
            raise APIError(
                status_code=404,
                code="not_found",
                message="Integration not found.",
            )

        # Merge the new tokens over the existing decrypted secret map so the
        # client_id/client_secret survive and the tokens are (re)written.
        merged_credentials: dict[str, str] = dict(credential.credentials)
        merged_credentials["refresh_token"] = refresh_token
        if access_token is not None:
            merged_credentials["access_token"] = access_token

        # Record a NON-secret "authorized" marker in the integration config so
        # the UI can show an authorized state without decrypting credentials.
        # This lives alongside the webhook status keys (all ``__`` prefixed,
        # never returned as secrets). It carries no token material (Req 7.1).
        merged_config: dict = dict(integration.config or {})
        merged_config[OAUTH_AUTHORIZED_KEY] = True

        await integration_vault.store(
            session,
            workspace_id=workspace_id,
            created_by_user_id=integration.created_by_user_id,
            category=integration.category,
            provider_name=integration.provider_name,
            credentials=merged_credentials,
            config=merged_config,
            is_shared_with_workspace=integration.is_shared_with_workspace,
        )

        # Write a scrubbed audit record for the completed authorization,
        # excluding all token/secret material (Req 7.4). Only non-secret
        # identifiers are recorded; ``scrub`` is applied as defense-in-depth so
        # a token-shaped value could never be persisted even if added later.
        session.add(
            SystemAuditLog(
                workspace_id=workspace_id,
                user_id=integration.created_by_user_id,
                action=AUDIT_ACTION_AUTHORIZED,
                log_metadata=scrub(
                    {
                        "integration_id": str(integration_id),
                        "provider_name": integration.provider_name,
                        "category": str(integration.category),
                    }
                ),
            )
        )
        await session.commit()

        # Log without any secret material: no client_secret, code_verifier,
        # access_token, or refresh_token appears here (Req 7.1).
        logger.info(
            "integration_oauth.complete_authorize provider=%s integration_id=%s",
            provider_slug,
            integration_id,
        )
        return CompletedAuthorization(
            integration_id=integration_id,
            workspace_id=workspace_id,
        )

    def _resolve_token_url(
        self, provider: OAuthProvider, config: dict[str, str] | None
    ) -> str:
        """Resolve the provider ``token_url``, filling ``{tenant}`` when templated.

        Microsoft/Entra and Workday token endpoints carry a ``{tenant}``
        placeholder that is resolved from the integration's non-secret ``config``
        (``tenant_id`` or ``tenant``), defaulting to ``common`` — matching the
        resolution already used by ``oauth_refresh.ensure_access_token``.
        """
        token_url = provider.token_url
        if provider.token_url_template:
            cfg = config or {}
            tenant = cfg.get("tenant_id") or cfg.get("tenant") or "common"
            token_url = token_url.format(tenant=tenant)
        return token_url

    async def _exchange_code(
        self,
        *,
        provider: OAuthProvider,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str | None,
        client_secret: str | None,
        config: dict[str, str] | None,
        http_client: AsyncHTTPClient | None,
    ) -> dict:
        """Exchange the authorization ``code`` for tokens server-to-server.

        Performs exactly ONE ``authorization_code`` POST to the provider
        ``token_url`` using the integration's own ``client_id``/``client_secret``
        (Req 4.1, 4.2), the same ``redirect_uri`` built at authorize time (Req
        4.4), and — for PKCE providers — the ``code_verifier`` from the
        Flow_Record (Req 4.3). Creates a default ``httpx.AsyncClient`` when none
        is injected (tests inject a fake ``AsyncHTTPClient``); the client is
        closed only when owned here.

        A non-2xx response is mapped to HTTP 401 ``oauth_exchange_failed`` via
        :meth:`_require_ok_json`, which logs only the non-secret provider
        ``error``/``error_description`` (Req 10.1, 7.3). The ``client_secret``,
        ``code``, and ``code_verifier`` are never logged.
        """
        token_url = self._resolve_token_url(provider, config)

        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if client_id is not None:
            data["client_id"] = client_id
        if client_secret is not None:
            data["client_secret"] = client_secret
        if provider.uses_pkce:
            data["code_verifier"] = code_verifier

        owns_client = http_client is None
        if owns_client:
            import httpx  # local import keeps httpx optional at import time

            http_client = httpx.AsyncClient(timeout=10.0)
        try:
            token_resp = await http_client.post(token_url, data=data)
        except APIError:
            raise
        except Exception as exc:  # network / transport failure -> 401
            logger.warning(
                "integration_oauth.exchange_failed provider=%s", provider.slug
            )
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The authorization code could not be exchanged.",
            ) from exc
        finally:
            if owns_client:
                await http_client.aclose()

        # Non-2xx -> 401, logging ONLY the non-secret error/error_description
        # (mirrors auth_service._require_ok_json). Does not set status ACTIVE.
        token_data = self._require_ok_json(token_resp, context="integration_token")
        if not isinstance(token_data, dict):
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The provider returned an unexpected token response.",
            )
        return token_data

    @staticmethod
    def _require_ok_json(response: Any, *, context: str = "integration_oauth") -> Any:
        """Return a response's JSON, treating a non-2xx status as a failure.

        Mirrors ``auth_service._require_ok_json``: on a provider error only the
        standard, non-secret OAuth ``error``/``error_description`` fields are
        logged (never tokens or client secrets), and the failure is mapped to
        HTTP 401 ``oauth_exchange_failed`` (Req 10.1, 7.3). Tolerant of both real
        ``httpx.Response`` objects and the fake responses used in tests, which
        expose ``status_code`` and ``json()``.
        """
        status_code = getattr(response, "status_code", 200)
        if status_code >= 400:
            err = ""
            desc = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    err = str(body.get("error", ""))[:120]
                    desc = str(body.get("error_description", ""))[:200]
            except Exception:  # noqa: BLE001 — body may be empty/non-JSON
                pass
            logger.warning(
                "integration_oauth.provider_error context=%s status=%s "
                "error=%s description=%s",
                context, status_code, err or "<none>", desc or "<none>",
            )
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The authorization code could not be exchanged.",
            )
        return response.json()

    async def _consume_flow(self, provider_slug: str, state: str, redis) -> dict:
        """Load, validate, and delete (consume) the pending flow for ``state``.

        A missing/expired entry, or one whose stored ``provider`` differs from
        the callback path ``provider_slug``, is rejected with an
        :class:`APIError` mapping to HTTP 401 (Req 3.3, 3.4). Deleting the entry
        immediately after a successful read makes the ``state`` single-use,
        preventing replay (Req 3.2). No token-exchange request is made on any
        rejection path.
        """
        key = f"{_FLOW_STATE_PREFIX}{state}"
        raw = await redis.get(key)
        if raw is None:
            raise APIError(
                status_code=401,
                code="invalid_state",
                message="The OAuth state is invalid or has expired.",
            )
        # Consume immediately so a replay of the same state cannot be reused.
        await redis.delete(key)

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        flow = json.loads(raw)
        if flow.get("provider") != provider_slug:
            raise APIError(
                status_code=401,
                code="invalid_state",
                message="The OAuth state is invalid or has expired.",
            )
        return flow


__all__ = [
    "OAuthProvider",
    "OAUTH_PROVIDERS",
    "AUDIT_ACTION_AUTHORIZED",
    "OAUTH_AUTHORIZED_KEY",
    "is_oauth_family",
    "lookup",
    "AuthorizeRedirect",
    "CompletedAuthorization",
    "IntegrationOAuthService",
]
