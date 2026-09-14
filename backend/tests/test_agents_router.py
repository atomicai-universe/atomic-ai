"""Tests for the agents router: trigger tenant-scoped agent runs (task 11.5).

Coverage:

- **TRIGGER_WORKFLOW enforcement** — Owner/Admin/Member may trigger a run
  (202); Viewer is read-only and is rejected (403).
- **Req 16.3** — the enqueued job carries the caller's active ``workspace_id``
  and ``user_id`` so the task re-applies that tenant scope when it runs. A fake
  arq pool records the enqueue, and the test asserts the serialized scope.
- **Workspace guarding** — a body ``workspace_id`` that disagrees with the
  active workspace is rejected (400); no active workspace is rejected (400).

No live Redis or database is needed: ``require_session`` is overridden to return
a chosen :class:`RequestContext`, and the pool dependency is overridden with a
fake exposing ``enqueue_job``. The app is driven with ``httpx.AsyncClient`` +
``ASGITransport`` (mirroring ``tests/test_rules_router.py``).

Requirements: 16.3.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.agents.router import get_pool, router as agents_router
from app.api import deps
from app.core.errors import install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import MemberRole


class _FakePool:
    """Fake arq pool recording ``enqueue_job`` calls (no Redis)."""

    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, function, *args, **kwargs):
        self.enqueued.append((function, kwargs))
        return type("Job", (), {"job_id": "job-abc"})()


def _make_context(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID | None, role: MemberRole | None
) -> RequestContext:
    roles = {workspace_id: role} if (workspace_id and role) else {}
    return RequestContext(
        user_id=user_id,
        active_workspace_id=workspace_id,
        is_superadmin=False,
        roles=roles,
    )


def _make_app(ctx: RequestContext, pool: _FakePool) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(agents_router)

    def _require_session_override() -> RequestContext:
        return ctx

    async def _get_pool_override():
        yield pool

    app.dependency_overrides[deps.require_session] = _require_session_override
    app.dependency_overrides[get_pool] = _get_pool_override
    return app


def _client(ctx: RequestContext, pool: _FakePool) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_make_app(ctx, pool))
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    )


# ===========================================================================
# TRIGGER_WORKFLOW enforcement + Req 16.3 scope carried into the job
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MEMBER])
async def test_member_and_above_can_trigger_and_scope_is_enqueued(role):
    ws = uuid.uuid4()
    user = uuid.uuid4()
    ctx = _make_context(user_id=user, workspace_id=ws, role=role)
    pool = _FakePool()
    async with _client(ctx, pool) as client:
        resp = await client.post(
            "/api/v1/agents/run",
            json={"kind": "webhook", "prompt": "Do the thing."},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["workspace_id"] == str(ws)
    assert body["triggered_by_user_id"] == str(user)
    assert body["job_id"] == "job-abc"

    # Req 16.3 — the job carries the caller's active workspace + user id.
    assert len(pool.enqueued) == 1
    function, kwargs = pool.enqueued[0]
    assert function == "process_webhook"
    assert kwargs["workspace_id"] == str(ws)
    assert kwargs["triggered_by_user_id"] == str(user)
    # The prompt is carried through the webhook payload.
    assert kwargs["payload"]["prompt"] == "Do the thing."


@pytest.mark.asyncio
async def test_viewer_cannot_trigger_workflow():
    ws = uuid.uuid4()
    ctx = _make_context(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.VIEWER)
    pool = _FakePool()
    async with _client(ctx, pool) as client:
        resp = await client.post("/api/v1/agents/run", json={"kind": "webhook"})
    assert resp.status_code == 403, resp.text
    # Nothing enqueued on a rejected trigger.
    assert pool.enqueued == []


# ===========================================================================
# scheduled kind + explicit prompt
# ===========================================================================


@pytest.mark.asyncio
async def test_scheduled_kind_enqueues_scheduled_task_with_prompt():
    ws = uuid.uuid4()
    user = uuid.uuid4()
    ctx = _make_context(user_id=user, workspace_id=ws, role=MemberRole.ADMIN)
    pool = _FakePool()
    async with _client(ctx, pool) as client:
        resp = await client.post(
            "/api/v1/agents/run",
            json={"kind": "scheduled", "prompt": "Nightly run."},
        )
    assert resp.status_code == 202, resp.text
    function, kwargs = pool.enqueued[0]
    assert function == "run_scheduled_agent"
    assert kwargs["workspace_id"] == str(ws)
    assert kwargs["triggered_by_user_id"] == str(user)
    assert kwargs["prompt"] == "Nightly run."
    # scheduled runs carry no webhook payload.
    assert "payload" not in kwargs


# ===========================================================================
# Workspace guarding (Req 16.2 / scope integrity)
# ===========================================================================


@pytest.mark.asyncio
async def test_no_active_workspace_is_rejected():
    ctx = _make_context(user_id=uuid.uuid4(), workspace_id=None, role=None)
    pool = _FakePool()
    async with _client(ctx, pool) as client:
        resp = await client.post("/api/v1/agents/run", json={"kind": "webhook"})
    assert resp.status_code == 400, resp.text
    assert pool.enqueued == []


@pytest.mark.asyncio
async def test_body_workspace_mismatch_is_rejected():
    ws = uuid.uuid4()
    other = uuid.uuid4()
    ctx = _make_context(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    pool = _FakePool()
    async with _client(ctx, pool) as client:
        resp = await client.post(
            "/api/v1/agents/run",
            json={"kind": "webhook", "workspace_id": str(other)},
        )
    assert resp.status_code == 400, resp.text
    assert pool.enqueued == []


@pytest.mark.asyncio
async def test_matching_body_workspace_is_accepted():
    ws = uuid.uuid4()
    ctx = _make_context(user_id=uuid.uuid4(), workspace_id=ws, role=MemberRole.OWNER)
    pool = _FakePool()
    async with _client(ctx, pool) as client:
        resp = await client.post(
            "/api/v1/agents/run",
            json={"kind": "webhook", "workspace_id": str(ws)},
        )
    assert resp.status_code == 202, resp.text
    _function, kwargs = pool.enqueued[0]
    assert kwargs["workspace_id"] == str(ws)
