"""Tests for the Amazon SNS SMS service (pure math + publish/record flow).

No live AWS and no database: the segment/pricing/country helpers are pure, and
``send_reply_approval_sms`` is exercised with an INJECTED fake SNS client plus a
fake ``session_factory`` that captures the recorded ``SmsNotification`` so we can
assert the publish call shape, the computed cost, best-effort failure handling,
and that the phone number / body are never logged.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from app.services import sns_service


class _Settings:
    """Minimal settings stub so the service does not touch the env-based loader
    (the conftest autouse fixture clears config vars, which would make the real
    ``get_settings()`` fail-fast)."""

    def __init__(self, enabled: bool = True):
        self.SNS_ENABLED = enabled
        self.SNS_SMS_TYPE = "Transactional"
        self.SNS_SMS_SENDER_ID = ""
        self.AWS_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    """Route the service's ``get_settings`` to a stub for every test here."""
    monkeypatch.setattr(sns_service, "get_settings", lambda: _Settings(enabled=True))


# ---------------------------------------------------------------------------
# Pure helpers: E.164, country, segments, pricing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phone,valid",
    [
        ("+14155550123", True),
        ("+442071838750", True),
        ("+2348012345678", True),
        ("14155550123", False),  # no +
        ("+0155550123", False),  # leading 0 after +
        ("+123", False),  # too short
        ("", False),
        (None, False),
        ("+1415555012a", False),  # non-digit
    ],
)
def test_is_valid_e164(phone, valid):
    assert sns_service.is_valid_e164(phone) is valid


@pytest.mark.parametrize(
    "phone,country",
    [
        ("+14155550123", "US"),
        ("+447911123456", "GB"),
        ("+2348012345678", "NG"),
        ("+919812345678", "IN"),
        ("+61412345678", "AU"),
        ("+9999999999", None),  # unknown prefix
    ],
)
def test_country_from_e164(phone, country):
    assert sns_service.country_from_e164(phone) == country


def test_compute_segments_gsm7():
    assert sns_service.compute_segments("") == 1
    assert sns_service.compute_segments("a" * 160) == 1
    assert sns_service.compute_segments("a" * 161) == 2
    assert sns_service.compute_segments("a" * 306) == 2  # 2*153
    assert sns_service.compute_segments("a" * 307) == 3
    # A GSM-7 extension char counts as 2 units: 80 '€' = 160 units = 1 segment.
    assert sns_service.compute_segments("€" * 80) == 1
    assert sns_service.compute_segments("€" * 81) == 2


def test_compute_segments_ucs2():
    # An emoji forces UCS-2: 70 chars single, 67 per concatenated part.
    assert sns_service.compute_segments("😀") == 1
    assert sns_service.compute_segments("😀" + "a" * 69) == 1  # 70 chars
    assert sns_service.compute_segments("😀" + "a" * 70) == 2  # 71 chars


def test_price_for_country_and_default():
    assert sns_service.price_for_country("US") == Decimal("0.00645")
    assert sns_service.price_for_country("us") == Decimal("0.00645")  # case-insensitive
    assert sns_service.price_for_country("NG") == Decimal("0.0410")
    assert sns_service.price_for_country("ZZ") == sns_service.DEFAULT_SMS_PRICE_USD
    assert sns_service.price_for_country(None) == sns_service.DEFAULT_SMS_PRICE_USD


def test_estimate_cost_multiplies_segments():
    text = "a" * 200  # 2 GSM-7 segments
    segments, unit, total = sns_service.estimate_cost(text, "US")
    assert segments == 2
    assert unit == Decimal("0.00645")
    assert total == Decimal("0.01290")


# ---------------------------------------------------------------------------
# Publish + record flow (fake SNS client + fake session)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Captures added rows; commit/close are no-ops."""

    def __init__(self, sink: list):
        self._sink = sink

    def add(self, obj) -> None:
        self._sink.append(obj)

    async def commit(self) -> None:  # noqa: D401
        return None

    async def close(self) -> None:
        return None


