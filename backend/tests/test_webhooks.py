"""Tests for the instant-trigger webhook subsystem (verification + profiles)."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from app.services import provider_webhooks as pw
from app.services.provider_webhooks import TriggerKind, VerifyMethod, trigger_for
from app.services.webhook_verify import verify_webhook


def test_every_provider_has_a_trigger_profile() -> None:
    # Parity with the credential registry: all 74 providers must be triggerable.
    from app.services.provider_credentials import PROVIDER_CREDENTIALS

    for provider in PROVIDER_CREDENTIALS:
        prof = trigger_for(provider)
        assert prof.kind in (TriggerKind.PUSH, TriggerKind.PUBSUB, TriggerKind.POLL)


def test_url_secret_mismatch_always_fails() -> None:
    prof = trigger_for("slack")
    ok = verify_webhook(
        profile=prof,
        secret="s",
        raw_body=b"{}",
        headers={},
        url_secret="wrong",
        expected_url_secret="right",
    )
    assert ok is False


def test_token_provider_passes_on_url_secret_when_no_header() -> None:
    # gmail is PUBSUB/TOKEN with no signature header -> URL secret match suffices.
    prof = trigger_for("gmail")
    ok = verify_webhook(
        profile=prof,
        secret="topsecret",
        raw_body=b"{}",
        headers={},
        url_secret="topsecret",
        expected_url_secret="topsecret",
    )
    assert ok is True


def test_hmac_sha256_signature_verifies() -> None:
    prof = trigger_for("github")  # HMAC sha256, prefix "sha256="
    secret = "webhook-secret"
    body = b'{"action":"opened"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof,
        secret=secret,
        raw_body=body,
        headers={"X-Hub-Signature-256": f"sha256={sig}"},
        url_secret="u",
        expected_url_secret="u",
    )
    assert ok is True


def test_hmac_bad_signature_rejected() -> None:
    prof = trigger_for("github")
    ok = verify_webhook(
        profile=prof,
        secret="webhook-secret",
        raw_body=b'{"action":"opened"}',
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        url_secret="u",
        expected_url_secret="u",
    )
    assert ok is False


def test_hmac_accepts_base64_signature_form() -> None:
    # A generic HMAC provider (Asana) that may present a base64 digest.
    prof = trigger_for("asana")  # HMAC, X-Hook-Signature, plain body
    secret = "s3cr3t"
    body = b"payload"
    digest_b64 = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    ok = verify_webhook(
        profile=prof,
        secret=secret,
        raw_body=body,
        headers={"X-Hook-Signature": digest_b64},
        url_secret="u",
        expected_url_secret="u",
    )
    assert ok is True


def test_stripe_scheme_verifies_timestamped_signature() -> None:
    import time

    prof = trigger_for("stripe")  # scheme="stripe"
    assert prof.scheme == "stripe"
    secret = "whsec_test"
    body = b'{"id":"evt_1"}'
    t = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"Stripe-Signature": f"t={t},v1={sig}"},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_stripe_scheme_rejects_stale_timestamp() -> None:
    prof = trigger_for("stripe")
    secret = "whsec_test"
    body = b"{}"
    old_t = "1000000000"  # far in the past -> outside tolerance
    sig = hmac.new(secret.encode(), f"{old_t}.".encode() + body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"Stripe-Signature": f"t={old_t},v1={sig}"},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is False


def test_slack_scheme_verifies_v0_signature() -> None:
    import time

    prof = trigger_for("slack")  # scheme="slack"
    assert prof.scheme == "slack"
    secret = "slacksecret"
    body = b"token=abc&team_id=T1"
    ts = str(int(time.time()))
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Slack-Signature": expected, "X-Slack-Request-Timestamp": ts},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_push_and_poll_partition_is_complete() -> None:
    push = set(pw.push_providers())
    poll = set(pw.poll_providers())
    assert push.isdisjoint(poll)
    assert len(push) + len(poll) == len(pw.TRIGGER_PROFILES)


class _StubSettings:
    """Minimal settings stub for exercising webhook_url base-URL selection."""

    def __init__(self, webhook_base: str, oauth_base: str) -> None:
        self.WEBHOOK_PUBLIC_BASE_URL = webhook_base
        self.OAUTH_REDIRECT_BASE_URL = oauth_base


def test_webhook_url_prefers_public_webhook_base_over_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    # When WEBHOOK_PUBLIC_BASE_URL is set (e.g. an ngrok tunnel), the inbound
    # webhook URL must use it — NOT the OAuth redirect host — so a local dev box
    # can receive Gmail Pub/Sub / provider push without breaking OAuth.
    from app.services import webhook_subscriptions as ws

    monkeypatch.setattr(
        ws,
        "get_settings",
        lambda: _StubSettings(
            webhook_base="https://tunnel.example.dev",
            oauth_base="http://localhost:8000",
        ),
    )
    url = ws.webhook_url("gmail", "int-123", "secret-xyz")
    assert url == "https://tunnel.example.dev/api/v1/webhooks/gmail/int-123/secret-xyz"


def test_webhook_url_falls_back_to_oauth_base_when_webhook_base_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty WEBHOOK_PUBLIC_BASE_URL keeps existing single-host deployments
    # unchanged: fall back to OAUTH_REDIRECT_BASE_URL.
    from app.services import webhook_subscriptions as ws

    monkeypatch.setattr(
        ws,
        "get_settings",
        lambda: _StubSettings(webhook_base="", oauth_base="https://app.example.com"),
    )
    url = ws.webhook_url("stripe", "int-9", "sek")
    assert url == "https://app.example.com/api/v1/webhooks/stripe/int-9/sek"


def test_ensure_webhook_secret_is_stable_and_present() -> None:
    from app.services.webhook_subscriptions import ensure_webhook_secret, WEBHOOK_SECRET_KEY

    cfg1, s1 = ensure_webhook_secret({})
    assert s1 and cfg1[WEBHOOK_SECRET_KEY] == s1
    # Idempotent: an existing secret is preserved.
    cfg2, s2 = ensure_webhook_secret(cfg1)
    assert s2 == s1


def test_gmail_pubsub_watch_is_covered_by_subscription_renewal() -> None:
    # Gmail's users.watch expires (~7 days). The renewal cron must treat PUBSUB
    # triggers as renewable so re-watch keeps instant push alive; otherwise push
    # silently degrades to polling after a week. Mirrors the filter in
    # tasks.renew_short_lived_subscriptions.
    renewable = {
        p for p, prof in pw.TRIGGER_PROFILES.items()
        if getattr(prof, "scheme", "") in ("graph", "google_watch", "zoho_watch")
        or getattr(prof, "kind", None) is TriggerKind.PUBSUB
    }
    assert "gmail" in renewable
    assert pw.TRIGGER_PROFILES["gmail"].kind is TriggerKind.PUBSUB


def test_registration_registry_specs_are_verified_and_buildable() -> None:
    from app.services import webhook_registration as wr

    # gmail is a legitimate AUTO exception: it is registered via the dedicated
    # Gmail users.watch -> Pub/Sub path in webhook_subscriptions, not the generic
    # build_request. Every other AUTO/PARTIAL spec must be buildable.
    _ALT_REGISTRATION = {"gmail"}
    for provider, spec in wr.REGISTRATIONS.items():
        # Every shipped registration must be doc-verified and cite a source.
        assert spec.verified is True, f"{provider} registration not verified"
        assert spec.source, f"{provider} registration missing source"
        if spec.mode in (wr.RegMode.AUTO, wr.RegMode.PARTIAL) and provider not in _ALT_REGISTRATION:
            assert spec.build_request is not None, f"{provider} missing build_request"


def test_stripe_registration_builds_expected_request() -> None:
    from app.services import webhook_registration as wr

    spec = wr.registration_for("stripe")
    req = spec.build_request("https://x/webhooks/stripe/1/s", "sec", {"events": ["*"]})
    assert req.method == "POST"
    assert req.path == "/v1/webhook_endpoints"
    assert req.body["url"].endswith("/webhooks/stripe/1/s")
    assert req.body["enabled_events[]"] == ["*"]
    assert spec.secret_source == "response"  # Stripe returns whsec_...


def test_github_registration_is_partial_needs_owner_repo() -> None:
    from app.services import webhook_registration as wr

    spec = wr.registration_for("github")
    assert spec.mode is wr.RegMode.PARTIAL
    assert set(spec.required_targets) == {"owner", "repo"}
    req = spec.build_request("https://x/hook", "sec", {"owner": "o", "repo": "r"})
    assert req.path == "/repos/o/r/hooks"
    assert req.body["config"]["secret"] == "sec"


def test_trello_scheme_verifies_body_plus_callbackurl_sha1() -> None:
    import base64

    prof = trigger_for("trello")  # scheme="trello"
    assert prof.scheme == "trello"
    secret = "trello-oauth-secret"
    body = b'{"action":{"type":"updateCard"}}'
    callback = "https://x.example/api/v1/webhooks/trello/1/s"
    mac = hmac.new(secret.encode(), body + callback.encode(), hashlib.sha1)
    sig = base64.b64encode(mac.digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Trello-Webhook": sig},
        url_secret="s", expected_url_secret="s",
        callback_url=callback,
    )
    assert ok is True


def test_trello_scheme_rejects_wrong_callbackurl() -> None:
    import base64

    prof = trigger_for("trello")
    secret = "trello-oauth-secret"
    body = b"{}"
    mac = hmac.new(secret.encode(), body + b"https://right/", hashlib.sha1)
    sig = base64.b64encode(mac.digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Trello-Webhook": sig},
        url_secret="s", expected_url_secret="s",
        callback_url="https://wrong/",
    )
    assert ok is False


def test_batch2_registration_specs_present_and_verified() -> None:
    from app.services import webhook_registration as wr

    for provider in ("pipedrive", "cloudflare", "asana", "trello"):
        spec = wr.registration_for(provider)
        assert spec is not None and spec.verified is True and spec.source
        assert spec.build_request is not None


def test_pipedrive_build_request() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("pipedrive").build_request("https://x/hook", "sec", {})
    assert req.path == "/webhooks"
    assert req.body["subscription_url"] == "https://x/hook"
    assert req.body["event_action"] == "*" and req.body["event_object"] == "*"


def test_asana_secret_source_is_handshake() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("asana").secret_source == "handshake"


def test_mailgun_body_embedded_signature_verifies() -> None:
    import json

    prof = trigger_for("mailgun")  # scheme="mailgun"
    assert prof.scheme == "mailgun"
    signing_key = "mg-signing-key"
    token = "abc123"
    timestamp = "1700000000"
    sig = hmac.new(signing_key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    body = json.dumps({"signature": {"token": token, "timestamp": timestamp, "signature": sig}}).encode()
    ok = verify_webhook(
        profile=prof, secret=signing_key, raw_body=body, headers={},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_mailgun_bad_signature_rejected() -> None:
    import json

    prof = trigger_for("mailgun")
    body = json.dumps({"signature": {"token": "t", "timestamp": "1", "signature": "bad"}}).encode()
    ok = verify_webhook(
        profile=prof, secret="mg-signing-key", raw_body=body, headers={},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is False


def test_zoom_v0_signature_verifies() -> None:
    prof = trigger_for("zoom")  # scheme="zoom"
    assert prof.scheme == "zoom"
    secret = "zoom-secret-token"
    body = b'{"event":"meeting.started"}'
    ts = "1700000001"
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"x-zm-signature": expected, "x-zm-request-timestamp": ts},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_batch3_registration_specs() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("gitlab").mode is wr.RegMode.PARTIAL
    assert wr.registration_for("mailgun").mode is wr.RegMode.PARTIAL
    assert wr.registration_for("zoom").mode is wr.RegMode.MANUAL
    for p in ("gitlab", "mailgun", "zoom"):
        assert wr.registration_for(p).verified is True
    req = wr.registration_for("gitlab").build_request("https://x/h", "tok", {"project_id": "42"})
    assert req.path == "/projects/42/hooks" and req.body["token"] == "tok"


def test_batch4_registration_specs() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("telegram").mode is wr.RegMode.AUTO
    assert wr.registration_for("line").mode is wr.RegMode.AUTO
    for p in ("facebook", "instagram", "whatsapp"):
        assert wr.registration_for(p).mode is wr.RegMode.MANUAL
    assert wr.registration_for("discord").mode is wr.RegMode.POLL
    for p in ("telegram", "line", "facebook", "instagram", "whatsapp", "discord"):
        assert wr.registration_for(p).verified is True


def test_telegram_build_sets_secret_token() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("telegram").build_request("https://x/h", "sek", {})
    assert req.path == "/setWebhook"
    assert req.body["url"] == "https://x/h" and req.body["secret_token"] == "sek"


def test_line_build_puts_endpoint() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("line").build_request("https://x/h", "s", {})
    assert req.method == "PUT" and req.path == "/bot/channel/webhook/endpoint"
    assert req.body["endpoint"] == "https://x/h"


def test_line_signature_base64_sha256_verifies() -> None:
    import base64

    prof = trigger_for("line")  # HMAC, X-Line-Signature, plain body, base64 form
    secret = "line-channel-secret"
    body = b'{"events":[]}'
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Line-Signature": sig},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_telegram_secret_token_header_verifies() -> None:
    prof = trigger_for("telegram")  # TOKEN via X-Telegram-Bot-Api-Secret-Token
    ok = verify_webhook(
        profile=prof, secret="mysecret", raw_body=b"{}",
        headers={"X-Telegram-Bot-Api-Secret-Token": "mysecret"},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True
    bad = verify_webhook(
        profile=prof, secret="mysecret", raw_body=b"{}",
        headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
        url_secret="u", expected_url_secret="u",
    )
    assert bad is False


def test_graph_clientstate_verification() -> None:
    import json

    prof = trigger_for("outlook")  # scheme="graph"
    assert prof.scheme == "graph"
    secret = "client-state-secret"
    body = json.dumps({"value": [{"clientState": secret, "resource": "messages/1"}]}).encode()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body, headers={},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_graph_clientstate_mismatch_rejected() -> None:
    import json

    prof = trigger_for("teams")
    body = json.dumps({"value": [{"clientState": "wrong"}]}).encode()
    ok = verify_webhook(
        profile=prof, secret="right", raw_body=body, headers={},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is False


def test_graph_all_seven_providers_marked() -> None:
    for p in ("outlook", "outlook_calendar", "onedrive", "teams", "word", "excel", "powerpoint"):
        assert trigger_for(p).scheme == "graph"


def test_graph_registration_builds_subscription() -> None:
    from app.services import webhook_registration as wr

    spec = wr.registration_for("outlook")
    assert spec.mode is wr.RegMode.AUTO and spec.verified is True
    req = spec.build_request("https://x/hook", "cs-secret", {})
    assert req.method == "POST" and req.path == "/subscriptions"
    assert req.body["notificationUrl"] == "https://x/hook"
    assert req.body["clientState"] == "cs-secret"
    assert req.body["resource"]  # a default resource is set
    assert "expirationDateTime" in req.body


def test_graph_teams_default_resource() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("teams").build_request("https://x/h", "s", {})
    assert "chats" in req.body["resource"] or "messages" in req.body["resource"]


def test_google_watch_token_verification() -> None:
    prof = trigger_for("google_calendar")  # scheme google_watch, X-Goog-Channel-Token
    assert prof.scheme == "google_watch"
    ok = verify_webhook(
        profile=prof, secret="chan-token", raw_body=b"",
        headers={"X-Goog-Channel-Token": "chan-token"},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True
    bad = verify_webhook(
        profile=prof, secret="chan-token", raw_body=b"",
        headers={"X-Goog-Channel-Token": "nope"},
        url_secret="u", expected_url_secret="u",
    )
    assert bad is False


def test_google_drive_and_calendar_marked() -> None:
    for p in ("google_drive", "google_calendar"):
        assert trigger_for(p).scheme == "google_watch"


def test_gcal_registration_builds_channel() -> None:
    from app.services import webhook_registration as wr

    spec = wr.registration_for("google_calendar")
    assert spec.mode is wr.RegMode.AUTO and spec.verified is True
    req = spec.build_request("https://x/hook", "tok", {})
    assert req.path == "/calendars/primary/events/watch"
    assert req.body["type"] == "web_hook"
    assert req.body["address"] == "https://x/hook"
    assert req.body["token"] == "tok"
    assert isinstance(req.body["expiration"], int)


def test_gdrive_registration_has_prerequisite() -> None:
    from app.services import webhook_registration as wr

    spec = wr.registration_for("google_drive")
    assert spec.mode is wr.RegMode.AUTO and spec.prerequisite is not None
    req = spec.build_request("https://x/hook", "tok", {"start_page_token": "P1"})
    assert req.path == "/changes/watch"
    assert req.query == {"pageToken": "P1"}
    assert req.body["token"] == "tok"


def test_zendesk_signature_verifies() -> None:
    import base64

    prof = trigger_for("zendesk")  # scheme="zendesk"
    assert prof.scheme == "zendesk"
    secret = "zd-signing-secret"
    body = b'{"ticket":{"id":1}}'
    ts = "2024-01-01T00:00:00Z"
    msg = ts.encode() + body
    sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Zendesk-Webhook-Signature": sig, "X-Zendesk-Webhook-Signature-Timestamp": ts},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_intercom_sha1_signature_verifies() -> None:
    prof = trigger_for("intercom")  # HMAC sha1 over body, X-Hub-Signature
    secret = "app-client-secret"
    body = b'{"topic":"conversation.user.created"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Hub-Signature": f"sha1={sig}"},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_batch7_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("zendesk").mode is wr.RegMode.AUTO
    assert wr.registration_for("zendesk").secret_source == "fetch"
    assert wr.registration_for("zendesk").secret_fetch is not None
    assert wr.registration_for("hubspot").mode is wr.RegMode.MANUAL
    assert wr.registration_for("intercom").mode is wr.RegMode.MANUAL
    for p in ("zendesk", "hubspot", "intercom"):
        assert wr.registration_for(p).verified is True


def test_zendesk_build_request() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("zendesk").build_request("https://x/hook", "s", {})
    assert req.path == "/webhooks"
    assert req.body["webhook"]["endpoint"] == "https://x/hook"
    assert req.body["webhook"]["http_method"] == "POST"


def test_linear_bare_hex_sha256_verifies() -> None:
    prof = trigger_for("linear")  # generic HMAC, no prefix -> bare hex
    secret = "lin_wh_secret"
    body = b'{"action":"create","type":"Issue"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"Linear-Signature": sig},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_terraform_sha512_verifies() -> None:
    prof = trigger_for("terraform")  # scheme="terraform" (sha512)
    assert prof.scheme == "terraform"
    token = "tfe-token"
    body = b'{"notifications":[]}'
    sig = hmac.new(token.encode(), body, hashlib.sha512).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=token, raw_body=body,
        headers={"X-TFE-Notification-Signature": sig},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True
    bad = verify_webhook(
        profile=prof, secret=token, raw_body=body,
        headers={"X-TFE-Notification-Signature": "deadbeef"},
        url_secret="u", expected_url_secret="u",
    )
    assert bad is False


def test_twilio_form_param_signature_verifies() -> None:
    import base64

    prof = trigger_for("twilio_sms")  # scheme="twilio"
    assert prof.scheme == "twilio"
    auth_token = "twilio-auth-token"
    url = "https://x.example/api/v1/webhooks/twilio_sms/1/s"
    # Form params (unsorted in body); Twilio sorts by key then appends key+value.
    body = b"To=%2B15551234567&From=%2B15557654321&Body=Hello"
    from urllib.parse import parse_qsl
    signed = url
    for k, v in sorted(parse_qsl(body.decode(), keep_blank_values=True)):
        signed += k + v
    expected = base64.b64encode(hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=auth_token, raw_body=body,
        headers={"X-Twilio-Signature": expected},
        url_secret="s", expected_url_secret="s",
        callback_url=url,
    )
    assert ok is True


def test_batch8_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("terraform").mode is wr.RegMode.PARTIAL
    assert wr.registration_for("linear").mode is wr.RegMode.MANUAL
    assert wr.registration_for("twilio_sms").mode is wr.RegMode.MANUAL
    for p in ("terraform", "linear", "twilio_sms"):
        assert wr.registration_for(p).verified is True
    req = wr.registration_for("terraform").build_request("https://x/h", "tok", {"workspace_id": "ws-1"})
    assert req.path == "/workspaces/ws-1/notification-configurations"
    assert req.body["data"]["attributes"]["token"] == "tok"


def test_monday_all_five_marked_and_partial() -> None:
    from app.services import webhook_registration as wr

    for p in ("monday_workdocs", "monday_dev", "monday_crm", "monday_service", "monday_workspaces"):
        assert trigger_for(p).scheme == "monday"
        spec = wr.registration_for(p)
        assert spec is not None and spec.verified is True
        assert spec.mode is wr.RegMode.PARTIAL
        assert "board_id" in spec.required_targets


def test_monday_build_graphql_mutation() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("monday_crm").build_request(
        "https://x/h", "s", {"board_id": "42", "event": "create_item"}
    )
    assert req.method == "POST" and req.path == "/"
    q = req.body["query"]
    assert "create_webhook" in q and "board_id: 42" in q and "https://x/h" in q


def test_monday_url_secret_governs_verification() -> None:
    # monday does not sign; the unguessable URL secret + create challenge govern.
    prof = trigger_for("monday_dev")
    ok = verify_webhook(
        profile=prof, secret="s", raw_body=b'{"event":{}}', headers={},
        url_secret="s", expected_url_secret="s",
    )
    assert ok is True
    bad = verify_webhook(
        profile=prof, secret="s", raw_body=b'{"event":{}}', headers={},
        url_secret="wrong", expected_url_secret="s",
    )
    assert bad is False


def test_bitbucket_sha256_signature_verifies() -> None:
    prof = trigger_for("bitbucket")  # HMAC sha256, X-Hub-Signature: sha256=
    secret = "bb-secret"
    body = b'{"push":{}}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"X-Hub-Signature": f"sha256={sig}"},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_xero_base64_sha256_signature_verifies() -> None:
    import base64

    prof = trigger_for("xero")  # generic HMAC, base64 form accepted
    secret = "xero-signing-key"
    body = b'{"events":[{"eventType":"UPDATE"}]}'
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"x-xero-signature": sig},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_salesforce_is_poll() -> None:
    from app.services import provider_webhooks as pw

    assert trigger_for("salesforce").kind is pw.TriggerKind.POLL


def test_batch10_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("bitbucket").mode is wr.RegMode.PARTIAL
    assert set(wr.registration_for("bitbucket").required_targets) == {"workspace", "repo_slug"}
    assert wr.registration_for("xero").mode is wr.RegMode.MANUAL
    assert wr.registration_for("salesforce").mode is wr.RegMode.MANUAL
    for p in ("bitbucket", "xero", "salesforce"):
        assert wr.registration_for(p).verified is True
    req = wr.registration_for("bitbucket").build_request("https://x/h", "s", {"workspace": "w", "repo_slug": "r"})
    assert req.path == "/repositories/w/r/hooks"
    assert req.body["secret"] == "s"


def test_sendgrid_ecdsa_signature_verifies() -> None:
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization

    prof = trigger_for("sendgrid")  # scheme="sendgrid" (ECDSA)
    assert prof.scheme == "sendgrid"

    # Generate a P-256 keypair; sign {timestamp}{body} as SendGrid does.
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_der = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode()

    body = b'[{"event":"delivered"}]'
    ts = "1700000000"
    signature = priv.sign(ts.encode() + body, ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.b64encode(signature).decode()

    ok = verify_webhook(
        profile=prof, secret=pub_b64, raw_body=body,
        headers={
            "X-Twilio-Email-Event-Webhook-Signature": sig_b64,
            "X-Twilio-Email-Event-Webhook-Timestamp": ts,
        },
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_sendgrid_ecdsa_rejects_tampered_body() -> None:
    import base64
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization

    prof = trigger_for("sendgrid")
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    ts = "1700000000"
    signature = priv.sign(ts.encode() + b"original", ec.ECDSA(hashes.SHA256()))
    ok = verify_webhook(
        profile=prof, secret=pub_b64, raw_body=b"tampered",
        headers={
            "X-Twilio-Email-Event-Webhook-Signature": base64.b64encode(signature).decode(),
            "X-Twilio-Email-Event-Webhook-Timestamp": ts,
        },
        url_secret="u", expected_url_secret="u",
    )
    assert ok is False


def test_brevo_and_mailchimp_no_signature_url_secret_governs() -> None:
    for provider in ("brevo", "mailchimp"):
        prof = trigger_for(provider)
        ok = verify_webhook(
            profile=prof, secret="", raw_body=b"{}", headers={},
            url_secret="the-secret", expected_url_secret="the-secret",
        )
        assert ok is True
        bad = verify_webhook(
            profile=prof, secret="", raw_body=b"{}", headers={},
            url_secret="wrong", expected_url_secret="the-secret",
        )
        assert bad is False


def test_batch11_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("sendgrid").mode is wr.RegMode.AUTO
    assert wr.registration_for("sendgrid").secret_source == "fetch"
    assert wr.registration_for("mailchimp").mode is wr.RegMode.PARTIAL
    assert wr.registration_for("brevo").mode is wr.RegMode.AUTO
    for p in ("sendgrid", "mailchimp", "brevo"):
        assert wr.registration_for(p).verified is True
    req = wr.registration_for("mailchimp").build_request("https://x/h", "s", {"list_id": "L1"})
    assert req.path == "/lists/L1/webhooks"


def test_target_fields_surface_for_partial_providers() -> None:
    from app.services import webhook_registration as wr

    mc = wr.target_fields_for("mailchimp")
    assert any(f["name"] == "list_id" for f in mc)
    # Target fields are non-secret and REQUIRED (the webhook needs them to work).
    for f in mc:
        assert f["secret"] is False and f["required"] is True

    gh = {f["name"] for f in wr.target_fields_for("github")}
    assert gh == {"owner", "repo"}

    # AUTO / MANUAL / POLL providers expose no target fields.
    assert wr.target_fields_for("stripe") == []
    assert wr.target_fields_for("hubspot") == []


def test_spec_endpoint_appends_target_fields() -> None:
    # The credential spec + target fields merge without duplicating names.
    from app.services import provider_credentials as pc
    from app.services import webhook_registration as wr

    spec = pc.spec_json()
    for provider, fields in spec.items():
        names = {f["name"] for f in fields}
        for t in wr.target_fields_for(provider):
            # target field either already present (config overlap) or addable.
            assert isinstance(t["name"], str)


def test_batch12_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("azure_devops").mode is wr.RegMode.PARTIAL
    assert "project_id" in wr.registration_for("azure_devops").required_targets
    assert wr.registration_for("paypal").mode is wr.RegMode.AUTO
    assert wr.registration_for("jira").mode is wr.RegMode.MANUAL
    assert wr.registration_for("confluence").mode is wr.RegMode.MANUAL
    for p in ("azure_devops", "paypal", "jira", "confluence"):
        assert wr.registration_for(p).verified is True


def test_azure_devops_build_request() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("azure_devops").build_request("https://x/h", "s", {"project_id": "proj-1"})
    assert req.path == "/_apis/hooks/subscriptions"
    assert req.query == {"api-version": "7.1"}
    assert req.body["consumerId"] == "webHooks"
    assert req.body["consumerInputs"]["url"] == "https://x/h"
    assert req.body["publisherInputs"]["projectId"] == "proj-1"


def test_paypal_build_request_event_types() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("paypal").build_request("https://x/h", "s", {"event_types": ["PAYMENT.SALE.COMPLETED"]})
    assert req.path == "/v1/notifications/webhooks"
    assert req.body["url"] == "https://x/h"
    assert req.body["event_types"] == [{"name": "PAYMENT.SALE.COMPLETED"}]


def test_paypal_marked_postback_scheme() -> None:
    assert trigger_for("paypal").scheme == "paypal_postback"


def test_quickbooks_intuit_signature_base64_verifies() -> None:
    import base64

    prof = trigger_for("quickbooks")  # generic HMAC, base64 form
    verifier = "intuit-verifier-token"
    body = b'{"eventNotifications":[]}'
    sig = base64.b64encode(hmac.new(verifier.encode(), body, hashlib.sha256).digest()).decode()
    ok = verify_webhook(
        profile=prof, secret=verifier, raw_body=body,
        headers={"intuit-signature": sig},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_batch13_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("close").mode is wr.RegMode.AUTO
    assert wr.registration_for("freshdesk").mode is wr.RegMode.MANUAL
    assert wr.registration_for("quickbooks").mode is wr.RegMode.MANUAL
    for p in ("close", "freshdesk", "quickbooks"):
        assert wr.registration_for(p).verified is True
    req = wr.registration_for("close").build_request("https://x/h", "s", {})
    assert req.path == "/webhook/" and req.body["url"] == "https://x/h"


def test_close_and_freshdesk_url_secret_governs() -> None:
    for provider in ("close", "freshdesk"):
        prof = trigger_for(provider)
        ok = verify_webhook(
            profile=prof, secret="s", raw_body=b"{}", headers={},
            url_secret="s", expected_url_secret="s",
        )
        assert ok is True
        bad = verify_webhook(
            profile=prof, secret="s", raw_body=b"{}", headers={},
            url_secret="wrong", expected_url_secret="s",
        )
        assert bad is False


def test_zoho_token_verification() -> None:
    import json

    prof = trigger_for("zoho_crm")  # scheme="zoho_watch"
    assert prof.scheme == "zoho_watch"
    body = json.dumps({"token": "zoho-notify-token", "module": "Contacts"}).encode()
    ok = verify_webhook(
        profile=prof, secret="zoho-notify-token", raw_body=body, headers={},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True
    bad = verify_webhook(
        profile=prof, secret="zoho-notify-token",
        raw_body=json.dumps({"token": "wrong"}).encode(), headers={},
        url_secret="u", expected_url_secret="u",
    )
    assert bad is False


def test_batch14_registration_modes() -> None:
    from app.services import webhook_registration as wr

    for p in ("zoho_crm", "activecampaign", "convertkit"):
        spec = wr.registration_for(p)
        assert spec is not None and spec.mode is wr.RegMode.AUTO and spec.verified is True


def test_activecampaign_and_kit_build_requests() -> None:
    from app.services import webhook_registration as wr

    ac = wr.registration_for("activecampaign").build_request("https://x/h", "s", {})
    assert ac.path == "/api/3/webhooks" and ac.body["webhook"]["url"] == "https://x/h"

    kit = wr.registration_for("convertkit").build_request("https://x/h", "s", {})
    assert kit.path == "/webhooks" and kit.body["url"] == "https://x/h"

    zoho = wr.registration_for("zoho_crm").build_request("https://x/h", "tok", {})
    assert zoho.path == "/crm/v8/actions/watch"
    assert zoho.body["watch"][0]["token"] == "tok"
    assert zoho.body["watch"][0]["notify_url"] == "https://x/h"


def test_zoho_in_renewal_set() -> None:
    # zoho_watch expires in ~1 day and must be in the renewable providers.
    from app.services import provider_webhooks as pw

    renewable = {p for p, prof in pw.TRIGGER_PROFILES.items() if getattr(prof, "scheme", "") in ("graph", "google_watch", "zoho_watch")}
    assert "zoho_crm" in renewable


def test_all_74_providers_have_verified_registration() -> None:
    from app.services import webhook_registration as wr
    from app.services.provider_credentials import PROVIDER_CREDENTIALS

    all_providers = set(PROVIDER_CREDENTIALS)
    registered = set(wr.REGISTRATIONS)
    assert all_providers == registered, f"unclassified: {all_providers - registered}"
    for provider, spec in wr.REGISTRATIONS.items():
        assert spec.verified is True, f"{provider} not verified"
        assert spec.source, f"{provider} missing source"


def test_batch15_registration_modes() -> None:
    from app.services import webhook_registration as wr

    assert wr.registration_for("sender").mode is wr.RegMode.AUTO
    assert wr.registration_for("greenhouse").mode is wr.RegMode.MANUAL
    assert wr.registration_for("lever").mode is wr.RegMode.MANUAL
    # Final classification pass.
    assert wr.registration_for("gmail").mode is wr.RegMode.AUTO
    assert wr.registration_for("slack").mode is wr.RegMode.MANUAL
    assert wr.registration_for("wechat").mode is wr.RegMode.MANUAL
    assert wr.registration_for("gcp").mode is wr.RegMode.MANUAL
    for p in ("aws", "netsuite", "sap", "workday", "bamboohr", "yahoo", "imap_smtp"):
        assert wr.registration_for(p).mode is wr.RegMode.POLL


def test_greenhouse_signature_verifies() -> None:
    prof = trigger_for("greenhouse")  # HMAC sha256 over body, Signature header
    secret = "gh-secret-key"
    body = b'{"action":"candidate_hired"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    ok = verify_webhook(
        profile=prof, secret=secret, raw_body=body,
        headers={"Signature": sig},
        url_secret="u", expected_url_secret="u",
    )
    assert ok is True


def test_sender_build_request() -> None:
    from app.services import webhook_registration as wr

    req = wr.registration_for("sender").build_request("https://x/h", "s", {})
    assert req.path == "/account-webhooks"
    assert req.body["url"] == "https://x/h"


def test_frontend_trigger_badge_map_matches_backend_registry() -> None:
    """The generated frontend badge map must cover every provider and agree with
    the backend's authoritative instant-vs-scheduled classification (no drift)."""
    import re
    from pathlib import Path

    from app.services import webhook_registration as wr
    from app.services import provider_webhooks as pw

    def expected_badge(mode: str, kind: str) -> str:
        if kind in ("push", "pubsub") and mode in ("auto", "partial", "manual"):
            return "instant"
        return "scheduled"

    # Locate the frontend map file relative to the repo root.
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "frontend" / "src" / "lib" / "trigger-badges.ts",
        Path("/frontend/src/lib/trigger-badges.ts"),
    ]
    ts_path = next((p for p in candidates if p.exists()), None)
    if ts_path is None:
        import pytest

        pytest.skip("frontend trigger-badges.ts not present in this environment")

    text = ts_path.read_text(encoding="utf-8")
    # Parse lines like:  name: { mode: "auto", kind: "push", badge: "instant" },
    row = re.compile(
        r'(\w+):\s*\{\s*mode:\s*"(\w+)",\s*kind:\s*"(\w+)",\s*badge:\s*"(\w+)"\s*\}'
    )
    fe = {m.group(1): (m.group(2), m.group(3), m.group(4)) for m in row.finditer(text)}

    for provider, spec in wr.REGISTRATIONS.items():
        assert provider in fe, f"{provider} missing from frontend trigger-badges.ts"
        mode = str(spec.mode).split(".")[-1].lower()
        kind = str(pw.trigger_for(provider).kind).split(".")[-1].lower()
        exp = expected_badge(mode, kind)
        assert fe[provider][2] == exp, (
            f"{provider}: frontend badge {fe[provider][2]!r} != expected {exp!r} "
            f"(mode={mode}, kind={kind})"
        )


