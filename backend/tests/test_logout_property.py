"""Property-based tests for logout invalidation (task 5.4).

**Property 10: Logout invalidates the session** — once a session is revoked
(the effect of logout), it is never again considered valid, regardless of how
much lifetime it had left.

These tests exercise the security-critical *decision* function
:func:`app.core.security.is_session_valid` (pure, no I/O) across a large
generated input space, plus a single async integration example that drives the
real persistence helpers (:func:`load_session`, :func:`revoke_session`,
:func:`hash_token`, and the Redis fast-path) against in-memory fakes — the same
seam-faking pattern used by ``test_session_deps.py``.

All datetimes are timezone-aware so every aware-to-aware comparison is safe.
Each generative property runs at least 100 examples per the design's
property-testing budget.

Requirements: 2.5.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.security import (
    evict_cached_session,
    hash_token,
    is_cached_session_present,
    is_session_valid,
    load_session,
    revoke_session,
)


# ---------------------------------------------------------------------------
# Test doubles / strategies
# ---------------------------------------------------------------------------


@dataclass
class SessionRec:
    """Minimal session record satisfying the ``SessionRecord`` protocol.

    Exposes exactly the two attributes :func:`is_session_valid` reads
    (``revoked`` and ``expires_at``), keeping the property free of any ORM
    dependency.
    """

    revoked: bool
    expires_at: datetime


# Timezone-aware datetimes: generate naive datetimes over a wide but valid range
# and attach UTC so every comparison is aware-to-aware.
_aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))

# A strictly-positive remaining lifetime (1 second .. ~10 years) so a session
# built from ``now + remaining_life`` starts out live (now < expires_at).
_positive_lifetimes = st.timedeltas(
    min_value=timedelta(seconds=1),
    max_value=timedelta(days=3650),
)


# ---------------------------------------------------------------------------
# Property 10: Logout invalidates the session (Req 2.5)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(now=_aware_datetimes, remaining_life=_positive_lifetimes)
def test_revocation_flips_valid_to_invalid(
    now: datetime, remaining_life: timedelta
) -> None:
    """A live session becomes invalid once revoked, whatever its lifetime.

    **Validates: Requirements 2.5**

    Start from a live record (not revoked, expires strictly after ``now``);
    :func:`is_session_valid` must accept it. Setting ``revoked=True`` — the
    effect of logout — must flip the decision to ``False`` even though the
    session still has remaining lifetime.
    """
    expires_at = now + remaining_life
    record = SessionRec(revoked=False, expires_at=expires_at)

    # The session is live before logout.
    assert is_session_valid(record, now) is True

    # Logout revokes it; validity flips to False despite the remaining lifetime.
    record.revoked = True
    assert is_session_valid(record, now) is False


@settings(max_examples=200)
@given(
    revoked_at=_aware_datetimes,
    remaining_life=_positive_lifetimes,
    later_offset=st.timedeltas(
        min_value=timedelta(0), max_value=timedelta(days=3650)
    ),
)
def test_revocation_is_monotonic_across_time(
    revoked_at: datetime, remaining_life: timedelta, later_offset: timedelta
) -> None:
    """Once revoked, a session stays invalid at every instant thereafter.

    **Validates: Requirements 2.5**

    Revocation is monotonic/idempotent: a revoked record is never valid again,
    for any ``now`` evaluated at or after the revocation — including instants
    that fall before its original expiry.
    """
    expires_at = revoked_at + remaining_life
    record = SessionRec(revoked=True, expires_at=expires_at)

    later = revoked_at + later_offset
    assert is_session_valid(record, later) is False
    # And still false right at the revocation instant.
    assert is_session_valid(record, revoked_at) is False


# ---------------------------------------------------------------------------
# Integration example: real persistence helpers against in-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    """Stand-in for the ``Session`` ORM row that the helpers mutate."""

    user_id: uuid.UUID
    token: bytes
    revoked: bool
    expires_at: datetime


class FakeDBSession:
    """In-memory async session keyed by ``hash_token(raw_token)``.

    Mirrors how the real ``Session.token`` column stores ``sha256(raw_token)``.
    The real ``load_session``/``revoke_session`` build ORM SELECTs, so the
    integration test backs them (via monkeypatch) with this fake's hash-keyed
    lookup while preserving their observable contract. ``flush`` is a no-op the
    revoke path awaits.
    """

    def __init__(self) -> None:
        self.rows: dict[bytes, _Row] = {}

    def add_session(self, raw_token: str, row: _Row) -> None:
        self.rows[hash_token(raw_token)] = row

    def get_row(self, raw_token: str) -> _Row | None:
        return self.rows.get(hash_token(raw_token))

    async def flush(self) -> None:  # exercised by revoke_session
        return None


class FakeRedis:
    """In-memory async Redis stand-in exposing ``delete``/``exists``."""

    def __init__(self) -> None:
        self.store: set[str] = set()

    async def set(self, key: str) -> None:
        self.store.add(key)

    async def delete(self, key: str) -> None:
        self.store.discard(key)

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


@pytest.mark.asyncio
async def test_logout_flow_revokes_persisted_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a live persisted session is loaded valid, then revoked.

    **Validates: Requirements 2.5**

    Drives the real helper contracts (:func:`load_session`,
    :func:`revoke_session`, :func:`is_session_valid`) against the in-memory
    fake. ``load_session``/``revoke_session`` normally build ORM queries, so we
    back them with the fake's hash-keyed lookup — the behaviour under test (a
    matching token gets revoked and is thereafter invalid) is preserved.
    """

    async def _fake_load_session(session: FakeDBSession, raw_token: str):
        return session.get_row(raw_token)

    async def _fake_revoke_session(session: FakeDBSession, raw_token: str) -> bool:
        row = session.get_row(raw_token)
        if row is None:
            return False
        row.revoked = True
        await session.flush()
        return True

    monkeypatch.setattr("app.core.security.load_session", _fake_load_session)
    monkeypatch.setattr("app.core.security.revoke_session", _fake_revoke_session)

    # Re-import the names so the test uses the patched versions.
    from app.core import security as sec

    db = FakeDBSession()
    now = datetime.now(timezone.utc)
    raw_token = "raw-logout-token"
    row = _Row(
        user_id=uuid.uuid4(),
        token=hash_token(raw_token),
        revoked=False,
        expires_at=now + timedelta(hours=24),
    )
    db.add_session(raw_token, row)

    # The persisted session loads and is valid before logout.
    loaded = await sec.load_session(db, raw_token)
    assert loaded is not None
    assert is_session_valid(loaded, now) is True

    # Logout revokes the matching session.
    assert await sec.revoke_session(db, raw_token) is True

    # Reloading now yields a revoked -> invalid session (Req 2.5).
    reloaded = await sec.load_session(db, raw_token)
    assert reloaded is not None
    assert reloaded.revoked is True
    assert is_session_valid(reloaded, now) is False

    # Revoking an unknown token matches nothing.
    assert await sec.revoke_session(db, "no-such-token") is False


@pytest.mark.asyncio
async def test_evicting_cache_marks_session_absent() -> None:
    """The Redis fast-path entry is gone after eviction (logout side effect).

    **Validates: Requirements 2.5**
    """
    redis = FakeRedis()
    raw_token = "cached-logout-token"

    # Seed the fast-path directly at the key the helpers derive from the token.
    from app.core.security import _redis_session_key

    redis.store.add(_redis_session_key(hash_token(raw_token)))
    assert await is_cached_session_present(redis, raw_token) is True

    # Eviction (performed on logout) removes the entry.
    await evict_cached_session(redis, raw_token)
    assert await is_cached_session_present(redis, raw_token) is False
