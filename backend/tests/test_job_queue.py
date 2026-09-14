"""Tests for the Job_Queue ARQ tasks (task 11.5).

Focus: **tenant isolation of queued jobs** (Req 16.3). Both task functions
(:func:`app.agents.tasks.process_webhook` and
:func:`app.agents.tasks.run_scheduled_agent`) must carry the originating
``workspace_id`` / ``triggered_by_user_id`` in their arguments and RE-APPLY that
scope when they run — i.e. thread the *same* ids into the Strands engine run.

These are pure unit tests: the engine run and the DB session are injected as
fakes, so no live Redis, no live Postgres, and no model call are needed. The
fake engine-run records exactly what ids it was invoked with, and the tests
assert they equal the ids handed to the job (proving the scope is carried
across — Req 16.3).

:func:`enqueue_agent_run` is tested against a fake pool exposing ``enqueue_job``
to confirm the workspace scope is serialized into the job kwargs.

Requirements: 16.3.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.agents import tasks as job_queue


class _FakeSession:
    """A stand-in DB session; the fake engine-run never touches it."""


@asynccontextmanager
async def _fake_session_factory():
    """Async-context-manager factory yielding a throwaway session."""
    yield _FakeSession()


class _RecordingRunAgent:
    """Fake ``strands_engine.run_agent`` recording its scoping arguments."""

    def __init__(self, result="ran"):
        self.calls: list[dict] = []
        self.result = result

    async def __call__(self, session, **kwargs):
        self.calls.append({"session": session, **kwargs})
        return self.result


# ===========================================================================
# Req 16.3 — process_webhook carries + re-applies the workspace scope
# ===========================================================================


@pytest.mark.asyncio
async def test_process_webhook_carries_workspace_scope_into_run():
    ws = uuid.uuid4()
    user = uuid.uuid4()
    fake_run = _RecordingRunAgent()

    result = await job_queue.process_webhook(
        {},  # arq ctx
        workspace_id=ws,
        triggered_by_user_id=user,
        payload={"prompt": "Handle the inbound event."},
        run_agent=fake_run,
        session_factory=_fake_session_factory,
    )

    assert result == "ran"
    assert len(fake_run.calls) == 1
    call = fake_run.calls[0]
    # The SAME ids given to the job are used for the run (Req 16.3).
    assert call["workspace_id"] == ws
    assert call["triggered_by_user_id"] == user
    assert call["prompt"] == "Handle the inbound event."
    # A run loop is NOT forced when not injected (engine uses its default).
    assert "run_loop" not in call


@pytest.mark.asyncio
async def test_process_webhook_accepts_string_ids_and_forwards_run_loop():
    """arq serializes args to JSON, so string ids must be coerced back to UUID."""
    ws = uuid.uuid4()
    user = uuid.uuid4()
    fake_run = _RecordingRunAgent()
    sentinel_loop = object()

    await job_queue.process_webhook(
        {},
        workspace_id=str(ws),
        triggered_by_user_id=str(user),
        payload={},
        run_agent=fake_run,
        run_loop=sentinel_loop,
        session_factory=_fake_session_factory,
    )

    call = fake_run.calls[0]
    assert call["workspace_id"] == ws
    assert call["triggered_by_user_id"] == user
    # An injected loop is forwarded so tests never call a real model.
    assert call["run_loop"] is sentinel_loop


# ===========================================================================
# Req 16.3 — run_scheduled_agent carries + re-applies the workspace scope
# ===========================================================================


@pytest.mark.asyncio
async def test_run_scheduled_agent_carries_workspace_scope_into_run():
    ws = uuid.uuid4()
    user = uuid.uuid4()
    fake_run = _RecordingRunAgent()

    await job_queue.run_scheduled_agent(
        {},
        workspace_id=ws,
        triggered_by_user_id=user,
        prompt="Nightly summary.",
        category="scheduled",
        run_agent=fake_run,
        session_factory=_fake_session_factory,
    )

    call = fake_run.calls[0]
    assert call["workspace_id"] == ws
    assert call["triggered_by_user_id"] == user
    assert call["prompt"] == "Nightly summary."
    assert call["category"] == "scheduled"


@pytest.mark.asyncio
async def test_two_jobs_never_cross_tenant_scope():
    """Distinct jobs each run only with their own workspace id (Req 16.3)."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    fake_run = _RecordingRunAgent()

    await job_queue.process_webhook(
        {}, workspace_id=ws_a, triggered_by_user_id=user_a, payload={},
        run_agent=fake_run, session_factory=_fake_session_factory,
    )
    await job_queue.run_scheduled_agent(
        {}, workspace_id=ws_b, triggered_by_user_id=user_b,
        run_agent=fake_run, session_factory=_fake_session_factory,
    )

    assert fake_run.calls[0]["workspace_id"] == ws_a
    assert fake_run.calls[0]["triggered_by_user_id"] == user_a
    assert fake_run.calls[1]["workspace_id"] == ws_b
    assert fake_run.calls[1]["triggered_by_user_id"] == user_b
    # No leakage across the two runs.
    assert fake_run.calls[0]["workspace_id"] != fake_run.calls[1]["workspace_id"]


# ===========================================================================
# enqueue_agent_run — serializes the workspace scope into the job kwargs
# ===========================================================================


class _FakePool:
    """Fake arq pool recording ``enqueue_job`` calls (no Redis)."""

    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, function, *args, **kwargs):
        self.enqueued.append((function, kwargs))
        return type("Job", (), {"job_id": "job-123"})()


@pytest.mark.asyncio
async def test_enqueue_agent_run_serializes_workspace_scope():
    ws = uuid.uuid4()
    user = uuid.uuid4()
    pool = _FakePool()

    job = await job_queue.enqueue_agent_run(
        pool,
        task_name=job_queue.WEBHOOK_TASK_NAME,
        workspace_id=ws,
        triggered_by_user_id=user,
        payload={"prompt": "go"},
        category="webhook",
    )

    assert job.job_id == "job-123"
    assert len(pool.enqueued) == 1
    function, kwargs = pool.enqueued[0]
    assert function == job_queue.WEBHOOK_TASK_NAME
    # The tenant scope is carried in the job kwargs as JSON-safe strings.
    assert kwargs["workspace_id"] == str(ws)
    assert kwargs["triggered_by_user_id"] == str(user)
    assert kwargs["payload"] == {"prompt": "go"}
    assert kwargs["category"] == "webhook"


@pytest.mark.asyncio
async def test_enqueue_agent_run_omits_payload_when_none():
    pool = _FakePool()
    await job_queue.enqueue_agent_run(
        pool,
        task_name=job_queue.SCHEDULED_TASK_NAME,
        workspace_id=uuid.uuid4(),
        triggered_by_user_id=uuid.uuid4(),
        prompt="scheduled goal",
    )
    _function, kwargs = pool.enqueued[0]
    assert "payload" not in kwargs
    assert kwargs["prompt"] == "scheduled goal"


def test_worker_settings_registers_both_tasks():
    """WorkerSettings must register both Job_Queue tasks for a worker to run."""
    assert job_queue.process_webhook in job_queue.WorkerSettings.functions
    assert job_queue.run_scheduled_agent in job_queue.WorkerSettings.functions
