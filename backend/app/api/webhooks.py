"""Webhook gateway — public inbound provider webhooks that trigger agent runs.

Public endpoint (no Session_Token — providers can't present one):

    POST /api/v1/webhooks/{provider}/{integration_id}/{secret}

Flow:
1. Load the integration by id; 404 if missing/disconnected.
2. Verify authenticity for the provider (HMAC signature / shared token / the
   unguessable URL ``secret`` segment) using the per-integration webhook secret
   stored in the integration ``config`` (see webhook_subscriptions).
3. On success, enqueue a tenant-scoped agent run (``process_webhook``) carrying
   the workspace + triggering user + the (untrusted) payload, so the worker runs
   the agent under the workspace's rules and tools (Req 16.3).

The endpoint returns 200 quickly (webhooks must be acked fast); the heavy work
happens asynchronously in the worker. Payloads are treated as untrusted input
and never as authorization.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response

from app.db.models import Integration, IntegrationStatus
from app.db.session import get_session
from app.services import job_queue_bridge as jq
from app.services import provider_webhooks
from app.services.webhook_verify import verify_webhook
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("atomic_ai.webhooks")

async def _paypal_postback_ok(integration, raw_body: bytes, lower: dict, config: dict) -> bool:
    """Verify a PayPal webhook via the verify-webhook-signature postback.

    Source: https://developer.paypal.com/api/rest/webhooks/rest — POST the
    transmission headers + the stored webhook id + the event body to
    /v1/notifications/verify-webhook-signature; PayPal returns
    {"verification_status": "SUCCESS"|"FAILURE"}. Uses the integration's
    decrypted PayPal credentials (client id/secret) minted into an access token.
    """
    import json as _json

    from app.services import agent_tools
    from app.services import integration_vault as _vault
    from app.services import oauth_refresh as _oauth
    from app.db.session import session_scope

    webhook_id = (config or {}).get(WEBHOOK_SECRET_KEY, "")
    if not webhook_id:
        return False
    try:
        event = _json.loads(raw_body) if raw_body else {}
    except Exception:  # noqa: BLE001
        return False

    # Decrypt credentials and ensure a usable access token.
    async with session_scope() as s:
        cred = await _vault.use_credential(s, integration_id=integration.id)
    creds = await _oauth.ensure_access_token("paypal", dict(cred.credentials), dict(cred.config or {}))

    body = {
        "transmission_id": lower.get("paypal-transmission-id", ""),
        "transmission_time": lower.get("paypal-transmission-time", ""),
        "cert_url": lower.get("paypal-cert-url", ""),
        "auth_algo": lower.get("paypal-auth-algo", ""),
        "transmission_sig": lower.get("paypal-transmission-sig", ""),
        "webhook_id": webhook_id,
        "webhook_event": event,
    }
    resp = await agent_tools.call_provider_api(
        provider_name="paypal",
        credentials=creds,
        config=None,
        method="POST",
        path="/v1/notifications/verify-webhook-signature",
        body=body,
    )
    rb = resp.get("body") if isinstance(resp, dict) else None
    return isinstance(rb, dict) and rb.get("verification_status") == "SUCCESS"


router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

# The config key under which each integration stores its webhook secret.
WEBHOOK_SECRET_KEY = "__webhook_secret"


@router.head("/{provider}/{integration_id}/{secret}", status_code=200)
async def webhook_head(provider: str, integration_id: uuid.UUID, secret: str) -> Response:
    """Answer create-time URL checks (e.g. Trello HEADs the callback URL).

    Some providers verify the endpoint is live with a HEAD request before
    creating the webhook; respond 200 so registration succeeds.
    """
    return Response(status_code=200)


@router.get("/{provider}/{integration_id}/{secret}")
async def webhook_verify_challenge(
    provider: str,
    integration_id: uuid.UUID,
    secret: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Handle GET verification challenges (Meta hub.challenge).

    Meta (Facebook/Instagram/WhatsApp) verifies a webhook by GETting the URL with
    hub.mode=subscribe, hub.verify_token, hub.challenge. If the verify_token
    matches the integration's stored webhook secret, echo hub.challenge back as
    plain text. Source:
    https://developers.facebook.com/docs/graph-api/webhooks/getting-started
    """
    params = dict(request.query_params)

    # Mailchimp verifies the endpoint with a plain GET expecting 200.
    # Source: https://mailchimp.com/developer/marketing/api/list-webhooks/
    if provider_webhooks.trigger_for(provider).scheme == "mailchimp_get":
        return Response(status_code=200)

    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    verify_token = params.get("hub.verify_token")
    if mode == "subscribe" and challenge is not None:
        integration = await session.get(Integration, integration_id)
        expected = ""
        if integration is not None and integration.provider_name == provider:
            expected = (integration.config or {}).get(WEBHOOK_SECRET_KEY, "")
        # Constant-time compare of the verify token against the stored secret.
        import hmac as _hmac

        if verify_token and _hmac.compare_digest(verify_token, expected):
            return Response(content=challenge, media_type="text/plain", status_code=200)
        return Response(status_code=403)
    return Response(status_code=400)


