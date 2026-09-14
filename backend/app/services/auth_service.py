"""Auth_Service — OAuth 2.0 Authorization Code + PKCE login for Google/GitHub.

This module implements the two halves of the interactive login flow described
in the design's "Auth_Service" section:

- :func:`AuthService.begin_login` mints the PKCE ``code_verifier``/``code_challenge``
  (S256), a CSRF ``state``, and a ``nonce``; stashes the flow state server-side
  in Redis under a short TTL keyed by ``state``; and builds the provider's
  authorization URL (Req 1.1).
- :func:`AuthService.complete_login` validates the returned ``state`` against
  the stored flow (rejecting a mismatch/expiry, Req 1.4), consumes it to prevent
  replay, exchanges the authorization ``code`` for provider tokens over a
  server-to-server request (Req 1.2), reads the provider's email/name, and then
  finds-or-creates the ``User`` by ``(email, auth_provider)`` (Req 1.3) — issuing
  a Session_Token on success. A provider error/invalid code rejects the login and
  creates no user (Req 1.5), an email already bound to a *different* provider is
  an account conflict (Req 1.7), and ``auth_provider`` is constrained to
  ``google``/``github`` throughout (Req 1.6).

Security notes:

- Secrets (``code_verifier``, provider ``client_secret``, access tokens) are
  never logged. When context needs logging, it is passed through
  :func:`app.core.scrubbing.scrub` first.
- The security-critical *decision* logic — PKCE challenge derivation, state
  generation, and the find-or-create/conflict resolution — is factored into
  small pure functions (:func:`derive_code_challenge`, :func:`generate_state`,
  :func:`generate_pkce_verifier`, :func:`resolve_user_identity`) so task 5.2 can
  property-test them without any I/O.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import APIError
from app.core.security import issue_session_token, persist_session
from app.db.models import AuthProvider, User

logger = logging.getLogger("atomic_ai.auth")

# TTL (seconds) for a pending OAuth flow held in Redis. The authorization
# redirect + user consent + callback must complete within this window; a stale
# flow simply expires and its ``state`` no longer validates (Req 1.4).
OAUTH_FLOW_TTL_SECONDS: int = 600

# Redis key prefix for pending OAuth flows, keyed by the opaque ``state``.
_OAUTH_STATE_PREFIX = "oauth:state:"

# Bytes of entropy for the PKCE ``code_verifier`` seed. ``token_urlsafe(64)``
# yields ~86 URL-safe characters, comfortably within RFC 7636's 43–128 range.
_VERIFIER_ENTROPY_BYTES: int = 64

# --- Provider endpoints ------------------------------------------------------
_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GOOGLE_SCOPE = "openid email profile"

_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"
_GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"
_GITHUB_SCOPE = "read:user user:email"

ProviderName = Literal["google", "github"]


# ---------------------------------------------------------------------------
# HTTP client protocol (so tests can inject a fake)
# ---------------------------------------------------------------------------


class AsyncHTTPClient(Protocol):
    """Minimal async HTTP surface used for the server-to-server exchange.

    ``httpx.AsyncClient`` satisfies this structurally; tests inject a small
    fake exposing the same two coroutine methods and canned responses.
    """

    async def post(self, url: str, *args: Any, **kwargs: Any) -> Any: ...

    async def get(self, url: str, *args: Any, **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Pure, I/O-free helpers (property-testable — task 5.2)
# ---------------------------------------------------------------------------


def generate_pkce_verifier() -> str:
    """Return a fresh, high-entropy PKCE ``code_verifier`` (RFC 7636)."""
    return secrets.token_urlsafe(_VERIFIER_ENTROPY_BYTES)


def generate_state() -> str:
    """Return a fresh, opaque CSRF ``state`` value for an OAuth flow."""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    """Return a fresh ``nonce`` for OpenID Connect replay protection."""
    return secrets.token_urlsafe(32)


def derive_code_challenge(code_verifier: str) -> str:
    """Derive the S256 PKCE ``code_challenge`` from a ``code_verifier``.

    The challenge is the base64url (no padding) encoding of the SHA-256 digest
    of the ASCII verifier, exactly as RFC 7636 §4.2 specifies for the ``S256``
    method. Pure and deterministic: the same verifier always yields the same
    challenge.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def resolve_user_identity(
    existing_provider_for_email: AuthProvider | None,
    requested_provider: AuthProvider,
) -> str:
    """Decide how to reconcile a login against any existing user for the email.

    This is the pure core of the find-or-create rule (Req 1.3) and the
    cross-provider conflict rule (Req 1.7). Given the provider currently bound
    to the login's email (or ``None`` when no user exists for it) and the
    provider the user is logging in with, it returns one of three decisions:

    - ``"create"`` — no user exists for the email; provision a new one.
    - ``"return_existing"`` — a user exists for the email under the *same*
      provider; return it (idempotent login).
    - ``"conflict"`` — a user exists for the email under a *different* provider;
      reject with an account-conflict error and neither create nor merge.
    """
    if existing_provider_for_email is None:
        return "create"
    if existing_provider_for_email == requested_provider:
        return "return_existing"
    return "conflict"


