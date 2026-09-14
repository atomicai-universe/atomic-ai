"""Session token issuance and validity logic.

This module is split into two clearly separated layers so the security-critical
*decision* logic can be property-tested without any I/O:

- **Pure functions** — :func:`issue_session_token` and :func:`is_session_valid`.
  They take a timezone-aware ``now`` as an argument (they never call
  :func:`datetime.now` internally), so they are deterministic and exhaustively
  testable across a large generated input space.
- **Persistence helpers** — :func:`persist_session`, :func:`load_session`,
  :func:`revoke_session`, and the Redis fast-path helpers. These perform the
  actual database and cache I/O and are kept out of the pure functions.

Design decisions (see design.md "Auth_Service"):

- Session tokens are **opaque, server-tracked records** (a ``sessions`` table
  plus a Redis fast-path), not self-contained JWTs, so logout (Req 2.5) and a
  super admin ban (Req 12.4) are true invalidation.
- The raw token is a cryptographically random URL-safe value produced with
  :func:`secrets.token_urlsafe`. Only a SHA-256 **hash** of the token is
  persisted in the ``sessions.token`` ``bytea`` column; the raw token is
  returned to the caller and never stored in plaintext.

Requirements: 2.3 (expiry bounded to 24h). The pure validity check additionally
backs Req 2.2 and 2.4 (reject missing/invalid/expired sessions).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

# Session lifetime. Tokens expire no later than 24 hours after issuance
# (Req 2.3). Exposed as a module-level constant so callers and tests share a
# single source of truth.
SESSION_TTL: timedelta = timedelta(hours=24)

# Number of random bytes of entropy in a raw session token. 32 bytes (256 bits)
# is well beyond guessing range; ``token_urlsafe`` renders it as ~43 URL-safe
# characters.
_TOKEN_ENTROPY_BYTES: int = 32

# Redis key prefix for the session fast-path. The value stored is the session's
# expiry, and the key's own TTL mirrors the session expiry so stale entries
# self-evict.
_REDIS_SESSION_PREFIX = "session:"


# ---------------------------------------------------------------------------
# Session record protocol (structural typing)
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionRecord(Protocol):
    """The minimal read surface :func:`is_session_valid` needs.

    The ``Session`` ORM row (``app.db.models.Session``) satisfies this protocol
    structurally, and so does any lightweight dataclass exposing the same two
    fields, which keeps the pure validity check free of any ORM dependency.
    """

    revoked: bool
    expires_at: datetime


# ---------------------------------------------------------------------------
# Token hashing (shared by issuance and lookup)
# ---------------------------------------------------------------------------


def hash_token(raw_token: str) -> bytes:
    """Return the SHA-256 digest of a raw token as ``bytes``.

    The digest is what gets persisted in the ``sessions.token`` column and what
    is used to look a session up, so the raw token is never stored at rest.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# Pure decision functions (no I/O; safe to property-test)
# ---------------------------------------------------------------------------


def issue_session_token(user_id: uuid.UUID, now: datetime) -> tuple[str, datetime]:
    """Mint a new opaque session token and its expiry.

    Pure and deterministic apart from the cryptographic randomness of the token
    value: ``expires_at`` is computed solely from the supplied ``now`` and
    :data:`SESSION_TTL`, so it is exactly ``now + 24h`` and always satisfies
    ``now < expires_at <= now + 24h`` (Req 2.3). ``now`` must be a
    timezone-aware datetime supplied by the caller; this function never reads
    the wall clock.

    Args:
        user_id: The user the session belongs to. Accepted so callers pass it
            straight through to :func:`persist_session`; the token itself is
            random and does not encode the id.
        now: The timezone-aware issuance instant.

    Returns:
        A ``(raw_token, expires_at)`` pair. ``raw_token`` is the opaque value to
        hand to the client; only its hash should be persisted.
    """
    del user_id  # Not encoded in the opaque token; kept for a stable signature.
    raw_token = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    expires_at = now + SESSION_TTL
    return raw_token, expires_at