async def _handle_gmail_push(session, integration) -> None:
    """Cost-controlled enqueue for a verified Gmail Pub/Sub push (root-cause fix
    for post-fix token accumulation on the WEBHOOK path).

    A Gmail push must NOT trigger a generic "scan the whole inbox and reply to
    everything" run (that cost ~155K tokens/push and re-replied to already-seen
    mail). This reuses the poll-path machinery so push and poll behave the same:

      1. per-integration in-flight lock -> no overlapping runs on one inbox;
      2. cheap unread pre-check (plain Gmail API, NO Bedrock) + durable
         processed_messages de-dup -> never re-check/re-reply a seen message;
      3. enqueue a run SCOPED to only the genuinely-new ids, or NOTHING.

    Best-effort and never raises: the caller has already ack'd the webhook.
    """
    from app.agents import tasks as _tasks
    from app.services import agent_tools as _agent_tools
    from app.services import approval_service as _approval

    try:
        async with _tasks._integration_poll_lock(integration.id) as acquired:
            if not acquired:
                logger.info(
                    "webhook.gmail_locked_skip integration=%s (run in flight)",
                    integration.id,
                )
                return
            new_ids = await _tasks._gmail_new_ids_for_poll(
                session,
                integration,
                resolve_gmail_credentials=_approval.resolve_gmail_credentials,
                call_provider_api=_agent_tools.call_provider_api,
            )
            if not new_ids:
                logger.info("webhook.gmail_no_new integration=%s", integration.id)
                return
            await jq.enqueue_webhook_run(
                workspace_id=integration.workspace_id,
                triggered_by_user_id=integration.created_by_user_id,
                provider_name="gmail",
                category=str(integration.category),
                payload={
                    "provider": "gmail",
                    "integration_id": str(integration.id),
                    # SCOPED prompt: process ONLY these new ids (poll-path prompt).
                    "prompt": _tasks._poll_prompt_for(
                        "gmail", str(integration.category), new_ids
                    ),
                },
            )
            logger.info(
                "webhook.gmail_enqueued integration=%s new_count=%d",
                integration.id, len(new_ids),
            )
    except Exception:  # noqa: BLE001 - never fail the webhook ack on error
        logger.exception("failed to process gmail webhook for %s", integration.id)


