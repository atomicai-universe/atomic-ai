"""Agent tools — generic authenticated provider API calls for the agent loop.

Turns each resolved, connected integration into a real capability the Strands
agent can use, without hand-writing 74 SDKs: a single, well-tested, secure
"call this provider's API" tool per integration. Given a provider and its
decrypted credential map (from the Integration_Vault), it builds a correctly
authenticated ``httpx`` request per the provider's :class:`ApiProfile`.

Security properties:
- Credentials are resolved per call from the encrypted vault and applied to the
  request auth; they are never logged, echoed, or placed in tool output.
- High-impact calls (writes: POST/PUT/PATCH/DELETE) pass through the injected
  ``before_tool_call`` approval hook first; if the hook returns "gate", the call
  is not made and the agent is told a human must approve it (Req 10.x).
- Requests use a bounded timeout and never follow redirects to a different host
  without the same auth.

The tools are built as plain callables the run-loop adapter registers with the
Strands ``Agent``. Each tool is bound to one integration id + provider so the
agent picks the right connected account.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.services import provider_api
from app.services.provider_api import AuthStyle

# Methods considered high-impact (state-changing) — gated by approval.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_REQUEST_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class BoundToolSpec:
    """One provider-bound tool the agent can call.

    Attributes:
        integration_id: The backing integration.
        provider_name: Provider slug (selects the ApiProfile).
        display: Human tool name shown to the agent (e.g. ``gmail_api``).
    """

    integration_id: uuid.UUID
    provider_name: str
    display: str


def _apply_auth(
    profile: provider_api.ApiProfile,
    credentials: dict[str, str],
    config: dict[str, str] | None,
    headers: dict[str, str],
    params: dict[str, str],
) -> httpx.Auth | None:
    """Apply the provider's auth style to headers/params; return httpx.Auth if basic.

    Handles the special-case usernames encoded in ``basic_user_key`` (e.g.
    Mailgun's literal ``api`` user, Freshdesk/BambooHR's ``X`` password, Zendesk's
    ``email/token`` username form). Returns an ``httpx.BasicAuth`` for BASIC
    styles (so httpx sets the header), otherwise ``None`` and mutates
    headers/params in place.
    """
    values: dict[str, str] = {**(config or {}), **credentials}
    token = credentials.get(profile.cred_key, "")

    if profile.auth is AuthStyle.BEARER:
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif profile.auth is AuthStyle.TOKEN_HEADER:
        if token:
            headers["Authorization"] = f"token {token}"
    elif profile.auth is AuthStyle.HEADER:
        if token:
            headers[profile.header_name or "Authorization"] = f"{profile.header_prefix}{token}"
    elif profile.auth is AuthStyle.QUERY:
        if token:
            params[profile.query_param or "token"] = token
    elif profile.auth is AuthStyle.BASIC:
        user_key = profile.basic_user_key
        password = token
        if user_key == "__literal_api":
            username = "api"
        elif user_key == "__literal_x":
            username = "X"
        elif user_key == "__literal_empty":
            username = ""
        elif user_key == "__pat_user":
            username = ""  # Azure DevOps: empty user, PAT as password
        elif user_key == "__zendesk_email_token":
            # Zendesk email/token form: username is "<email>/token", password is the token.
            username = f"{values.get('email', '')}/token"
        else:
            username = values.get(user_key, "")
        return httpx.BasicAuth(username, password)
    # NONE / Telegram / AWS handled by the caller building a full URL.
    return None


async def call_provider_api(
    *,
    provider_name: str,
    credentials: dict[str, str],
    config: dict[str, str] | None,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
) -> dict[str, Any]:
    """Make one authenticated request to a provider's API.

    ``path`` is appended to the resolved base URL, or used as-is if it is an
    absolute URL (for providers with tenant-specific hosts). Returns a
    JSON-serializable dict describing the response (status + parsed body/text),
    never raising for non-2xx — the status is reported so the agent can react.
    Credentials never appear in the returned dict.
    """
    profile = provider_api.profile_for(provider_name)
    values = {**(config or {}), **credentials}

    # Telegram embeds the bot token in the URL path.
    if provider_name == "telegram":
        base = f"https://api.telegram.org/bot{credentials.get('bot_token','')}"
        url = base + (path if path.startswith("/") else "/" + path)
    else:
        base = provider_api.resolve_base_url(profile, values)
        if path.lower().startswith("http"):
            url = path
        elif base:
            url = base.rstrip("/") + "/" + path.lstrip("/")
        else:
            return {"error": "no_base_url", "message": f"No API base URL configured for {provider_name}; pass a full URL."}

    headers: dict[str, str] = {"Accept": "application/json"}
    params: dict[str, str] = {}
    auth = _apply_auth(profile, credentials, config, headers, params)
    if query:
        params.update({k: str(v) for k, v in query.items()})

    json_body = None
    content = None
    if body is not None:
        if isinstance(body, (dict, list)):
            json_body = body
        else:
            content = str(body)

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=headers,
                params=params or None,
                json=json_body,
                content=content,
                auth=auth,
            )
        ctype = resp.headers.get("content-type", "")
        parsed: Any
        if "application/json" in ctype:
            try:
                parsed = resp.json()
            except Exception:  # noqa: BLE001
                parsed = resp.text
        else:
            parsed = resp.text[:10000]
        return {"status_code": resp.status_code, "ok": resp.is_success, "body": parsed}
    except httpx.HTTPError as exc:
        return {"error": "request_failed", "message": str(exc)}


def is_write(method: str) -> bool:
    """Whether an HTTP method is state-changing (gated by approval)."""
    return method.upper() in _WRITE_METHODS


__all__ = ["BoundToolSpec", "call_provider_api", "is_write"]
