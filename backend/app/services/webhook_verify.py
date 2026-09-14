"""Webhook signature/token verification (per-provider).

Given a provider's :class:`TriggerProfile`, the raw request body, headers, and
the per-integration secret, decide whether an inbound webhook is authentic.

- HMAC: recompute the signature over the raw body with the shared secret and
  compare in constant time to the provider's signature header.
- TOKEN: compare a shared verification token (from a header or the URL secret
  segment) in constant time.
- NONE: no provider signature available — authenticity rests on the unguessable
  per-integration secret in the URL path (the caller enforces that match).

All comparisons are constant-time. Secrets are never logged.
"""

from __future__ import annotations

import hashlib
import hmac

from app.services.provider_webhooks import TriggerProfile, VerifyMethod


def _ct_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


def verify_webhook(
    *,
    profile: TriggerProfile,
    secret: str,
    raw_body: bytes,
    headers: dict[str, str],
    url_secret: str,
    expected_url_secret: str,
    callback_url: str = "",
) -> bool:
    """Return True if the inbound webhook is authentic for this integration.

    ``url_secret`` is the unguessable secret segment from the webhook URL path;
    it must always match ``expected_url_secret`` (defense in depth for every
    verify method). Beyond that:

    - HMAC: the provider signs the raw body; recompute and compare.
    - TOKEN: compare ``secret`` against the token the provider sends (in a
      header named by the profile, or falls back to the URL secret match).
    - NONE: URL secret match is the only check.
    """
    # The URL path secret must always match (constant-time).
    if not _ct_eq(url_secret, expected_url_secret):
        return False

    if profile.verify is VerifyMethod.NONE:
        return True

    # Normalize headers to case-insensitive lookup.
    lower = {k.lower(): v for k, v in headers.items()}

    if profile.verify is VerifyMethod.HMAC:
        # Mailgun signs in the body (no signature header); handle before the
        # header presence guard below.
        if profile.scheme == "mailgun":
            return _verify_mailgun(secret, raw_body)
        header_name = (profile.signature_header or "").lower()
        provided = lower.get(header_name, "")
        if not provided or not secret:
            return False

        # Provider-specific signature schemes that do NOT sign the raw body
        # directly. Verified against each provider's official docs.
        if profile.scheme == "stripe":
            return _verify_stripe(secret, raw_body, provided)
        if profile.scheme == "slack":
            return _verify_slack(secret, raw_body, provided, lower)
        if profile.scheme == "trello":
            return _verify_trello(secret, raw_body, provided, callback_url)
        if profile.scheme == "mailgun":
            return _verify_mailgun(secret, raw_body)
        if profile.scheme == "zoom":
            return _verify_zoom(secret, raw_body, provided, lower)
        if profile.scheme == "zendesk":
            return _verify_zendesk(secret, raw_body, provided, lower)
        if profile.scheme == "terraform":
            return _verify_terraform(secret, raw_body, provided)
        if profile.scheme == "twilio":
            return _verify_twilio(secret, raw_body, provided, callback_url)
        if profile.scheme == "sendgrid":
            return _verify_sendgrid(secret, raw_body, provided, lower)
        # Strip a known prefix (e.g. "sha256=") if present.
        if profile.signature_prefix and provided.startswith(profile.signature_prefix):
            provided_sig = provided[len(profile.signature_prefix):]
        else:
            provided_sig = provided
        # SHA1 for a small set of providers; SHA256 default.
        algo = hashlib.sha1 if "sha1" in profile.signature_prefix else hashlib.sha256
        computed = hmac.new(secret.encode("utf-8"), raw_body, algo).hexdigest()
        # Some providers base64 rather than hex; accept either form.
        import base64
        computed_b64 = base64.b64encode(
            hmac.new(secret.encode("utf-8"), raw_body, algo).digest()
        ).decode()
        return _ct_eq(provided_sig, computed) or _ct_eq(provided_sig, computed_b64)

    if profile.verify is VerifyMethod.TOKEN:
        # Microsoft Graph: notifications carry `clientState` in the body matching
        # the value set at subscription creation. Verify that (no HMAC header).
        if profile.scheme == "graph":
            return _verify_graph_client_state(secret, raw_body)
        # Zoho CRM notifications echo the `token` we set at watch creation.
        if profile.scheme == "zoho_watch":
            return _verify_zoho_token(secret, raw_body)
        # Token may arrive in a signature header or simply rely on the URL secret.
        header_name = (profile.signature_header or "").lower()
        if header_name:
            return _ct_eq(lower.get(header_name, ""), secret)
        # No specific header — the URL secret match above is sufficient.
        return True

    return False