def build_authorization_url(
    provider: AuthProvider,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    nonce: str,
) -> str:
    """Build the provider's OAuth authorization URL (pure; Req 1.1).

    Google receives the full PKCE + OIDC parameter set (``code_challenge`` with
    ``code_challenge_method=S256`` and a ``nonce``); GitHub — which does not
    implement PKCE on its authorize endpoint — receives the code-flow subset
    with its own scopes. Query parameters are URL-encoded via ``urlencode``.
    """
    if provider is AuthProvider.GOOGLE:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": _GOOGLE_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        return f"{_GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
    if provider is AuthProvider.GITHUB:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": _GITHUB_SCOPE,
            "state": state,
        }
        return f"{_GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    raise APIError(  # pragma: no cover - guarded by _coerce_provider upstream
        status_code=400, code="unsupported_provider", message="Unsupported OAuth provider."
    )


def _coerce_provider(provider: str | AuthProvider) -> AuthProvider:
    """Normalize/validate a provider argument to :class:`AuthProvider`.

    Constrains the provider to exactly ``google``/``github`` (Req 1.6); any
    other value is rejected with a 400 authentication error before any I/O.
    """
    if isinstance(provider, AuthProvider):
        return provider
    try:
        return AuthProvider(provider)
    except ValueError:
        raise APIError(
            status_code=400,
            code="unsupported_provider",
            message="Unsupported OAuth provider.",
        ) from None


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginRedirect:
    """The outcome of :func:`AuthService.begin_login`."""

    authorization_url: str
    state: str


@dataclass(frozen=True)
class _ProviderProfile:
    """Normalized identity read from a provider after token exchange."""

    email: str
    name: str


# ---------------------------------------------------------------------------
# Auth_Service
# ---------------------------------------------------------------------------