@router.post("/{provider}/{integration_id}/{secret}", status_code=200)
async def receive_webhook(
    provider: str,
    integration_id: uuid.UUID,
    secret: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Receive, verify, and enqueue an agent run for an inbound provider webhook."""
    integration = await session.get(Integration, integration_id)
    if (
        integration is None
        or integration.status is IntegrationStatus.DISCONNECTED
        or integration.provider_name != provider
    ):
        # Do not disclose existence; a generic 200 ack avoids probing, but a 404
        # is fine here since the id+secret are unguessable.
        response.status_code = 404
        return {"status": "not_found"}

    raw_body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    lower = {k.lower(): v for k, v in headers.items()}

    config = dict(integration.config or {})
    expected_secret = config.get(WEBHOOK_SECRET_KEY, "")
    profile = provider_webhooks.trigger_for(provider)

    # Asana handshake: on webhook creation Asana POSTs an X-Hook-Secret header.
    # We must echo it back with 200 AND persist it as the signing secret used to
    # verify subsequent X-Hook-Signature (HMAC-SHA256) events.
    # Source: https://developers.asana.com/reference/createwebhook
    if profile.scheme == "asana" and "x-hook-secret" in lower and not raw_body.strip():
        handshake_secret = lower["x-hook-secret"]
        config[WEBHOOK_SECRET_KEY] = handshake_secret
        integration.config = config
        await session.flush()
        await session.commit()
        response.headers["X-Hook-Secret"] = handshake_secret
        return {"status": "handshake_ok"}

    # monday.com challenge: on webhook creation monday POSTs {"challenge": "..."}
    # to the URL; echo it back or the create fails.
    # Source: https://developer.monday.com/api-reference/reference/webhooks
    if profile.scheme == "monday" and raw_body:
        try:
            import json as _json

            _peek = _json.loads(raw_body)
        except Exception:  # noqa: BLE001
            _peek = None
        if isinstance(_peek, dict) and "challenge" in _peek:
            return {"challenge": _peek["challenge"]}

    # Microsoft Graph subscription validation: on create, Graph POSTs the
    # notificationUrl with ?validationToken=...; echo it back as text/plain 200
    # within 10s. Source:
    # https://learn.microsoft.com/en-us/graph/change-notifications-overview
    if profile.scheme == "graph":
        validation_token = request.query_params.get("validationToken")
        if validation_token is not None:
            return Response(
                content=validation_token, media_type="text/plain", status_code=200
            )

    # Zoom URL-validation challenge: Zoom POSTs event "endpoint.url_validation"
    # with payload.plainToken; respond {plainToken, encryptedToken} where
    # encryptedToken = HMAC_SHA256(secret, plainToken).
    # Source: https://developers.zoom.us/docs/api/webhooks/
    if profile.scheme == "zoom" and raw_body:
        try:
            import json as _json

            _peek = _json.loads(raw_body)
        except Exception:  # noqa: BLE001
            _peek = None
        if isinstance(_peek, dict) and _peek.get("event") == "endpoint.url_validation":
            import hashlib as _h
            import hmac as _hm

            plain = str((_peek.get("payload") or {}).get("plainToken", ""))
            enc = _hm.new(expected_secret.encode(), plain.encode(), _h.sha256).hexdigest()
            return {"plainToken": plain, "encryptedToken": enc}

    # The full callback URL is required by some signature schemes (Trello);
    # use the canonical URL stored at registration (exact match matters).
    callback_url = config.get("__webhook_callback_url", str(request.url))

    if profile.scheme == "paypal_postback":
        # PayPal verifies via a postback to its verify-webhook-signature endpoint.
        ok = await _paypal_postback_ok(integration, raw_body, lower, config)
    else:
        ok = verify_webhook(
            profile=profile,
            secret=expected_secret,
            raw_body=raw_body,
            headers=headers,
            url_secret=secret,
            expected_url_secret=expected_secret,
            callback_url=callback_url,
        )
    if not ok:
        response.status_code = 401
        return {"status": "unauthorized"}

    # Google watch channels send an initial "sync" state message on channel
    # creation; acknowledge it with 200 without triggering a run.
    # Source: https://developers.google.com/workspace/calendar/api/guides/push
    if profile.scheme == "google_watch" and lower.get("x-goog-resource-state") == "sync":
        return {"status": "sync_ack"}

    # Parse the payload best-effort (never trusted for authorization).
    payload: dict = {}
    try:
        import json

        if raw_body:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                payload = parsed
    except Exception:  # noqa: BLE001
        payload = {"raw": raw_body.decode("utf-8", "replace")[:2000]}

    # GMAIL PUSH COST-CONTROL (root-cause fix for the post-fix token accumulation):
    # A Gmail Pub/Sub push must NOT trigger a generic "scan the whole inbox and
    # reply to everything" run. That prompt made each push cost ~155K tokens and
    # re-reply to already-seen mail. Instead, reuse the EXACT poll-path machinery:
    #   1) a per-integration in-flight lock (no overlapping runs on the same inbox),
    #   2) the cheap unread pre-check (plain Gmail API, NO Bedrock) + durable
    #      processed_messages de-dup, and
    #   3) a run SCOPED to only the genuinely-new message ids (or nothing at all).
    # This mirrors poll_integrations so push and poll behave identically.
    if provider == "gmail":
        await _handle_gmail_push(session, integration)
        return {"status": "accepted"}

    # Enqueue a tenant-scoped agent run (best-effort; ack the webhook regardless).
    try:
        await jq.enqueue_webhook_run(
            workspace_id=integration.workspace_id,
            triggered_by_user_id=integration.created_by_user_id,
            provider_name=provider,
            category=str(integration.category),
            payload={
                "provider": provider,
                "integration_id": str(integration_id),
                "event": payload,
                "prompt": (
                    f"An event was received from {provider}. Apply the workspace "
                    f"automation rules for the {integration.category} category to "
                    f"this event and take the appropriate action."
                ),
            },
        )
    except Exception:  # noqa: BLE001 - never fail the webhook ack on enqueue error
        logger.exception("failed to enqueue webhook run for %s", integration_id)

    logger.info("webhook.received provider=%s integration=%s", provider, integration_id)
    return {"status": "accepted"}


__all__ = ["router", "WEBHOOK_SECRET_KEY"]