def _verify_stripe(secret: str, raw_body: bytes, header: str, tolerance_s: int = 300) -> bool:
    """Verify Stripe's ``t=..,v1=..`` signature over ``{t}.{body}`` (SHA256).

    Source: https://docs.stripe.com/webhooks/signature — signed_payload is
    ``"{timestamp}.{raw_body}"``; compare v1 in constant time and enforce a
    5-minute timestamp tolerance to reject replays.
    """
    import time

    parts = dict(
        p.split("=", 1) for p in header.split(",") if "=" in p
    )
    t = parts.get("t")
    v1 = parts.get("v1")
    if not t or not v1:
        return False
    try:
        ts = int(t)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > tolerance_s:
        return False
    signed_payload = f"{t}.".encode() + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return _ct_eq(v1, expected)


def _verify_slack(secret: str, raw_body: bytes, header: str, lower_headers: dict, tolerance_s: int = 300) -> bool:
    """Verify Slack's ``v0=`` signature over ``v0:{timestamp}:{body}`` (SHA256).

    Source: https://api.slack.com/authentication/verifying-requests-from-slack —
    basestring is ``"v0:{X-Slack-Request-Timestamp}:{raw_body}"``; compare in
    constant time and enforce a timestamp tolerance.
    """
    import time

    ts = lower_headers.get("x-slack-request-timestamp", "")
    if not ts:
        return False
    try:
        if abs(int(time.time()) - int(ts)) > tolerance_s:
            return False
    except ValueError:
        return False
    basestring = f"v0:{ts}:".encode() + raw_body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return _ct_eq(header, expected)


def _verify_trello(secret: str, raw_body: bytes, header: str, callback_url: str) -> bool:
    """Verify Trello's ``X-Trello-Webhook`` = base64(HMAC-SHA1(secret, body + callbackURL)).

    Source: https://developers.trello.com/webhooks — the hashed content is the
    concatenation of the full request body and the callbackURL exactly as
    provided at creation. The secret is the Trello API "secret" (OAuth secret /
    app secret). We compare in constant time.
    """
    import base64

    if not callback_url or not secret:
        return False
    mac = hmac.new(secret.encode("utf-8"), raw_body + callback_url.encode("utf-8"), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode()
    return _ct_eq(header, expected)


def _verify_mailgun(signing_key: str, raw_body: bytes) -> bool:
    """Verify Mailgun's body-embedded signature.

    Source: https://documentation.mailgun.com/docs/mailgun/user-manual/webhooks/securing-webhooks
    The JSON body carries ``signature: {token, timestamp, signature}`` where
    ``signature == HMAC_SHA256(signing_key, "{timestamp}{token}")``. Compared
    in constant time. (Subaccount events include parent-signature; we verify the
    primary signature block.)
    """
    import json

    if not signing_key:
        return False
    try:
        body = json.loads(raw_body)
    except Exception:  # noqa: BLE001
        return False
    sig = body.get("signature") if isinstance(body, dict) else None
    if not isinstance(sig, dict):
        return False
    token = sig.get("token", "")
    timestamp = sig.get("timestamp", "")
    provided = sig.get("signature", "")
    expected = hmac.new(
        signing_key.encode("utf-8"), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()
    return _ct_eq(provided, expected)


def _verify_zoom(secret: str, raw_body: bytes, header: str, lower_headers: dict) -> bool:
    """Verify Zoom's ``x-zm-signature`` = ``v0=HMAC_SHA256(secret, "v0:{ts}:{body}")``.

    Source: https://developers.zoom.us/docs/api/webhooks/ — message string is
    ``"v0:{x-zm-request-timestamp}:{raw_body}"`` signed with the app's Webhook
    Secret Token; header format ``v0=<hex>``. The url_validation challenge is
    handled separately at the gateway.
    """
    ts = lower_headers.get("x-zm-request-timestamp", "")
    if not ts or not secret:
        return False
    message = f"v0:{ts}:".encode() + raw_body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return _ct_eq(header, expected)


def _verify_graph_client_state(secret: str, raw_body: bytes) -> bool:
    """Verify Microsoft Graph change notifications via ``clientState``.

    Source: https://learn.microsoft.com/en-us/graph/change-notifications-overview
    Graph does not sign notifications with an HMAC header; instead each
    notification in ``value[]`` echoes the ``clientState`` we set when creating
    the subscription. We compare it (constant time) to the stored secret. The
    ``validationToken`` handshake is answered separately at the gateway.
    """
    import json

    if not secret:
        return False
    try:
        body = json.loads(raw_body)
    except Exception:  # noqa: BLE001
        return False
    values = body.get("value") if isinstance(body, dict) else None
    if not isinstance(values, list) or not values:
        return False
    # Every notification entry must carry our clientState.
    return all(
        isinstance(v, dict) and _ct_eq(str(v.get("clientState", "")), secret)
        for v in values
    )


def _verify_zendesk(secret: str, raw_body: bytes, header: str, lower_headers: dict) -> bool:
    """Verify Zendesk's ``X-Zendesk-Webhook-Signature`` = base64(HMAC-SHA256(secret, ts+body)).

    Source: https://developer.zendesk.com/documentation/webhooks/verifying/ — the
    signed message is the timestamp header value concatenated with the raw body;
    the signing secret is the webhook's generated secret. Compared in constant time.
    """
    import base64

    ts = lower_headers.get("x-zendesk-webhook-signature-timestamp", "")
    if not ts or not secret:
        return False
    message = ts.encode() + raw_body
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    ).decode()
    return _ct_eq(header, expected)


def _verify_terraform(token: str, raw_body: bytes, header: str) -> bool:
    """Verify Terraform Cloud's X-TFE-Notification-Signature = HMAC-SHA512(token, body) hex.

    Source: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/notification-configurations
    """
    if not token:
        return False
    expected = hmac.new(token.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return _ct_eq(header, expected)


def _verify_twilio(auth_token: str, raw_body: bytes, header: str, callback_url: str) -> bool:
    """Verify Twilio's X-Twilio-Signature.

    Source: https://www.twilio.com/docs/usage/webhooks/webhooks-security — Twilio
    concatenates the full request URL with the alphabetically-sorted POST params
    (key then value, no delimiters) and signs with HMAC-SHA1 keyed by the Auth
    Token, base64-encoded. For JSON bodies Twilio signs URL + raw body instead.
    """
    import base64
    from urllib.parse import parse_qsl

    if not auth_token or not callback_url:
        return False
    # Form-encoded params: sort by key and append key+value.
    signed = callback_url
    body_text = raw_body.decode("utf-8", "replace")
    if body_text and "=" in body_text and "{" not in body_text[:1]:
        params = parse_qsl(body_text, keep_blank_values=True)
        for k, v in sorted(params):
            signed += k + v
        message = signed.encode("utf-8")
    else:
        # JSON body: Twilio signs URL + raw body.
        message = callback_url.encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), message, hashlib.sha1).digest()
    ).decode()
    return _ct_eq(header, expected)