class AuthService:
    """Coordinates the OAuth login flow and session issuance.

    The service holds only configuration; all request-scoped resources (the DB
    session, the Redis client, and an optional injected HTTP client) are passed
    into the individual methods so the same instance is safe to reuse.
    """

    def __init__(self, settings=None) -> None:
        self._settings = settings or get_settings()

    # -- Per-provider client credentials ----------------------------------

    def _client_id(self, provider: AuthProvider) -> str:
        if provider is AuthProvider.GOOGLE:
            return self._settings.GOOGLE_OAUTH_CLIENT_ID
        return self._settings.GITHUB_OAUTH_CLIENT_ID

    def _client_secret(self, provider: AuthProvider) -> str:
        if provider is AuthProvider.GOOGLE:
            return self._settings.GOOGLE_OAUTH_CLIENT_SECRET.get_secret_value()
        return self._settings.GITHUB_OAUTH_CLIENT_SECRET.get_secret_value()

    # -- begin_login -------------------------------------------------------

    async def begin_login(
        self,
        provider: str | AuthProvider,
        redirect_uri: str,
        *,
        redis,
    ) -> LoginRedirect:
        """Start an OAuth login: stash flow state and build the redirect URL.

        Generates PKCE material, a ``state``, and a ``nonce``; persists the flow
        (``code_verifier``, ``nonce``, ``provider``, ``created_at``,
        ``redirect_uri``) in Redis keyed by ``state`` with a short TTL; and
        returns the provider authorization URL plus the ``state`` (Req 1.1).
        """
        resolved = _coerce_provider(provider)

        code_verifier = generate_pkce_verifier()
        code_challenge = derive_code_challenge(code_verifier)
        state = generate_state()
        nonce = generate_nonce()

        flow = {
            "code_verifier": code_verifier,
            "nonce": nonce,
            "provider": resolved.value,
            "redirect_uri": redirect_uri,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await redis.set(
            f"{_OAUTH_STATE_PREFIX}{state}",
            json.dumps(flow),
            ex=OAUTH_FLOW_TTL_SECONDS,
        )

        authorization_url = build_authorization_url(
            resolved,
            client_id=self._client_id(resolved),
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            nonce=nonce,
        )
        # Log without any secret material (no verifier/nonce/state value).
        logger.info("oauth.begin_login provider=%s", resolved.value)
        return LoginRedirect(authorization_url=authorization_url, state=state)

    # -- complete_login ----------------------------------------------------

    async def complete_login(
        self,
        provider: str | AuthProvider,
        code: str,
        state: str,
        *,
        session: AsyncSession,
        redis,
        http_client: AsyncHTTPClient | None = None,
    ) -> tuple[User, str, datetime]:
        """Finish an OAuth login and issue a session.

        Returns ``(user, raw_token, expires_at)``. Rejects a missing/expired or
        mismatched ``state`` (Req 1.4), a provider error/invalid code (Req 1.5),
        and a cross-provider email conflict (Req 1.7); otherwise finds-or-creates
        the user by ``(email, provider)`` (Req 1.3) and issues a session.
        """
        resolved = _coerce_provider(provider)

        flow = await self._consume_flow(state, resolved, redis)

        profile = await self._exchange_and_profile(
            resolved,
            code=code,
            code_verifier=flow["code_verifier"],
            redirect_uri=flow.get("redirect_uri", ""),
            http_client=http_client,
        )

        user = await self._find_or_create_user(session, resolved, profile)

        now = datetime.now(timezone.utc)
        raw_token, expires_at = issue_session_token(user.id, now)
        await persist_session(
            session, user_id=user.id, raw_token=raw_token, expires_at=expires_at
        )
        logger.info("oauth.complete_login provider=%s user_id=%s", resolved.value, user.id)
        return user, raw_token, expires_at

    # -- internal steps ----------------------------------------------------

    async def _consume_flow(self, state: str, provider: AuthProvider, redis) -> dict:
        """Load, validate, and delete (consume) the pending flow for ``state``.

        A missing/expired entry, or one recorded for a different provider, is a
        state mismatch and rejects the login (Req 1.4). Deleting the entry after
        a successful read makes the ``state`` single-use, preventing replay.
        """
        key = f"{_OAUTH_STATE_PREFIX}{state}"
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
        if flow.get("provider") != provider.value:
            raise APIError(
                status_code=401,
                code="invalid_state",
                message="The OAuth state is invalid or has expired.",
            )
        return flow

    async def _exchange_and_profile(
        self,
        provider: AuthProvider,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        http_client: AsyncHTTPClient | None,
    ) -> _ProviderProfile:
        """Exchange the code server-to-server and read the provider profile.

        Creates a default ``httpx.AsyncClient`` when none is injected. Any
        provider error or invalid code surfaces as an authentication error and
        no user is created (Req 1.5).
        """
        owns_client = http_client is None
        if owns_client:
            import httpx  # local import keeps httpx optional at import time

            http_client = httpx.AsyncClient(timeout=10.0)
        try:
            if provider is AuthProvider.GOOGLE:
                return await self._google_profile(
                    http_client, code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
                )
            return await self._github_profile(
                http_client, code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
            )
        except APIError:
            raise
        except Exception as exc:  # network / parsing failures -> auth error
            logger.warning("oauth.exchange_failed provider=%s", provider.value)
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The authorization code could not be exchanged.",
            ) from exc
        finally:
            if owns_client:
                await http_client.aclose()

    async def _google_profile(
        self,
        client: AsyncHTTPClient,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> _ProviderProfile:
        token_resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id(AuthProvider.GOOGLE),
                "client_secret": self._client_secret(AuthProvider.GOOGLE),
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        token_data = self._require_ok_json(token_resp, context="google_token")
        access_token = token_data.get("access_token")
        if not access_token:
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The provider did not return an access token.",
            )
        userinfo_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info = self._require_ok_json(userinfo_resp)
        email = info.get("email")
        if not email:
            raise APIError(
                status_code=401,
                code="oauth_no_email",
                message="The provider account has no usable email.",
            )
        name = info.get("name") or info.get("given_name") or email
        return _ProviderProfile(email=email, name=name)

    async def _github_profile(
        self,
        client: AsyncHTTPClient,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> _ProviderProfile:
        token_resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": self._client_id(AuthProvider.GITHUB),
                "client_secret": self._client_secret(AuthProvider.GITHUB),
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
        token_data = self._require_ok_json(token_resp, context="github_token")
        access_token = token_data.get("access_token")
        if not access_token:
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The provider did not return an access token.",
            )
        auth_header = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        user_resp = await client.get(_GITHUB_USER_URL, headers=auth_header)
        user_info = self._require_ok_json(user_resp)
        name = user_info.get("name") or user_info.get("login")

        email = user_info.get("email")
        if not email:
            emails_resp = await client.get(_GITHUB_USER_EMAILS_URL, headers=auth_header)
            emails = self._require_ok_json(emails_resp)
            email = _primary_verified_email(emails)
        if not email:
            raise APIError(
                status_code=401,
                code="oauth_no_email",
                message="The provider account has no usable verified email.",
            )
        return _ProviderProfile(email=email, name=name or email)

    @staticmethod
    def _require_ok_json(response: Any, *, context: str = "oauth") -> dict:
        """Return a response's JSON, treating a non-2xx status as a failure.

        On a provider error we log the provider's *non-secret* error fields
        (``error`` / ``error_description``) so failures like
        ``redirect_uri_mismatch`` or ``invalid_grant`` are diagnosable from the
        logs instead of being hidden behind a generic message. Access tokens or
        other secret fields are never logged. Kept tolerant of both real
        ``httpx.Response`` objects and the fakes used in tests, which expose
        ``status_code`` and ``json()``.
        """
        status_code = getattr(response, "status_code", 200)
        if status_code >= 400:
            # Parse the provider error body defensively; only surface the
            # standard OAuth error identifiers (never tokens/secrets).
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
                "oauth.provider_error context=%s status=%s error=%s description=%s",
                context, status_code, err or "<none>", desc or "<none>",
            )
            # Map the most common, actionable Google/GitHub errors to a clearer
            # message so the user knows exactly what to fix.
            hint = {
                "redirect_uri_mismatch": (
                    "The redirect URI does not match one registered with the "
                    "provider. Register the exact callback URL "
                    "(OAUTH_REDIRECT_BASE_URL + /auth/callback/{provider}) in the "
                    "provider's OAuth client settings."
                ),
                "invalid_grant": (
                    "The authorization code was already used, expired, or the "
                    "PKCE/redirect values did not match. Start the login again."
                ),
                "invalid_client": (
                    "The OAuth client id/secret is wrong or missing. Check the "
                    "provider client credentials in the environment."
                ),
            }.get(err)
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message=hint or "The authorization code could not be exchanged.",
            )
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The provider returned an unexpected response.",
            )
        return data

    async def _find_or_create_user(
        self, session: AsyncSession, provider: AuthProvider, profile: _ProviderProfile
    ) -> User:
        """Find-or-create the user by ``(email, provider)`` (Req 1.3, 1.7).

        Looks up any existing user for the email, resolves the decision with the
        pure :func:`resolve_user_identity`, and acts on it: returning an existing
        same-provider user, raising a 409 ``account_conflict`` for a different
        provider (creating/merging nothing), or provisioning a new non-superadmin
        user. A same-provider user who has been banned by a super admin is
        rejected with a 403 ``account_banned`` so a banned account cannot
        authenticate (Req 12.4).
        """
        existing = (
            await session.execute(select(User).where(User.email == profile.email))
        ).scalar_one_or_none()

        decision = resolve_user_identity(
            existing.auth_provider if existing is not None else None, provider
        )
        if decision == "return_existing":
            # A super-admin ban blocks *future* authentication (Req 12.4): a
            # banned existing user is rejected here so no new session is issued,
            # complementing the session revocation performed at ban time.
            if getattr(existing, "is_banned", False):
                raise APIError(
                    status_code=403,
                    code="account_banned",
                    message="This account has been suspended.",
                )
            return existing
        if decision == "conflict":
            raise APIError(
                status_code=409,
                code="account_conflict",
                message="This email is already registered with a different provider.",
            )
        user = User(
            email=profile.email,
            name=profile.name,
            auth_provider=provider,
            is_superadmin=False,
        )
        session.add(user)
        await session.flush()
        return user


def _primary_verified_email(emails: Any) -> str | None:
    """Pick GitHub's primary verified email from the ``/user/emails`` payload.

    Prefers an entry flagged both ``primary`` and ``verified``; falls back to
    any verified address; returns ``None`` when none qualify.
    """
    if not isinstance(emails, list):
        return None
    verified = [e for e in emails if isinstance(e, dict) and e.get("verified")]
    for entry in verified:
        if entry.get("primary"):
            return entry.get("email")
    if verified:
        return verified[0].get("email")
    return None


__all__ = [
    "AuthService",
    "LoginRedirect",
    "OAUTH_FLOW_TTL_SECONDS",
    "AsyncHTTPClient",
    "generate_pkce_verifier",
    "generate_state",
    "generate_nonce",
    "derive_code_challenge",
    "resolve_user_identity",
    "build_authorization_url",
]