def _session_factory(sink: list):
    @asynccontextmanager
    async def _factory():
        yield _FakeSession(sink)

    return _factory


class _FakeSnsOk:
    def __init__(self):
        self.calls: list = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "msg-123"}


class _FakeSnsFail:
    def publish(self, **kwargs):
        raise RuntimeError("SNS unavailable")


@pytest.mark.asyncio
async def test_send_records_cost_and_publishes(monkeypatch):
    import uuid

    recorded: list = []
    fake = _FakeSnsOk()
    result = await sns_service.send_reply_approval_sms(
        session_factory=_session_factory(recorded),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
        phone_number="+14155550123",
        phone_country=None,
        pending_count=1,
        sns_client=fake,
    )

    # Published to the right number with the SMSType attribute.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["PhoneNumber"] == "+14155550123"
    assert "awaiting approval" in call["Message"].lower()
    assert call["MessageAttributes"]["AWS.SNS.SMS.SMSType"]["StringValue"] in (
        "Transactional",
        "Promotional",
    )

    assert result["status"] == "sent"
    assert result["country"] == "US"
    assert result["segments"] == 1
    assert result["message_id"] == "msg-123"
    assert result["total_cost_usd"] == "0.00645"

    # One SmsNotification row was recorded with the cost.
    assert len(recorded) == 1
    row = recorded[0]
    assert row.status == "sent"
    assert row.country == "US"
    assert row.sns_message_id == "msg-123"
    assert row.total_cost_usd == "0.00645"


@pytest.mark.asyncio
async def test_send_failure_is_best_effort_and_records_failed(monkeypatch):
    import uuid

    recorded: list = []
    result = await sns_service.send_reply_approval_sms(
        session_factory=_session_factory(recorded),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
        phone_number="+2348012345678",
        phone_country="NG",
        pending_count=2,
        sns_client=_FakeSnsFail(),
    )
    # Never raises; reports failed; records a failed row with zero billed cost.
    assert result["status"] == "failed"
    assert result["message_id"] is None
    assert result["total_cost_usd"] == "0.00000"
    assert len(recorded) == 1 and recorded[0].status == "failed"
    assert recorded[0].country == "NG"


@pytest.mark.asyncio
async def test_send_skipped_when_disabled(monkeypatch):
    import uuid

    monkeypatch.setattr(sns_service, "get_settings", lambda: _Settings(enabled=False))
    recorded: list = []
    result = await sns_service.send_reply_approval_sms(
        session_factory=_session_factory(recorded),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
        phone_number="+14155550123",
        phone_country=None,
        sns_client=_FakeSnsOk(),
    )
    assert result["status"] == "skipped"
    assert recorded == []


@pytest.mark.asyncio
async def test_send_skipped_for_invalid_phone():
    import uuid

    recorded: list = []
    result = await sns_service.send_reply_approval_sms(
        session_factory=_session_factory(recorded),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
        phone_number="not-a-number",
        phone_country=None,
        sns_client=_FakeSnsOk(),
    )
    assert result["status"] == "skipped"
    assert result["error"] == "invalid_phone"
    assert recorded == []


@pytest.mark.asyncio
async def test_phone_and_body_are_not_logged(caplog):
    import uuid

    recorded: list = []
    with caplog.at_level(logging.DEBUG):
        await sns_service.send_reply_approval_sms(
            session_factory=_session_factory(recorded),
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            approval_request_id=uuid.uuid4(),
            phone_number="+14155550123",
            phone_country=None,
            pending_count=1,
            sns_client=_FakeSnsOk(),
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    # The full number never appears (only a masked ***0123 suffix may).
    assert "+14155550123" not in logged
    assert "4155550123" not in logged
    # The message body is not logged.
    assert "awaiting approval" not in logged.lower()