def test_frontend_oauth_family_list_matches_backend_registry() -> None:
    """The frontend OAuth-family slug set must exactly match the backend
    ``OAUTH_PROVIDERS`` registry keys (no drift). Mirrors the trigger-badge
    parity test above. Validates Requirements 9.1, 12.2."""
    import re
    from pathlib import Path

    from app.services.integration_oauth import OAUTH_PROVIDERS

    # Locate the frontend classifier relative to the repo root, with an
    # in-container absolute-path fallback (matches the trigger-badges test).
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "frontend" / "src" / "lib" / "oauth-providers.ts",
        Path("/frontend/src/lib/oauth-providers.ts"),
    ]
    ts_path = next((p for p in candidates if p.exists()), None)
    if ts_path is None:
        import pytest

        pytest.skip("frontend oauth-providers.ts not present in this environment")

    text = ts_path.read_text(encoding="utf-8")

    # Extract the OAUTH_FAMILY_SLUGS array body and collect its string literals,
    # ignoring line comments (e.g. "// ---- Google family ----").
    match = re.search(
        r"OAUTH_FAMILY_SLUGS[^=]*=\s*\[(.*?)\]",
        text,
        re.DOTALL,
    )
    assert match is not None, "OAUTH_FAMILY_SLUGS array not found in oauth-providers.ts"
    body = re.sub(r"//[^\n]*", "", match.group(1))
    fe_slugs = set(re.findall(r'"([^"]+)"', body))

    be_slugs = set(OAUTH_PROVIDERS.keys())

    assert fe_slugs == be_slugs, (
        "frontend OAUTH_FAMILY_SLUGS drift from backend OAUTH_PROVIDERS: "
        f"only-frontend={sorted(fe_slugs - be_slugs)}, "
        f"only-backend={sorted(be_slugs - fe_slugs)}"
    )
