"""Profile schema/view + reply-approval SMS notification hook (DB-free).

The DB-backed profile *router* is covered by live smoke (401 unauth + schema
validation) and the throwaway-Postgres router harness on CI. Here we unit-test
the pure/near-pure pieces without a database:

- ``UpdateProfileRequest`` validation (E.164 phone, ISO-2 country, clearing).
- ``_profile_view`` serialization (owner-only view shape).
- ``approval_service._notify_reply_awaiting_approval``: it texts only
  Owner/Admin members who have a phone + notifications enabled, respects the
  per-user monthly spend cap, and is best-effort (never raises), using fake
  sessions + a patched SNS sender so no DB or AWS is touched.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.api.profile import UpdateProfileRequest, _profile_view
from app.db.models import AuthProvider, MemberRole


# ---------------------------------------------------------------------------
# UpdateProfileRequest validation
# ---------------------------------------------------------------------------


def test_update_profile_normalizes_phone():
    body = UpdateProfileRequest(phone_number="+1 (415) 555-0123")
    assert body.phone_number == "+14155550123"


def test_update_profile_empty_phone_clears():
    assert UpdateProfileRequest(phone_number="").phone_number is None


def test_update_profile_uppercases_country():
    assert UpdateProfileRequest(phone_country="us").phone_country == "US"


@pytest.mark.parametrize("bad", ["not-a-number", "1234567", "+abc"])
def test_update_profile_rejects_invalid_phone(bad):
    with pytest.raises(Exception):
        UpdateProfileRequest(phone_number=bad)


@pytest.mark.parametrize("bad", ["USA", "u", "1"])
def test_update_profile_rejects_bad_country(bad):
    with pytest.raises(Exception):
        UpdateProfileRequest(phone_country=bad)


def test_update_profile_forbids_unknown_field():
    with pytest.raises(Exception):
        UpdateProfileRequest(nickname="x")


class _FakeUser:
    def __init__(self):
        self.id = uuid.uuid4()
        self.email = "a@b.test"
        self.name = "A B"
        self.auth_provider = AuthProvider.GOOGLE
        self.phone_number = "+14155550123"
        self.phone_country = "US"
        self.sms_notifications_enabled = True


def test_profile_view_shape():
    view = _profile_view(_FakeUser())
    assert set(view) == {
        "id", "email", "name", "auth_provider",
        "phone_number", "phone_country", "sms_notifications_enabled",
    }
    assert view["auth_provider"] == "google"
    assert view["phone_number"] == "+14155550123"
    assert view["sms_notifications_enabled"] is True


# ---------------------------------------------------------------------------
# Reply-approval notification hook (fakes; no DB / no AWS)
# ---------------------------------------------------------------------------


class _Member:
    def __init__(self, *, phone, enabled=True, banned=False):
        self.id = uuid.uuid4()
        self.phone_number = phone
        self.phone_country = "US"
        self.sms_notifications_enabled = enabled
        self.is_banned = banned


class _Result:
    def __init__(self, rows=None, scalar=None, scalars=None):
        self._rows = rows or []
        self._scalar = scalar
        self._scalars = scalars or []

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        parent = self

        class _S:
            def all(self_inner):
                return parent._scalars

        return _S()


class _FakeSession:
    """Answers the three queries the hook runs, by call order."""

    def __init__(self, members_with_roles, pending_count, spent_rows):
        self._members = members_with_roles
        self._pending = pending_count
        self._spent = spent_rows
        self._n = 0

    async def execute(self, _stmt):
        self._n += 1
        if self._n == 1:
            # SELECT User, role  -> rows of (user, role)
            return _Result(rows=self._members)
        if self._n == 2:
            # SELECT count(pending)
            return _Result(scalar=self._pending)
        # SELECT total_cost_usd for the month -> scalars().all()
        return _Result(scalars=self._spent)

    async def commit(self):
        return None

    async def close(self):
        return None


def _factory(session):
    @asynccontextmanager
    async def _f():
        yield session

    return _f


@pytest.mark.asyncio
async def test_notify_texts_only_eligible_owners(monkeypatch):
    from app.services import approval_service

    owner_ok = _Member(phone="+14155550001")
    admin_ok = _Member(phone="+14155550002")
    member_no_cap = _Member(phone="+14155550003")  # Member: not a resolver
    owner_no_phone = _Member(phone=None)
    owner_disabled = _Member(phone="+14155550004", enabled=False)

    members = [
        (owner_ok, MemberRole.OWNER),
        (admin_ok, MemberRole.ADMIN),
        (member_no_cap, MemberRole.MEMBER),
        (owner_no_phone, MemberRole.OWNER),
        (owner_disabled, MemberRole.OWNER),
    ]
    # Two fresh sessions are opened: (1) recipients+count, then per-recipient the
    # cap-check session. Use one shared fake that resets its counter each open.
    sent: list = []

    async def _fake_send(**kwargs):
        sent.append(kwargs["phone_number"])
        return {"status": "sent"}

    import app.config as config_module
    from app.services import sns_service as sns_module

    monkeypatch.setattr(sns_module, "send_reply_approval_sms", _fake_send)

    # Disable the spend cap so the per-user cap-check session isn't needed.
    class _S:
        SNS_ENABLED = True
        SNS_MONTHLY_USER_SPEND_CAP_USD = 0

    monkeypatch.setattr(config_module, "get_settings", lambda: _S())

    session = _FakeSession(members, pending_count=1, spent_rows=[])
    await approval_service._notify_reply_awaiting_approval(
        session_factory=_factory(session),
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
    )

    # Only the Owner + Admin with a phone + enabled + not banned are texted.
    assert sorted(sent) == ["+14155550001", "+14155550002"]


@pytest.mark.asyncio
async def test_notify_is_best_effort_on_error(monkeypatch):
    from app.services import approval_service  # noqa: F401
    import app.config as config_module

    class _S:
        SNS_ENABLED = True
        SNS_MONTHLY_USER_SPEND_CAP_USD = 0

    monkeypatch.setattr(config_module, "get_settings", lambda: _S())

    class _Boom:
        async def execute(self, _stmt):
            raise RuntimeError("db down")

        async def close(self):
            return None

    # Must not raise even if the recipient query blows up.
    await approval_service._notify_reply_awaiting_approval(
        session_factory=_factory(_Boom()),
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_notify_noop_when_sns_disabled(monkeypatch):
    from app.services import approval_service  # noqa: F401
    import app.config as config_module
    from app.services import sns_service as sns_module

    class _S:
        SNS_ENABLED = False
        SNS_MONTHLY_USER_SPEND_CAP_USD = 0

    monkeypatch.setattr(config_module, "get_settings", lambda: _S())

    called = {"n": 0}

    async def _fake_send(**kwargs):
        called["n"] += 1
        return {"status": "sent"}

    monkeypatch.setattr(sns_module, "send_reply_approval_sms", _fake_send)

    # No session should even be opened; pass a factory that would error if used.
    @asynccontextmanager
    async def _boom_factory():
        raise AssertionError("should not open a session when SNS disabled")
        yield None

    await approval_service._notify_reply_awaiting_approval(
        session_factory=_boom_factory,
        workspace_id=uuid.uuid4(),
        approval_request_id=uuid.uuid4(),
    )
    assert called["n"] == 0
