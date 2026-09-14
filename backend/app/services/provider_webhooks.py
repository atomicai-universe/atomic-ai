"""Provider trigger profiles — how each of the 74 providers triggers an agent run.

Instant automation needs a way for a real-world event (an email arrives, a
message is posted, a payment succeeds) to trigger an agent run. Providers offer
three trigger mechanisms; each provider maps to a :class:`TriggerProfile`:

- ``PUSH`` — the provider calls our webhook when an event happens (Slack Events,
  Stripe, GitHub, Twilio, Zoom, Calendly, monday.com, Intercom, etc.). We expose
  a public endpoint per integration and verify the payload.
- ``PUBSUB`` — Gmail's model: we call ``users.watch`` to have Gmail publish
  change notifications to a Google Cloud Pub/Sub topic, whose push subscription
  targets our webhook. Requires a Pub/Sub topic (configured via env).
- ``POLL`` — no push available (IMAP, some HR/ERP systems); a scheduler checks
  for new items on an interval and triggers a run when something changed.

Verification (how we authenticate an inbound webhook is genuinely from the
provider) is captured by :class:`VerifyMethod`:

- ``HMAC`` — signature header over the raw body with a shared secret
  (Slack, Stripe, GitHub, ...).
- ``TOKEN`` — a shared secret/verification token compared in a header or query
  (Pub/Sub OIDC-less token, monday.com, generic).
- ``NONE`` — no verification available; the endpoint still requires the correct
  per-integration secret path segment (defense in depth), and payloads are
  treated as untrusted input only.

This module is pure data + helpers; no network, no secrets at import.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TriggerKind(StrEnum):
    PUSH = "push"
    PUBSUB = "pubsub"
    POLL = "poll"


class VerifyMethod(StrEnum):
    HMAC = "hmac"
    TOKEN = "token"
    NONE = "none"


@dataclass(frozen=True)
class TriggerProfile:
    """How a provider triggers agent runs.

    Attributes:
        kind: push / pubsub / poll.
        verify: how inbound webhooks are authenticated.
        signature_header: header carrying the HMAC signature (for HMAC verify).
        signature_prefix: optional prefix in the signature header (e.g. "sha256=").
        poll_interval_s: polling interval when kind is POLL.
        docs: human note (unused at runtime).
    """

    kind: TriggerKind
    verify: VerifyMethod = VerifyMethod.TOKEN
    signature_header: str = ""
    signature_prefix: str = ""
    poll_interval_s: int = 300
    docs: str = ""
    # Named custom signature scheme when the provider does NOT sign a plain HMAC
    # over the raw body. One of: "" (plain HMAC over body), "stripe" (t=..,v1=..
    # over "{t}.{body}"), "slack" (v0:{t}:{body}). Verified per provider docs.
    scheme: str = ""


PUSH = TriggerKind.PUSH
PUBSUB = TriggerKind.PUBSUB
POLL = TriggerKind.POLL
HMAC = VerifyMethod.HMAC
TOKEN = VerifyMethod.TOKEN
NONE = VerifyMethod.NONE


TRIGGER_PROFILES: dict[str, TriggerProfile] = {
    # ---- Email ----
    "gmail": TriggerProfile(PUBSUB, TOKEN, docs="Gmail users.watch -> Pub/Sub push"),
    "outlook": TriggerProfile(PUSH, TOKEN, scheme="graph", docs="Microsoft Graph change notifications"),
    "yahoo": TriggerProfile(POLL, NONE, poll_interval_s=300),
    "imap_smtp": TriggerProfile(POLL, NONE, poll_interval_s=180),
    # ---- Email marketing (mostly push webhooks) ----
    "sender": TriggerProfile(PUSH, TOKEN),
    "mailgun": TriggerProfile(PUSH, HMAC, signature_header="", scheme="mailgun"),
    "sendgrid": TriggerProfile(PUSH, HMAC, signature_header="X-Twilio-Email-Event-Webhook-Signature", scheme="sendgrid"),
    "mailchimp": TriggerProfile(PUSH, NONE, scheme="mailchimp_get"),
    "brevo": TriggerProfile(PUSH, NONE),
    "convertkit": TriggerProfile(PUSH, TOKEN),
    "activecampaign": TriggerProfile(PUSH, TOKEN),
    # ---- Social & messaging ----
    "facebook": TriggerProfile(PUSH, HMAC, signature_header="X-Hub-Signature-256", signature_prefix="sha256="),
    "instagram": TriggerProfile(PUSH, HMAC, signature_header="X-Hub-Signature-256", signature_prefix="sha256="),
    "telegram": TriggerProfile(PUSH, TOKEN, signature_header="X-Telegram-Bot-Api-Secret-Token", docs="setWebhook with secret_token"),
    "whatsapp": TriggerProfile(PUSH, HMAC, signature_header="X-Hub-Signature-256", signature_prefix="sha256="),
    "line": TriggerProfile(PUSH, HMAC, signature_header="X-Line-Signature"),
    "wechat": TriggerProfile(PUSH, TOKEN),
    "twilio_sms": TriggerProfile(PUSH, HMAC, signature_header="X-Twilio-Signature", scheme="twilio"),
    # ---- Office & productivity (Graph push / Drive push / poll) ----
    "word": TriggerProfile(PUSH, TOKEN, scheme="graph"),
    "powerpoint": TriggerProfile(PUSH, TOKEN, scheme="graph"),
    "excel": TriggerProfile(PUSH, TOKEN, scheme="graph"),
    "google_docs": TriggerProfile(POLL, NONE, poll_interval_s=300),
    "google_sheets": TriggerProfile(POLL, NONE, poll_interval_s=300),
    "google_slides": TriggerProfile(POLL, NONE, poll_interval_s=300),
    "onedrive": TriggerProfile(PUSH, TOKEN, scheme="graph"),
    "google_drive": TriggerProfile(PUSH, TOKEN, signature_header="X-Goog-Channel-Token", scheme="google_watch", docs="Drive changes.watch push channel"),
    "monday_workdocs": TriggerProfile(PUSH, TOKEN, scheme="monday"),
    # ---- Developer & issue trackers ----
    "github": TriggerProfile(PUSH, HMAC, signature_header="X-Hub-Signature-256", signature_prefix="sha256="),
    "gitlab": TriggerProfile(PUSH, TOKEN, signature_header="X-Gitlab-Token"),
    "jira": TriggerProfile(PUSH, TOKEN),
    "linear": TriggerProfile(PUSH, HMAC, signature_header="Linear-Signature"),
    "monday_dev": TriggerProfile(PUSH, TOKEN, scheme="monday"),
    "bitbucket": TriggerProfile(PUSH, HMAC, signature_header="X-Hub-Signature", signature_prefix="sha256="),
    "azure_devops": TriggerProfile(PUSH, TOKEN),
    "asana": TriggerProfile(PUSH, HMAC, signature_header="X-Hook-Signature", scheme="asana"),
    "trello": TriggerProfile(PUSH, HMAC, signature_header="X-Trello-Webhook", scheme="trello"),
    # ---- CRM & sales ----
    "salesforce": TriggerProfile(POLL, NONE, poll_interval_s=300, docs="No native HTTP webhook; poll SOQL for recent changes (or admin Outbound Message)"),
    "hubspot": TriggerProfile(PUSH, HMAC, signature_header="X-HubSpot-Signature-v3"),
    "pipedrive": TriggerProfile(PUSH, TOKEN),
    "monday_crm": TriggerProfile(PUSH, TOKEN, scheme="monday"),
    "zoho_crm": TriggerProfile(PUSH, TOKEN, scheme="zoho_watch"),
    "close": TriggerProfile(PUSH, TOKEN),
    # ---- Support & knowledge ----
    "zendesk": TriggerProfile(PUSH, HMAC, signature_header="X-Zendesk-Webhook-Signature", scheme="zendesk"),
    "intercom": TriggerProfile(PUSH, HMAC, signature_header="X-Hub-Signature", signature_prefix="sha1="),
    "freshdesk": TriggerProfile(PUSH, TOKEN),
    "notion": TriggerProfile(POLL, NONE, poll_interval_s=300),
    "confluence": TriggerProfile(PUSH, TOKEN),
    "monday_service": TriggerProfile(PUSH, TOKEN, scheme="monday"),
    # ---- ERP & financial ----
    "quickbooks": TriggerProfile(PUSH, HMAC, signature_header="intuit-signature"),
    "xero": TriggerProfile(PUSH, HMAC, signature_header="x-xero-signature"),
    "stripe": TriggerProfile(PUSH, HMAC, signature_header="Stripe-Signature", scheme="stripe"),
    "sap": TriggerProfile(POLL, NONE, poll_interval_s=600),
    "netsuite": TriggerProfile(POLL, NONE, poll_interval_s=600),
    "paypal": TriggerProfile(PUSH, TOKEN, scheme="paypal_postback"),
    # ---- Team chat & meetings ----
    "slack": TriggerProfile(PUSH, HMAC, signature_header="X-Slack-Signature", scheme="slack"),
    "teams": TriggerProfile(PUSH, TOKEN, scheme="graph"),
    "discord": TriggerProfile(POLL, NONE, poll_interval_s=300, docs="Discord bots use the Gateway (WebSocket), not inbound webhooks"),
    "zoom": TriggerProfile(PUSH, HMAC, signature_header="x-zm-signature", scheme="zoom"),
    "google_meet": TriggerProfile(POLL, NONE, poll_interval_s=300),
    "teamviewer": TriggerProfile(POLL, NONE, poll_interval_s=600),
    "monday_workspaces": TriggerProfile(PUSH, TOKEN, scheme="monday"),
    # ---- Cloud & DevOps ----
    "aws": TriggerProfile(POLL, NONE, poll_interval_s=600),
    "gcp": TriggerProfile(PUSH, TOKEN),
    "cloudflare": TriggerProfile(POLL, NONE, poll_interval_s=600),
    "terraform": TriggerProfile(PUSH, HMAC, signature_header="X-TFE-Notification-Signature", scheme="terraform"),
    "digitalocean": TriggerProfile(POLL, NONE, poll_interval_s=600),
    "kubernetes": TriggerProfile(POLL, NONE, poll_interval_s=300),
    # ---- HR & recruiting ----
    "workday": TriggerProfile(POLL, NONE, poll_interval_s=900),
    "bamboohr": TriggerProfile(POLL, NONE, poll_interval_s=900),
    "greenhouse": TriggerProfile(PUSH, HMAC, signature_header="Signature"),
    "lever": TriggerProfile(PUSH, HMAC, signature_header="Lever-Signature"),
    # ---- Calendar & scheduling ----
    "google_calendar": TriggerProfile(PUSH, TOKEN, signature_header="X-Goog-Channel-Token", scheme="google_watch", docs="Calendar events.watch push channel"),
    "outlook_calendar": TriggerProfile(PUSH, TOKEN, scheme="graph"),
    "calendly": TriggerProfile(PUSH, HMAC, signature_header="Calendly-Webhook-Signature"),
}


def trigger_for(provider_name: str) -> TriggerProfile:
    """Return the trigger profile for a provider (polling fallback if unknown)."""
    return TRIGGER_PROFILES.get(provider_name, TriggerProfile(POLL, NONE, poll_interval_s=600))


def push_providers() -> list[str]:
    return [p for p, t in TRIGGER_PROFILES.items() if t.kind in (PUSH, PUBSUB)]


def poll_providers() -> list[str]:
    return [p for p, t in TRIGGER_PROFILES.items() if t.kind is POLL]


__all__ = [
    "TriggerKind", "VerifyMethod", "TriggerProfile", "TRIGGER_PROFILES",
    "trigger_for", "push_providers", "poll_providers",
]
