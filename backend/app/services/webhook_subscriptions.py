"""Webhook subscription manager — register/renew provider triggers per integration.

When an integration is connected (or renewed), this service:

1. Generates a per-integration webhook secret (stored in the integration
   ``config`` under ``__webhook_secret``) so the public webhook URL is
   unguessable and verifiable.
2. Registers the provider's trigger:
   - PUBSUB (Gmail): calls ``users.watch`` with the configured Pub/Sub topic so
     Gmail publishes change notifications; Google's push subscription delivers
     them to our webhook.
   - PUSH: many providers require creating a webhook via their API; where an API
     call is supported we make it, otherwise we return the webhook URL for the
     user to paste into the provider's dashboard.
   - POLL: nothing to register; the scheduler handles it.

The webhook URL delivered to providers is:

    {OAUTH_REDIRECT_BASE_URL}/api/v1/webhooks/{provider}/{integration_id}/{secret}

Registration is best-effort and never blocks connecting an integration; failures
are recorded but the credential is still stored.
"""

from __future__ import annotations

import logging
import secrets

from app.config import get_settings
from app.services import agent_tools, provider_webhooks, webhook_registration
from app.services.provider_webhooks import TriggerKind

logger = logging.getLogger("atomic_ai.webhook_subscriptions")

WEBHOOK_SECRET_KEY = "__webhook_secret"
# Compact webhook-status summary persisted in config for the UI (no secret).
WEBHOOK_STATUS_KEY = "__webhook_status"        # auto|partial|manual|poll
WEBHOOK_REGISTERED_KEY = "__webhook_registered"  # bool
WEBHOOK_NOTE_KEY = "__webhook_note"            # human next-step note


def webhook_url(provider: str, integration_id: str, secret: str) -> str:
    """Build the public webhook URL a provider should call for this integration.

    Prefers WEBHOOK_PUBLIC_BASE_URL (a public tunnel like ngrok, used ONLY for
    inbound webhooks) so a local dev box can receive provider push / Gmail
    Pub/Sub callbacks without changing the OAuth redirect host. Falls back to
    OAUTH_REDIRECT_BASE_URL when the webhook base is unset (single-host
    deployments are unchanged).
    """
    settings = get_settings()
    base = (
        getattr(settings, "WEBHOOK_PUBLIC_BASE_URL", "") or settings.OAUTH_REDIRECT_BASE_URL
    ).rstrip("/")
    return f"{base}/api/v1/webhooks/{provider}/{integration_id}/{secret}"


def ensure_webhook_secret(config: dict) -> tuple[dict, str]:
    """Ensure the integration config has a webhook secret; return (config, secret)."""
    cfg = dict(config or {})
    secret = cfg.get(WEBHOOK_SECRET_KEY)
    if not secret:
        secret = secrets.token_urlsafe(32)
        cfg[WEBHOOK_SECRET_KEY] = secret
    return cfg, secret


async def register_gmail_watch(
    *,
    credentials: dict[str, str],
    topic: str,
) -> dict:
    """Call Gmail ``users.watch`` to start Pub/Sub change notifications.

    ``topic`` is a full Pub/Sub topic name:
    ``projects/{project}/topics/{topic}``. Requires a Gmail access token in
    ``credentials`` (minted from the refresh token upstream). Returns the API
    response (historyId + expiration) or an error dict.
    """
    return await agent_tools.call_provider_api(
        provider_name="gmail",
        credentials=credentials,
        config=None,
        method="POST",
        path="/gmail/v1/users/me/watch",
        body={"topicName": topic, "labelIds": ["INBOX"]},
    )


async def _auto_register(
    provider: str,
    webhook_url: str,
    secret: str,
    credentials: dict[str, str],
    config: dict,
) -> dict:
    """Attempt verified API-based webhook creation for a provider.

    Uses the verified :mod:`webhook_registration` spec: runs any prerequisite
    lookup, builds the create request, calls the provider API, and reads back a
    provider-issued signing secret when applicable. Returns an info dict; on a
    PARTIAL spec missing its required targets, returns a clear needs-input note
    instead of calling the API. Never raises — failures are reported.
    """
    spec = webhook_registration.registration_for(provider)
    if spec is None:
        return {"registered": False, "mode": "unverified"}

    if spec.mode is webhook_registration.RegMode.MANUAL:
        return {"registered": False, "mode": "manual",
                "note": spec.manual_instructions or "Add the webhook URL in the provider dashboard."}
    if spec.mode is webhook_registration.RegMode.POLL:
        return {"registered": False, "mode": "poll", "note": "Polled on a schedule."}

    values: dict = {**(config or {}), "events": spec.events}

    # PARTIAL: require the user-supplied resource targets.
    missing = [t for t in spec.required_targets if not values.get(t)]
    if missing:
        return {
            "registered": False,
            "mode": str(spec.mode),
            "needs": missing,
            "note": f"Provide {', '.join(missing)} in the integration config to auto-register the webhook.",
        }

    # Prerequisite lookup (e.g. Calendly organization URI).
    if spec.prerequisite is not None:
        try:
            extra = await spec.prerequisite(credentials, config)
            if isinstance(extra, dict):
                values.update(extra)
        except Exception as exc:  # noqa: BLE001
            return {"registered": False, "mode": str(spec.mode), "error": f"prerequisite_failed: {exc}"}

    if spec.build_request is None:
        return {"registered": False, "mode": str(spec.mode)}

    req = spec.build_request(webhook_url, secret, values)
    resp = await agent_tools.call_provider_api(
        provider_name=provider,
        credentials=credentials,
        config=config,
        method=req.method,
        path=req.path,
        query=req.query,
        body=req.body,
    )
    ok = bool(isinstance(resp, dict) and resp.get("ok"))
    info: dict = {"registered": ok, "mode": str(spec.mode), "source": spec.source}

    # Read a provider-issued signing secret when the create call returns one.
    if ok and spec.secret_source == "response" and spec.secret_response_path:
        body = resp.get("body") if isinstance(resp, dict) else None
        cur: Any = body
        for part in spec.secret_response_path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur:
            info["provider_secret"] = cur
    # Some providers require a follow-up call to read the signing secret
    # (e.g. Zendesk GET /webhooks/{id}/signing_secret).
    elif ok and spec.secret_source == "fetch" and spec.secret_fetch is not None:
        try:
            fetched = await spec.secret_fetch(credentials, config, resp)
            if isinstance(fetched, str) and fetched:
                info["provider_secret"] = fetched
        except Exception as exc:  # noqa: BLE001
            info["secret_fetch_error"] = str(exc)
    if not ok:
        info["response_status"] = resp.get("status_code") if isinstance(resp, dict) else None
    return info


