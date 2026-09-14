"""Gmail PUSH (Pub/Sub webhook) cost-control.

Root cause of post-fix token accumulation: the WEBHOOK path (process_webhook)
had no unread pre-check, no de-dup, and no lock, so every Gmail push triggered a
generic "scan the whole inbox and reply to everything" run (~155K tokens each).
``_handle_gmail_push`` reuses the poll-path machinery so push == poll:
lock -> cheap pre-check + de-dup -> SCOPED run (or nothing).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

import app.api.webhooks as wh
from app.agents import tasks as tasks


class _Integ:
    def __init__(self):
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.created_by_user_id = uuid.uuid4()
        self.category = "email"


def _lock(acquired: bool):
    @asynccontextmanager
    async def _l(_integration_id, *, redis=None):
        yield acquired
    return _l


@pytest.mark.asyncio
async def test_gmail_push_new_mail_enqueues_scoped_run(monkeypatch):
    # New unread ids -> exactly one SCOPED run (prompt names the ids, and tells
    # the agent NOT to scan the inbox). No whole-inbox scan.
    enq: list[dict] = []

    async def _fake_enqueue(**kw):
        enq.append(kw)

    async def _fake_precheck(session, integ, **kw):
        return ["m1", "m2"]

    monkeypatch.setattr(wh.jq, "enqueue_webhook_run", _fake_enqueue)
    monkeypatch.setattr(tasks, "_integration_poll_lock", _lock(True))
    monkeypatch.setattr(tasks, "_gmail_new_ids_for_poll", _fake_precheck)

    await wh._handle_gmail_push(session=None, integration=_Integ())

    assert len(enq) == 1
    prompt = enq[0]["payload"]["prompt"]
    assert "m1" in prompt and "m2" in prompt
    assert "Do NOT list or scan the inbox" in prompt


@pytest.mark.asyncio
async def test_gmail_push_no_new_mail_enqueues_nothing(monkeypatch):
    # Empty/already-seen inbox -> ZERO runs (no Bedrock spent). This is the
    # primary saving versus the old generic webhook prompt.
    enq: list[dict] = []

    async def _fake_enqueue(**kw):
        enq.append(kw)

    async def _fake_precheck(session, integ, **kw):
        return []

    monkeypatch.setattr(wh.jq, "enqueue_webhook_run", _fake_enqueue)
    monkeypatch.setattr(tasks, "_integration_poll_lock", _lock(True))
    monkeypatch.setattr(tasks, "_gmail_new_ids_for_poll", _fake_precheck)

    await wh._handle_gmail_push(session=None, integration=_Integ())

    assert enq == []


@pytest.mark.asyncio
async def test_gmail_push_locked_skips(monkeypatch):
    # Another run already in flight (lock held) -> skip; the pre-check is not
    # even reached and nothing is enqueued (stops duplicate concurrent pushes).
    enq: list[dict] = []
    precheck_called = {"n": 0}

    async def _fake_enqueue(**kw):
        enq.append(kw)

    async def _fake_precheck(session, integ, **kw):
        precheck_called["n"] += 1
        return ["m1"]

    monkeypatch.setattr(wh.jq, "enqueue_webhook_run", _fake_enqueue)
    monkeypatch.setattr(tasks, "_integration_poll_lock", _lock(False))
    monkeypatch.setattr(tasks, "_gmail_new_ids_for_poll", _fake_precheck)

    await wh._handle_gmail_push(session=None, integration=_Integ())

    assert enq == []
    assert precheck_called["n"] == 0
