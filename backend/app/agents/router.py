"""Agents router — trigger tenant-scoped agent runs via the Job_Queue (task 11.5).

Wires the Job_Queue tasks in :mod:`app.agents.tasks` behind a protected HTTP
endpoint under the ``/api/v1/agents`` prefix. The one route here — ``POST
/api/v1/agents/run`` — lets an authorized member trigger an agent workflow,
which is enqueued as an ARQ job carrying the originating workspace's scope
(Req 16.3).

Authorization
-------------
The route requires a valid Session_Token (:func:`app.api.deps.require_session`)
and the :class:`~app.core.rbac.Capability` ``TRIGGER_WORKFLOW`` (held by
**Owner/Admin/Member**; **Viewer** is read-only → **403**). As in the rules
router, the decision is resolved directly from the request context
(``ctx.member_role`` + :func:`app.core.rbac.can`) rather than the not-yet-wired
``require`` dependency factory, keeping the single source of truth
(:data:`app.core.rbac.ROLE_CAPABILITIES`).

The workspace the job is scoped to is the caller's **active workspace**
(``ctx.active_workspace_id``); a body ``workspace_id`` — when supplied — must
equal it, and a caller who is not a member of the target workspace gets a
membership rejection (Req 16.2). This guarantees the enqueued job's
``workspace_id`` is one the caller actually belongs to, so the job (which
re-applies that scope, Req 16.3) can only ever touch that workspace's data.

Enqueue seam
------------
The arq pool is provided by an injectable dependency (:func:`get_pool`) so the
route enqueues against a real pool in production and a fake in tests — no live
Redis is required to exercise the route. The route returns **202 Accepted** with
the enqueued job's reference, since the run happens asynchronously in the worker.

Requirements: 16.3 (also uses 4.x RBAC, 16.2).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends

from app.agents import tasks as job_queue
from app.api.deps import require_session
from app.core.errors import APIError
from app.core.rbac import Capability, can
from app.core.tenancy import RequestContext
from app.schemas.base import BaseRequest

logger = logging.getLogger("atomic_ai.agents")

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Request schema (strict input)
# ---------------------------------------------------------------------------


class TriggerAgentRequest(BaseRequest):
    """Body for triggering an agent run.

    ``workspace_id`` is optional; when present it MUST equal the caller's active
    workspace (otherwise the request is rejected), so the job's tenant scope is
    always a workspace the caller belongs to. ``kind`` selects the task
    (``webhook`` → :func:`app.agents.tasks.process_webhook`, ``scheduled`` →
    :func:`app.agents.tasks.run_scheduled_agent`). ``prompt`` / ``payload`` /
    ``category`` / ``provider_name`` parameterize the run.
    """

    workspace_id: uuid.UUID | None = None
    kind: str = "webhook"
    prompt: str | None = None
    payload: dict[str, Any] | None = None
    category: str | None = None
    provider_name: str | None = None


# ---------------------------------------------------------------------------
# Enqueue-pool dependency (overridden in tests)
# ---------------------------------------------------------------------------


async def get_pool():
    """Yield an arq pool for the request, closing it afterwards.

    Uses :func:`app.agents.tasks.arq_pool_scope` (a real pool built lazily from
    ``REDIS_URL``). Tests override this dependency with a fake exposing
    ``enqueue_job`` so the route needs no live Redis.
    """
    async with job_queue.arq_pool_scope() as pool:
        yield pool


# ---------------------------------------------------------------------------
# Authorization helper
# ---------------------------------------------------------------------------


def _resolve_scoped_workspace(
    ctx: RequestContext, requested: uuid.UUID | None
) -> uuid.UUID:
    """Return the workspace the job will be scoped to, enforcing membership.

    The scope is the caller's active workspace. A caller with no active
    workspace gets **400**. A body ``workspace_id`` that disagrees with the
    active workspace gets **400** (the caller must switch workspace explicitly
    rather than target a foreign one via the body). A caller who is not a member
    of the active workspace gets **404** so the workspace's existence is not
    disclosed (Req 16.2).
    """
    workspace_id = ctx.active_workspace_id
    if workspace_id is None:
        raise APIError(
            status_code=400, code="no_active_workspace",
            message="No active workspace selected.",
        )
    if requested is not None and requested != workspace_id:
        raise APIError(
            status_code=400, code="workspace_mismatch",
            message="workspace_id must match the active workspace.",
        )
    role = ctx.member_role(workspace_id)
    if role is None:
        raise APIError(status_code=404, code="not_found", message="Workspace not found.")
    if not can(role, Capability.TRIGGER_WORKFLOW):
        raise APIError(
            status_code=403, code="forbidden",
            message="Insufficient role to trigger a workflow.",
        )
    return workspace_id


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/run", status_code=202)
async def trigger_agent_run(
    body: TriggerAgentRequest,
    ctx: RequestContext = Depends(require_session),
    pool=Depends(get_pool),
) -> dict:
    """Trigger a tenant-scoped agent run via the Job_Queue (Req 16.3).

    Authorizes ``TRIGGER_WORKFLOW`` (Owner/Admin/Member; Viewer → 403) on the
    caller's active workspace, then enqueues a job carrying that workspace's
    ``workspace_id`` and the caller's ``user_id`` so the task re-applies the
    same tenant scope when it runs. Returns **202 Accepted** with the job
    reference.
    """
    workspace_id = _resolve_scoped_workspace(ctx, body.workspace_id)

    extra: dict[str, Any] = {}
    if body.category is not None:
        extra["category"] = body.category
    if body.provider_name is not None:
        extra["provider_name"] = body.provider_name

    if body.kind == "scheduled":
        task_name = job_queue.SCHEDULED_TASK_NAME
        if body.prompt is not None:
            extra["prompt"] = body.prompt
        payload_arg = None
    else:
        task_name = job_queue.WEBHOOK_TASK_NAME
        # process_webhook derives its prompt from the payload; carry it there.
        if body.prompt is not None:
            payload_arg = dict(body.payload or {})
            payload_arg.setdefault("prompt", body.prompt)
        else:
            payload_arg = body.payload

    job = await job_queue.enqueue_agent_run(
        pool,
        task_name=task_name,
        workspace_id=workspace_id,
        triggered_by_user_id=ctx.user_id,
        payload=payload_arg,
        **extra,
    )

    job_id = getattr(job, "job_id", None)
    logger.info(
        "agent.run.enqueued workspace_id=%s user_id=%s task=%s job_id=%s",
        workspace_id, ctx.user_id, task_name, job_id,
    )
    return {
        "status": "accepted",
        "task": task_name,
        "workspace_id": str(workspace_id),
        "triggered_by_user_id": str(ctx.user_id),
        "job_id": job_id,
    }


__all__ = ["router", "get_pool", "TriggerAgentRequest"]
