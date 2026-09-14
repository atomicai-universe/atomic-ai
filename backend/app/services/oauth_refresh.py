"""OAuth token refresh — mint short-lived access tokens from stored refresh tokens.

Several providers (Google family, Microsoft/Entra family, and other standard
OAuth2 apps) authenticate API calls with a short-lived ACCESS token, but what we
store long-term is the CLIENT credentials + a REFRESH token. Before the agent
calls such a provider, we exchange the refresh token for a fresh access token at
the provider's token endpoint.

This module is best-effort and provider-family aware: given a provider slug and
its decrypted credential map, if the map already has a usable ``access_token`` it
is returned as-is; otherwise, for a known OAuth family with a ``refresh_token`` +
``client_id``/``client_secret``, it performs the standard
``grant_type=refresh_token`` exchange and returns an updated credential map with
a freshly minted ``access_token``. Failures leave the map unchanged (the call
will then surface a 401 the agent can report).

Secrets are used only to perform the exchange and never logged.
"""

from __future__ import annotations

import logging

import httpx

# Provider -> OAuth2 token endpoint for the refresh_token grant.
logger = logging.getLogger("atomic_ai.oauth_refresh")

_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_MS_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

_GOOGLE_PROVIDERS = {
    "gmail", "google_docs", "google_sheets", "google_slides", "google_drive",
    "google_calendar", "google_meet", "gcp",
}
_MS_PROVIDERS = {
    "outlook", "outlook_calendar", "onedrive", "teams", "word", "excel",
    "powerpoint",
}


async def ensure_access_token(
    provider: str,
    credentials: dict[str, str],
    config: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a credential map guaranteed to have a usable ``access_token`` when possible.

    For a known OAuth family (Google/Microsoft) that has a ``refresh_token`` plus
    ``client_id``/``client_secret``, ALWAYS exchange the refresh token for a
    fresh ``access_token`` — provider access tokens are short-lived (~1 hour) and
    a stored one is almost always stale by the next agent run, which would 401.
    When we cannot refresh (no refresh token, or an unknown family), a
    pre-existing ``access_token`` is returned unchanged. On any refresh failure,
    the original map is returned unchanged (the call then surfaces a 401 the
    agent can report).
    """
    creds = dict(credentials)

    refresh_token = creds.get("refresh_token")
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")

    # If we can mint a fresh access token for a known OAuth family, ALWAYS do so:
    # provider access tokens are short-lived (~1h) and a stored one is very
    # likely stale by the next agent run. Only fall back to a pre-existing
    # access_token when we cannot refresh (no refresh_token / unknown family).
    _can_refresh = bool(
        refresh_token
        and client_id
        and client_secret
        and (provider in _GOOGLE_PROVIDERS or provider in _MS_PROVIDERS)
    )
    if creds.get("access_token") and not _can_refresh:
        return creds

    if not refresh_token:
        logger.warning(
            "oauth.no_token provider=%s has_client_id=%s has_client_secret=%s "
            "has_refresh_token=False -> API calls will 401; the integration must "
            "be reconnected via the OAuth consent flow to obtain a refresh token.",
            provider, bool(client_id), bool(client_secret),
        )
        return creds

    try:
        if provider in _GOOGLE_PROVIDERS and client_id and client_secret:
            token = await _refresh(
                _GOOGLE_TOKEN,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        elif provider in _MS_PROVIDERS and client_id and client_secret:
            tenant = (config or {}).get("tenant_id") or creds.get("tenant_id") or "common"
            token = await _refresh(
                _MS_TOKEN.format(tenant=tenant),
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
        else:
            return creds
    except Exception:  # noqa: BLE001 - best-effort; leave creds unchanged on failure
        return creds

    if token:
        creds["access_token"] = token
    else:
        logger.warning(
            "oauth.refresh_failed provider=%s: the refresh_token grant did not "
            "return an access token (token may be revoked/expired). Reconnect the "
            "integration.", provider,
        )
    return creds


async def _refresh(token_url: str, data: dict[str, str]) -> str | None:
    """POST the refresh grant and return the new access_token, or None."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(token_url, data=data)
    if not resp.is_success:
        return None
    payload = resp.json()
    return payload.get("access_token")


__all__ = ["ensure_access_token"]