async def register_subscription(
    *,
    provider: str,
    integration_id: str,
    credentials: dict[str, str],
    config: dict,
) -> dict:
    """Register the provider's trigger for an integration; return updated config + info.

    Always ensures a webhook secret exists. For Gmail (PUBSUB), calls users.watch
    when a Pub/Sub topic is configured. For PUSH providers we return the webhook
    URL (auto-registration via each provider's API is provider-specific and can
    be layered per provider). For POLL providers, nothing to register.
    """
    cfg, secret = ensure_webhook_secret(config)
    profile = provider_webhooks.trigger_for(provider)
    url = webhook_url(provider, integration_id, secret)
    # Persist the canonical callback URL; some signature schemes (Trello) sign
    # over the exact callbackURL provided at creation.
    cfg["__webhook_callback_url"] = url
    result: dict = {"webhook_url": url, "kind": str(profile.kind), "registered": False}

    settings = get_settings()

    if profile.kind is TriggerKind.PUBSUB and provider == "gmail":
        topic = getattr(settings, "GMAIL_PUBSUB_TOPIC", "") or ""
        if topic and credentials.get("access_token"):
            watch = await register_gmail_watch(credentials=credentials, topic=topic)
            result["watch"] = watch
            result["registered"] = bool(watch.get("ok", False) or "historyId" in str(watch))
            result["mode"] = "auto"
        else:
            # Push isn't configured (no Pub/Sub topic) — Gmail is polled instead,
            # so present it as a scheduled-checks provider with a clear next step.
            result["mode"] = "poll"
            result["note"] = (
                "Gmail is being checked on a schedule. To enable instant push "
                "notifications, an admin must set GMAIL_PUBSUB_TOPIC (a Google "
                "Cloud Pub/Sub topic) and expose a public HTTPS webhook URL; "
                "then reconnect Gmail."
            )
    elif profile.kind is TriggerKind.PUSH:
        # Attempt verified API-based auto-registration; fall back to manual URL.
        reg = await _auto_register(provider, url, secret, credentials, cfg)
        result.update(reg)
        # If the provider issued its own signing secret, store it as the secret
        # we verify inbound signatures against (overrides our generated one).
        provider_secret = reg.get("provider_secret")
        if provider_secret:
            cfg[WEBHOOK_SECRET_KEY] = provider_secret
        if not reg.get("registered") and "note" not in result:
            result["note"] = (
                "Add this webhook URL in the provider's dashboard; auto-"
                "registration was not completed."
            )
    else:  # POLL
        result["note"] = "This provider is polled on a schedule; no webhook needed."

    # Persist a compact status summary into config so the UI can render it later
    # without re-calling provider APIs (secret is never included).
    cfg[WEBHOOK_STATUS_KEY] = result.get("mode", str(profile.kind))
    cfg[WEBHOOK_REGISTERED_KEY] = bool(result.get("registered", False))
    if result.get("note"):
        cfg[WEBHOOK_NOTE_KEY] = result["note"]

    return {"config": cfg, "info": result}


def webhook_status_view(provider: str, config: dict | None) -> dict:
    """Build the UI-facing webhook status for an integration (pure).

    Combines the provider's verified registration spec with what we persisted at
    connect time (mode, registered flag, note) plus the callback URL, into a
    compact object the frontend can render as a clear status card. Never exposes
    the webhook secret.
    """
    from app.services import webhook_registration as _reg
    from app.services import provider_webhooks as _pw

    cfg = config or {}
    spec = _reg.registration_for(provider)
    profile = _pw.trigger_for(provider)
    mode = cfg.get(WEBHOOK_STATUS_KEY) or (str(spec.mode) if spec else str(profile.kind))
    registered = bool(cfg.get(WEBHOOK_REGISTERED_KEY, False))
    note = cfg.get(WEBHOOK_NOTE_KEY) or (spec.manual_instructions if spec else "")
    url = cfg.get("__webhook_callback_url", "")

    # Which config target fields (if any) the provider still needs for push.
    needs = []
    if spec is not None:
        needs = [t for t in spec.required_targets if not cfg.get(t)]

    return {
        "mode": mode,                 # auto | partial | manual | poll
        "registered": registered,     # did auto-registration succeed?
        "webhook_url": url,           # URL to paste for manual providers
        "needs": needs,               # missing target fields for partial providers
        "note": note,                 # human-friendly next step
        "trigger_kind": str(profile.kind),  # push | pubsub | poll
    }


__all__ = [
    "WEBHOOK_SECRET_KEY",
    "WEBHOOK_STATUS_KEY",
    "WEBHOOK_REGISTERED_KEY",
    "WEBHOOK_NOTE_KEY",
    "webhook_status_view",
    "webhook_url",
    "ensure_webhook_secret",
    "register_gmail_watch",
    "register_subscription",
]