def is_session_valid(record: SessionRecord | None, now: datetime) -> bool:
    """Return ``True`` only for a live session at time ``now``.

    A session is valid exactly when the record exists, is not revoked, and the
    current time is strictly before its expiry. Missing (``None``), revoked, or
    expired records are never valid (Req 2.2, 2.4).

    Args:
        record: The session record (ORM row or any :class:`SessionRecord`), or
            ``None`` when no matching session was found.
        now: The timezone-aware instant to evaluate validity at.
    """
    if record is None:
        return False
    if record.revoked:
        return False
    return now < record.expires_at


# ---------------------------------------------------------------------------
# Persistence helpers (DB + Redis fast-path) — kept separate from the pure logic
# ---------------------------------------------------------------------------


def _redis_session_key(token_hash: bytes) -> str:
    """Return the Redis key for a session, derived from its token hash."""
    return f"{_REDIS_SESSION_PREFIX}{token_hash.hex()}"


async def persist_session(
    session,  # sqlalchemy.ext.asyncio.AsyncSession
    *,
    user_id: uuid.UUID,
    raw_token: str,
    expires_at: datetime,
):
    """Persist a new session row storing only the token's hash.

    Creates and flushes a ``Session`` row whose ``token`` column holds
    ``sha256(raw_token)`` (never the raw token). Returns the persisted row.
    """
    from app.db.models import Session  # local import avoids a module import cycle

    record = Session(
        user_id=user_id,
        token=hash_token(raw_token),
        revoked=False,
        expires_at=expires_at,
    )
    session.add(record)
    await session.flush()
    return record


async def load_session(session, raw_token: str):
    """Load the ``Session`` row for a raw token, or ``None`` if absent.

    Looks the row up by the token's hash so the raw value is never used in the
    query.
    """
    from sqlalchemy import select

    from app.db.models import Session

    result = await session.execute(
        select(Session).where(Session.token == hash_token(raw_token))
    )
    return result.scalar_one_or_none()


async def revoke_session(session, raw_token: str) -> bool:
    """Mark the session for ``raw_token`` revoked. Return whether one matched.

    Backs logout (Req 2.5) and ban-driven invalidation (Req 12.4). Also removes
    the Redis fast-path entry when a client is supplied by the caller elsewhere.
    """
    record = await load_session(session, raw_token)
    if record is None:
        return False
    record.revoked = True
    await session.flush()
    return True


async def cache_session(redis, raw_token: str, expires_at: datetime, now: datetime) -> None:
    """Write the Redis fast-path entry with a TTL mirroring session expiry.

    The stored value is the ISO-8601 expiry; the key's own TTL is the remaining
    lifetime so a stale entry self-evicts. A non-positive remaining lifetime is
    not cached.
    """
    remaining = expires_at - now
    ttl_seconds = int(remaining.total_seconds())
    if ttl_seconds <= 0:
        return
    key = _redis_session_key(hash_token(raw_token))
    await redis.set(key, expires_at.isoformat(), ex=ttl_seconds)


async def evict_cached_session(redis, raw_token: str) -> None:
    """Remove a session's Redis fast-path entry (used on logout/ban)."""
    await redis.delete(_redis_session_key(hash_token(raw_token)))


async def is_cached_session_present(redis, raw_token: str) -> bool:
    """Return whether the Redis fast-path still holds a live entry for a token.

    Presence implies the session has not been evicted (logout/ban) and has not
    yet reached its TTL; a ``True`` result lets callers skip the DB round-trip.
    Absence falls back to the authoritative DB check.
    """
    return bool(await redis.exists(_redis_session_key(hash_token(raw_token))))


__all__ = [
    "SESSION_TTL",
    "SessionRecord",
    "hash_token",
    "issue_session_token",
    "is_session_valid",
    "persist_session",
    "load_session",
    "revoke_session",
    "cache_session",
    "evict_cached_session",
    "is_cached_session_present",
]
