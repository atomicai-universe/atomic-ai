"""Strands_Engine — agent session lifecycle and rule loading (task 11.3).

Implements the ``Strands_Engine`` component from the design (see design.md
"Strands_Engine and MCP_Registry"). Its responsibility is the *lifecycle* of a
single agent execution:

1. Create an :class:`~app.db.models.AgentSession` row scoped to the workspace
   and triggering user with status ``running`` (Req 9.1).
2. Load the APPLICABLE active rules for the execution via
   :func:`app.services.rules_service.applicable_rules` over the workspace's
   rules and a :class:`~app.services.rules_service.RuleContext` describing the
   execution's category/provider (Req 8.4).
3. Resolve the workspace-scoped tool servers via
   :func:`app.services.mcp_registry.resolve` — the triggering user's personal
   integrations plus the workspace's shared integrations, never another
   workspace's (Req 9.2/9.3/9.6, already enforced there).
4. Run the Strands agent loop.
5. On completion, record ``total_tokens_used``, ``execution_time_ms``, and
   ``execution_logs`` (JSONB) and set the terminal status ``completed``
   (Req 9.4).
6. On an unrecoverable error, set status ``failed`` with error details in
   ``execution_logs`` (Req 9.5).

Injectable run-loop seam
------------------------
The actual Strands SDK invocation is abstracted behind an injectable
``run_loop`` callable (:data:`RunLoop`). The real integration
(:func:`strands_run_loop`) is a thin adapter around the ``strands`` SDK's
``Agent``; it is imported lazily and only exercised when a model is actually
configured. Tests inject a deterministic fake so the lifecycle can be verified
without any network/model access. This keeps the SDK integration isolated so it
never breaks the test suite when no model credentials exist.

Extension seam for the approval hook
------------------------------------
:func:`run_agent` accepts an optional ``before_tool_call`` hook parameter and
forwards it to the run loop. Task 13.1 (the ``BeforeToolCall`` approval hook and
high-impact gating) plugs in there without changing this module's lifecycle.

Transaction ownership
----------------------
:func:`run_agent` manages its own commits so a session's terminal state is
durable regardless of the caller: the ``running`` row is committed on creation
(so an interrupted process still leaves a visible ``running`` session), and the
terminal ``completed``/``failed`` row is committed before returning/raising. The
caller therefore does not need to wrap the call in a transaction; it may reuse
the passed ``session`` afterwards.

Error contract
--------------
On an unrecoverable error :func:`run_agent` persists ``failed`` (committed) and
then **re-raises** the original exception, so callers (e.g. the job queue in
task 11.5) can observe the failure. The persisted session id is attached to the
returned/raised result via :class:`AgentRunResult` on success and via the
committed row on failure.

Requirements: 8.4, 9.1, 9.4, 9.5.
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentSession, AgentSessionStatus
from app.services import mcp_registry, rules_service
from app.services.mcp_registry import ToolServer
from app.services.rules_service import RuleApplies, RuleContext

# ---------------------------------------------------------------------------
# Run-loop seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunLoopContext:
    """Everything the agent run loop needs, assembled by :func:`run_agent`.

    A frozen value object handed to the injectable :data:`RunLoop`. It carries
    the identifiers, the loaded applicable rules, the resolved workspace-scoped
    tool servers, the trigger input, and the optional ``before_tool_call`` hook
    the loop should consult before each proposed tool call (task 13.1).

    Attributes:
        agent_session_id: The id of the created ``running`` session.
        workspace_id: The workspace the execution is scoped to.
        triggered_by_user_id: The user (or system principal) that triggered it.
        thread_id: The conversation/thread identifier for this run.
        prompt: The trigger input / goal for the agent.
        rules: The applicable active rules for this execution (Req 8.4).
        tool_servers: The workspace-scoped tool-server descriptors (Req 9.2/9.3).
        before_tool_call: Optional hook consulted before each tool call; wired
            by task 13.1. ``None`` means no gating.
    """

    agent_session_id: uuid.UUID
    workspace_id: uuid.UUID
    triggered_by_user_id: uuid.UUID
    thread_id: str
    prompt: str
    rules: Sequence[RuleApplies]
    tool_servers: Sequence[ToolServer]
    before_tool_call: BeforeToolCall | None = None
    # Per-integration decrypted credentials the tools use to call provider APIs.
    # Keyed by integration id; each entry: {"provider": str, "credentials": {...},
    # "config": {...}}. Assembled by run_agent; never logged.
    resolved_credentials: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunLoopResult:
    """The outcome the run loop reports back to :func:`run_agent`.

    Attributes:
        total_tokens_used: Total tokens consumed by the run (Req 9.4).
        logs: JSON-serializable execution logs/reasoning trace (Req 9.4).
        output: Optional final agent output (opaque, JSON-serializable).
    """

    total_tokens_used: int = 0
    logs: list[Any] | dict[str, Any] | None = None
    output: Any = None


class BeforeToolCall(Protocol):
    """Structural type for the BeforeToolCall approval hook.

    Task 13.1 supplies a concrete implementation in
    :mod:`app.services.approval_service`; this module only forwards it into the
    :class:`RunLoopContext` so the run loop can consult it before each proposed
    tool call.

    The return value is intentionally ``Any`` so an implementation may return a
    decision object *or* an awaitable resolving to one. The concrete hook in
    :mod:`app.services.approval_service` is ``async`` (it persists a ``pending``
    :class:`~app.db.models.ApprovalRequest` for high-impact calls), so a
    run-loop adapter must ``await`` the result when it is awaitable. This keeps
    the Protocol backward compatible with a synchronous implementation.
    """

    def __call__(
        self, tool_name: str, arguments: Any
    ) -> Any: ...  # pragma: no cover


class RunLoop(Protocol):
    """The injectable agent run-loop strategy.

    :func:`run_agent` calls this with a :class:`RunLoopContext` and expects a
    :class:`RunLoopResult`. Any exception raised is treated as an unrecoverable
    error (Req 9.5). The default is :func:`strands_run_loop` (the real SDK
    adapter); tests inject a deterministic fake.
    """

    def __call__(self, ctx: RunLoopContext) -> RunLoopResult: ...  # pragma: no cover


def _build_system_prompt(ctx: RunLoopContext) -> str:
    """Assemble the agent system prompt from the workspace's applicable rules.

    The rules ARE the automation policy — they are turned into explicit
    instructions the model must follow (Req 8.4). Also lists the connected
    providers the agent may act on.
    """
    lines: list[str] = [
        "You are Atomic AI, an autonomous automation agent acting on behalf of a "
        "team workspace. You complete the user's goal by calling the connected "
        "provider APIs via the tools provided.",
        "",
        "You MUST obey the following workspace automation rules at all times. "
        "If a rule conflicts with the goal, the rule wins:",
    ]
    rule_items = []
    for r in ctx.rules:
        prompt_text = getattr(getattr(r, "rule", r), "rule_prompt", None) or getattr(r, "rule_prompt", None)
        if prompt_text:
            rule_items.append(str(prompt_text))
    if rule_items:
        for i, rp in enumerate(rule_items, 1):
            lines.append(f"  {i}. {rp}")
    else:
        lines.append("  (No specific rules configured; act conservatively and "
                     "never take a destructive or high-impact action without approval.)")

    providers = sorted({v["provider"] for v in ctx.resolved_credentials.values()})
    lines.append("")
    if providers:
        lines.append("Connected providers you can act on: " + ", ".join(providers) + ".")
        lines.append(
            "Use the `<provider>_api` tool to make authenticated API calls. Do "
            "not merely describe what you would do — actually CALL the tool to "
            "carry out each step the goal and rules require (e.g. to save a "
            "reply as a draft, call the API with the appropriate write request). "
            "Write/high-impact calls are automatically routed for human approval "
            "by the platform: still ATTEMPT the call so it is queued for review; "
            "when a call comes back marked gated, that means it is awaiting "
            "approval — record that and move on to the next item rather than "
            "retrying or skipping the action."
        )
        if "gmail" in providers:
            lines.append(
                "GMAIL REPLIES — PREFER the `gmail_reply` tool; it builds a "
                "complete, well-formed email for you so you never hand-build MIME "
                "or call the drafts endpoint directly for replies. To reply to an "
                "email, first GET the message with format=metadata (query "
                "format=metadata and metadataHeaders=[From, Reply-To, Subject, "
                "Message-ID]) and use the returned `snippet` to read its From, "
                "Subject, threadId, Message-ID header, and the message's `id`. "
                "Do NOT request format=full — metadata headers plus the snippet "
                "are enough to decide and to draft, and they keep the amount of "
                "text sent to the model (and therefore the cost) small. "
                "Then call the `gmail_reply` tool with to=<original sender>, "
                "subject=<original subject> (a 'Re:' prefix is added "
                "automatically), a real prose body that actually responds to the "
                "email, in_reply_to=<original Message-ID>, thread_id=<threadId>, "
                "and source_message_id=<the original message's id>. "
                "The reply recipient MUST be the person who SENT the email — the "
                "original message's From (or Reply-To) header — NEVER the To "
                "header (which is your own address). Always pass "
                "source_message_id; the platform sets the correct recipient "
                "from the original message automatically. "
                "Do not send "
                "an empty body or a bare URL, and never fabricate a From/date. "
                "IMPORTANT — do NOT re-reply to a conversation you have already "
                "drafted a reply for in this run, and skip any thread that is no "
                "longer unread; each handled email is marked read on approval so "
                "it will not reappear in later polls. "
                "For non-reply Gmail actions, use the generic `gmail_api` tool "
                "with the appropriate endpoint."
            )
    else:
        lines.append("No providers are connected; explain what the user should connect.")
    return "\n".join(lines)


def _make_provider_tools(ctx: RunLoopContext) -> list:
    """Build one Strands tool per connected integration (real API capability).

    Each tool is bound to a specific integration's decrypted credentials and
    provider profile, and calls the generic authenticated HTTP layer. Write
    methods are routed through the approval hook first (Req 10.x). Returns a
    list of ``@tool``-decorated callables to register on the Agent.
    """
    import asyncio

    from strands import tool  # type: ignore[import-not-found]

    from app.services import agent_tools
    from app.services import provider_api

    def _interpret_decision(decision):
        """Interpret an approval-hook decision into a (proceed, approval_request_id).

        Handles the decision shapes the generic write path supports:
          - a ``ToolCallDecision`` with ``proceed`` (bool) + ``approval_request_id``
          - a mapping with ``proceed`` (bool) or ``action`` ("proceed"/"allow")
          - ``None`` (treated as proceed)
        """
        if asyncio.iscoroutine(decision):
            decision = _run_async(decision)
        proceed = True
        if decision is not None:
            if hasattr(decision, "proceed"):
                proceed = bool(decision.proceed)
            elif isinstance(decision, dict):
                if "proceed" in decision:
                    proceed = bool(decision["proceed"])
                elif "action" in decision:
                    proceed = str(decision["action"]) in ("proceed", "allow")
        req_id = getattr(decision, "approval_request_id", None)
        return proceed, req_id

    def _gated_response(req_id):
        return {
            "gated": True,
            "approval_request_id": str(req_id) if req_id else None,
            "message": (
                "This high-impact action was submitted for human "
                "approval and is now pending review. Do NOT retry "
                "it; move on to the next item."
            ),
        }

    tools: list = []
    _gmail_entry: dict | None = None
    # De-duplicate by provider so tool names are stable (first integration wins
    # per provider; multiple accounts of the same provider are addressed by the
    # agent via the same tool since creds are bound here).
    for integration_id, entry in ctx.resolved_credentials.items():
        provider = entry["provider"]
        credentials = entry["credentials"]
        config = entry.get("config") or {}
        tool_name = f"{provider}_api".replace("-", "_")

        # Remember the first gmail integration so we can also register a
        # dedicated, structured `gmail_reply` tool bound to its credentials.
        if provider == "gmail" and _gmail_entry is None:
            _gmail_entry = entry

        _hint = provider_api.api_hint(provider)
        _hint_text = f" Verified endpoints for {provider}: {_hint}" if _hint else ""

        def _make(provider=provider, credentials=credentials, config=config,
                  hint_text=_hint_text):
            @tool(name=tool_name, description=(
                f"Make an authenticated API call to the connected {provider} "
                "account. Args: method (GET/POST/PUT/PATCH/DELETE), path (API "
                "path or full URL), query (optional dict), body (optional dict). "
                "Returns the provider's JSON response. Use the exact endpoint "
                "paths below; do not guess REST paths." + hint_text
            ))
            def _provider_api_tool(
                method: str,
                path: str,
                query: dict | None = None,
                body: dict | None = None,
            ) -> dict:
                # Gate write actions through the approval hook when present.
                if agent_tools.is_write(method) and ctx.before_tool_call is not None:
                    decision = ctx.before_tool_call(  # type: ignore[misc]
                        tool_name, {"method": method, "path": path, "body": body}
                    )
                    proceed, req_id = _interpret_decision(decision)
                    if not proceed:
                        return _gated_response(req_id)
                return _run_async(
                    agent_tools.call_provider_api(
                        provider_name=provider,
                        credentials=credentials,
                        config=config,
                        method=method,
                        path=path,
                        query=query,
                        body=body,
                    )
                )

            return _provider_api_tool

        tools.append(_make())

    # Also register a dedicated, structured Gmail reply tool when a gmail
    # integration is connected. Our code assembles a correct RFC 2822 message
    # and base64url-encodes it, so the small model never hand-builds MIME.
    if _gmail_entry is not None:
        gmail_credentials = _gmail_entry["credentials"]
        gmail_config = _gmail_entry.get("config") or {}

        @tool(name="gmail_reply", description=(
            "Create a Gmail draft reply. Provide simple fields and this tool "
            "builds a correct, well-formed email for you (you do NOT build MIME "
            "or base64). Args: to (recipient address), subject (original subject; "
            "a 'Re:' prefix is added automatically for replies), body (the reply "
            "prose), in_reply_to (optional original Message-ID header to thread "
            "the reply), thread_id (optional Gmail threadId to attach the draft to "
            "the original conversation), source_message_id (optional Gmail id of "
            "the ORIGINAL message you are replying to; pass the message's `id` so "
            "the platform can mark it read on approval and avoid re-proposing it), "
            "cc (optional). The draft is routed for human approval automatically. "
            "Prefer this over the drafts endpoint for replying to email."
        ))
        def _gmail_reply_tool(
            to: str,
            subject: str,
            body: str,
            in_reply_to: str | None = None,
            thread_id: str | None = None,
            source_message_id: str | None = None,
            cc: str | None = None,
        ) -> dict:
            from app.services import gmail_message

            # 6. Validate inputs: refuse empty drafts without gating/creating.
            if not (to and to.strip()) or not (body and body.strip()):
                return {
                    "error": "invalid_reply",
                    "message": "gmail_reply requires a non-empty 'to' and 'body'.",
                }

            # 0. Derive the reply recipient SERVER-SIDE so it never depends on
            # the model guessing From vs To. Small models frequently confuse the
            # original ``To`` (the mailbox owner's own address) with the original
            # ``From`` (the actual sender), producing a reply addressed to
            # oneself. When ``source_message_id`` is provided we fetch the
            # ORIGINAL message's headers directly from Gmail and OVERRIDE the
            # model-provided ``to`` with the true sender (Reply-To > From). We
            # also backfill In-Reply-To / Subject from the original when the
            # model omitted them. If the lookup fails for any reason we fall
            # back to the model-provided values (resilience — never block a
            # reply on a metadata fetch hiccup).
            if source_message_id:
                try:
                    lookup = _run_async(
                        agent_tools.call_provider_api(
                            provider_name="gmail",
                            credentials=gmail_credentials,
                            config=gmail_config,
                            method="GET",
                            path=(
                                "/gmail/v1/users/me/messages/"
                                f"{source_message_id}"
                            ),
                            # format=metadata returns headers WITHOUT the
                            # message body (cheap). We intentionally do NOT pass a
                            # metadataHeaders filter: our provider caller does not
                            # serialize repeated query params the way Gmail needs,
                            # so the filter returns an EMPTY header set and the
                            # server-side recipient override silently falls back to
                            # the model's (often wrong) `to`. Fetching all metadata
                            # headers is still body-free and reliably includes From.
                            query={"format": "metadata"},
                        )
                    )
                except Exception:  # pragma: no cover - defensive
                    lookup = None

                headers = _parse_gmail_headers(lookup)
                if headers:
                    derived_to = (
                        headers.get("reply-to") or ""
                    ).strip() or (headers.get("from") or "").strip()
                    if derived_to:
                        to = derived_to
                    if not (in_reply_to and in_reply_to.strip()):
                        msg_id = (headers.get("message-id") or "").strip()
                        if msg_id:
                            in_reply_to = msg_id
                    if not (subject and subject.strip()):
                        orig_subject = (headers.get("subject") or "").strip()
                        if orig_subject:
                            subject = orig_subject

            # 0a. VALIDATE the recipient has a real email address. A bare
            # display name ("Atomic AI" with no <addr>) makes Gmail reject the
            # send with 400 "Invalid To header" — and the scheduled-send cron
            # would then retry it forever. Refuse to build such a draft. We check
            # the FINAL `to` (after the server-side override above).
            from email.utils import parseaddr as _parseaddr

            _addr = _parseaddr(to or "")[1]
            if "@" not in _addr:
                return {
                    "skipped": True,
                    "reason": "invalid_recipient",
                    "message": (
                        "Could not determine a valid reply-to email address for "
                        "this message; no reply was drafted. Move on to the next "
                        "item."
                    ),
                }

            # 0b. NEVER reply to a no-reply / do-not-reply sender. Checked AFTER
            # the true recipient is derived server-side (step 0), so we test the
            # address we would actually reply to — not a model guess. Replying to
            # such a mailbox bounces / is ignored and only wastes credits, so we
            # refuse to build the draft or create an approval at all.
            if gmail_message.is_no_reply_address(to):
                return {
                    "skipped": True,
                    "reason": "no_reply_sender",
                    "message": (
                        "The sender is a no-reply address; no reply was drafted. "
                        "Move on to the next item."
                    ),
                }

            # 1-3. Build the clean base64url RFC 2822 draft payload via the one
            # shared builder (base64 CTE => no quoted-printable "=" soft breaks;
            # threadId attached on the message object). See gmail_message.py.
            payload = gmail_message.build_draft_payload(
                to=to,
                subject=subject,
                body=body,
                in_reply_to=in_reply_to,
                thread_id=thread_id,
                cc=cc,
            )

            # 4. Gate through the approval hook exactly like the generic write.
            # The source message/thread ids are included in the GATED arguments
            # (NOT inside the raw message) so the approval row records which
            # original email this reply handles. This lets the execution path
            # mark the original read on approval and lets the poll guard skip
            # threads that already have a pending/approved reply (PART 2).
            if ctx.before_tool_call is not None:
                gated_arguments: dict = {
                    "method": "POST",
                    "path": "/gmail/v1/users/me/drafts",
                    "body": payload,
                }
                if source_message_id:
                    gated_arguments["source_message_id"] = source_message_id
                if thread_id:
                    gated_arguments["thread_id"] = thread_id
                decision = ctx.before_tool_call(  # type: ignore[misc]
                    "gmail_reply",
                    gated_arguments,
                )
                proceed, req_id = _interpret_decision(decision)
                if not proceed:
                    return _gated_response(req_id)

            # 5. If not gated (hook absent), actually create the draft.
            return _run_async(
                agent_tools.call_provider_api(
                    provider_name="gmail",
                    credentials=gmail_credentials,
                    config=gmail_config,
                    method="POST",
                    path="/gmail/v1/users/me/drafts",
                    body=payload,
                )
            )

        tools.append(_gmail_reply_tool)

    return tools


def _parse_gmail_headers(response: Any) -> dict[str, str]:
    """Parse ``body.payload.headers`` from a Gmail messages.get response.

    Returns a case-insensitively keyed dict (lowercased header names → values)
    or an empty dict if the response is missing/failed/malformed. Used by
    ``gmail_reply`` to derive the true reply recipient server-side.

    Expected shape::

        {"status_code": 200, "ok": true,
         "body": {"payload": {"headers": [{"name": "From", "value": "..."}, ...]}}}
    """
    if not isinstance(response, dict):
        return {}
    if response.get("ok") is False:
        return {}
    status = response.get("status_code")
    if isinstance(status, int) and not (200 <= status < 300):
        return {}
    body = response.get("body")
    if not isinstance(body, dict):
        return {}
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return {}
    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, list):
        return {}
    out: dict[str, str] = {}
    for h in raw_headers:
        if isinstance(h, dict):
            name = h.get("name")
            value = h.get("value")
            if isinstance(name, str) and value is not None:
                out[name.lower()] = str(value)
    return out


def _run_async(coro):
    """Run an async coroutine to completion from a sync tool callable.

    The Strands agent invokes tool callables synchronously inside its loop; our
    provider calls are async. Run them on a private event loop in a worker
    thread so we never touch a running loop in the current thread.
    """
    import asyncio
    import concurrent.futures

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


def strands_run_loop(ctx: RunLoopContext) -> RunLoopResult:  # pragma: no cover
    """Real Strands SDK adapter: rules -> system prompt, integrations -> tools.

    Builds an Amazon Bedrock-backed :class:`~strands.Agent` whose system prompt
    encodes the workspace's applicable rules (Req 8.4) and whose tools are one
    authenticated provider-API tool per connected integration (Req 9.x), with
    write actions gated by the approval hook (Req 10.x). Imported lazily so the
    module never requires the SDK/model at import time and tests can inject a
    fake loop.

    Raises:
        RuntimeError: if the SDK is unavailable.
    """
    try:
        from strands import Agent  # type: ignore[import-not-found]
        from strands.models import BedrockModel  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - surface a clear lifecycle error
        raise RuntimeError(
            "Strands SDK is not available; inject a run_loop to run the engine."
        ) from exc

    from app.config import get_settings

    settings = get_settings()
    # COST-CONTROL: cap per-response output so one turn can't emit a huge body.
    model = BedrockModel(
        model_id=settings.BEDROCK_MODEL_ID,
        region_name=settings.AWS_REGION,
        streaming=settings.BEDROCK_STREAMING,
        max_tokens=settings.AGENT_MAX_OUTPUT_TOKENS,
    )

    system_prompt = _build_system_prompt(ctx)
    tools = _make_provider_tools(ctx)

    # COST-CONTROL (root-cause fix for the multi-million-token runs): bound the
    # context so history cannot grow unbounded across turns. A single "process
    # the inbox" run otherwise accumulated every email body + every draft and
    # re-sent them each turn (quadratic token growth). The sliding window keeps
    # only the most recent N messages and truncates oversized tool results.
    conversation_manager = None
    try:  # Best-effort: never let an SDK shape change crash a run.
        from strands.agent.conversation_manager import (  # type: ignore[import-not-found]
            SlidingWindowConversationManager,
        )

        conversation_manager = SlidingWindowConversationManager(
            window_size=settings.AGENT_CONVERSATION_WINDOW,
            should_truncate_results=True,
        )
    except Exception:  # noqa: BLE001 - fall back to default (no window) if unavailable
        conversation_manager = None

    agent_kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "tools": tools,
    }
    if conversation_manager is not None:
        agent_kwargs["conversation_manager"] = conversation_manager
    agent = Agent(**agent_kwargs)

    # COST-CONTROL: HARD per-run budget. When any cap trips, the run stops
    # gracefully (stop_reason limit_turns / limit_total_tokens) with messages in
    # a reinvokable state — no exception. This is the definitive stop for a
    # runaway agent loop regardless of prompt/inbox size.
    call_kwargs: dict[str, Any] = {}
    try:
        from strands.types.agent import Limits  # type: ignore[import-not-found]

        call_kwargs["limits"] = Limits(
            turns=settings.AGENT_MAX_TURNS,
            total_tokens=settings.AGENT_MAX_TOTAL_TOKENS,
        )
    except Exception:  # noqa: BLE001 - older SDK without Limits: rely on the window
        call_kwargs = {}

    result = agent(ctx.prompt, **call_kwargs)  # type: ignore[operator]

    # Capture token usage so the admin "Total tokens used" reflects real numbers
    # (previously always 0). The Strands SDK exposes accumulated usage on the
    # AgentResult's EventLoopMetrics as ``result.metrics.accumulated_usage`` — a
    # ``Usage`` TypedDict with ``inputTokens`` / ``outputTokens`` / ``totalTokens``
    # (camelCase). We read it robustly across a few possible shapes/paths and
    # default to 0 if none are present, so a future SDK change never crashes the
    # run or the metrics write.
    input_tokens, output_tokens, tokens = _extract_token_usage(result)

    return RunLoopResult(
        total_tokens_used=tokens,
        logs={
            "output": str(result),
            "providers": sorted({v["provider"] for v in ctx.resolved_credentials.values()}),
            # Record the input/output split alongside the total for observability.
            "token_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": tokens,
            },
        },
        output=result,
    )


def _coerce_int(value: Any) -> int:
    """Best-effort non-negative int from ``value`` (0 on None/garbage)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def _usage_field(usage: Any, key: str) -> int:
    """Read a usage counter that may be a TypedDict key or an attribute.

    The Strands ``Usage`` is a ``TypedDict`` (so ``usage["inputTokens"]``), but
    we also tolerate an object with attribute access, so this works if the SDK
    later swaps the representation. Returns 0 when absent.
    """
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return _coerce_int(usage.get(key))
    return _coerce_int(getattr(usage, key, 0))


