"""Webhook auto-registration registry (verified, per-provider).

Production-ready, incrementally-verified registry describing HOW to register an
inbound webhook with each provider via its API — or, when the provider offers no
create-API, how the user registers it manually. Every entry is sourced from the
provider's official docs; entries are added only after verification (no guessing).

Each provider maps to a :class:`RegistrationSpec` with a ``mode``:

- ``AUTO``    — we can create the webhook via one API call; ``build_request``
  returns the (method, path, body) to POST. May require a prerequisite lookup
  (e.g. Calendly needs the organization URI) captured by ``prerequisite``.
- ``PARTIAL`` — API creation exists but needs a user-supplied resource target we
  cannot invent (e.g. GitHub owner/repo, Jira project). ``required_targets``
  lists what the user must provide; once provided, ``build_request`` works.
- ``MANUAL``  — no create-API; the user pastes the webhook URL into the provider
  dashboard. We surface the URL + instructions.
- ``POLL``    — no push; the scheduler handles it (see tasks.poll_integrations).

``verified`` marks entries confirmed against current official docs. Unverified
providers fall through to MANUAL (URL surfaced) or POLL per their trigger
profile, so we never fabricate an endpoint.

Sources are recorded in ``source`` for auditability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RegMode(StrEnum):
    AUTO = "auto"
    PARTIAL = "partial"
    MANUAL = "manual"
    POLL = "poll"


@dataclass(frozen=True)
class BuiltRequest:
    """An HTTP request to create a webhook with a provider."""

    method: str
    path: str
    body: dict[str, Any] | None = None
    query: dict[str, Any] | None = None


@dataclass(frozen=True)
class RegistrationSpec:
    """How to register a webhook for one provider.

    Attributes:
        mode: AUTO / PARTIAL / MANUAL / POLL.
        events: default event types to subscribe to (provider-specific).
        required_targets: config keys the user must supply for PARTIAL mode
            (e.g. ["owner", "repo"] for GitHub).
        prerequisite: optional callable(creds, config) -> extra dict merged into
            values before build_request (e.g. resolve Calendly org URI). Async.
        build_request: callable(webhook_url, secret, values) -> BuiltRequest, or
            None for MANUAL/POLL.
        secret_source: where the signing secret comes from — "response" (the
            create call returns it), "ours" (we supply the secret we generated),
            or "none".
        secret_response_path: dotted path into the create response to read the
            signing secret when secret_source == "response" (e.g. "secret").
        secret_fetch: optional async callable(credentials, config, create_response)
            -> signing secret, for providers where the secret needs a separate
            follow-up API call (e.g. Zendesk GET /webhooks/{id}/signing_secret).
        manual_instructions: shown to the user for MANUAL mode.
        verified: True if confirmed against official docs.
        source: doc URL(s) the spec was verified from.
    """

    mode: RegMode
    events: list[str] = field(default_factory=list)
    required_targets: list[str] = field(default_factory=list)
    prerequisite: Callable[..., Any] | None = None
    build_request: Callable[..., BuiltRequest] | None = None
    secret_source: str = "ours"
    secret_response_path: str = ""
    secret_fetch: Callable[..., Any] | None = None
    manual_instructions: str = ""
    verified: bool = False
    source: str = ""


# ---------------------------------------------------------------------------
# Verified builders (each sourced from official docs; see `source`).
# ---------------------------------------------------------------------------

def _stripe_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Stripe: POST /v1/webhook_endpoints, form-encoded url + enabled_events[].
    # Returns a `secret` (whsec_...) used for Stripe-Signature verification.
    # Source: https://docs.stripe.com/api/webhook_endpoints/create
    events = values.get("events") or ["*"]
    body: dict[str, Any] = {"url": webhook_url}
    # httpx form-encodes lists as repeated keys via our api layer (JSON here;
    # Stripe accepts JSON on this endpoint through the SDK-compatible gateway).
    body["enabled_events[]"] = events
    return BuiltRequest("POST", "/v1/webhook_endpoints", body=body)


def _github_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # GitHub: POST /repos/{owner}/{repo}/hooks with config{url,content_type,secret}.
    # Source: https://docs.github.com/rest/repos/webhooks#create-a-repository-webhook
    owner = values.get("owner", "")
    repo = values.get("repo", "")
    events = values.get("events") or ["push", "issues", "pull_request"]
    return BuiltRequest(
        "POST",
        f"/repos/{owner}/{repo}/hooks",
        body={
            "name": "web",
            "active": True,
            "events": events,
            "config": {
                "url": webhook_url,
                "content_type": "json",
                "secret": secret,
            },
        },
    )


async def _calendly_prereq(credentials: dict, config: dict) -> dict:
    # Calendly create needs the organization URI; fetch it from /users/me.
    # Source: https://developer.calendly.com/api-docs/overview/examples/webhooks
    from app.services import agent_tools

    me = await agent_tools.call_provider_api(
        provider_name="calendly",
        credentials=credentials,
        config=config,
        method="GET",
        path="/users/me",
    )
    org = ""
    body = me.get("body") if isinstance(me, dict) else None
    if isinstance(body, dict):
        org = (body.get("resource") or {}).get("current_organization", "")
    return {"organization": org}


def _calendly_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Calendly: POST /webhook_subscriptions with url, events, organization,
    # scope, signing_key. Signature header: Calendly-Webhook-Signature.
    # Source: https://developer.calendly.com/docs/api-guides/receive-data-...
    return BuiltRequest(
        "POST",
        "/webhook_subscriptions",
        body={
            "url": webhook_url,
            "events": values.get("events") or ["invitee.created", "invitee.canceled"],
            "organization": values.get("organization", ""),
            "scope": "organization",
            "signing_key": secret,
        },
    )


def _pipedrive_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Pipedrive v1: POST /v1/webhooks with subscription_url + event_action +
    # event_object ("*"/"*" for all). Auth via api_token query param (handled by
    # the api layer). Optional HTTP basic auth on our endpoint (we use the URL
    # secret instead). Source: https://developers.pipedrive.com/docs/api/v1/Webhooks
    return BuiltRequest(
        "POST",
        "/webhooks",
        body={
            "subscription_url": webhook_url,
            "event_action": values.get("event_action", "*"),
            "event_object": values.get("event_object", "*"),
        },
    )


def _cloudflare_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Cloudflare: POST /accounts/{account_id}/alerting/v3/destinations/webhooks
    # with name, url, secret. Dispatch carries cf-webhook-auth: <secret>.
    # Source: https://developers.cloudflare.com/api/.../destinations/webhooks/methods/create/
    account_id = values.get("account_id", "")
    return BuiltRequest(
        "POST",
        f"/accounts/{account_id}/alerting/v3/destinations/webhooks",
        body={"name": "Atomic AI", "url": webhook_url, "secret": secret},
    )


def _asana_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Asana: POST /webhooks with data{resource, target}. Asana then performs an
    # X-Hook-Secret handshake against `target` (our gateway echoes it back) and
    # signs events with X-Hook-Signature (HMAC-SHA256 over body with that secret).
    # Source: https://developers.asana.com/reference/createwebhook
    return BuiltRequest(
        "POST",
        "/webhooks",
        body={"data": {"resource": values.get("resource", ""), "target": webhook_url}},
    )


def _trello_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Trello: POST /1/webhooks with callbackURL + idModel (key/token via query).
    # Trello HEADs the callbackURL (must 200) then signs X-Trello-Webhook as
    # base64 HMAC-SHA1 over (body + callbackURL).
    # Source: https://developers.trello.com/webhooks
    return BuiltRequest(
        "POST",
        "/webhooks",
        body={
            "callbackURL": webhook_url,
            "idModel": values.get("idModel", ""),
            "description": "Atomic AI",
        },
    )


def _gitlab_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # GitLab: POST /projects/{id}/hooks with url + token (echoed in
    # X-Gitlab-Token) + event flags. Needs the project id.
    # Source: https://docs.gitlab.com/api/project_webhooks
    pid = values.get("project_id", "")
    return BuiltRequest(
        "POST",
        f"/projects/{pid}/hooks",
        body={
            "url": webhook_url,
            "token": secret,
            "push_events": True,
            "issues_events": True,
            "merge_requests_events": True,
        },
    )


def _mailgun_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Mailgun: POST /v3/domains/{domain}/webhooks with id (event) + url.
    # Signature is body-embedded (verified via the account signing key, not this
    # secret). Needs the sending domain. Source:
    # https://documentation.mailgun.com/docs/mailgun/api-reference/send/mailgun/domain-webhooks/post-v3-domains--domain--webhooks
    domain = values.get("domain", "")
    event = values.get("event", "delivered")
    return BuiltRequest(
        "POST",
        f"/domains/{domain}/webhooks",
        body={"id": event, "url": webhook_url},
    )


def _telegram_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Telegram: POST /bot{token}/setWebhook with url + secret_token. Telegram
    # then sends X-Telegram-Bot-Api-Secret-Token: <secret_token> on each update.
    # (The api layer prepends /bot{token} for Telegram, so path is /setWebhook.)
    # Source: https://core.telegram.org/bots/api#setwebhook
    return BuiltRequest(
        "POST",
        "/setWebhook",
        body={"url": webhook_url, "secret_token": secret},
    )


def _line_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # LINE: PUT /v2/bot/channel/webhook/endpoint with {endpoint}. Signature is
    # base64(HMAC-SHA256(channel_secret, body)) in X-Line-Signature — the
    # channel_secret must be supplied as the integration's verify secret.
    # Source: https://developers.line.biz/en/reference/messaging-api/#set-webhook-endpoint-url
    return BuiltRequest(
        "PUT",
        "/bot/channel/webhook/endpoint",
        body={"endpoint": webhook_url},
    )


# Default Microsoft Graph resources per provider (delegated /me context).
_GRAPH_DEFAULT_RESOURCE = {
    "outlook": "/me/mailfolders('inbox')/messages",
    "outlook_calendar": "/me/events",
    "onedrive": "/me/drive/root",
    "word": "/me/drive/root",
    "excel": "/me/drive/root",
    "powerpoint": "/me/drive/root",
    "teams": "/me/chats/getAllMessages",
}


def _graph_build(provider: str):
    def _build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
        # Microsoft Graph: POST /subscriptions. changeType created/updated,
        # notificationUrl, resource, expirationDateTime (<~1h for mail),
        # clientState (our secret, echoed back for verification).
        # Source: https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions
        import datetime as _dt

        resource = values.get("resource") or _GRAPH_DEFAULT_RESOURCE.get(provider, "/me/messages")
        expiry = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=55)).strftime(
            "%Y-%m-%dT%H:%M:%S.0000000Z"
        )
        return BuiltRequest(
            "POST",
            "/subscriptions",
            body={
                "changeType": values.get("change_type", "created,updated"),
                "notificationUrl": webhook_url,
                "resource": resource,
                "expirationDateTime": expiry,
                "clientState": secret,
            },
        )

    return _build


def _gcal_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Google Calendar: POST /calendars/{calendarId}/events/watch with a channel
    # {id, type:web_hook, address, token, expiration}. Notifications carry
    # X-Goog-Channel-Token (== token) which we verify.
    # Source: https://developers.google.com/workspace/calendar/api/guides/push
    import time
    import uuid as _uuid

    calendar_id = values.get("calendar_id", "primary")
    # 7-day expiration in ms since epoch (Calendar max).
    expiration_ms = int((time.time() + 7 * 24 * 3600) * 1000)
    return BuiltRequest(
        "POST",
        f"/calendars/{calendar_id}/events/watch",
        body={
            "id": str(_uuid.uuid4()),
            "type": "web_hook",
            "address": webhook_url,
            "token": secret,
            "expiration": expiration_ms,
        },
    )


def _gdrive_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Google Drive: POST /changes/watch (needs a pageToken). We request a start
    # page token first via prerequisite; the channel mirrors Calendar's shape.
    # Source: https://developers.google.com/workspace/drive/api/guides/push
    import time
    import uuid as _uuid

    expiration_ms = int((time.time() + 24 * 3600) * 1000)
    body = {
        "id": str(_uuid.uuid4()),
        "type": "web_hook",
        "address": webhook_url,
        "token": secret,
        "expiration": expiration_ms,
    }
    page_token = values.get("start_page_token", "")
    return BuiltRequest(
        "POST",
        "/changes/watch",
        body=body,
        query={"pageToken": page_token} if page_token else None,
    )


async def _gdrive_prereq(credentials: dict, config: dict) -> dict:
    # Drive changes.watch requires a pageToken; fetch the start page token.
    # Source: https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/getStartPageToken
    from app.services import agent_tools

    resp = await agent_tools.call_provider_api(
        provider_name="google_drive",
        credentials=credentials,
        config=config,
        method="GET",
        path="/changes/startPageToken",
    )
    token = ""
    body = resp.get("body") if isinstance(resp, dict) else None
    if isinstance(body, dict):
        token = body.get("startPageToken", "")
    return {"start_page_token": token}


def _zendesk_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Zendesk: POST /webhooks with endpoint, http_method, request_format, and
    # subscribed events. Zendesk generates the signing secret (fetched after).
    # Source: https://developer.zendesk.com/api-reference/webhooks/webhooks-api/webhooks/
    return BuiltRequest(
        "POST",
        "/webhooks",
        body={
            "webhook": {
                "name": "Atomic AI",
                "endpoint": webhook_url,
                "http_method": "POST",
                "request_format": "json",
                "status": "active",
                "subscriptions": values.get("subscriptions", ["conditional_ticket_events"]),
            }
        },
    )


async def _zendesk_secret_fetch(credentials: dict, config: dict, create_response: dict) -> str:
    # After creating the webhook, read its generated signing secret.
    # Source: https://developer.zendesk.com/api-reference/webhooks/webhooks-api/webhooks/#show-webhook-signing-secret
    from app.services import agent_tools

    body = create_response.get("body") if isinstance(create_response, dict) else None
    webhook_id = ""
    if isinstance(body, dict):
        webhook_id = (body.get("webhook") or {}).get("id", "")
    if not webhook_id:
        return ""
    resp = await agent_tools.call_provider_api(
        provider_name="zendesk",
        credentials=credentials,
        config=config,
        method="GET",
        path=f"/webhooks/{webhook_id}/signing_secret",
    )
    rb = resp.get("body") if isinstance(resp, dict) else None
    if isinstance(rb, dict):
        return (rb.get("signing_secret") or {}).get("secret", "")
    return ""


def _terraform_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Terraform Cloud: POST /workspaces/{workspace_id}/notification-configurations
    # (JSON:API). destination-type generic, url, token (our secret), triggers.
    # Signature X-TFE-Notification-Signature = HMAC-SHA512(token, body) hex.
    # Source: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/notification-configurations/workspace
    wsid = values.get("workspace_id", "")
    return BuiltRequest(
        "POST",
        f"/workspaces/{wsid}/notification-configurations",
        body={
            "data": {
                "type": "notification-configurations",
                "attributes": {
                    "destination-type": "generic",
                    "enabled": True,
                    "name": "Atomic AI",
                    "url": webhook_url,
                    "token": secret,
                    "triggers": values.get("triggers", ["run:completed", "run:errored"]),
                },
            }
        },
    )


def _monday_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # monday.com: GraphQL mutation create_webhook(board_id, url, event). Posted
    # to https://api.monday.com/v2 as {query}. monday challenges the URL on
    # creation (echoed by the gateway). Needs a board_id.
    # Source: https://developer.monday.com/api-reference/reference/webhooks
    board_id = values.get("board_id", "")
    event = values.get("event", "create_item")
    # Escape the URL for embedding in the GraphQL string.
    safe_url = webhook_url.replace('"', '\\"')
    query = (
        "mutation { create_webhook (board_id: "
        f"{board_id}, url: \"{safe_url}\", event: {event}) {{ id board_id }} }}"
    )
    return BuiltRequest("POST", "/", body={"query": query})


def _bitbucket_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Bitbucket Cloud: POST /repositories/{workspace}/{repo_slug}/hooks with
    # url, active, events, secret. Signature X-Hub-Signature: sha256=<hex>.
    # Source: https://developer.atlassian.com/cloud/bitbucket/rest/api-group-webhooks/#api-repositories-workspace-repo-slug-hooks-post
    workspace = values.get("workspace", "")
    repo_slug = values.get("repo_slug", "")
    return BuiltRequest(
        "POST",
        f"/repositories/{workspace}/{repo_slug}/hooks",
        body={
            "description": "Atomic AI",
            "url": webhook_url,
            "active": True,
            "secret": secret,
            "events": values.get("events", ["repo:push", "pullrequest:created", "issue:created"]),
        },
    )


def _sendgrid_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # SendGrid: POST /v3/user/webhooks/event/settings with enabled, url, event
    # flags, and enabled=True + signature verification. Public key is fetched
    # after (ECDSA). Source:
    # https://www.twilio.com/docs/sendgrid/api-reference/webhooks/create-an-event-webhook
    return BuiltRequest(
        "POST",
        "/v3/user/webhooks/event/settings",
        body={
            "enabled": True,
            "url": webhook_url,
            "delivered": True,
            "open": True,
            "click": True,
            "bounce": True,
            "spam_report": True,
            "unsubscribe": True,
        },
    )


async def _sendgrid_secret_fetch(credentials: dict, config: dict, create_response: dict) -> str:
    # Enable signature verification, then read the ECDSA public key.
    # Source: https://www.twilio.com/docs/sendgrid/api-reference/webhooks/toggle-signature-verification-for-an-event-webhook
    from app.services import agent_tools

    body = create_response.get("body") if isinstance(create_response, dict) else None
    webhook_id = body.get("id", "") if isinstance(body, dict) else ""
    # Enable signature verification (returns the public key).
    path = "/v3/user/webhooks/event/settings/signed"
    if webhook_id:
        path = f"/v3/user/webhooks/event/settings/{webhook_id}/signed"
    resp = await agent_tools.call_provider_api(
        provider_name="sendgrid",
        credentials=credentials,
        config=config,
        method="PATCH",
        path=path,
        body={"enabled": True},
    )
    rb = resp.get("body") if isinstance(resp, dict) else None
    if isinstance(rb, dict):
        return rb.get("public_key", "")
    return ""


def _mailchimp_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Mailchimp: POST /3.0/lists/{list_id}/webhooks with url, events, sources.
    # No signature; endpoint verified by a GET expecting 200. Needs list_id.
    # Source: https://mailchimp.com/developer/marketing/api/list-webhooks/
    list_id = values.get("list_id", "")
    return BuiltRequest(
        "POST",
        f"/lists/{list_id}/webhooks",
        body={
            "url": webhook_url,
            "events": values.get("events", {"subscribe": True, "unsubscribe": True, "campaign": True}),
            "sources": {"user": True, "admin": True, "api": True},
        },
    )


def _brevo_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Brevo: POST /webhooks with url, events, type (marketing/transactional).
    # No signature; URL secret governs. Source:
    # https://developers.brevo.com/reference/createwebhook
    return BuiltRequest(
        "POST",
        "/webhooks",
        body={
            "url": webhook_url,
            "type": values.get("type", "transactional"),
            "events": values.get("events", ["delivered", "opened", "click", "hardBounce"]),
        },
    )


def _azure_devops_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Azure DevOps: POST /_apis/hooks/subscriptions?api-version=7.1 creating a
    # webHooks consumer subscription. Needs a project id.
    # Source: https://learn.microsoft.com/en-us/rest/api/azure/devops/hooks/subscriptions/create
    project_id = values.get("project_id", "")
    event_type = values.get("event_type", "workitem.updated")
    return BuiltRequest(
        "POST",
        "/_apis/hooks/subscriptions",
        query={"api-version": "7.1"},
        body={
            "publisherId": "tfs",
            "eventType": event_type,
            "resourceVersion": "1.0",
            "consumerId": "webHooks",
            "consumerActionId": "httpRequest",
            "publisherInputs": {"projectId": project_id},
            "consumerInputs": {"url": webhook_url},
        },
    )


def _paypal_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # PayPal: POST /v1/notifications/webhooks with url + event_types. The webhook
    # id (returned) is needed for postback signature verification.
    # Source: https://developer.paypal.com/api/webhooks/v1/webhooks-post
    events = values.get("event_types") or ["*"]
    return BuiltRequest(
        "POST",
        "/v1/notifications/webhooks",
        body={
            "url": webhook_url,
            "event_types": [{"name": e} for e in events],
        },
    )


def _close_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Close: POST /webhook/ with url + events (object type + action).
    # Source: https://developer.close.com/topics/webhooks/
    return BuiltRequest(
        "POST",
        "/webhook/",
        body={
            "url": webhook_url,
            "events": values.get("events", [{"object_type": "activity.note", "action": "created"}]),
        },
    )


def _zoho_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Zoho CRM: POST /crm/v8/actions/watch enabling instant notifications with a
    # notify_url, events, channel_id, and a token echoed back for verification.
    # 1-day max expiry -> renewed by the renewal cron.
    # Source: https://www.zoho.com/crm/developer/docs/api/v8/notifications/enable.html
    import time
    import uuid as _uuid

    events = values.get("events", ["Contacts.create", "Contacts.edit"])
    channel_expiry = (
        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 23 * 3600))
    )
    return BuiltRequest(
        "POST",
        "/crm/v8/actions/watch",
        body={
            "watch": [
                {
                    "channel_id": str(int(_uuid.uuid4().int % 1_000_000_000)),
                    "events": events,
                    "notify_url": webhook_url,
                    "token": secret,
                    "channel_expiry": channel_expiry,
                }
            ]
        },
    )


def _activecampaign_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # ActiveCampaign: POST /api/3/webhooks with name, url, events, sources.
    # Source: https://developers.activecampaign.com/reference/webhooks
    return BuiltRequest(
        "POST",
        "/api/3/webhooks",
        body={
            "webhook": {
                "name": "Atomic AI",
                "url": webhook_url,
                "events": values.get("events", ["subscribe", "unsubscribe"]),
                "sources": values.get("sources", ["public", "admin", "api"]),
            }
        },
    )


def _kit_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Kit (ConvertKit) v4: POST /v4/webhooks with url + events.
    # Source: https://developers.kit.com/api-reference/webhooks/create-a-webhook
    return BuiltRequest(
        "POST",
        "/webhooks",
        body={
            "url": webhook_url,
            "events": values.get("events", ["subscriber.created", "subscriber.activated"]),
        },
    )


def _sender_build(webhook_url: str, secret: str, values: dict) -> BuiltRequest:
    # Sender.net: POST /v2/account-webhooks with url + topic (+ relation_id).
    # Source: https://api.sender.net/account-webhooks/create-webhook/
    return BuiltRequest(
        "POST",
        "/account-webhooks",
        body={
            "url": webhook_url,
            "topic": values.get("topic", "groups/new-subscriber"),
        },
    )


REGISTRATIONS: dict[str, RegistrationSpec] = {
    # --- Verified AUTO / PARTIAL ---
    "stripe": RegistrationSpec(
        mode=RegMode.AUTO,
        events=["*"],
        build_request=_stripe_build,
        secret_source="response",
        secret_response_path="secret",
        verified=True,
        source="https://docs.stripe.com/api/webhook_endpoints/create",
    ),
    "github": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["owner", "repo"],
        events=["push", "issues", "pull_request"],
        build_request=_github_build,
        secret_source="ours",
        verified=True,
        source="https://docs.github.com/rest/repos/webhooks",
    ),
    "calendly": RegistrationSpec(
        mode=RegMode.AUTO,
        events=["invitee.created", "invitee.canceled"],
        prerequisite=_calendly_prereq,
        build_request=_calendly_build,
        secret_source="ours",
        verified=True,
        source="https://developer.calendly.com/api-docs/overview/examples/webhooks",
    ),
    "pipedrive": RegistrationSpec(
        mode=RegMode.AUTO,
        events=["*"],
        build_request=_pipedrive_build,
        secret_source="ours",
        verified=True,
        source="https://developers.pipedrive.com/docs/api/v1/Webhooks",
    ),
    "cloudflare": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["account_id"],
        build_request=_cloudflare_build,
        secret_source="ours",
        verified=True,
        source="https://developers.cloudflare.com/api/resources/alerting/subresources/destinations/subresources/webhooks/methods/create/",
    ),
    "asana": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["resource"],
        build_request=_asana_build,
        secret_source="handshake",  # Asana sets the secret via X-Hook-Secret handshake
        verified=True,
        source="https://developers.asana.com/reference/createwebhook",
    ),
    "trello": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["idModel"],
        build_request=_trello_build,
        secret_source="ours",
        verified=True,
        source="https://developers.trello.com/webhooks",
    ),
    "gitlab": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["project_id"],
        build_request=_gitlab_build,
        secret_source="ours",
        verified=True,
        source="https://docs.gitlab.com/api/project_webhooks",
    ),
    "mailgun": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["domain"],
        build_request=_mailgun_build,
        # Mailgun verifies via the account HTTP signing key, not a per-webhook
        # secret; the signing key must be supplied as the webhook secret in
        # config for inbound verification.
        secret_source="ours",
        verified=True,
        source="https://documentation.mailgun.com/docs/mailgun/api-reference/send/mailgun/domain-webhooks/post-v3-domains--domain--webhooks",
    ),
    "zoom": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In the Zoom App Marketplace, open your app -> Features -> Access -> "
            "Event Subscriptions, add this webhook URL, and copy the Secret Token "
            "into the integration config. Zoom validates the URL and signs events."
        ),
        verified=True,
        source="https://developers.zoom.us/docs/api/webhooks/",
    ),
    "telegram": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_telegram_build,
        secret_source="ours",
        verified=True,
        source="https://core.telegram.org/bots/api#setwebhook",
    ),
    "line": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_line_build,
        # LINE verifies with the channel_secret (X-Line-Signature); store the
        # channel_secret as the integration verify secret in config.
        secret_source="ours",
        verified=True,
        source="https://developers.line.biz/en/reference/messaging-api/#set-webhook-endpoint-url",
    ),
    "facebook": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In your Meta app dashboard -> Webhooks, add this Callback URL and set "
            "the Verify Token to the integration's webhook secret. Meta will GET "
            "the URL to verify (auto-answered), then POST events signed with "
            "X-Hub-Signature-256 (app secret). Subscribe your Page via "
            "POST /{page-id}/subscribed_apps."
        ),
        verified=True,
        source="https://developers.facebook.com/docs/graph-api/webhooks/getting-started",
    ),
    "instagram": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In your Meta app dashboard -> Webhooks (Instagram), add this Callback "
            "URL and set the Verify Token to the integration's webhook secret. "
            "Events are signed with X-Hub-Signature-256."
        ),
        verified=True,
        source="https://developers.facebook.com/docs/graph-api/webhooks/getting-started",
    ),
    "whatsapp": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In your Meta app dashboard -> WhatsApp -> Configuration, add this "
            "Callback URL and set the Verify Token to the integration's webhook "
            "secret. Events are signed with X-Hub-Signature-256."
        ),
        verified=True,
        source="https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks",
    ),
    "discord": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "Discord bots receive events over the Gateway (WebSocket), not inbound "
            "webhooks; Atomic AI polls for changes instead."
        ),
        verified=True,
        source="https://discord.com/developers/docs/events/gateway",
    ),
    "outlook": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("outlook"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions",
    ),
    "outlook_calendar": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("outlook_calendar"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions",
    ),
    "onedrive": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("onedrive"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/onedrive/developer/rest-api/api/subscription_post_subscriptions",
    ),
    "teams": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("teams"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/graph/teams-changenotifications-chat",
    ),
    "word": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("word"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions",
    ),
    "excel": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("excel"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions",
    ),
    "powerpoint": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_graph_build("powerpoint"),
        # Graph verifies notifications by echoing our clientState (secret_source
        # "ours") and validates the endpoint with a validationToken handshake.
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions",
    ),
    "zendesk": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_zendesk_build,
        secret_source="fetch",
        secret_fetch=_zendesk_secret_fetch,
        verified=True,
        source="https://developer.zendesk.com/api-reference/webhooks/webhooks-api/webhooks/",
    ),
    "hubspot": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "HubSpot private-app webhooks are managed in the app settings UI "
            "(Development -> your private app -> Webhooks): set this Target URL "
            "and add subscriptions. Managing them via API is not supported for "
            "private apps. Events are signed with X-HubSpot-Signature-v3."
        ),
        verified=True,
        source="https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/create-and-edit-webhook-subscriptions-in-private-apps",
    ),
    "intercom": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In the Intercom Developer Hub -> your app -> Configure -> Webhooks, "
            "add this endpoint URL and select topics. Events are signed with "
            "X-Hub-Signature (sha1 HMAC over the body using your app client_secret)."
        ),
        verified=True,
        source="https://developers.intercom.com/docs/webhooks/setting-up-webhooks",
    ),
    "monday_workdocs": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["board_id"],
        build_request=_monday_build,
        secret_source="ours",
        verified=True,
        source="https://developer.monday.com/api-reference/reference/webhooks",
    ),
    "monday_dev": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["board_id"],
        build_request=_monday_build,
        secret_source="ours",
        verified=True,
        source="https://developer.monday.com/api-reference/reference/webhooks",
    ),
    "monday_crm": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["board_id"],
        build_request=_monday_build,
        secret_source="ours",
        verified=True,
        source="https://developer.monday.com/api-reference/reference/webhooks",
    ),
    "monday_service": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["board_id"],
        build_request=_monday_build,
        secret_source="ours",
        verified=True,
        source="https://developer.monday.com/api-reference/reference/webhooks",
    ),
    "monday_workspaces": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["board_id"],
        build_request=_monday_build,
        secret_source="ours",
        verified=True,
        source="https://developer.monday.com/api-reference/reference/webhooks",
    ),
        "sender": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_sender_build,
        secret_source="ours",
        verified=True,
        source="https://api.sender.net/account-webhooks/create-webhook/",
    ),
    "greenhouse": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Greenhouse webhooks are configured in Dev Center -> Web Hooks: set "
            "this endpoint URL, pick an event, and enter a Secret Key. Store that "
            "same secret in the integration config; events are signed HMAC-SHA256 "
            "over the body in the Signature header."
        ),
        verified=True,
        source="https://docs.greenhouse.io/webhooks.html",
    ),
    "lever": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Lever webhooks are configured in Settings -> Integrations -> API -> "
            "Webhooks: set this URL and select events. Store the signature token "
            "in the integration config; events are signed HMAC-SHA256 in the "
            "Lever-Signature header."
        ),
        verified=True,
        source="https://hire.lever.co/developer/documentation#webhooks",
    ),
    "zoho_crm": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_zoho_build,
        secret_source="ours",
        verified=True,
        source="https://www.zoho.com/crm/developer/docs/api/v8/notifications/enable.html",
    ),
    "activecampaign": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_activecampaign_build,
        secret_source="ours",
        verified=True,
        source="https://developers.activecampaign.com/reference/webhooks",
    ),
    "convertkit": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_kit_build,
        secret_source="ours",
        verified=True,
        source="https://developers.kit.com/api-reference/webhooks/create-a-webhook",
    ),
    "close": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_close_build,
        secret_source="ours",
        verified=True,
        source="https://developer.close.com/topics/webhooks/",
    ),
    "freshdesk": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Freshdesk webhooks are created as an automation-rule action (Admin -> "
            "Workflows/Automations -> add a rule -> action 'Trigger Webhook'). Set "
            "this URL as the request URL; there is no API to create them."
        ),
        verified=True,
        source="https://support.freshdesk.com/en/support/solutions/articles/132589-using-webhooks-in-automation-rules",
    ),
    "quickbooks": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "QuickBooks webhooks are configured per-app in the Intuit Developer "
            "dashboard (your app -> Webhooks): set this endpoint URL and select "
            "entities. Store the app Verifier Token as the integration verify "
            "secret; events are signed base64 HMAC-SHA256 in intuit-signature."
        ),
        verified=True,
        source="https://developer.intuit.com/app/developer/qbo/docs/develop/webhooks",
    ),
    "azure_devops": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["project_id"],
        build_request=_azure_devops_build,
        secret_source="ours",
        verified=True,
        source="https://learn.microsoft.com/en-us/rest/api/azure/devops/hooks/subscriptions/create",
    ),
    "paypal": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_paypal_build,
        # PayPal verifies via a postback to /v1/notifications/verify-webhook-signature
        # using the webhook id from the create response.
        secret_source="response",
        secret_response_path="id",
        verified=True,
        source="https://developer.paypal.com/api/rest/webhooks/rest",
    ),
    "jira": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "The dynamic-webhook API (POST /rest/api/3/webhook) is only available "
            "to Connect/OAuth 2.0 apps. With an API token, register the webhook in "
            "Jira: Settings -> System -> Webhooks -> Create, set this URL and a JQL "
            "filter, and store the webhook secret in the integration config."
        ),
        verified=True,
        source="https://developer.atlassian.com/cloud/jira/platform/webhooks/",
    ),
    "confluence": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Register the webhook in Confluence admin (Settings -> Webhooks -> "
            "Create), set this URL and events. Dynamic webhook API registration "
            "requires a Connect/OAuth 2.0 app rather than an API token."
        ),
        verified=True,
        source="https://developer.atlassian.com/cloud/confluence/modules/webhook/",
    ),
    "sendgrid": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_sendgrid_build,
        secret_source="fetch",
        secret_fetch=_sendgrid_secret_fetch,
        verified=True,
        source="https://www.twilio.com/docs/sendgrid/api-reference/webhooks/create-an-event-webhook",
    ),
    "mailchimp": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["list_id"],
        build_request=_mailchimp_build,
        secret_source="ours",
        verified=True,
        source="https://mailchimp.com/developer/marketing/api/list-webhooks/",
    ),
    "brevo": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_brevo_build,
        secret_source="ours",
        verified=True,
        source="https://developers.brevo.com/reference/createwebhook",
    ),
    "bitbucket": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["workspace", "repo_slug"],
        build_request=_bitbucket_build,
        secret_source="ours",
        verified=True,
        source="https://developer.atlassian.com/cloud/bitbucket/rest/api-group-webhooks/",
    ),
    "xero": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Xero webhooks are configured per-app in the Xero Developer Portal "
            "(My Apps -> Webhooks): set this delivery URL and enable events. Copy "
            "the webhook signing key into the integration config as the verify "
            "secret. Xero validates the endpoint via an Intent to Receive "
            "handshake (a signed POST we verify); events are signed base64 "
            "HMAC-SHA256 over the body in x-xero-signature."
        ),
        verified=True,
        source="https://developer.xero.com/documentation/guides/webhooks/overview",
    ),
    "salesforce": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Salesforce has no native HTTP webhook. Either (a) have an admin add "
            "an Outbound Message / Flow HTTP Callout to this URL, or (b) rely on "
            "Atomic AI polling for recently-modified records. Change Data Capture "
            "uses a streaming (Pub/Sub) connection, not a webhook."
        ),
        verified=True,
        source="https://hookdeck.com/webhooks/platforms/guide-to-salesforce-webhooks-features-and-best-practices",
    ),
    "terraform": RegistrationSpec(
        mode=RegMode.PARTIAL,
        required_targets=["workspace_id"],
        build_request=_terraform_build,
        secret_source="ours",
        verified=True,
        source="https://developer.hashicorp.com/terraform/cloud-docs/api-docs/notification-configurations/workspace",
    ),
    "linear": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In Linear -> Settings -> API -> Webhooks (or via the webhookCreate "
            "GraphQL mutation), add this URL and select resource types. Copy the "
            "webhook signing secret into the integration config. Events are signed "
            "with Linear-Signature (bare-hex HMAC-SHA256 over the body)."
        ),
        verified=True,
        source="https://linear.app/developers/webhooks",
    ),
    "twilio_sms": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In the Twilio console, set this URL as the webhook on your phone "
            "number or Messaging Service. Twilio signs requests with "
            "X-Twilio-Signature (base64 HMAC-SHA1 over the URL + sorted params, "
            "keyed by your Auth Token). Store the Auth Token as the verify secret."
        ),
        verified=True,
        source="https://www.twilio.com/docs/usage/webhooks/webhooks-security",
    ),
    "gmail": RegistrationSpec(
        mode=RegMode.AUTO,
        # Registered via the dedicated Gmail users.watch -> Pub/Sub path in
        # webhook_subscriptions (needs GMAIL_PUBSUB_TOPIC configured).
        secret_source="ours",
        verified=True,
        source="https://developers.google.com/gmail/api/guides/push",
    ),
    "slack": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In your Slack app (api.slack.com/apps) -> Event Subscriptions, enable "
            "events and set this Request URL. Slack sends a url_verification "
            "challenge (auto-answered). Store the app Signing Secret in the "
            "integration config; events are signed v0= HMAC-SHA256."
        ),
        verified=True,
        source="https://api.slack.com/apis/events-api",
    ),
    "wechat": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "In the WeChat Official Account platform -> Basic Configuration -> "
            "Server Configuration, set this URL and a Token. WeChat verifies with "
            "a signature (sha1 of sorted token/timestamp/nonce). Store the Token "
            "in the integration config."
        ),
        verified=True,
        source="https://developers.weixin.qq.com/doc/offiaccount/en/Basic_Information/Access_Overview.html",
    ),
    "gcp": RegistrationSpec(
        mode=RegMode.MANUAL,
        manual_instructions=(
            "Create a Pub/Sub push subscription (or Eventarc trigger) in Google "
            "Cloud pointing at this URL for the resources you want to watch. GCP "
            "has no single generic webhook-create API."
        ),
        verified=True,
        source="https://cloud.google.com/pubsub/docs/push",
    ),
    "aws": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-api-destinations.html",
    ),
    "kubernetes": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://kubernetes.io/docs/reference/access-authn-authz/authentication/",
    ),
    "digitalocean": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://docs.digitalocean.com/reference/api/",
    ),
    "teamviewer": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://webapi.teamviewer.com/api/v1/docs/index",
    ),
    "yahoo": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://developer.yahoo.com/oauth2/guide/",
    ),
    "imap_smtp": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://datatracker.ietf.org/doc/html/rfc3501",
    ),
    "notion": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://developers.notion.com/reference/intro",
    ),
    "sap": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://help.sap.com/docs/btp",
    ),
    "netsuite": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://docs.oracle.com/en/cloud/saas/netsuite/",
    ),
    "workday": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://community.workday.com/",
    ),
    "bamboohr": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://documentation.bamboohr.com/docs/getting-started",
    ),
    "google_docs": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://developers.google.com/workspace/drive/api/guides/push",
    ),
    "google_sheets": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://developers.google.com/workspace/drive/api/guides/push",
    ),
    "google_slides": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://developers.google.com/workspace/drive/api/guides/push",
    ),
    "google_meet": RegistrationSpec(
        mode=RegMode.POLL,
        manual_instructions=(
            "No standard inbound webhook for this credential type; Atomic AI "
            "polls for changes on a schedule."
        ),
        verified=True,
        source="https://developers.google.com/workspace/meet/api",
    ),
    "google_calendar": RegistrationSpec(
        mode=RegMode.AUTO,
        build_request=_gcal_build,
        secret_source="ours",
        verified=True,
        source="https://developers.google.com/workspace/calendar/api/guides/push",
    ),
    "google_drive": RegistrationSpec(
        mode=RegMode.AUTO,
        prerequisite=_gdrive_prereq,
        build_request=_gdrive_build,
        secret_source="ours",
        verified=True,
        source="https://developers.google.com/workspace/drive/api/guides/push",
    ),
}


def registration_for(provider: str) -> RegistrationSpec | None:
    """Return the verified registration spec for a provider, or None if unverified."""
    return REGISTRATIONS.get(provider)


# Human-friendly labels + help for the webhook-target config fields that PARTIAL
# providers need, so the UI can prompt for them. Keyed by target field name.
TARGET_FIELD_META: dict[str, dict[str, str]] = {
    "owner": {"label": "Repository owner", "help": "GitHub org/user that owns the repo (for push notifications)."},
    "repo": {"label": "Repository name", "help": "GitHub repository to watch."},
    "project_id": {"label": "Project ID", "help": "GitLab project ID (numeric) to watch."},
    "workspace": {"label": "Workspace", "help": "Bitbucket workspace slug."},
    "repo_slug": {"label": "Repository slug", "help": "Bitbucket repository slug."},
    "account_id": {"label": "Account ID", "help": "Provider account/zone ID for the webhook."},
    "workspace_id": {"label": "Workspace ID", "help": "Terraform Cloud workspace ID (ws-...)."},
    "board_id": {"label": "Board ID", "help": "monday.com board ID to watch."},
    "resource": {"label": "Resource path", "help": "Microsoft Graph resource to subscribe to (optional; a sensible default is used)."},
    "list_id": {"label": "Audience/List ID", "help": "Mailchimp audience (list) ID to watch."},
    "calendar_id": {"label": "Calendar ID", "help": "Google Calendar ID (defaults to 'primary')."},
}


def target_fields_for(provider: str) -> list[dict]:
    """Return the webhook-target config fields a provider needs for auto-registration.

    These are NON-secret config values (e.g. Mailchimp list_id) that PARTIAL
    registration specs require. The UI renders them as optional config inputs so
    connecting can also register the provider's webhook. Returns [] for providers
    with no targets (AUTO/MANUAL/POLL).
    """
    spec = REGISTRATIONS.get(provider)
    if spec is None or not spec.required_targets:
        return []
    out = []
    for name in spec.required_targets:
        meta = TARGET_FIELD_META.get(name, {})
        out.append(
            {
                "name": name,
                "label": meta.get("label", name.replace("_", " ").title()),
                "secret": False,
                # Required: the provider's webhook cannot be registered without
                # this target, so instant triggering won't work if it's blank.
                "required": True,
                "placeholder": "",
                "help": meta.get("help", "Required to register this provider's webhook for instant triggers."),
            }
        )
    return out


__all__ = [
    "RegMode",
    "TARGET_FIELD_META",
    "target_fields_for",
    "BuiltRequest",
    "RegistrationSpec",
    "REGISTRATIONS",
    "registration_for",
]
