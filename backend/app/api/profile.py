"""Account profile router — the signed-in user's own profile + SMS settings.

Endpoints under ``/api/v1/profile`` (all require a valid Session_Token via
:func:`app.api.deps.require_session`; a user always acts on THEIR OWN row — the
``user_id`` comes from the resolved context, never from the body/path, so there
is no way to read or edit another user's profile here):

- ``GET  /api/v1/profile`` — the caller's profile (name, email, provider) plus
  their SMS notification settings (phone number, country, enabled flag).
- ``PATCH /api/v1/profile`` — update the display name and/or the SMS settings:
  the phone number (validated E.164), its country (ISO 3166-1 alpha-2, inferred
  from the number when omitted), and the notifications-enabled toggle. Sending
  ``phone_number: null`` clears the number and disables notifications.
- ``GET  /api/v1/profile/sms-usage`` — the caller's SMS COUNT and estimated
  SPEND (USD), with a per-country breakdown, computed from the recorded
  :class:`~app.db.models.SmsNotification` rows (Amazon SNS pricing estimate).

The phone number is PII: it is returned to its owner but never written to logs
(the router logs only coarse actions, no number).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_session
from app.core.errors import APIError
from app.core.tenancy import RequestContext
from app.db.models import SmsNotification, User
from app.db.session import get_session
from app.schemas.base import BaseRequest
from app.services import sns_service

logger = logging.getLogger("atomic_ai.profile")

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class UpdateProfileRequest(BaseRequest):
    """Body for ``PATCH /api/v1/profile`` (all fields optional / partial update).

    - ``name``: new display name (non-empty when provided).
    - ``phone_number``: E.164 mobile number ("+" then 7–15 digits), or ``null``
      to clear it (which also disables notifications).
    - ``phone_country``: ISO 3166-1 alpha-2 (e.g. "US"); inferred from the number
      when omitted.
    - ``sms_notifications_enabled``: pause/resume notifications without clearing
      the number.
    """

    name: str | None = None
    phone_number: str | None = None
    phone_country: str | None = None
    sms_notifications_enabled: bool | None = None

    @field_validator("phone_number")
    @classmethod
    def _validate_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Strip common formatting (spaces, hyphens, parens, dots) but keep "+".
        stripped = re.sub(r"[\s()\-.]", "", value.strip())
        if stripped == "":
            return None
        if not sns_service.is_valid_e164(stripped):
            raise ValueError(
                "phone_number must be E.164 format, e.g. +14155550123"
            )
        return stripped

    @field_validator("phone_country")
    @classmethod
    def _validate_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        v = value.strip().upper()
        if v == "":
            return None
        if len(v) != 2 or not v.isalpha():
            raise ValueError("phone_country must be a 2-letter ISO country code")
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        v = value.strip()
        if v == "":
            raise ValueError("name must not be empty")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _current_user(session: AsyncSession, ctx: RequestContext) -> User:
    user = await session.get(User, ctx.user_id)
    if user is None:  # pragma: no cover - a valid session implies a live user
        raise APIError(status_code=404, code="not_found", message="User not found.")
    return user


def _profile_view(user: User) -> dict:
    """Serialize a user's own profile + SMS settings (owner-only view)."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "auth_provider": user.auth_provider.value,
        "phone_number": user.phone_number,
        "phone_country": user.phone_country,
        "sms_notifications_enabled": bool(user.sms_notifications_enabled),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", status_code=200)
async def get_profile(
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the signed-in user's own profile + SMS notification settings."""
    user = await _current_user(session, ctx)
    return _profile_view(user)


@router.patch("", status_code=200)
async def update_profile(
    body: UpdateProfileRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update the caller's display name and/or SMS notification settings.

    Partial update: only provided fields change. Setting ``phone_number`` to an
    empty/``null`` value clears it and disables notifications. When a phone
    number is set without a country, the country is inferred from the number's
    dialing code. The user only ever edits their own row (``ctx.user_id``).
    """
    user = await _current_user(session, ctx)
    fields = body.model_dump(exclude_unset=True)

    if "name" in fields and fields["name"] is not None:
        user.name = fields["name"]

    # Phone number: explicit clear vs set.
    if "phone_number" in fields:
        new_phone = fields["phone_number"]
        if new_phone is None:
            user.phone_number = None
            user.phone_country = None
            user.sms_notifications_enabled = False
        else:
            user.phone_number = new_phone
            # Country: explicit value wins, else infer from the number.
            country = fields.get("phone_country") or sns_service.country_from_e164(
                new_phone
            )
            user.phone_country = country

    # Country update without a number change.
    if (
        "phone_country" in fields
        and fields["phone_country"] is not None
        and "phone_number" not in fields
    ):
        user.phone_country = fields["phone_country"]

    if "sms_notifications_enabled" in fields and fields[
        "sms_notifications_enabled"
    ] is not None:
        # Can't enable notifications without a number on file.
        if fields["sms_notifications_enabled"] and not user.phone_number:
            raise APIError(
                status_code=422,
                code="invalid_request",
                message="Add a phone number before enabling SMS notifications.",
            )
        user.sms_notifications_enabled = bool(fields["sms_notifications_enabled"])

    await session.commit()
    await session.refresh(user)
    logger.info("profile.updated user_id=%s", user.id)
    return _profile_view(user)


@router.get("/sms-usage", status_code=200)
async def get_sms_usage(
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the caller's SMS count + estimated spend, with a per-country split.

    Aggregates the caller's :class:`~app.db.models.SmsNotification` rows: the
    total number of messages sent, total billed segments, and total estimated
    spend (USD, an Amazon SNS pricing estimate — see :mod:`app.services.sns_service`),
    plus a breakdown per destination country. Only ``status = "sent"`` rows count
    toward spend; failed sends are counted separately.
    """
    rows = (
        await session.execute(
            select(SmsNotification).where(SmsNotification.user_id == ctx.user_id)
        )
    ).scalars().all()

    total_sent = 0
    total_failed = 0
    total_segments = 0
    total_cost = Decimal("0")
    by_country: dict[str, dict] = {}

    for row in rows:
        if row.status == "sent":
            total_sent += 1
            total_segments += int(row.segments or 0)
            cost = Decimal(row.total_cost_usd or "0")
            total_cost += cost
            key = (row.country or "??").upper()
            bucket = by_country.setdefault(
                key, {"country": key, "count": 0, "segments": 0, "spend_usd": Decimal("0")}
            )
            bucket["count"] += 1
            bucket["segments"] += int(row.segments or 0)
            bucket["spend_usd"] += cost
        else:
            total_failed += 1

    breakdown = [
        {
            "country": b["country"],
            "count": b["count"],
            "segments": b["segments"],
            "spend_usd": str(b["spend_usd"].quantize(Decimal("0.00001"))),
        }
        for b in sorted(by_country.values(), key=lambda b: b["spend_usd"], reverse=True)
    ]

    return {
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_segments": total_segments,
        "total_spend_usd": str(total_cost.quantize(Decimal("0.00001"))),
        "currency": "USD",
        "by_country": breakdown,
        "note": (
            "Spend is an estimate from Amazon SNS published SMS prices, not a "
            "billed amount."
        ),
    }


__all__ = ["router"]