def _extract_token_usage(result: Any) -> tuple[int, int, int]:
    """Return (input, output, total) tokens from a Strands AgentResult, robustly.

    Tries, in order, the paths the installed SDK actually uses and a couple of
    plausible fallbacks so a version bump can't silently break token capture:

    1. ``result.metrics.accumulated_usage`` (the real path in the installed
       SDK) — a ``Usage`` mapping with camelCase ``inputTokens`` /
       ``outputTokens`` / ``totalTokens``.
    2. ``result.usage`` — an older/simpler shape exposing ``total_tokens`` (and
       optionally ``input_tokens`` / ``output_tokens`` or the camelCase forms).

    Whatever is found, ``total`` falls back to ``input + output`` when a total
    field is absent. Everything defaults to 0. Never raises.
    """
    # Path 1: result.metrics.accumulated_usage (camelCase Usage TypedDict).
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) if metrics is not None else None
    if usage is not None:
        inp = _usage_field(usage, "inputTokens")
        out = _usage_field(usage, "outputTokens")
        total = _usage_field(usage, "totalTokens")
        if total == 0:
            total = inp + out
        if inp or out or total:
            return inp, out, total

    # Path 2: result.usage with snake_case or camelCase fields.
    usage2 = getattr(result, "usage", None)
    if usage2 is not None:
        inp = _usage_field(usage2, "input_tokens") or _usage_field(usage2, "inputTokens")
        out = _usage_field(usage2, "output_tokens") or _usage_field(usage2, "outputTokens")
        total = (
            _usage_field(usage2, "total_tokens")
            or _usage_field(usage2, "totalTokens")
        )
        if total == 0:
            total = inp + out
        return inp, out, total

    return 0, 0, 0


