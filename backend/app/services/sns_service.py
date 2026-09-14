"""Amazon SNS SMS notifications — reply-approval alerts + usage/spend tracking.

When a reply awaits a user's approval, the platform texts the user's stored
phone number via Amazon SNS so they can act without watching the dashboard.
This module owns:

- Building a boto3 SNS client that reuses the SAME AWS credentials + region as
  Bedrock (the standard AWS credential chain + ``AWS_REGION``); no separate
  secret is introduced (see :mod:`app.config`).
- Publishing an SMS (``Publish`` with ``PhoneNumber`` + the ``SMSType`` /
  optional ``SenderID`` message attributes), computing the billed segment count,
  pricing it by the destination country's Amazon SNS per-message rate, and
  recording a :class:`~app.db.models.SmsNotification` row so the app can report
  SMS COUNT and SMS SPEND per country WITHOUT AWS billing access.

Security / privacy invariants:

- The full phone number and the message body are treated as sensitive and are
  NEVER written to application logs (only coarse "sent"/"failed" + a masked
  suffix at debug). The number IS stored in the DB row (it is the user's own
  number, needed for per-number accounting).
- Sending is BEST-EFFORT: every public entry point swallows AWS/boto errors and
  returns a structured result, so a notification failure can never block or
  crash approval creation.
- Tenancy is server-derived: the caller passes the resolved ``user_id`` /
  ``workspace_id``; nothing here trusts model input.

Pricing note: the per-country rates in :data:`SMS_PRICE_USD_BY_COUNTRY` are a
built-in ESTIMATE of Amazon SNS SMS list prices (USD, per message segment) for
tracking/forecasting in-app. They are not billed by us and can drift from AWS's
live rates — operators should treat the stored cost as an estimate and update
the table when AWS changes prices. Source: Amazon SNS SMS pricing
(https://aws.amazon.com/sns/sms-pricing/). Content was rephrased/derived for
compliance with licensing restrictions.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "send_reply_approval_sms",
    "compute_segments",
    "price_for_country",
    "country_from_e164",
    "is_valid_e164",
    "SMS_PRICE_USD_BY_COUNTRY",
    "DEFAULT_SMS_PRICE_USD",
]

# ---------------------------------------------------------------------------
# Phone number helpers (pure)
# ---------------------------------------------------------------------------

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def is_valid_e164(phone: str | None) -> bool:
    """Whether ``phone`` is a plausible E.164 number ("+" then 7–15 digits)."""
    return bool(phone) and bool(_E164_RE.match(phone or ""))


#: E.164 dialing-code prefixes -> ISO 3166-1 alpha-2 country. Longest-prefix
#: wins (checked longest-first), so shared parents (e.g. +1 US/CA regions) still
#: resolve. This is a pragmatic subset covering the common destinations; unknown
#: prefixes fall back to the user-supplied ``phone_country`` or the default
#: price. Kept small + explicit rather than pulling a phonenumbers dependency.
_DIALING_CODE_TO_COUNTRY: dict[str, str] = {
    "1": "US",       # US/Canada/NANP (priced as US by default)
    "44": "GB",
    "91": "IN",
    "234": "NG",
    "61": "AU",
    "49": "DE",
    "33": "FR",
    "34": "ES",
    "39": "IT",
    "31": "NL",
    "353": "IE",
    "351": "PT",
    "27": "ZA",
    "254": "KE",
    "233": "GH",
    "55": "BR",
    "52": "MX",
    "54": "AR",
    "81": "JP",
    "82": "KR",
    "86": "CN",
    "65": "SG",
    "60": "MY",
    "63": "PH",
    "62": "ID",
    "66": "TH",
    "971": "AE",
    "966": "SA",
    "972": "IL",
    "90": "TR",
    "7": "RU",
    "380": "UA",
    "48": "PL",
    "46": "SE",
    "47": "NO",
    "45": "DK",
    "358": "FI",
    "41": "CH",
    "43": "AT",
    "32": "BE",
    "64": "NZ",
}


def country_from_e164(phone: str | None) -> str | None:
    """Infer the ISO 3166-1 alpha-2 country from an E.164 number's dialing code.

    Longest-prefix match against :data:`_DIALING_CODE_TO_COUNTRY`. Returns
    ``None`` when the number is not E.164 or no known prefix matches (the caller
    then falls back to the user's stored ``phone_country`` or the default rate).
    Pure.
    """
    if not is_valid_e164(phone):
        return None
    digits = (phone or "")[1:]  # drop the leading "+"
    for code in sorted(_DIALING_CODE_TO_COUNTRY, key=len, reverse=True):
        if digits.startswith(code):
            return _DIALING_CODE_TO_COUNTRY[code]
    return None


# ---------------------------------------------------------------------------
# Pricing + segment math (pure)
# ---------------------------------------------------------------------------

#: Fallback per-segment price (USD) when a destination country is unknown. A
#: deliberately conservative mid-range estimate.
DEFAULT_SMS_PRICE_USD: Decimal = Decimal("0.05")

#: Estimated Amazon SNS SMS list price (USD per message segment) by ISO country.
#: An in-app estimate for usage forecasting — NOT a billing source of truth (see
#: the module docstring). Update when AWS changes published rates.
SMS_PRICE_USD_BY_COUNTRY: dict[str, Decimal] = {
    "US": Decimal("0.00645"),
    "CA": Decimal("0.00645"),
    "GB": Decimal("0.0311"),
    "IN": Decimal("0.0043"),
    "NG": Decimal("0.0410"),
    "AU": Decimal("0.0466"),
    "DE": Decimal("0.0777"),
    "FR": Decimal("0.0700"),
    "ES": Decimal("0.0653"),
    "IT": Decimal("0.0700"),
    "NL": Decimal("0.0905"),
    "IE": Decimal("0.0435"),
    "PT": Decimal("0.0450"),
    "ZA": Decimal("0.0293"),
    "KE": Decimal("0.0350"),
    "GH": Decimal("0.0330"),
    "BR": Decimal("0.0374"),
    "MX": Decimal("0.0332"),
    "AR": Decimal("0.0600"),
    "JP": Decimal("0.0745"),
    "KR": Decimal("0.0289"),
    "CN": Decimal("0.0303"),
    "SG": Decimal("0.0400"),
    "MY": Decimal("0.0300"),
    "PH": Decimal("0.0400"),
    "ID": Decimal("0.3720"),
    "TH": Decimal("0.0230"),
    "AE": Decimal("0.0289"),
    "SA": Decimal("0.0289"),
    "IL": Decimal("0.0189"),
    "TR": Decimal("0.0300"),
    "RU": Decimal("0.0480"),
    "UA": Decimal("0.0700"),
    "PL": Decimal("0.0300"),
    "SE": Decimal("0.0500"),
    "NO": Decimal("0.0500"),
    "DK": Decimal("0.0400"),
    "FI": Decimal("0.0500"),
    "CH": Decimal("0.0700"),
    "AT": Decimal("0.0700"),
    "BE": Decimal("0.0800"),
    "NZ": Decimal("0.0400"),
}

# GSM-7 basic-alphabet characters. A message using only these is encoded 7-bit:
# 160 chars in a single segment, 153 per segment when concatenated. Anything
# else forces UCS-2 (16-bit): 70 chars single, 67 per concatenated segment.
_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# GSM-7 extension chars each cost TWO 7-bit units.
_GSM7_EXTENSION = set("^{}\\[~]|€")


def _is_gsm7(text: str) -> bool:
    return all(ch in _GSM7_BASIC or ch in _GSM7_EXTENSION for ch in text)


def compute_segments(text: str) -> int:
    """Return the number of billed SMS segments for ``text`` (pure).

    Mirrors carrier concatenation rules: GSM-7 messages fit 160 chars in one
    segment or 153 per part when split; UCS-2 (any non-GSM char, e.g. emoji or
    many non-Latin scripts) fits 70 or 67 per part. GSM-7 extension characters
    count as two units. Empty text is a single (zero-length) segment.
    """
    if not text:
        return 1
    if _is_gsm7(text):
        units = sum(2 if ch in _GSM7_EXTENSION else 1 for ch in text)
        if units <= 160:
            return 1
        # ceil(units / 153)
        return (units + 152) // 153
    length = len(text)
    if length <= 70:
        return 1
    return (length + 66) // 67


def price_for_country(country: str | None) -> Decimal:
    """Per-segment SMS price (USD) for an ISO country, or the default fallback."""
    if country:
        price = SMS_PRICE_USD_BY_COUNTRY.get(country.upper())
        if price is not None:
            return price
    return DEFAULT_SMS_PRICE_USD


def _quantize_usd(value: Decimal) -> str:
    """Round a USD amount to 5 decimal places and return it as a plain string."""
    return str(value.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def estimate_cost(text: str, country: str | None) -> tuple[int, Decimal, Decimal]:
    """Return ``(segments, unit_price, total_cost)`` for ``text`` to ``country``.

    Pure. ``total_cost = unit_price * segments``. Used by the sender to record
    spend and by tests to assert the math without any AWS call.
    """
    segments = compute_segments(text)
    unit = price_for_country(country)
    total = unit * segments
    return segments, unit, total


# ---------------------------------------------------------------------------
# SNS client (reuses the Bedrock AWS credential chain + region)
# ---------------------------------------------------------------------------


def _build_sns_client() -> Any:
    """Build a boto3 SNS client using the same region/creds as Bedrock.

    boto3 resolves credentials from the standard chain (env vars / shared
    profile / instance role) exactly as the Bedrock client does; we only pin the
    region to ``AWS_REGION``. Imported lazily so importing this module never
    requires boto3 at import time (tests inject a fake client instead).
    """
    import boto3  # type: ignore[import-not-found]

    settings = get_settings()
    return boto3.client("sns", region_name=settings.AWS_REGION)


def _mask(phone: str) -> str:
    """Mask a phone number for safe debug logging: keep only the last 4 digits."""
    tail = phone[-4:] if len(phone) >= 4 else phone
    return f"***{tail}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def send_reply_approval_sms(
    *,
    session_factory,
    user_id,
    workspace_id,
    approval_request_id,
    phone_number: str,
    phone_country: str | None,
    pending_count: int | None = None,
    sns_client: Any | None = None,
) -> dict[str, Any]:
    """Send a reply-approval SMS and record its cost. Best-effort (never raises).

    Publishes a short transactional SMS ("You have a reply awaiting approval…")
    to ``phone_number`` via Amazon SNS, computes the billed segment count +
    estimated spend for the destination country, and persists an
    :class:`~app.db.models.SmsNotification` row on a FRESH DB session (opened via
    ``session_factory`` so it is loop-safe when called from a worker thread or a
    post-commit hook). Returns a structured result dict — it NEVER raises, so a
    caller in the approval-creation path is never affected by an SMS failure.

    Country is resolved from the E.164 number's dialing code, falling back to the
    caller-supplied ``phone_country``. ``sns_client`` is injectable for tests;
    the default builds a real client via :func:`_build_sns_client`.

    Returns:
        ``{"status": "sent"|"failed"|"skipped", "segments": int,
           "unit_price_usd": str, "total_cost_usd": str,
           "message_id": str|None, "country": str|None, "error": str|None}``.
    """
    settings = get_settings()
    if not settings.SNS_ENABLED:
        return {"status": "skipped", "error": "sns_disabled"}
    if not is_valid_e164(phone_number):
        return {"status": "skipped", "error": "invalid_phone"}

    country = country_from_e164(phone_number) or (
        phone_country.upper() if phone_country else None
    )
    message = _build_message(pending_count)
    segments, unit_price, total_cost = estimate_cost(message, country)

    message_id: str | None = None
    status = "sent"
    error: str | None = None
    try:
        client = sns_client if sns_client is not None else _build_sns_client()
        attributes = {
            "AWS.SNS.SMS.SMSType": {
                "DataType": "String",
                "StringValue": settings.SNS_SMS_TYPE or "Transactional",
            }
        }
        if settings.SNS_SMS_SENDER_ID:
            attributes["AWS.SNS.SMS.SenderID"] = {
                "DataType": "String",
                "StringValue": settings.SNS_SMS_SENDER_ID,
            }
        # boto3 is synchronous; run it off the event loop so we never block.
        import asyncio

        def _publish() -> dict:
            return client.publish(
                PhoneNumber=phone_number,
                Message=message,
                MessageAttributes=attributes,
            )

        response = await asyncio.to_thread(_publish)
        message_id = (
            response.get("MessageId") if isinstance(response, dict) else None
        )
        logger.info(
            "sns sms sent | to=%s country=%s segments=%s",
            _mask(phone_number),
            country or "?",
            segments,
        )
    except Exception as exc:  # noqa: BLE001 - SMS must never break the caller
        status = "failed"
        error = type(exc).__name__
        logger.warning(
            "sns sms failed | to=%s country=%s error=%s",
            _mask(phone_number),
            country or "?",
            error,
        )

    await _record(
        session_factory=session_factory,
        user_id=user_id,
        workspace_id=workspace_id,
        approval_request_id=approval_request_id,
        phone_number=phone_number,
        country=country,
        sns_message_id=message_id,
        segments=segments,
        unit_price_usd=_quantize_usd(unit_price),
        total_cost_usd=_quantize_usd(total_cost if status == "sent" else Decimal("0")),
        status=status,
        error=error,
    )

    return {
        "status": status,
        "segments": segments,
        "unit_price_usd": _quantize_usd(unit_price),
        "total_cost_usd": _quantize_usd(
            total_cost if status == "sent" else Decimal("0")
        ),
        "message_id": message_id,
        "country": country,
        "error": error,
    }


def _build_message(pending_count: int | None) -> str:
    """Compose the short reply-approval SMS body (GSM-7 to keep it 1 segment)."""
    if pending_count and pending_count > 1:
        return (
            f"Atomic AI: you have {pending_count} replies awaiting approval. "
            "Open the app to review and approve."
        )
    return (
        "Atomic AI: you have a reply awaiting approval. "
        "Open the app to review and approve."
    )


async def _record(
    *,
    session_factory,
    user_id,
    workspace_id,
    approval_request_id,
    phone_number: str,
    country: str | None,
    sns_message_id: str | None,
    segments: int,
    unit_price_usd: str,
    total_cost_usd: str,
    status: str,
    error: str | None,
) -> None:
    """Persist one :class:`SmsNotification` row on a fresh session (best-effort)."""
    try:
        from app.db.models import SmsNotification

        async with session_factory() as session:
            session.add(
                SmsNotification(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    approval_request_id=approval_request_id,
                    phone_number=phone_number,
                    country=country,
                    sns_message_id=sns_message_id,
                    segments=segments,
                    unit_price_usd=unit_price_usd,
                    total_cost_usd=total_cost_usd,
                    status=status,
                    error=error,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - recording must never crash the caller
        logger.debug("sns sms recording failed; continuing")
