"""Job queue bridge — enqueue agent runs from HTTP handlers (webhooks/schedulers).

A thin async helper that opens a short-lived arq pool, enqueues a tenant-scoped
agent run, and closes the pool. Keeps arq specifics out of the routers and
lets tests patch a single seam.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agents import tasks as agent_tasks


async def enqueue_webhook_run(
    *,
    workspace_id: uuid.UUID,
    triggered_by_user_id: uuid.UUID,
    provider_name: str,
    category: str,
    payload: dict[str, Any],
) -> Any:
    """Enqueue a ``process_webhook`` agent run scoped to the workspace (Req 16.3)."""
    async with agent_tasks.arq_pool_scope() as pool:
        return await agent_tasks.enqueue_agent_run(
            pool,
            task_name=agent_tasks.WEBHOOK_TASK_NAME,
            workspace_id=workspace_id,
            triggered_by_user_id=triggered_by_user_id,
            payload=payload,
            category=category,
            provider_name=provider_name,
        )


async def enqueue_scheduled_run(
    *,
    workspace_id: uuid.UUID,
    triggered_by_user_id: uuid.UUID,
    provider_name: str,
    category: str,
    prompt: str,
) -> Any:
    """Enqueue a ``run_scheduled_agent`` run scoped to the workspace (Req 16.3)."""
    async with agent_tasks.arq_pool_scope() as pool:
        return await agent_tasks.enqueue_agent_run(
            pool,
            task_name=agent_tasks.SCHEDULED_TASK_NAME,
            workspace_id=workspace_id,
            triggered_by_user_id=triggered_by_user_id,
            prompt=prompt,
            category=category,
            provider_name=provider_name,
        )


__all__ = ["enqueue_webhook_run", "enqueue_scheduled_run"]
