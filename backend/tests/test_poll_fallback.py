"""poll_integrations fallback: push providers whose live webhook never
registered (persisted status == "poll") must still be polled so they automate.

Covers the Gmail-on-localhost case: Gmail is structurally PUBSUB (not in
poll_providers()), but when no Pub/Sub topic is configured its persisted
webhook status is "poll" and the scheduler must sweep it.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.agents import tasks
from app.services.webhook_subscriptions import WEBHOOK_STATUS_KEY


@asynccontextmanager
async def _noop_lock(_integration_id, *, redis=None):
    """Always-acquire, Redis-free poll lock for unit tests (injected so these
    tests never touch a live Redis; the real lock is covered separately)."""
    yield True



class _Integ:
    def __init__(self, provider_name, category, status_val=None):
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.created_by_user_id = uuid.uuid4()
        self.provider_name = provider_name
        self.category = category
        self.status = None  # set to ACTIVE-equivalent by the query stub
        self.config = {WEBHOOK_STATUS_KEY: status_val} if status_val else {}


class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _ExecResult:
    """Minimal result for session.execute(): .all() returns row tuples."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """Fake session: scalars() yields integrations; execute() serves the
    processed_messages SELECT (returns no already-seen ids) and swallows the
    idempotent INSERT/flush from record_seen."""

    def __init__(self, items):
        self._items = items

    async def scalars(self, *_a, **_k):
        return _Scalars(self._items)

    async def execute(self, *_a, **_k):
        # No rows already seen -> filter_new_ids treats every unread id as new;
        # also serves the INSERT (returns an empty result which record_seen
        # ignores).
        return _ExecResult([])

    async def flush(self, *_a, **_k):
        return None


def _session_factory(items):
    @asynccontextmanager
    async def _factory():
        yield _Session(items)

    return _factory


def _fake_gmail_seams(unread_ids):
    """Return (resolve_gmail_credentials, call_provider_api) fakes for the cheap
    Gmail pre-check: resolve yields dummy creds; the API caller returns the given
    unread message ids from messages.list. NO Bedrock, NO network."""

    async def _resolve(_session, *, workspace_id):
        return (uuid.uuid4(), {"access_token": "t"}, {})

    async def _call(**kwargs):
        # Only messages.list is exercised by the pre-check.
        return {
            "status_code": 200,
            "ok": True,
            "body": {"messages": [{"id": mid} for mid in unread_ids]},
        }

    return _resolve, _call


@pytest.mark.asyncio
async def test_gmail_status_poll_is_enqueued(monkeypatch):
    # Gmail with genuinely-new unread mail -> exactly one run enqueued. The cheap
    # pre-check seams are injected so no live Gmail/Bedrock is touched.
    items = [_Integ("gmail", "email", status_val="poll")]
    enqueued: list[str] = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs["provider_name"])

    monkeypatch.setattr(tasks, "session_scope", _session_factory(items))
    import app.services.job_queue_bridge as jq
    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)
    # The function imports session_scope from app.db.session inside the body.
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "session_scope", _session_factory(items))

    resolve, call = _fake_gmail_seams(["m1", "m2"])
    count = await tasks.poll_integrations(
        {}, resolve_gmail_credentials=resolve, call_provider_api=call,
        poll_lock=_noop_lock,
    )
    assert count == 1
    assert enqueued == ["gmail"]


@pytest.mark.asyncio
async def test_gmail_no_unread_enqueues_nothing(monkeypatch):
    # The main saving: an empty/unchanged inbox enqueues ZERO runs (no Bedrock).
    items = [_Integ("gmail", "email", status_val="poll")]
    enqueued: list[str] = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs["provider_name"])

    monkeypatch.setattr(tasks, "session_scope", _session_factory(items))
    import app.services.job_queue_bridge as jq
    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "session_scope", _session_factory(items))

    resolve, call = _fake_gmail_seams([])  # no unread ids
    count = await tasks.poll_integrations(
        {}, resolve_gmail_credentials=resolve, call_provider_api=call,
        poll_lock=_noop_lock,
    )
    assert count == 0
    assert enqueued == []


@pytest.mark.asyncio
async def test_registered_push_provider_is_not_polled(monkeypatch):
    # A push provider with no poll status (e.g. registered) must be skipped.
    items = [_Integ("stripe", "erp", status_val="auto")]
    enqueued: list[str] = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs["provider_name"])

    import app.services.job_queue_bridge as jq
    import app.db.session as dbsess
    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)
    monkeypatch.setattr(dbsess, "session_scope", _session_factory(items))

    count = await tasks.poll_integrations({}, poll_lock=_noop_lock)
    assert count == 0
    assert enqueued == []


@pytest.mark.asyncio
async def test_structural_poll_provider_is_enqueued(monkeypatch):
    # A provider structurally in poll_providers() (e.g. notion) is always polled.
    items = [_Integ("notion", "office")]
    enqueued: list[str] = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs["provider_name"])

    import app.services.job_queue_bridge as jq
    import app.db.session as dbsess
    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)
    monkeypatch.setattr(dbsess, "session_scope", _session_factory(items))

    count = await tasks.poll_integrations({}, poll_lock=_noop_lock)
    assert count == 1
    assert enqueued == ["notion"]


# ---------------------------------------------------------------------------
# Per-integration poll lock (prevents overlapping runs on the same inbox)
# ---------------------------------------------------------------------------


def _lock_factory(acquired: bool, seen: list):
    """Return a poll_lock seam that records which integration ids it locked and
    yields ``acquired`` for the acquire result (True = got the lock)."""

    @asynccontextmanager
    async def _lock(integration_id, *, redis=None):
        seen.append(integration_id)
        yield acquired

    return _lock


@pytest.mark.asyncio
async def test_poll_lock_held_skips_integration(monkeypatch):
    # When the per-integration lock is already held (another run in flight), the
    # sweep must SKIP that integration and enqueue NOTHING — the structural fix
    # for the overlapping same-second runs behind the token blowup.
    items = [_Integ("gmail", "email", status_val="poll")]
    enqueued: list[str] = []
    locked: list = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs["provider_name"])

    monkeypatch.setattr(tasks, "session_scope", _session_factory(items))
    import app.services.job_queue_bridge as jq
    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "session_scope", _session_factory(items))

    resolve, call = _fake_gmail_seams(["m1", "m2"])  # inbox HAS new mail
    count = await tasks.poll_integrations(
        {},
        resolve_gmail_credentials=resolve,
        call_provider_api=call,
        poll_lock=_lock_factory(acquired=False, seen=locked),
    )
    # Lock was attempted for the integration, but not acquired -> no enqueue.
    assert locked == [items[0].id]
    assert count == 0
    assert enqueued == []


@pytest.mark.asyncio
async def test_poll_lock_acquired_processes_once(monkeypatch):
    # When the lock IS acquired, the integration is processed exactly once and
    # the lock was taken for that specific integration id.
    items = [_Integ("gmail", "email", status_val="poll")]
    enqueued: list[str] = []
    locked: list = []

    async def _fake_enqueue(**kwargs):
        enqueued.append(kwargs["provider_name"])

    monkeypatch.setattr(tasks, "session_scope", _session_factory(items))
    import app.services.job_queue_bridge as jq
    monkeypatch.setattr(jq, "enqueue_scheduled_run", _fake_enqueue)
    import app.db.session as dbsess
    monkeypatch.setattr(dbsess, "session_scope", _session_factory(items))

    resolve, call = _fake_gmail_seams(["m1"])
    count = await tasks.poll_integrations(
        {},
        resolve_gmail_credentials=resolve,
        call_provider_api=call,
        poll_lock=_lock_factory(acquired=True, seen=locked),
    )
    assert locked == [items[0].id]
    assert count == 1
    assert enqueued == ["gmail"]


@pytest.mark.asyncio
async def test_pre_check_caps_batch_to_max_ids_per_run():
    # Batch-size cap (task 1): when MANY unread ids are new, the pre-check hands
    # only ``per_run_cap`` of them to a single run. Bounds tokens/cost per run
    # even for a scoped run. Uses the DB-free fakes so it runs everywhere and
    # pins the cap explicitly (no dependency on env/settings, which conftest
    # strips per test).
    from app.agents import tasks

    cap = 5
    integ = _Integ("gmail", "email", status_val="poll")
    many = [f"m{i}" for i in range(cap + 7)]  # more unread than the cap
    resolve, call = _fake_gmail_seams(many)

    scoped = await tasks._gmail_new_ids_for_poll(
        _Session([integ]),
        integ,
        resolve_gmail_credentials=resolve,
        call_provider_api=call,
        per_run_cap=cap,
    )
    assert scoped is not None
    assert len(scoped) == cap
    assert scoped == many[:cap]
