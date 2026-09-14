"""Gmail poll helpers — cheap unread pre-check + durable "already-seen" set.

WHY this module exists (Bedrock cost-control)
---------------------------------------------
The scheduled poller (:func:`app.agents.tasks.poll_integrations`) used to
enqueue a FULL, multi-step Bedrock agent run every ``GMAIL_POLL_MINUTES`` for
every active integration UNCONDITIONALLY — even with an empty inbox — and the
agent re-read the SAME unread emails on every poll. Each run cost ~12 Bedrock
round-trips resending full email bodies + tool schemas.

These helpers let the poller do a PLAIN Gmail API call (NO Bedrock) to see if
there is anything genuinely new before spending a single model token, and to
remember which message ids have already been handed to a run so they are never
re-checked:

- :func:`list_unread_ids`  — one cheap ``messages.list?q=is:unread`` GET; returns
  the unread message ids (no bodies, no model).
- :func:`filter_new_ids`   — subtract ids already in ``processed_messages`` for
  this integration, preserving order, so only genuinely NEW ids remain.
- :func:`record_seen`       — idempotently insert ids into ``processed_messages``
  (UNIQUE ``(integration_id, provider_message_id)`` => re-inserts are no-ops).
- :func:`record_seen_one`   — convenience single-id wrapper used by the approval
  execution path so a sent/drafted reply's source email is marked seen too.

All three are dependency-injected (``call_provider_api`` / ``session``) so they
are unit-testable with fakes and NO live Gmail or database.

Overflow policy (DOCUMENTED CHOICE)
-----------------------------------
A poll caps how many new ids it hands to ONE run (``per_run_cap``, e.g. 10) to
bound cost. We record as seen ONLY the ids we actually pass to that run this
cycle; the overflow ids are left UNrecorded so a LATER poll picks them up (they
re-appear in the next ``list_unread_ids`` and become "new" again because they
were never recorded). This is the simpler of the two options in the brief: the
caller slices first, then records exactly the slice. See
:func:`app.agents.tasks.poll_integrations` for the call sequence.

Security / privacy
------------------
Tenancy is server-derived: ``workspace_id`` / ``integration_id`` come from the
integration DB row, never from model input. We log/return only ids and counts —
never email bodies, tokens, or credentials. Credentials are passed through to
``call_provider_api`` and never logged here.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedMessage

__all__ = [
    "list_unread_ids",
    "filter_new_ids",
    "record_seen",
    "record_seen_one",
    "DEFAULT_UNREAD_CAP",
]

#: Default cap on how many unread ids the cheap pre-check fetches per poll. This
#: bounds the size of the plain Gmail list call (still NO Bedrock); the caller
#: separately caps how many of the NEW ids it hands to a single agent run.
DEFAULT_UNREAD_CAP = 25

# The provider slug this module handles. Kept as a constant so a future poll
# provider can reuse filter_new_ids/record_seen by passing its own slug.
_GMAIL = "gmail"

# Injectable Gmail API seam signature (a subset of agent_tools.call_provider_api).
_CallProviderApi = Callable[..., Awaitable[dict[str, Any]]]


def _resp_ok(result: Any) -> bool:
    """Whether a call_provider_api result is a successful 2xx response.

    Mirrors the same predicate used in voice_tools/approval_service so the
    contract stays identical across callers.
    """
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    if "ok" in result:
        return bool(result.get("ok"))
    status = result.get("status_code")
    return isinstance(status, int) and 200 <= status < 300


async def list_unread_ids(
    *,
    credentials: dict[str, str],
    config: dict[str, str] | None,
    call_provider_api: _CallProviderApi,
    max_results: int = DEFAULT_UNREAD_CAP,
) -> list[str]:
    """Cheap unread pre-check: return the ids of unread Gmail messages (NO Bedrock).

    Makes ONE plain, authenticated Gmail API call
    (``GET /gmail/v1/users/me/messages?q=is:unread&maxResults=<cap>``) and parses
    the returned message ids. This is a normal HTTPS request — it never invokes
    a model — so running it on every poll is essentially free compared to a full
    agent run.

    Returns the ids in the order Gmail returned them (newest first). Returns an
    empty list when the inbox has no unread mail OR the call fails for any reason
    (fail-soft: a transient Gmail hiccup must NOT spuriously enqueue a run, and
    must not raise into the sweep). Never returns/logs bodies or credentials.

    Args:
        credentials: Decrypted Gmail credentials (passed straight through).
        config: Non-secret provider config.
        call_provider_api: Injectable Gmail API caller (the real
            ``agent_tools.call_provider_api`` in production; a fake in tests).
        max_results: Upper bound on ids fetched (clamped to 1..100).
    """
    cap = max(1, min(int(max_results or DEFAULT_UNREAD_CAP), 100))
    listing = await call_provider_api(
        provider_name=_GMAIL,
        credentials=credentials,
        config=config or {},
        method="GET",
        path="/gmail/v1/users/me/messages",
        query={"q": "is:unread", "maxResults": cap},
    )
    if not _resp_ok(listing):
        return []
    body = listing.get("body") if isinstance(listing, dict) else None
    messages = (body or {}).get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return []
    ids: list[str] = []
    for entry in messages:
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid:
                ids.append(mid)
    return ids


async def filter_new_ids(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID,
    ids: Sequence[str],
    provider: str = _GMAIL,
) -> list[str]:
    """Return the subset of ``ids`` NOT yet recorded as seen for this integration.

    Queries ``processed_messages`` for the rows matching this ``integration_id``
    and the given ids, then returns the input ids MINUS the already-seen ones,
    PRESERVING the input order and de-duplicating within the input. This is the
    "never re-check a seen email" filter: anything already handed to a previous
    run (or marked seen on send) is dropped so no Bedrock run is spent on it
    again.

    Tenant-safe: filters strictly by ``integration_id`` (which the caller derives
    from a server-side integration row), so one integration never sees another's
    processed set.
    """
    if not ids:
        return []
    # De-dup the input while preserving first-seen order.
    unique_ids: list[str] = []
    seen_input: set[str] = set()
    for mid in ids:
        if isinstance(mid, str) and mid and mid not in seen_input:
            seen_input.add(mid)
            unique_ids.append(mid)
    if not unique_ids:
        return []

    stmt = (
        select(ProcessedMessage.provider_message_id)
        .where(ProcessedMessage.integration_id == integration_id)
        .where(ProcessedMessage.provider == provider)
        .where(ProcessedMessage.provider_message_id.in_(unique_ids))
    )
    result = await session.execute(stmt)
    already: set[str] = {row[0] for row in result.all()}
    return [mid for mid in unique_ids if mid not in already]


async def record_seen(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    integration_id: uuid.UUID,
    ids: Iterable[str],
    provider: str = _GMAIL,
) -> int:
    """Idempotently mark ``ids`` as seen (processed) for this integration.

    Inserts one ``processed_messages`` row per id using ``INSERT ... ON CONFLICT
    DO NOTHING`` against the UNIQUE ``(integration_id, provider_message_id)``
    constraint, so calling this twice with overlapping ids is safe (the second
    call inserts nothing for the duplicates). Records the tenant ids
    server-side. Returns the number of ids submitted for insert (NOT the number
    of new rows, which the dialect does not reliably report cross-driver) so the
    caller can log a count without querying back.

    NOTE ON "seen" SEMANTICS: seen == processed, independent of read/unread. We
    record ids at the moment they are handed to a run (and again when a reply is
    sent/drafted) so a message is handled at most once even if it stays unread.
    """
    rows = []
    submitted: list[str] = []
    for mid in ids:
        if not isinstance(mid, str) or not mid:
            continue
        submitted.append(mid)
        rows.append(
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "integration_id": integration_id,
                "provider": provider,
                "provider_message_id": mid,
            }
        )
    if not rows:
        return 0
    stmt = pg_insert(ProcessedMessage).values(rows)
    # Idempotent: a re-insert of an already-seen (integration_id, id) is a no-op.
    stmt = stmt.on_conflict_do_nothing(
        constraint="uq_processed_message_integration_msg"
    )
    await session.execute(stmt)
    await session.flush()
    return len(submitted)


async def record_seen_one(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    integration_id: uuid.UUID,
    message_id: str | None,
    provider: str = _GMAIL,
) -> int:
    """Convenience wrapper: mark a single message id as seen (idempotent).

    Used by the approval execution path so a reply's ``source_message_id`` is
    recorded as seen when the reply is sent/drafted (see
    :func:`app.services.approval_service.execute_and_approve_request`). A falsy
    id is a no-op. Returns 1 when an id was submitted, else 0.
    """
    if not (isinstance(message_id, str) and message_id):
        return 0
    return await record_seen(
        session,
        workspace_id=workspace_id,
        integration_id=integration_id,
        ids=[message_id],
        provider=provider,
    )