# ---------------------------------------------------------------------------
# Result of the lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRunResult:
    """The result of a successful :func:`run_agent` lifecycle.

    Attributes:
        agent_session_id: The persisted session's id.
        status: The terminal status (``completed`` on success).
        total_tokens_used: Recorded token total (Req 9.4).
        execution_time_ms: Wall-clock run duration in milliseconds (Req 9.4).
        applicable_rules: The active rules that applied to this run (Req 8.4).
        tool_servers: The workspace-scoped tool servers used (Req 9.2/9.3).
        output: The run loop's final output (opaque).
    """

    agent_session_id: uuid.UUID
    status: AgentSessionStatus
    total_tokens_used: int
    execution_time_ms: int
    applicable_rules: list[RuleApplies] = field(default_factory=list)
    tool_servers: list[ToolServer] = field(default_factory=list)
    output: Any = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def run_agent(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    triggered_by_user_id: uuid.UUID,
    thread_id: str,
    prompt: str,
    category: str,
    provider_name: str | None = None,
    run_loop: RunLoop = strands_run_loop,
    before_tool_call: BeforeToolCall | None = None,
) -> AgentRunResult:
    """Run one agent execution end-to-end, managing its session lifecycle.

    Steps (in order):

    1. Create an :class:`~app.db.models.AgentSession` scoped to ``workspace_id``
       and ``triggered_by_user_id`` with status ``running`` and commit it, so a
       running execution is visible even if the process is later interrupted
       (Req 9.1).
    2. Load the applicable active rules: fetch the workspace's rules and filter
       them with :func:`rules_service.applicable_rules` against a
       :class:`RuleContext(category, provider_name)` — active rules that are
       workspace-wide or match the category/provider (Req 8.4).
    3. Resolve the workspace-scoped tool servers via
       :func:`mcp_registry.resolve` (Req 9.2/9.3/9.6).
    4. Time and run the injected ``run_loop`` with a :class:`RunLoopContext`
       carrying the rules, tool servers, prompt, and ``before_tool_call`` hook.
    5. On success, record ``total_tokens_used``, ``execution_time_ms``, and
       ``execution_logs`` and set status ``completed``, commit, and return an
       :class:`AgentRunResult` (Req 9.4).
    6. On any exception from the loop, record the elapsed time and error details
       in ``execution_logs``, set status ``failed``, commit, and re-raise the
       original exception (Req 9.5).

    The elapsed time uses :func:`time.perf_counter` (monotonic) for an accurate,
    clock-adjustment-immune duration.

    Args:
        session: Active async session. This function commits internally; the
            caller does not need to (and may reuse ``session`` afterwards).
        workspace_id: The workspace the execution is scoped to.
        triggered_by_user_id: The triggering user (or system principal).
        thread_id: Conversation/thread identifier for the run.
        prompt: The trigger input / goal for the agent.
        category: The execution's category, used for rule resolution (Req 8.4).
        provider_name: The execution's provider, or ``None`` when not
            provider-scoped; used for rule resolution (Req 8.4).
        run_loop: Injectable agent loop; defaults to the real Strands adapter.
        before_tool_call: Optional approval hook forwarded to the loop (task
            13.1); ``None`` means no gating.

    Returns:
        An :class:`AgentRunResult` describing the completed run.

    Raises:
        Exception: re-raises whatever ``run_loop`` raised, after persisting the
            session as ``failed`` (Req 9.5).
    """
    # --- 1. Create the running session (Req 9.1) and commit it. ----------------
    agent_session = AgentSession(
        workspace_id=workspace_id,
        triggered_by_user_id=triggered_by_user_id,
        thread_id=thread_id,
        status=AgentSessionStatus.RUNNING,
        total_tokens_used=0,
        execution_time_ms=0,
        execution_logs=None,
    )
    session.add(agent_session)
    await session.flush()
    agent_session_id = agent_session.id
    await session.commit()

    # --- 2. Load applicable active rules (Req 8.4). ---------------------------
    all_rules = await rules_service.list_rules(session, workspace_id=workspace_id)
    rules = rules_service.applicable_rules(
        all_rules, RuleContext(category=category, provider_name=provider_name)
    )

    # --- 3. Resolve workspace-scoped tool servers (Req 9.2/9.3/9.6). ----------
    tool_servers = await mcp_registry.resolve(
        session, workspace_id=workspace_id, user_id=triggered_by_user_id
    )

    # Decrypt each resolved integration's credentials in memory so the agent's
    # tools can call the provider APIs. Never logged/persisted (Req 6.3/6.4).
    from app.services import integration_vault as _vault
    from app.services import oauth_refresh as _oauth

    resolved_credentials: dict = {}
    for ts in tool_servers:
        try:
            cred = await _vault.use_credential(
                session, integration_id=ts.integration_id
            )
        except Exception:  # noqa: BLE001 - a bad credential just omits that tool
            continue
        creds = dict(cred.credentials)
        cfg = dict(cred.config or {})
        # For OAuth families, mint a fresh access token from the refresh token
        # so the API call authenticates (best-effort; unchanged on failure).
        creds = await _oauth.ensure_access_token(ts.provider_name, creds, cfg)
        resolved_credentials[str(ts.integration_id)] = {
            "provider": ts.provider_name,
            "credentials": creds,
            "config": cfg,
        }

    # Default gating: if no approval hook was injected, build the real one so
    # EVERY run (scheduled, webhook, manual) routes high-impact/write tool calls
    # through the Approval_Hub instead of executing them directly (Req 10.1).
    if before_tool_call is None:
        from app.services import approval_service as _approval
        # Use a LOOP-LOCAL (NullPool) session factory: the hook's DB work (dedup
        # lookup + persist) is driven from a per-call ephemeral event loop
        # (strands_engine._run_async), and the process-wide pooled engine would
        # raise "Future attached to a different loop" when that loop tears the
        # pooled connection down — which silently crashed the gmail_reply
        # approval path so no approval was ever created. loop_local_session_scope
        # builds + disposes a NullPool engine on the current loop, so it is safe.
        from app.db.session import loop_local_session_scope as _session_scope

        before_tool_call = _approval.make_before_tool_call(
            session,
            workspace_id=workspace_id,
            agent_session_id=agent_session_id,
            triggered_by_user_id=triggered_by_user_id,
            session_factory=_session_scope,
        )

    loop_ctx = RunLoopContext(
        agent_session_id=agent_session_id,
        workspace_id=workspace_id,
        triggered_by_user_id=triggered_by_user_id,
        thread_id=thread_id,
        prompt=prompt,
        rules=rules,
        tool_servers=tool_servers,
        before_tool_call=before_tool_call,
        resolved_credentials=resolved_credentials,
    )

    # --- 4. Run the loop, timing it with a monotonic clock. -------------------
    start = time.perf_counter()
    try:
        result = run_loop(loop_ctx)
    except Exception as exc:
        # --- 6. Unrecoverable error -> failed with error details (Req 9.5). ---
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        agent_session.status = AgentSessionStatus.FAILED
        agent_session.execution_time_ms = elapsed_ms
        agent_session.execution_logs = {
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        }
        await session.flush()
        await session.commit()
        raise

    # --- 5. Completion metrics + terminal status (Req 9.4). -------------------
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    agent_session.status = AgentSessionStatus.COMPLETED
    agent_session.total_tokens_used = int(result.total_tokens_used or 0)
    agent_session.execution_time_ms = elapsed_ms
    agent_session.execution_logs = result.logs
    await session.flush()
    await session.commit()

    return AgentRunResult(
        agent_session_id=agent_session_id,
        status=AgentSessionStatus.COMPLETED,
        total_tokens_used=agent_session.total_tokens_used,
        execution_time_ms=elapsed_ms,
        applicable_rules=list(rules),
        tool_servers=list(tool_servers),
        output=result.output,
    )


__all__ = [
    "BeforeToolCall",
    "RunLoop",
    "RunLoopContext",
    "RunLoopResult",
    "AgentRunResult",
    "strands_run_loop",
    "run_agent",
]
