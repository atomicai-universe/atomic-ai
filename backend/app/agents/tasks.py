"""Job_Queue — ARQ tasks + worker settings for agent runs (task 11.5).

Implements the ``Job_Queue`` component from the design (see design.md
"Job_Queue (ARQ on Redis)"): Redis-backed asynchronous tasks that process
inbound webhooks and scheduled agent runs. The single security-critical
property this module carries is **tenant isolation of queued jobs** (Req 16.3):

    THE Backend_Engine SHALL scope Job_Queue tasks to the originating Workspace
    so that a queued task operates only on resources of that Workspace.

How Req 16.3 is enforced here
-----------------------------
Every task carries the originating ``workspace_id`` (and the triggering
``triggered_by_user_id``) *in its own arguments*, and RE-APPLIES that scope when
it runs by threading the same ``workspace_id`` / ``triggered_by_user_id`` into
:func:`app.services.strands_engine.run_agent`. The engine in turn:

- creates the :class:`~app.db.models.AgentSession` scoped to that workspace/user,
- loads only that workspace's rules
  (:func:`app.services.rules_service.list_rules`), and
- resolves only that workspace's tool servers
  (:func:`app.services.mcp_registry.resolve`, already workspace-scoped, Req
  9.2/9.3/9.6).

Because the job never reaches across to another workspace's data — it only ever
passes its own ``workspace_id`` down the choke points that scope by
``workspace_id`` — no cross-tenant data is reachable from a queued task
(Req 16.3). This mirrors the request-path tenant scoping in
:mod:`app.core.tenancy`, re-applied inside the job.

Injectable seams (so tests need no live Redis or model)
-------------------------------------------------------
Two seams keep this testable without infrastructure:

1. **The engine run** — each task accepts an injectable ``run_agent`` callable
   (defaulting to :func:`app.services.strands_engine.run_agent`) and forwards an
   injectable ``run_loop`` (defaulting to the real Strands adapter) down to it.
   Tests inject a fake engine-run to assert the *same* ``workspace_id`` /
   ``triggered_by_user_id`` given to the job are used by the run, proving the
   tenant scope is carried into the job (Req 16.3) — without calling a model.
2. **The DB session** — each task obtains its session from an injectable
   ``session_factory`` (defaulting to the app's :func:`session_scope`). Tests
   inject a fake/throwaway session so no live database is required.
3. **The enqueue path** — :func:`enqueue_agent_run` accepts an arq pool (or any
   object exposing ``enqueue_job``), so the router can enqueue against a real
   pool in production and a fake in tests. :func:`get_arq_pool` builds the real
   pool lazily from ``REDIS_URL`` so importing this module never opens a
   connection.

The worker launch itself (running an arq worker process/container) is out of
scope for this task; :class:`WorkerSettings` is defined so a worker *can* be
started, but nothing here starts one.

Requirements: 16.3.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from app.config import get_settings
from app.db.session import session_scope
from app.services import strands_engine

logger = logging.getLogger("atomic_ai.tasks")

# Names ARQ registers the tasks under (also used by the router to enqueue).
WEBHOOK_TASK_NAME = "process_webhook"
SCHEDULED_TASK_NAME = "run_scheduled_agent"


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


class _SessionFactory(Protocol):
    """An async-context-manager factory yielding a DB session.

    Both :func:`app.db.session.session_scope` and the in-memory fakes used by
    the tests satisfy this: calling it returns an async context manager whose
    ``__aenter__`` yields the session to run the engine against.
    """

    def __call__(self) -> Any: ...  # pragma: no cover - structural typing only


class _EnqueuePool(Protocol):
    """Minimal surface :func:`enqueue_agent_run` needs from an arq pool.

    The real arq ``ArqRedis`` pool exposes ``enqueue_job(function, *args,
    **kwargs)``; the router-side fake in the tests exposes the same method, so
    neither the router nor the tests need a live Redis.
    """

    def enqueue_job(
        self, function: str, *args: Any, **kwargs: Any
    ) -> Awaitable[Any]: ...  # pragma: no cover - structural typing only


# The engine-run seam signature (a subset of strands_engine.run_agent's kwargs).
RunAgent = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# Shared execution core (re-applies tenant scope — Req 16.3)
# ---------------------------------------------------------------------------


async def _execute_agent_run(
    *,
    workspace_id: uuid.UUID,
    triggered_by_user_id: uuid.UUID,
    thread_id: str,
    prompt: str,
    category: str,
    provider_name: str | None,
    run_agent: RunAgent,
    run_loop: Any,
    session_factory: _SessionFactory,
) -> Any:
    """Open a session and run one agent execution scoped to ``workspace_id``.

    This is the single place both task functions funnel through. It RE-APPLIES
    tenant scoping (Req 16.3) by passing the job's own ``workspace_id`` and
    ``triggered_by_user_id`` straight into the engine run: the engine creates
    the session, loads rules, and resolves tools all scoped to that workspace,
    so the job can never touch another workspace's resources.

    ``run_loop`` is forwarded to the engine only when provided (``None`` lets
    the engine use its default real adapter), so tests inject a deterministic
    loop and production uses the SDK.
    """
    async with session_factory() as session:
        extra: dict[str, Any] = {}
        if run_loop is not None:
            extra["run_loop"] = run_loop
        return await run_agent(
            session,
            workspace_id=workspace_id,
            triggered_by_user_id=triggered_by_user_id,
            thread_id=thread_id,
            prompt=prompt,
            category=category,
            provider_name=provider_name,
            **extra,
        )


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Accept a UUID or its string form (arg values survive JSON round-trips)."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# ARQ task functions
# ---------------------------------------------------------------------------


async def process_webhook(
    ctx: dict,
    *,
    workspace_id: uuid.UUID | str,
    triggered_by_user_id: uuid.UUID | str,
    payload: dict[str, Any] | None = None,
    thread_id: str | None = None,
    category: str = "webhook",
    provider_name: str | None = None,
    run_agent: RunAgent = strands_engine.run_agent,
    run_loop: Any = None,
    session_factory: _SessionFactory | None = None,
) -> Any:
    """ARQ task: process an inbound webhook by running the agent (Req 16.3).

    Carries the originating ``workspace_id`` and ``triggered_by_user_id`` in its
    arguments and re-applies that scope when it runs (via
    :func:`_execute_agent_run`), so the queued task operates only on resources
    of its workspace. The ``payload`` is the (untrusted) webhook body; it is
    used to derive the agent prompt and is never treated as authorization.

    Args:
        ctx: The ARQ job context (unused here; present for the ARQ signature).
        workspace_id: Originating workspace (carried into the run — Req 16.3).
        triggered_by_user_id: Triggering user/principal (carried into the run).
        payload: The webhook body; ``prompt`` is derived from it.
        thread_id: Conversation/thread id; defaults to a webhook-scoped id.
        category / provider_name: Rule-resolution scope for the run.
        run_agent / run_loop / session_factory: Injectable seams (see module
            docstring); defaults use the real engine, SDK loop, and DB session.
    """
    ws = _coerce_uuid(workspace_id)
    user = _coerce_uuid(triggered_by_user_id)
    body = payload or {}
    prompt = str(body.get("prompt") or body.get("goal") or f"Process webhook: {body}")
    thread = thread_id or f"webhook:{ws}"
    return await _execute_agent_run(
        workspace_id=ws,
        triggered_by_user_id=user,
        thread_id=thread,
        prompt=prompt,
        category=category,
        provider_name=provider_name,
        run_agent=run_agent,
        run_loop=run_loop,
        session_factory=session_factory or session_scope,
    )


async def run_scheduled_agent(
    ctx: dict,
    *,
    workspace_id: uuid.UUID | str,
    triggered_by_user_id: uuid.UUID | str,
    prompt: str = "Run scheduled agent.",
    thread_id: str | None = None,
    category: str = "scheduled",
    provider_name: str | None = None,
    run_agent: RunAgent = strands_engine.run_agent,
    run_loop: Any = None,
    session_factory: _SessionFactory | None = None,
) -> Any:
    """ARQ task: run a scheduled agent execution (Req 16.3).

    Same tenant-scoped contract as :func:`process_webhook`: the scheduled run
    carries its originating ``workspace_id`` / ``triggered_by_user_id`` and
    re-applies that scope through the engine, so it only ever operates on its
    own workspace's rules, tools, and sessions.

    Args mirror :func:`process_webhook` but take an explicit ``prompt`` (the
    schedule's configured goal) rather than deriving one from a webhook body.
    """
    ws = _coerce_uuid(workspace_id)
    user = _coerce_uuid(triggered_by_user_id)
    thread = thread_id or f"scheduled:{ws}"
    return await _execute_agent_run(
        workspace_id=ws,
        triggered_by_user_id=user,
        thread_id=thread,
        prompt=prompt,
        category=category,
        provider_name=provider_name,
        run_agent=run_agent,
        run_loop=run_loop,
        session_factory=session_factory or session_scope,
    )


# ---------------------------------------------------------------------------
# Enqueue seam (router -> queue)
# ---------------------------------------------------------------------------


async def enqueue_agent_run(
    pool: _EnqueuePool,
    *,
    task_name: str,
    workspace_id: uuid.UUID,
    triggered_by_user_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    **extra: Any,
) -> Any:
    """Enqueue an agent-run job carrying its workspace scope (Req 16.3).

    Serializes the tenant scope into the job's kwargs — ``workspace_id`` and
    ``triggered_by_user_id`` as strings so they survive arq's JSON job
    serialization — so the task re-applies that same scope when it runs. The
    ``pool`` is any object exposing ``enqueue_job`` (a real arq pool in
    production, a fake in tests), keeping the Redis connection out of the router
    and the tests.

    Args:
        pool: The arq pool (or fake) to enqueue against.
        task_name: One of :data:`WEBHOOK_TASK_NAME` / :data:`SCHEDULED_TASK_NAME`.
        workspace_id: The originating workspace (carried into the job).
        triggered_by_user_id: The triggering user (carried into the job).
        payload: Optional webhook/trigger body forwarded to the task.
        **extra: Additional task kwargs (e.g. ``prompt``, ``category``).

    Returns:
        Whatever ``pool.enqueue_job`` returns (an arq ``Job`` in production).
    """
    kwargs: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "triggered_by_user_id": str(triggered_by_user_id),
        **extra,
    }
    if payload is not None:
        kwargs["payload"] = payload
    return await pool.enqueue_job(task_name, **kwargs)


async def get_arq_pool() -> Any:
    """Build a real arq Redis pool from ``REDIS_URL`` (lazy; not for tests).

    Imported and connected lazily so importing this module never opens a Redis
    connection and the test suite (which injects a fake pool) needs no live
    Redis. Callers own closing the returned pool.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = get_settings()
    return await create_pool(
        RedisSettings.from_dsn(settings.REDIS_URL.get_secret_value())
    )


@asynccontextmanager
async def arq_pool_scope():
    """Async context manager yielding a real arq pool and closing it after.

    A convenience for the router dependency so a pool is opened per request and
    closed afterwards. Tests override the router's pool dependency and never
    reach this.
    """
    pool = await get_arq_pool()
    try:
        yield pool
    finally:
        try:
            await pool.aclose()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


# ---------------------------------------------------------------------------
# Per-integration poll lock (prevents overlapping runs on the same inbox)
# ---------------------------------------------------------------------------

# Redis key prefix for the per-integration poll lock. A sweep must hold this
# lock to process an integration; overlapping sweeps (or a webhook run firing at
# the same time) fail to acquire it and skip, so the same unread inbox is never
# processed by two runs at once. This closes the race that produced the four
# same-second runs behind the token blowup (see TOKEN-BLOWUP.md).
_POLL_LOCK_PREFIX = "poll:lock:integration:"


@asynccontextmanager
async def _integration_poll_lock(integration_id: Any, *, redis: Any = None):
    """Yield True iff this caller acquired the exclusive lock for the integration.

    Uses Redis ``SET key val NX EX ttl`` (atomic acquire-or-fail). The lock
    self-expires after ``POLL_LOCK_TTL_SECONDS`` so a crashed worker never
    deadlocks future sweeps. ``redis`` is injectable for tests; when omitted a
    short-lived client is built from ``REDIS_URL``. If Redis is unavailable the
    lock FAILS OPEN (yields True) so polling never silently stops — the agent
    run caps (Limits/window) remain the hard backstop against cost.
    """
    settings = get_settings()
    key = f"{_POLL_LOCK_PREFIX}{integration_id}"
    token = uuid.uuid4().hex
    owns = False
    own_client = False
    client = redis
    try:
        if client is None:
            import redis.asyncio as redis_asyncio

            client = redis_asyncio.from_url(
                settings.REDIS_URL.get_secret_value(),
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            own_client = True
        # Atomic acquire: only set if absent; auto-expire after the TTL.
        acquired = await client.set(
            key, token, nx=True, ex=settings.POLL_LOCK_TTL_SECONDS
        )
        owns = bool(acquired)
        yield owns
    except Exception:  # noqa: BLE001 - lock must never crash the sweep
        logger.warning(
            "poll.lock_degraded integration=%s (failing open)", integration_id
        )
        # Fail open: let the caller proceed. Only yield here if we haven't yet.
        yield True
        return
    finally:
        # Release only if we own it (best-effort; TTL is the backstop). We do
        # not do a strict check-and-delete Lua here because the TTL bounds any
        # stale lock and a mistaken early release just allows the next sweep.
        try:
            if owns and client is not None:
                await client.delete(key)
        except Exception:  # noqa: BLE001 - best-effort release
            pass
        if own_client and client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# ARQ worker settings (worker launch is out of scope; this just defines it)
# ---------------------------------------------------------------------------


def _redis_settings() -> Any:
    """Return arq ``RedisSettings`` derived from ``REDIS_URL``.

    Kept as a function (not a module-level constant) so importing this module
    never requires ``REDIS_URL`` to be present — it is only read when a worker
    is actually constructed from :class:`WorkerSettings`.
    """
    from arq.connections import RedisSettings

    settings = get_settings()
    return RedisSettings.from_dsn(settings.REDIS_URL.get_secret_value())


def _poll_prompt_for(
    provider_name: str,
    category: str,
    new_message_ids: list[str] | None = None,
) -> str:
    """Build the scheduled poll prompt, with the Gmail re-propose guard baked in.

    Cost-control (PART 3) — the Gmail prompt is now SCOPED to specific ids:

    When ``new_message_ids`` is supplied (the cheap pre-check in
    :func:`poll_integrations` already found the genuinely NEW unread ids without
    Bedrock), the prompt tells the agent to process ONLY those exact message ids
    and to NOT list/scan the whole inbox. This stops the agent from re-reading
    the same unread mail every poll (the main waste) and eliminates the extra
    ``messages.list`` round-trip. Each id is read with ``format=metadata`` +
    the message ``snippet`` (NOT ``format=full``) to keep the per-step context —
    and therefore the token cost — small.

    Re-propose guard (still enforced, model-independent):

    1. **Durable "already-seen" set.** The pre-check records each id it hands to
       this run in ``processed_messages`` BEFORE enqueuing, so a later poll never
       hands the same id to another run — even if it stays unread.
    2. **Durable reply de-dup.** When a ``gmail_reply`` approval is
       approved-and-saved-to-draft OR approved-and-sent, the execution path
       (:func:`app.services.approval_service.execute_and_approve_request`) marks
       the original message read AND records it seen. The approval hook also
       refuses to create a second reply for the same source email
       (:func:`app.services.approval_service._gmail_reply_already_exists`).
    3. **Within a run**, the prompt instructs the agent not to re-reply to a
       thread it already drafted for.
    """
    if provider_name == "gmail":
        if new_message_ids:
            # SCOPED prompt: only the exact NEW ids, read cheaply (metadata +
            # snippet, never format=full) to bound tokens per step (cost).
            id_list = ", ".join(new_message_ids)
            return (
                "Process ONLY these specific new Gmail message ids and NOTHING "
                f"else: [{id_list}]. Do NOT list or scan the inbox — you were "
                "already given the exact ids to handle. For EACH id, read it with "
                "the `gmail_api` tool using "
                "GET /gmail/v1/users/me/messages/<id> with query "
                "format=metadata and metadataHeaders=[From, Reply-To, Subject, "
                "Message-ID]; use the returned message `snippet` (do NOT request "
                "format=full) to decide and to draft. Apply the workspace "
                "automation rules for the "
                f"{category} category. For each id a rule applies to, draft a "
                "reply using the `gmail_reply` tool — pass to, subject, a real "
                "prose body, in_reply_to (the original Message-ID), thread_id "
                "(the threadId), and source_message_id (the message id). Do NOT "
                "draft more than one reply per thread, and do NOT re-reply to a "
                "thread you already handled in this run. Actually perform the "
                "write calls — do not just describe them; the platform routes "
                "each high-impact call for human approval automatically."
            )
        # Fallback (no explicit ids — e.g. a manual/scheduled trigger not going
        # through the pre-check): keep the UNREAD-scoped behavior but STILL read
        # each message with metadata + snippet (not full) to save tokens.
        return (
            "Poll Gmail for UNREAD emails only (use q='is:unread' when listing "
            "messages) and apply the workspace automation rules for the "
            f"{category} category. Read each message with format=metadata and "
            "its snippet (NOT format=full) to keep context small. For EACH unread "
            "email a rule applies to, draft a reply using the `gmail_reply` tool "
            "— pass to, subject, a real prose body, in_reply_to (the original "
            "Message-ID), thread_id (the threadId), and source_message_id (the "
            "original message's id). Do NOT draft more than one reply per thread, "
            "and do NOT re-reply to a thread you already handled in this run; "
            "each handled email is marked read on approval so it will not "
            "reappear on the next poll. Actually perform the write calls — do "
            "not just describe them; the platform routes each high-impact call "
            "for human approval automatically. If there are no unread emails, do "
            "nothing."
        )
    return (
        f"Poll {provider_name} for new/unread items and apply the workspace "
        f"automation rules for the {category} category. For EACH new item that a "
        f"rule applies to, carry out the required action by calling the "
        f"{provider_name}_api tool. Actually perform the write calls — do not "
        f"just describe them; the platform will route any high-impact call for "
        f"human approval automatically. If there is nothing new, do nothing."
    )


# Cost-control: how many genuinely-new unread Gmail ids a SINGLE poll/push hands
# to one agent run (batch size). Bounds Bedrock cost per cycle; overflow ids are
# intentionally left UNrecorded so a later poll picks them up. Read from config
# (env GMAIL_MAX_IDS_PER_RUN) so it is tunable without a redeploy; a safe default
# is used if settings can't be read at import time.
def _default_max_ids_per_run() -> int:
    try:
        return int(get_settings().GMAIL_MAX_IDS_PER_RUN)
    except Exception:  # noqa: BLE001 - never fail import on config read
        return 5


GMAIL_MAX_IDS_PER_RUN = _default_max_ids_per_run()


async def _gmail_new_ids_for_poll(
    session: Any,
    integ: Any,
    *,
    resolve_gmail_credentials: Callable[..., Awaitable[Any]],
    call_provider_api: Callable[..., Awaitable[dict]],
    per_run_cap: int | None = None,
) -> list[str] | None:
    """Cheap (NO Bedrock) pre-check for ONE Gmail integration; returns ids to run.

    Sequence (all plain API / DB — no model tokens spent):

    1. Resolve the integration's Gmail credentials (same resolver voice_tools
       uses) — server-derived, never from model input.
    2. ``list_unread_ids`` — one plain ``messages.list?q=is:unread`` GET.
    3. ``filter_new_ids`` — subtract ids already in ``processed_messages`` for
       this integration (the "never re-check a seen email" filter).
    4. If none are new, return ``[]`` (caller enqueues NOTHING — the main
       savings).
    5. Otherwise slice to ``per_run_cap`` and ``record_seen`` EXACTLY that slice
       NOW (so a later poll never re-runs them), leaving overflow UNrecorded for
       a later poll. Return the recorded slice for the scoped run prompt.

    Returns ``None`` on any error (missing integration creds, transient Gmail
    failure) so the caller can skip WITHOUT enqueuing — a bad integration must
    never spuriously spend a run. Logs only counts, never ids/bodies.
    """
    from app.services import gmail_poll as _gp

    # Resolve the batch cap from live config unless the caller pinned one; this
    # picks up an env change to GMAIL_MAX_IDS_PER_RUN without a redeploy.
    if per_run_cap is None:
        try:
            per_run_cap = int(get_settings().GMAIL_MAX_IDS_PER_RUN)
        except Exception:  # noqa: BLE001
            per_run_cap = GMAIL_MAX_IDS_PER_RUN

    # 1. Resolve creds server-side (workspace derived from the integration row).
    _iid, creds, config = await resolve_gmail_credentials(
        session, workspace_id=integ.workspace_id
    )

    # 2. Cheap unread list (plain API, no Bedrock).
    unread_ids = await _gp.list_unread_ids(
        credentials=creds,
        config=config,
        call_provider_api=call_provider_api,
    )
    if not unread_ids:
        return []

    # 3. Drop ids we've already handed to a run (durable "seen" set).
    new_ids = await _gp.filter_new_ids(
        session, integration_id=integ.id, ids=unread_ids
    )
    if not new_ids:
        return []

    # 5. Cap per run; record ONLY the slice we actually pass to this run.
    scoped = new_ids[: max(1, per_run_cap)]
    await _gp.record_seen(
        session,
        workspace_id=integ.workspace_id,
        integration_id=integ.id,
        ids=scoped,
    )
    return scoped


async def poll_integrations(
    ctx: dict,
    *,
    resolve_gmail_credentials: Callable[..., Awaitable[Any]] | None = None,
    call_provider_api: Callable[..., Awaitable[dict]] | None = None,
    poll_lock: Callable[..., Any] | None = None,
) -> int:
    """Cron task: trigger agent runs for POLL-based provider integrations.

    Providers without push notifications (IMAP, Notion, some HR/ERP/cloud) are
    checked on a schedule. Runs in the worker on an interval.

    COST-CONTROL (the whole point of this task): before spending a single
    Bedrock token, GMAIL integrations get a CHEAP unread pre-check
    (:func:`_gmail_new_ids_for_poll`, a plain Gmail API call — NO model):

    - NO new unread ids  -> enqueue NOTHING (empty/unchanged inbox is free).
    - Some new unread ids -> record them in ``processed_messages`` NOW (so they
      are never re-checked on a later poll) and enqueue EXACTLY ONE run whose
      prompt is scoped to those ids (:func:`_poll_prompt_for`), passing the ids
      to the run so it does not re-list the inbox.

    Non-Gmail poll providers keep today's behavior (enqueue one run per eligible
    integration). Each integration is wrapped in try/except so one bad
    integration never breaks the sweep. Logs only counts — never ids/bodies.

    The Gmail seams (``resolve_gmail_credentials`` / ``call_provider_api``) are
    injectable so tests exercise the pre-check with fakes and NO live Gmail;
    they default to the real approval_service resolver + agent_tools caller.

    Returns the number of runs enqueued (for observability/tests).
    """
    from sqlalchemy import select

    from app.db.models import Integration, IntegrationStatus
    from app.db.session import session_scope
    from app.services import job_queue_bridge as _jq
    from app.services import provider_webhooks as _pw
    from app.services.webhook_subscriptions import WEBHOOK_STATUS_KEY

    # Default Gmail seams (real resolver + real API caller). Imported lazily so
    # importing this module never pulls the whole service graph.
    if resolve_gmail_credentials is None:
        from app.services import approval_service as _approval
        resolve_gmail_credentials = _approval.resolve_gmail_credentials
    if call_provider_api is None:
        from app.services import agent_tools as _agent_tools
        call_provider_api = _agent_tools.call_provider_api

    # Per-integration exclusive lock (injectable for tests). Prevents two
    # overlapping sweeps from processing the same inbox and enqueuing duplicate
    # runs — the race behind the token blowup. Default = the real Redis lock.
    if poll_lock is None:
        poll_lock = _integration_poll_lock

    poll_set = set(_pw.poll_providers())
    enqueued = 0
    async with session_scope() as session:
        rows = await session.scalars(
            select(Integration).where(Integration.status == IntegrationStatus.ACTIVE)
        )
        integrations = list(rows.all())

    def _should_poll(integ) -> bool:
        # Structurally poll-based providers are always polled.
        if integ.provider_name in poll_set:
            return True
        # Push/PubSub providers that never actually registered a live webhook
        # fall back to polling so they still automate (e.g. Gmail on localhost
        # with no Pub/Sub topic, or any push provider whose auto-registration
        # did not complete). This mirrors the "Scheduled checks" status shown
        # in the UI so the label is truthful.
        cfg = integ.config or {}
        status = str(cfg.get(WEBHOOK_STATUS_KEY, "")).lower()
        return status == "poll"

    logger.info("poll.sweep_start active_integrations=%d", len(integrations))
    for integ in integrations:
        if not _should_poll(integ):
            logger.debug(
                "poll.skip provider=%s integration=%s (not poll-eligible)",
                integ.provider_name, integ.id,
            )
            continue
        try:
            # Acquire the per-integration lock. If another sweep/worker already
            # holds it, skip this integration THIS cycle (it is already being
            # processed); the next sweep picks it up. This is the structural fix
            # for overlapping runs on the same inbox (see TOKEN-BLOWUP.md).
            async with poll_lock(integ.id) as acquired:
                if not acquired:
                    logger.info(
                        "poll.locked_skip provider=%s integration=%s (another run in flight)",
                        integ.provider_name, integ.id,
                    )
                    continue

                # GMAIL: cheap pre-check FIRST — only enqueue Bedrock when there
                # is genuinely new unread mail. The pre-check + record run in
                # their own session scope so the processed_messages rows commit
                # before (and independent of) the enqueue.
                if integ.provider_name == "gmail":
                    async with session_scope() as pre_session:
                        new_ids = await _gmail_new_ids_for_poll(
                            pre_session,
                            integ,
                            resolve_gmail_credentials=resolve_gmail_credentials,
                            call_provider_api=call_provider_api,
                        )
                    if not new_ids:
                        # No new unread (or a resolve/API hiccup returned []).
                        # Spend NOTHING — this is the primary cost saving.
                        logger.info(
                            "poll.gmail_no_new integration=%s workspace=%s",
                            integ.id, integ.workspace_id,
                        )
                        continue
                    await _jq.enqueue_scheduled_run(
                        workspace_id=integ.workspace_id,
                        triggered_by_user_id=integ.created_by_user_id,
                        provider_name=integ.provider_name,
                        category=str(integ.category),
                        prompt=_poll_prompt_for(
                            integ.provider_name, str(integ.category), new_ids
                        ),
                    )
                    enqueued += 1
                    logger.info(
                        "poll.gmail_enqueued integration=%s workspace=%s new_count=%d",
                        integ.id, integ.workspace_id, len(new_ids),
                    )
                    continue

                # NON-GMAIL poll providers: unchanged behavior (one run each).
                await _jq.enqueue_scheduled_run(
                    workspace_id=integ.workspace_id,
                    triggered_by_user_id=integ.created_by_user_id,
                    provider_name=integ.provider_name,
                    category=str(integ.category),
                    prompt=_poll_prompt_for(integ.provider_name, str(integ.category)),
                )
                enqueued += 1
                logger.info(
                    "poll.enqueued provider=%s integration=%s workspace=%s",
                    integ.provider_name, integ.id, integ.workspace_id,
                )
        except Exception:  # noqa: BLE001 - one bad integration must not stop the sweep
            logger.exception(
                "poll.enqueue_failed provider=%s integration=%s",
                integ.provider_name, integ.id,
            )
            continue
    logger.info("poll.sweep_done enqueued=%d", enqueued)
    return enqueued


async def renew_short_lived_subscriptions(ctx: dict) -> int:
    """Cron task: renew short-lived push subscriptions (Microsoft Graph).

    Graph subscriptions expire (~1 hour) and must be renewed. For each ACTIVE
    Graph-backed integration, re-run subscription registration (idempotent
    re-subscribe) so push keeps flowing. Returns the count re-registered.

    Runs in the worker; best-effort per integration (one failure never stops the
    sweep). Requires a valid (refreshed) access token, which the registration
    path mints from the stored refresh token.
    """
    from sqlalchemy import select

    from app.db.models import Integration, IntegrationStatus
    from app.db.session import session_scope
    from app.services import integration_vault as _vault
    from app.services import oauth_refresh as _oauth
    from app.services import provider_webhooks as _pw
    from app.services import webhook_subscriptions as _subs

    # Providers whose push subscriptions are short-lived and need renewal
    # (Microsoft Graph subscriptions and Google watch channels both expire).
    # Gmail's PUBSUB users.watch also expires (~7 days), so include PUBSUB-kind
    # triggers too — re-running users.watch is idempotent and just extends the
    # expiration, keeping instant push alive. Without this, Gmail push silently
    # stops after ~7 days and the integration falls back to polling.
    renewable = {
        p for p, prof in _pw.TRIGGER_PROFILES.items()
        if getattr(prof, "scheme", "") in ("graph", "google_watch", "zoho_watch")
        or getattr(prof, "kind", None) is _pw.TriggerKind.PUBSUB
    }
    renewed = 0
    async with session_scope() as session:
        rows = await session.scalars(
            select(Integration).where(Integration.status == IntegrationStatus.ACTIVE)
        )
        integrations = [i for i in rows.all() if i.provider_name in renewable]
        for integ in integrations:
            try:
                cred = await _vault.use_credential(session, integration_id=integ.id)
                creds = await _oauth.ensure_access_token(
                    integ.provider_name, dict(cred.credentials), dict(cred.config or {})
                )
                sub = await _subs.register_subscription(
                    provider=integ.provider_name,
                    integration_id=str(integ.id),
                    credentials=creds,
                    config=integ.config or {},
                )
                integ.config = sub["config"]
                await session.flush()
                if sub.get("info", {}).get("registered"):
                    renewed += 1
            except Exception:  # noqa: BLE001
                continue
        await session.commit()
    return renewed


async def send_scheduled_replies(ctx: dict) -> int:
    """Cron task: send replies that were scheduled via "Approve and Schedule".

    A scheduled reply is a STILL-``pending`` approval whose ``arguments`` carry
    ``scheduled_send_at`` (ISO-8601 UTC) and ``scheduled_by_user_id`` — there is
    NO new status/enum and NO DB migration (see
    :mod:`app.services.approval_service`). This sweep loads the due ones
    (``scheduled_send_at <= now``) and, for each, executes+approves it as a real
    Gmail send.

    Catch-up-on-startup semantics
    -----------------------------
    This runs ONLY while the worker is running. It is registered with a
    per-minute cron (a due reply is picked up within ~60s), so:

    - While the worker is up, due replies are sent within ~1 minute of their
      time.
    - If a scheduled time elapses while the app/worker is OFF, the reply is sent
      on the NEXT sweep after the worker comes back online (the startup run
      fires immediately). It becomes fully on-time reliable once the worker runs
      on an always-on server.

    Resilience: each send is best-effort inside its own try/except — one failure
    (e.g. a transient Gmail error) neither aborts the sweep nor resolves the
    approval, so the failed one stays ``pending`` and is retried on the next
    tick. Returns the number of replies actually sent (for observability/tests).
    """
    from datetime import datetime, timezone

    from app.db.session import session_scope
    from app.services import approval_service

    sent = 0
    async with session_scope() as session:
        due = await approval_service.list_due_scheduled(
            session, now=datetime.now(timezone.utc)
        )
        logger.info("scheduled_replies.sweep_start due=%d", len(due))
        for request in due:
            args = request.arguments if isinstance(request.arguments, dict) else {}
            # Prefer the user who scheduled it; fall back to the run's trigger.
            reviewer_id = request.triggered_by_user_id
            raw_uid = args.get("scheduled_by_user_id")
            if isinstance(raw_uid, str) and raw_uid:
                try:
                    reviewer_id = uuid.UUID(raw_uid)
                except ValueError:
                    reviewer_id = request.triggered_by_user_id
            try:
                await approval_service.execute_and_approve_request(
                    session,
                    approval_request_id=request.id,
                    reviewer_user_id=reviewer_id,
                    execution_action="send",
                    # None -> trusted system cron; skip RBAC in the service.
                    reviewer_role=None,
                )
                sent += 1
                logger.info(
                    "scheduled_replies.sent approval_request_id=%s workspace=%s",
                    request.id, request.workspace_id,
                )
            except Exception:  # noqa: BLE001 - leave pending; retry next tick
                logger.exception(
                    "scheduled_replies.send_failed approval_request_id=%s",
                    request.id,
                )
                continue
    logger.info("scheduled_replies.sweep_done sent=%d", sent)
    return sent


async def sweep_stale_sessions(ctx: dict) -> int:
    """Cron task: mark orphaned ``running`` AgentSessions as ``terminated``.

    A session is flipped to a terminal status only at the END of an agent run;
    if the worker dies/restarts mid-run (redeploy, crash) the row is stranded at
    ``running`` forever, inflating the admin "Active sessions" count with rows
    that are NOT live (they consume no AWS/Bedrock resources — just stale DB
    status). This periodic sweep terminates ``running`` rows older than the
    threshold in :func:`app.services.admin_service.sweep_stale_running_sessions`.

    Runs on the steady-state worker loop (NOT run_at_startup), so it uses the
    normal pooled ``session_scope`` safely — mirroring the other cron tasks.
    Returns the number of sessions swept (for observability/tests).
    """
    from app.db.session import session_scope
    from app.services import admin_service

    async with session_scope() as session:
        swept = await admin_service.sweep_stale_running_sessions(session)
    if swept:
        logger.info("stale_sessions.swept count=%d", swept)
    return swept


class WorkerSettings:
    """ARQ worker configuration for the Job_Queue.

    An arq worker started with ``arq app.agents.tasks.WorkerSettings`` registers
    :func:`process_webhook` and :func:`run_scheduled_agent` and connects to
    Redis via ``redis_settings``.

    arq reads ``redis_settings`` as a CLASS ATTRIBUTE (not a property), so it is
    resolved here at class-definition time from ``REDIS_URL``. Importing this
    module for the worker therefore requires ``REDIS_URL`` — which the worker
    container always has. Test imports that must avoid this can import the task
    functions directly without referencing ``WorkerSettings``.
    """

    functions = [process_webhook, run_scheduled_agent, poll_integrations, renew_short_lived_subscriptions, send_scheduled_replies, sweep_stale_sessions]
    # Poll-based providers are swept every 5 minutes so push-less integrations
    # still automate. Guarded so importing this module never requires arq.
    try:
        from arq import cron as _cron

        # Poll interval is configurable via GMAIL_POLL_MINUTES (default 10 min,
        # raised from 1 for Bedrock cost-control). The cron fires on minutes that
        # are multiples of the interval, so interval=10 => :00,:10,:20,...,
        # interval=1 => every minute. Clamped to 1..60 by config; guarded here so
        # a bad value never breaks worker boot.
        try:
            _poll_every = max(1, min(60, int(get_settings().GMAIL_POLL_MINUTES)))
        except Exception:  # noqa: BLE001 - never fail worker boot on config read
            # Fallback matches the config default (10 min) for cost-control.
            _poll_every = 10
        _poll_minutes = set(range(0, 60, _poll_every))

        cron_jobs = [
            _cron(poll_integrations, minute=_poll_minutes, run_at_startup=False),
            # Renew short-lived Graph subscriptions well before their ~1h expiry.
            _cron(renew_short_lived_subscriptions, minute={0, 45}, run_at_startup=True),
            # Send "Approve and Schedule" replies whose time has arrived. Every
            # Runs every minute (NOT run_at_startup): a startup run executes on
            # arq's boot event loop before the process-wide asyncpg pool is bound
            # to the steady-state loop, which raises "attached to a different
            # loop". The per-minute schedule still provides catch-up — the first
            # sweep fires within ~60s of the worker coming online, honoring any
            # schedule that elapsed while the worker was down (see docstring).
            _cron(send_scheduled_replies, minute=set(range(0, 60)), run_at_startup=False),
            # Clean up orphaned "running" sessions (worker restart/crash mid-run)
            # every 15 minutes. NOT run_at_startup — a startup cron runs on arq's
            # boot loop before the asyncpg pool binds to the steady-state loop
            # (same reason send_scheduled_replies avoids it); the 15-min cadence
            # cleans up shortly after boot anyway.
            _cron(sweep_stale_sessions, minute={0, 15, 30, 45}, run_at_startup=False),
        ]
    except Exception:  # noqa: BLE001
        cron_jobs = []
    # arq accesses this as a plain attribute, so resolve it at class-definition
    # time. Guard against a missing REDIS_URL at import (e.g. unit tests that
    # import this module) — only the worker container truly needs it resolved.
    try:
        redis_settings = _redis_settings()
    except Exception:  # noqa: BLE001
        redis_settings = None  # type: ignore[assignment]


__all__ = [
    "WEBHOOK_TASK_NAME",
    "SCHEDULED_TASK_NAME",
    "process_webhook",
    "run_scheduled_agent",
    "send_scheduled_replies",
    "sweep_stale_sessions",
    "enqueue_agent_run",
    "get_arq_pool",
    "arq_pool_scope",
    "WorkerSettings",
]