def _verify_sendgrid(public_key_b64: str, raw_body: bytes, signature_b64: str, lower_headers: dict) -> bool:
    """Verify SendGrid's ECDSA signed Event Webhook.

    Source: https://www.twilio.com/docs/sendgrid/for-developers/tracking-events/getting-started-event-webhook-security-features
    SendGrid signs ``{timestamp}{payload}`` with an ECDSA (P-256) private key.
    We verify ``X-Twilio-Email-Event-Webhook-Signature`` (base64 DER) against the
    base64 DER public key using ``X-Twilio-Email-Event-Webhook-Timestamp``.
    ``public_key_b64`` (the stored verify secret) is the base64 public key.
    """
    import base64

    if not public_key_b64 or not signature_b64:
        return False
    timestamp = lower_headers.get("x-twilio-email-event-webhook-timestamp", "")
    if not timestamp:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: F401
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        from cryptography.hazmat.primitives import hashes
        from cryptography.exceptions import InvalidSignature

        pub_der = base64.b64decode(public_key_b64)
        public_key = load_der_public_key(pub_der)
        signature = base64.b64decode(signature_b64)
        message = timestamp.encode("utf-8") + raw_body
        public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, Exception):  # noqa: BLE001 - any failure = not verified
        return False


def _verify_zoho_token(secret: str, raw_body: bytes) -> bool:
    """Verify Zoho CRM notifications by the echoed ``token``.

    Source: https://www.zoho.com/crm/developer/docs/api/v8/notifications/enable.html
    The watch is created with a ``token``; each notification body echoes it.
    Compared constant-time. Falls back to True on the URL-secret match (handled
    by the caller) when the body has no token.
    """
    import json

    if not secret:
        return False
    try:
        body = json.loads(raw_body) if raw_body else {}
    except Exception:  # noqa: BLE001
        return False
    token = body.get("token") if isinstance(body, dict) else None
    if token is None:
        # No token in payload — rely on the (already-checked) URL secret.
        return True
    return _ct_eq(str(token), secret)


__all__ = ["verify_webhook"]
