"""Approval_Hub — BeforeToolCall hook and high-impact gating (task 13.1).

Implements the first slice of the ``Approval_Hub`` component from the design
(see design.md "Approval_Hub and WebSocket_Gateway" and the agent-loop sequence
where the ``BeforeToolCall_Hook`` gates a proposed tool call). This module is
deliberately scoped to **task 13.1 only**:

- A *pure*, DB-free classifier :func:`is_high_impact` deciding whether a
  proposed tool call must be gated for human review (Req 10.1).
- A hook factory :func:`make_before_tool_call` that returns a callable matching
  the :class:`~app.services.strands_engine.BeforeToolCall` protocol. When the
  run loop consults it before a proposed tool call it either signals *proceed*
  (non-high-impact, no request created) or persists a ``pending``
  :class:`~app.db.models.ApprovalRequest` and signals *pause* (high-impact),
  never letting the call proceed (Req 10.1).

Explicitly **out of scope** here (later tasks, do not implement):

- approve/reject *resolution* of a request and the terminal state machine
  (task 13.3),
- the WebSocket broadcast of created/resolved requests (task 13.5),
- the approvals HTTP router (task 13.7).

Why a separate ``approval_service`` module
------------------------------------------
Task 13.3 builds the rest of the ``Approval_Hub`` (resolution + state machine)
in this same module, so keeping the hook and classifier here co-locates the
approval logic. Only 13.1's pieces are implemented now.

High-impact policy (documented, configurable, testable)
-------------------------------------------------------
A tool call is **high-impact** when it is *state-changing*, *outbound*, or
*destructive* — i.e. it can mutate external systems, send data outside the
platform, spend money, or deploy/execute code. Read-only calls (fetching,
listing, searching, reading) are **not** high-impact and proceed without review.

The policy is expressed as a set of case-insensitive verb *markers* matched
against the tool name (the leading verb token and/or as a prefix). The default
ruleset (:data:`DEFAULT_HIGH_IMPACT_MARKERS`) covers common outbound/mutating/
destructive verbs (``send``, ``delete``, ``post``, ``create``, ``update``,
``transfer``, ``deploy``, ``execute``, ``pay`` and more). Both the classifier and
the hook accept an overridable ruleset so callers/tests can tighten or widen the
policy without changing code. :func:`is_high_impact` is a pure function so task
13.2 (Property 23) can property-test it in isolation.

Transaction ownership
---------------------
For a *gated* (high-impact) call the hook persists the ``pending`` request
durably by committing, so the approval is immediately visible to reviewers (the
broadcast/resolution in tasks 13.5/13.3 rely on it existing). Arguments are
scrubbed with :func:`app.core.scrubbing.scrub` before being stored so no
secret-looking field value is ever persisted (Req 6.4/15.4 aligned).

Requirements: 10.1.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.rbac import Capability, can
from app.core.scrubbing import scrub
from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    MemberRole,
    SystemAuditLog,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# High-impact policy (pure, configurable)
# ---------------------------------------------------------------------------

#: Default set of verb markers that make a tool call high-impact. A call whose
#: tool name *starts with* any of these verbs (as its leading token, e.g.
#: ``send_email``, ``send-email``, ``sendEmail``, or exactly ``send``) is gated.
#: These are the state-changing / outbound / destructive verbs; read-only verbs
#: (``get``, ``list``, ``read``, ``search``, ``fetch``, ``describe`` ...) are
#: deliberately absent so such calls proceed without review.
DEFAULT_HIGH_IMPACT_MARKERS: frozenset[str] = frozenset(
    {
        "send",
        "post",
        "publish",
        "create",
        "update",
        "modify",
        "edit",
        "patch",
        "put",
        "write",
        "delete",
        "remove",
        "destroy",
        "drop",
        "purge",
        "transfer",
        "pay",
        "charge",
        "refund",
        "deploy",
        "release",
        "execute",
        "run",
        "invoke",
        "trigger",
        "revoke",
        "grant",
        "invite",
        "provision",
        "terminate",
        "cancel",
        "approve",
        "merge",
        "push",
    }
)

# Split a tool name into its leading verb token. Tool names arrive in many
# shapes (``send_email``, ``send-email``, ``sendEmail``, ``mcp.send_email``,
# ``Server.sendEmail``); we take the last dotted/namespaced segment then peel the
# leading token before the first separator (``_``/``-``) or the first camelCase
# boundary.
_LEADING_TOKEN_RE = re.compile(r"^[^._\-]*")

#: State-changing HTTP methods. A generic ``{provider}_api`` tool carries its
#: real intent in ``arguments["method"]`` (e.g. ``POST /drafts``), so a call
#: whose method is one of these is high-impact regardless of the tool name's
#: verb. Read-only methods (GET/HEAD/OPTIONS) are deliberately absent. Kept as a
#: local constant so :func:`is_high_impact` stays a pure function with no
#: coupling to the agent-tools layer.
_STATE_CHANGING_HTTP_METHODS: frozenset[str] = frozenset(
    {"POST", "PUT", "PATCH", "DELETE"}
)


def _leading_verb(tool_name: str) -> str:
    """Return the lower-cased leading verb token of ``tool_name`` (pure).

    Namespacing (``a.b.tool``) is stripped to the final segment; the leading
    token is then everything up to the first ``_``/``-`` separator or the first
    camelCase boundary. Examples: ``send_email`` -> ``send``,
    ``deleteRecord`` -> ``delete``, ``mcp.postMessage`` -> ``post``.
    """
    if not tool_name:
        return ""
    # Final namespaced segment, e.g. "mcp.send_email" -> "send_email".
    segment = tool_name.rsplit(".", 1)[-1]
    # Token before the first _/- separator, e.g. "send_email" -> "send".
    head = _LEADING_TOKEN_RE.match(segment)
    token = head.group(0) if head else segment
    # camelCase/PascalCase boundary within the token: take the leading run up to
    # the next uppercase letter, so "sendEmail" -> "send" and "SendEmail" ->
    # "Send". A single leading uppercase (Pascal) is kept with the run.
    camel = re.match(r"^[A-Za-z0-9]+?(?=[A-Z]|$)", token)
    if camel and camel.group(0):
        return camel.group(0).lower()
    return token.lower()


def is_high_impact(
    tool_name: str,
    arguments: Any = None,
    *,
    markers: frozenset[str] | set[str] = DEFAULT_HIGH_IMPACT_MARKERS,
) -> bool:
    """Return whether a proposed tool call is high-impact and must be gated (pure).

    Policy (two independent, OR-combined signals):

    1. **Tool-name verb** — a call is high-impact when its tool name's leading
       verb token is one of ``markers`` (state-changing / outbound /
       destructive). Read-only verbs (``get_*``, ``list_*``, ``read_*``,
       ``search_*`` ...) not in ``markers`` are not high-impact by this signal.
       The comparison is case-insensitive and separator-agnostic, so
       ``send_email``, ``send-email``, ``sendEmail``, and ``SendEmail`` all
       classify identically (Req 10.1).

    2. **Argument method (argument-aware)** — generic provider tools are named
       ``{provider}_api`` (e.g. ``gmail_api``), whose leading verb (``gmail``)
       is not a marker, so the real intent lives in ``arguments``:
       ``{"method": "POST"|"PUT"|"PATCH"|"DELETE", "path": ..., "body": ...}``.
       When ``arguments`` is a mapping carrying a ``method`` key whose
       uppercased value is one of the state-changing HTTP methods
       (:data:`_STATE_CHANGING_HTTP_METHODS`), the call is high-impact. Read-only
       methods (``GET``/``HEAD``/``OPTIONS``) are not. This gates generic write
       calls (create draft = ``POST /drafts``, send = ``POST /messages/send``)
       that the verb signal alone would miss.

    A call is high-impact if **either** signal fires. Passing no ``arguments``
    (or a non-mapping / one without a state-changing ``method``) falls back to
    the verb signal alone, preserving prior behavior for named tools.

    Args:
        tool_name: The proposed tool's name.
        arguments: The proposed call's arguments. When a mapping with a
            state-changing ``method``, it alone makes the call high-impact.
        markers: Overridable set of high-impact leading verbs; defaults to
            :data:`DEFAULT_HIGH_IMPACT_MARKERS`.

    Returns:
        ``True`` if the call is high-impact (must be gated), ``False`` otherwise.
    """
    # Argument-aware signal: a state-changing HTTP method makes the call
    # high-impact regardless of the (possibly generic) tool name's verb.
    if isinstance(arguments, Mapping):
        method = arguments.get("method")
        if isinstance(method, str) and method.upper() in _STATE_CHANGING_HTTP_METHODS:
            return True

    # Tool-name verb signal (unchanged): leading verb membership in markers.
    verb = _leading_verb(tool_name)
    if not verb:
        return False
    return verb in {m.lower() for m in markers}


# ---------------------------------------------------------------------------
# Tool-call decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallDecision:
    """The hook's decision about a single proposed tool call.

    Attributes:
        proceed: ``True`` when the call may proceed immediately (non-high-impact,
            no approval needed); ``False`` when the call is paused awaiting human
            approval (a ``pending`` request was created).
        approval_request_id: The id of the created ``pending``
            :class:`~app.db.models.ApprovalRequest` when ``proceed`` is
            ``False``; ``None`` when the call proceeds.
    """

    proceed: bool
    approval_request_id: uuid.UUID | None = None

    @classmethod
    def allow(cls) -> "ToolCallDecision":
        """A decision that lets the call proceed with no approval request."""
        return cls(proceed=True, approval_request_id=None)

    @classmethod
    def pause(cls, approval_request_id: uuid.UUID) -> "ToolCallDecision":
        """A decision that pauses the call, referencing the created request."""
        return cls(proceed=False, approval_request_id=approval_request_id)


# The concrete hook type: called with (tool_name, arguments), returns a decision
# (awaited by the run-loop adapter). Matches the structural ``BeforeToolCall``
# protocol in :mod:`app.services.strands_engine`.
BeforeToolCallHook = Callable[[str, Any], Awaitable[ToolCallDecision]]


# ---------------------------------------------------------------------------
# Hook factory
# ---------------------------------------------------------------------------


#: Tool name for the structured Gmail reply approvals (defined again lower for
#: the edit/regenerate section; kept here as a local constant so the dedup guard
#: below does not depend on import order).
_GMAIL_REPLY_TOOL_NAME = "gmail_reply"


def _reply_source_ids(arguments: Any) -> tuple[str | None, str | None]:
    """Extract (source_message_id, thread_id) from gmail_reply gated arguments.

    Both are stored at the TOP LEVEL of the gated arguments dict by the
    ``gmail_reply`` tool (not inside the raw MIME). Returns ``(None, None)`` when
    absent or the shape is unexpected.
    """
    if not isinstance(arguments, Mapping):
        return None, None
    smid = arguments.get("source_message_id")
    tid = arguments.get("thread_id")
    smid = smid.strip() if isinstance(smid, str) and smid.strip() else None
    tid = tid.strip() if isinstance(tid, str) and tid.strip() else None
    return smid, tid


async def _gmail_reply_already_exists(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_message_id: str | None,
    thread_id: str | None,
) -> bool:
    """Whether a gmail_reply approval already exists for this source email.

    DURABLE, model-independent de-duplication (the fix for the runaway
    reply-generation loop): a reply may be created for a given original email
    AT MOST ONCE, regardless of the approval's later status (pending / approved /
    rejected) and regardless of the email's read/unread state. We match on the
    original ``source_message_id`` first (the stable per-message key); if the
    tool only supplied a ``thread_id`` we fall back to matching the thread so we
    never draft two replies into the same conversation.

    Scans this workspace's ``gmail_reply`` rows and compares the stored
    ``arguments`` (JSONB) ids. Returns ``True`` if a matching reply already
    exists (=> skip creating another). Tenant-scoped: only this workspace's rows
    are considered.
    """
    if not source_message_id and not thread_id:
        # No stable id to dedup on — cannot guarantee uniqueness; allow creation
        # (the tool always supplies source_message_id in practice).
        return False

    # Match in the DB via JSONB key lookup (efficient + scales past hundreds of
    # rows). ``arguments`` is JSONB with source_message_id / thread_id stored at
    # the top level by the gmail_reply tool. Prefer the per-message id; fall back
    # to the thread id so two replies never land in the same conversation.
    from sqlalchemy import or_

    conditions = []
    if source_message_id:
        conditions.append(
            ApprovalRequest.arguments["source_message_id"].astext == source_message_id
        )
    if thread_id:
        conditions.append(
            ApprovalRequest.arguments["thread_id"].astext == thread_id
        )

    stmt = (
        select(ApprovalRequest.id)
        .where(ApprovalRequest.workspace_id == workspace_id)
        .where(ApprovalRequest.tool_name == _GMAIL_REPLY_TOOL_NAME)
        .where(or_(*conditions))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.first() is not None


def make_before_tool_call(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_session_id: uuid.UUID | None,
    triggered_by_user_id: uuid.UUID,
    is_high_impact: Callable[[str, Any], bool] = is_high_impact,
    session_factory: Any = None,
) -> BeforeToolCallHook:
    """Build the BeforeToolCall approval hook for one agent execution (Req 10.1).

    Returns an ``async`` callable matching the
    :class:`~app.services.strands_engine.BeforeToolCall` protocol. The run-loop
    adapter awaits it before each proposed tool call. For a proposed call
    ``(tool_name, arguments)`` the hook:

    - **Not high-impact** -> returns :meth:`ToolCallDecision.allow` **without**
      creating any :class:`~app.db.models.ApprovalRequest`; the call proceeds.
    - **High-impact** -> creates a ``pending``
      :class:`~app.db.models.ApprovalRequest` bound to ``workspace_id``,
      ``agent_session_id``, and ``triggered_by_user_id`` with ``tool_name`` and
      the **scrubbed** ``arguments`` (JSONB), persists it durably (flush +
      commit so reviewers see it immediately), and returns
      :meth:`ToolCallDecision.pause` referencing the new request id. The call
      does **not** proceed (Req 10.1).

    The hook only *signals* the pause; it does not itself block/await the
    reviewer's resolution — approve/reject (task 13.3) and the broadcast (task
    13.5) are handled elsewhere.

    Arguments are scrubbed with :func:`app.core.scrubbing.scrub` before being
    stored so no secret-looking field value (tokens, credentials, passwords) is
    ever persisted or later broadcast.

    Args:
        session: Active async session used to persist gated requests. The hook
            commits internally for a gated call so the ``pending`` request is
            durable and immediately visible.
        workspace_id: The owning workspace the request is bound to.
        agent_session_id: The originating agent session, or ``None`` if the call
            is not tied to a persisted session.
        triggered_by_user_id: The user (or system principal) whose execution
            proposed the call.
        is_high_impact: Overridable classifier deciding whether a call is gated;
            defaults to :func:`is_high_impact` (the default policy). Injecting a
            custom classifier lets callers/tests tighten or widen the policy.

    Returns:
        An ``async`` hook callable ``(tool_name, arguments) -> ToolCallDecision``.
    """

    async def _persist(target_session: AsyncSession, tool_name: str, arguments: Any) -> uuid.UUID:
        scrubbed_arguments = scrub(arguments) if arguments is not None else None
        request = ApprovalRequest(
            workspace_id=workspace_id,
            agent_session_id=agent_session_id,
            triggered_by_user_id=triggered_by_user_id,
            reviewed_by_user_id=None,
            tool_name=tool_name,
            arguments=scrubbed_arguments,
            status=ApprovalStatus.PENDING,
        )
        target_session.add(request)
        await target_session.flush()
        request_id = request.id
        await target_session.commit()
        return request_id

    async def before_tool_call(tool_name: str, arguments: Any) -> ToolCallDecision:
        if not is_high_impact(tool_name, arguments):
            # Non-high-impact: proceed, create nothing (Req 10.1).
            return ToolCallDecision.allow()

        # DURABLE DE-DUP (runaway-reply fix): never create more than ONE
        # gmail_reply approval for the same original email. A poll re-runs every
        # cycle and unapproved drafts leave the source email UNREAD, so without
        # this guard the same email is re-drafted on every poll (the bug that
        # generated 300+ duplicate replies and drained credits). We check for an
        # existing reply (ANY status) for this source_message_id / thread_id and,
        # if found, DO NOT create another row and DO NOT proceed — returning a
        # gated decision with no request id so the tool reports "already handled,
        # do not retry" and the model moves on. Checked in a fresh session when a
        # factory is provided (same loop-safety reason as _persist).
        if tool_name == _GMAIL_REPLY_TOOL_NAME:
            # NO-REPLY GUARD (defense in depth; the tool also checks): never
            # create a reply approval addressed to a no-reply / do-not-reply
            # mailbox. The recipient lives inside the gated raw MIME, so decode
            # it. Blocked here too so the rule holds no matter which path reaches
            # the gate. No row, no draft, no retry.
            from app.services import gmail_message

            _to = _decode_reply_fields(arguments).get("to")
            if gmail_message.is_no_reply_address(_to):
                return ToolCallDecision(proceed=False, approval_request_id=None)

            smid, tid = _reply_source_ids(arguments)
            if smid or tid:
                if session_factory is not None:
                    async with session_factory() as check:
                        dup = await _gmail_reply_already_exists(
                            check,
                            workspace_id=workspace_id,
                            source_message_id=smid,
                            thread_id=tid,
                        )
                else:
                    dup = await _gmail_reply_already_exists(
                        session,
                        workspace_id=workspace_id,
                        source_message_id=smid,
                        thread_id=tid,
                    )
                if dup:
                    # Already have a reply for this email — skip silently. No new
                    # row, no draft, no retry (approval_request_id=None).
                    return ToolCallDecision(proceed=False, approval_request_id=None)

        # High-impact: persist a pending request with scrubbed arguments and
        # pause the call (Req 10.1). Never persist secret-looking values.
        #
        # When a ``session_factory`` is provided, persist inside a FRESH session.
        # The run loop invokes this hook from a separate worker-thread event loop
        # (see strands_engine._run_async), so reusing the run's own AsyncSession
        # here would cross event loops on one asyncpg connection. A short-lived
        # session per gated call is loop/thread-safe and still durable.
        if session_factory is not None:
            async with session_factory() as fresh:
                request_id = await _persist(fresh, tool_name, arguments)
        else:
            request_id = await _persist(session, tool_name, arguments)

        # SMS notification (best-effort): a reply now awaits approval, so text
        # the workspace owner(s) who opted in. Never blocks or fails the gate —
        # the approval is already durably committed above. Only for reply
        # approvals (the user-facing "reply awaiting approval" alert).
        if tool_name == _GMAIL_REPLY_TOOL_NAME and session_factory is not None:
            try:
                await _notify_reply_awaiting_approval(
                    session_factory=session_factory,
                    workspace_id=workspace_id,
                    approval_request_id=request_id,
                )
            except Exception:  # noqa: BLE001 - notification must never break the gate
                logger.debug("reply-approval SMS notification failed; continuing")

        return ToolCallDecision.pause(request_id)

    return before_tool_call


async def _notify_reply_awaiting_approval(
    *,
    session_factory: Any,
    workspace_id: uuid.UUID,
    approval_request_id: uuid.UUID,
) -> None:
    """Text the workspace's approvers that a reply awaits approval (best-effort).

    Resolves the workspace members who CAN resolve approvals (Owner/Admin) and
    have a phone number on file with notifications enabled, counts the
    workspace's pending replies for the message body, and sends each an SMS via
    :func:`app.services.sns_service.send_reply_approval_sms`. Enforces the
    per-user monthly spend cap (``SNS_MONTHLY_USER_SPEND_CAP_USD``) so a runaway
    loop cannot rack up cost. Opens its own fresh sessions (loop-safe) and never
    raises — SMS is a convenience layered on top of the durable approval.
    """
    from app.config import get_settings
    from app.core.rbac import Capability, can
    from app.db.models import SmsNotification, User, WorkspaceMember
    from app.services import sns_service

    settings = get_settings()
    if not settings.SNS_ENABLED:
        return

    try:
        await _do_notify_reply_awaiting_approval(
            session_factory=session_factory,
            workspace_id=workspace_id,
            approval_request_id=approval_request_id,
            settings=settings,
            sns_service=sns_service,
            can=can,
            Capability=Capability,
            SmsNotification=SmsNotification,
            User=User,
            WorkspaceMember=WorkspaceMember,
        )
    except Exception:  # noqa: BLE001 - notification is best-effort, never raises
        logger.debug("reply-approval SMS notification errored; continuing")


async def _do_notify_reply_awaiting_approval(
    *,
    session_factory: Any,
    workspace_id: uuid.UUID,
    approval_request_id: uuid.UUID,
    settings: Any,
    sns_service: Any,
    can: Any,
    Capability: Any,
    SmsNotification: Any,
    User: Any,
    WorkspaceMember: Any,
) -> None:
    """Inner body of the reply-approval SMS notification (see the wrapper)."""
    # Resolve approver recipients + the current pending-reply count.
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(User, WorkspaceMember.role)
                .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
                .where(WorkspaceMember.workspace_id == workspace_id)
            )
        ).all()
        recipients: list[tuple[uuid.UUID, str, str | None]] = []
        for user, role in rows:
            if not can(role, Capability.RESOLVE_APPROVAL):
                continue
            if not user.sms_notifications_enabled or not user.phone_number:
                continue
            if user.is_banned:
                continue
            recipients.append((user.id, user.phone_number, user.phone_country))

        pending_count = (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.workspace_id == workspace_id)
                .where(ApprovalRequest.tool_name == _GMAIL_REPLY_TOOL_NAME)
                .where(ApprovalRequest.status == ApprovalStatus.PENDING)
            )
        ).scalar_one()

    if not recipients:
        return

    cap = Decimal(str(settings.SNS_MONTHLY_USER_SPEND_CAP_USD or 0))
    for user_id, phone, country in recipients:
        # Enforce the per-user monthly spend cap (0 disables).
        if cap > 0:
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            async with session_factory() as session:
                spent_rows = (
                    await session.execute(
                        select(SmsNotification.total_cost_usd)
                        .where(SmsNotification.user_id == user_id)
                        .where(SmsNotification.created_at >= month_start)
                    )
                ).scalars().all()
            spent = sum((Decimal(v or "0") for v in spent_rows), Decimal("0"))
            if spent >= cap:
                logger.info("sns sms skipped (monthly cap reached) | user=%s", user_id)
                continue

        await sns_service.send_reply_approval_sms(
            session_factory=session_factory,
            user_id=user_id,
            workspace_id=workspace_id,
            approval_request_id=approval_request_id,
            phone_number=phone,
            phone_country=country,
            pending_count=int(pending_count or 1),
        )


# ---------------------------------------------------------------------------
# Resolution state machine (task 13.3)
# ---------------------------------------------------------------------------

#: The two resolution actions a reviewer may take on a request.
ResolutionAction = Literal["approve", "reject"]

#: The pure resolver's verdict for a proposed resolution.
#:
#: - ``"apply"``  -> the transition is legal; the caller should write the
#:   corresponding terminal status (approve -> APPROVED, reject -> REJECTED).
#: - ``"conflict"`` -> the request is already terminal; the caller must reject
#:   the attempt with a conflict error (Req 10.7).
ResolutionOutcome = Literal["apply", "conflict"]

#: Maps a resolution action to the terminal status it produces on ``apply``.
_ACTION_TERMINAL_STATUS: dict[ResolutionAction, ApprovalStatus] = {
    "approve": ApprovalStatus.APPROVED,
    "reject": ApprovalStatus.REJECTED,
}

#: Maps a resolution action to the audit action string recorded on ``apply``.
_ACTION_AUDIT: dict[ResolutionAction, str] = {
    "approve": "approval.approved",
    "reject": "approval.rejected",
}


def resolve_approval_transition(
    current_status: ApprovalStatus,
    action: ResolutionAction,
) -> ResolutionOutcome:
    """Decide the outcome of resolving a request in ``current_status`` (pure).

    This is the DB-free heart of the ``Approval_Hub`` state machine, kept pure
    so task 13.4 (Property 24) can property-test it exhaustively over every
    ``(status, action)`` pair without any database.

    The only legal source state for a resolution is ``PENDING``: a pending
    request may transition to ``APPROVED`` (approve) or ``REJECTED`` (reject)
    (Req 10.3, 10.4). A request that is already terminal (``APPROVED`` or
    ``REJECTED``) yields ``"conflict"`` for *any* further resolution attempt,
    which the async layer surfaces as a 409 (Req 10.7). Because
    :class:`~app.db.models.ApprovalStatus` is a closed enum of exactly
    ``pending``/``approved``/``rejected`` (Req 10.6), those three are the only
    inputs this function must consider.

    Args:
        current_status: The request's current status.
        action: The proposed resolution (``"approve"`` or ``"reject"``).

    Returns:
        ``"apply"`` when the transition is legal (only from ``PENDING``);
        ``"conflict"`` when the request is already terminal.
    """
    if current_status is ApprovalStatus.PENDING:
        return "apply"
    # Already approved or rejected: any re-resolution is a conflict (Req 10.7).
    return "conflict"


async def resolve_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    action: ResolutionAction,
    reviewer_role: MemberRole | None = None,
) -> ApprovalRequest:
    """Resolve a pending approval request (approve/reject) and audit it.

    Implements the resolution half of the ``Approval_Hub`` (Req 10.3-10.7,
    15.3). The pure decision lives in :func:`resolve_approval_transition`; this
    async wrapper loads the row, applies the verdict, records the reviewer,
    writes an audit entry, and commits.

    Behaviour:

    - **Missing request** -> :class:`~app.core.errors.APIError` **404**.
    - **RBAC (optional, safe-by-default)**: when ``reviewer_role`` is provided,
      the service asserts the role holds
      :attr:`~app.core.rbac.Capability.RESOLVE_APPROVAL` and raises **403**
      otherwise (Req 10.5). The approvals router (task 13.7) is the primary
      RBAC guard and passes the resolved role here; leaving ``reviewer_role``
      as ``None`` skips the service-level check (used by trusted internal
      callers). The check runs *before* loading state so a forbidden caller
      never learns the request's status.
    - **Already terminal** -> the pure resolver returns ``"conflict"`` and this
      raises :class:`~app.core.errors.APIError` **409** without mutating the row
      (Req 10.7).
    - **Pending** -> sets ``status`` to ``APPROVED`` (approve, Req 10.3) or
      ``REJECTED`` (reject, Req 10.4) — never any other value (Req 10.6) — and
      records ``reviewed_by_user_id = reviewer_user_id``. Resuming or cancelling
      the paused tool call is the engine's concern; here we only record the
      terminal state and reviewer.
    - **Audit** -> appends a :class:`~app.db.models.SystemAuditLog` row for the
      owning workspace with action ``"approval.approved"``/``"approval.rejected"``
      and scrubbed metadata ``{approval_request_id, tool_name}`` (Req 15.3),
      then commits the state change + audit atomically.

    Args:
        session: Active async session; committed on success.
        approval_request_id: The request to resolve.
        reviewer_user_id: The resolving user, recorded on the request and audit.
        action: ``"approve"`` or ``"reject"``.
        reviewer_role: Optional resolved role; when given, enforced against
            ``RESOLVE_APPROVAL`` (Owner/Admin) with a 403 on failure (Req 10.5).

    Returns:
        The updated :class:`~app.db.models.ApprovalRequest` (terminal status).
    """
    # RBAC first (fail-closed): a forbidden caller must not learn any state.
    if reviewer_role is not None and not can(
        reviewer_role, Capability.RESOLVE_APPROVAL
    ):
        raise APIError(
            status_code=403,
            message="Only workspace owners or admins may resolve approvals.",
        )

    request = await session.get(ApprovalRequest, approval_request_id)
    if request is None:
        raise APIError(
            status_code=404,
            message="The approval request was not found.",
        )

    outcome = resolve_approval_transition(request.status, action)
    if outcome == "conflict":
        # Already approved or rejected — reject the re-resolution (Req 10.7).
        raise APIError(
            status_code=409,
            message="This approval request has already been resolved.",
        )

    # Legal transition from PENDING: write the terminal status + reviewer.
    request.status = _ACTION_TERMINAL_STATUS[action]
    request.reviewed_by_user_id = reviewer_user_id

    # Audit the resolution with scrubbed metadata (Req 15.3).
    audit = SystemAuditLog(
        workspace_id=request.workspace_id,
        user_id=reviewer_user_id,
        action=_ACTION_AUDIT[action],
        log_metadata=scrub(
            {
                "approval_request_id": str(approval_request_id),
                "tool_name": request.tool_name,
            }
        ),
    )
    session.add(audit)

    await session.commit()
    await session.refresh(request)
    return request


async def approve_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    reviewer_role: MemberRole | None = None,
) -> ApprovalRequest:
    """Approve a pending request: status -> ``approved``, reviewer recorded (Req 10.3).

    Thin wrapper over :func:`resolve_request` with ``action="approve"``. See that
    function for the full contract (404/403/409, audit, commit).
    """
    return await resolve_request(
        session,
        approval_request_id=approval_request_id,
        reviewer_user_id=reviewer_user_id,
        action="approve",
        reviewer_role=reviewer_role,
    )


# ---------------------------------------------------------------------------
# Editing / regenerating a PENDING gmail_reply (PART 3)
# ---------------------------------------------------------------------------

#: Tool name of the structured Gmail reply approvals we can edit/regenerate/execute.
GMAIL_REPLY_TOOL = "gmail_reply"


def _decode_reply_fields(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Decode a stored gmail_reply approval's arguments into editable fields.

    Reads the base64url RFC 2822 message out of
    ``arguments.body.message.raw`` and extracts ``to``, ``subject``, ``cc``,
    ``in_reply_to`` (from References/In-Reply-To), and the plain-text ``body``,
    plus ``thread_id`` and ``source_message_id`` recorded alongside the raw.
    Used by the EDIT and REGENERATE paths to rebuild a fresh, clean message from
    the SAME source email. Returns ``{}``-ish defaults when the shape doesn't
    match rather than raising.
    """
    import base64
    from email import policy
    from email.parser import BytesParser

    out: dict[str, Any] = {
        "to": "",
        "subject": "",
        "body": "",
        "cc": None,
        "in_reply_to": None,
        "thread_id": None,
        "source_message_id": None,
    }
    if not isinstance(arguments, Mapping):
        return out

    out["source_message_id"] = arguments.get("source_message_id")
    out["thread_id"] = arguments.get("thread_id")

    body = arguments.get("body")
    if not isinstance(body, Mapping):
        return out
    message = body.get("message")
    raw = None
    if isinstance(message, Mapping):
        raw = message.get("raw")
        # threadId lives on the message object; prefer it if not already set.
        out["thread_id"] = out["thread_id"] or message.get("threadId")
    if not isinstance(raw, str):
        return out

    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = base64.urlsafe_b64decode(padded)
        parsed = BytesParser(policy=policy.default).parsebytes(data)
    except Exception:  # noqa: BLE001 - malformed stored raw -> return best-effort
        return out

    out["to"] = parsed.get("To") or ""
    subj = parsed.get("Subject") or ""
    out["subject"] = str(subj)
    out["cc"] = parsed.get("Cc")
    out["in_reply_to"] = parsed.get("In-Reply-To") or parsed.get("References")
    try:
        payload = parsed.get_content()
        out["body"] = payload if isinstance(payload, str) else str(payload)
    except Exception:  # noqa: BLE001
        out["body"] = ""
    out["body"] = (out["body"] or "").strip("\n")
    return out


def _rebuild_reply_arguments(
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    thread_id: str | None,
    cc: str | None,
    source_message_id: str | None,
) -> dict[str, Any]:
    """Build the gated gmail_reply ``arguments`` from reply fields (ONE builder).

    Uses :func:`app.services.gmail_message.build_draft_payload` — the same clean
    builder the agent's tool uses — so an edited/regenerated approval stores an
    identically well-formed base64url message (no quoted-printable "=" soft
    breaks). ``source_message_id``/``thread_id`` are preserved on the arguments
    so the execution + poll guard keep working after an edit/regenerate.
    """
    from app.services import gmail_message

    payload = gmail_message.build_draft_payload(
        to=to,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        thread_id=thread_id,
        cc=cc,
    )
    args: dict[str, Any] = {
        "method": "POST",
        "path": "/gmail/v1/users/me/drafts",
        "body": payload,
    }
    if source_message_id:
        args["source_message_id"] = source_message_id
    if thread_id:
        args["thread_id"] = thread_id
    return args


async def _load_pending_gmail_reply(
    session: AsyncSession, approval_request_id: uuid.UUID
) -> ApprovalRequest:
    """Load a request, requiring it exists (404) and is pending (409)."""
    request = await session.get(ApprovalRequest, approval_request_id)
    if request is None:
        raise APIError(status_code=404, message="The approval request was not found.")
    if request.status is not ApprovalStatus.PENDING:
        raise APIError(
            status_code=409,
            message="This approval request has already been resolved.",
        )
    return request


async def edit_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    subject: str | None = None,
    body: str | None = None,
    to: str | None = None,
) -> ApprovalRequest:
    """Directly edit a PENDING reply's To/Subject/Body, rebuilding the draft (PART 3).

    Decodes the stored reply fields, overlays the provided edits, validates that
    the resulting ``to`` and ``body`` are non-empty, and rebuilds the gated
    ``arguments`` with the shared clean message builder. The request stays
    ``pending``. Missing -> 404, already-resolved -> 409, empty to/body -> 422.
    """
    request = await _load_pending_gmail_reply(session, approval_request_id)

    fields = _decode_reply_fields(request.arguments)
    new_to = (to if to is not None else fields["to"]) or ""
    new_subject = subject if subject is not None else fields["subject"]
    new_body = (body if body is not None else fields["body"]) or ""

    if not new_to.strip() or not new_body.strip():
        raise APIError(
            status_code=422,
            code="invalid_request",
            message="A reply requires a non-empty recipient and body.",
        )

    request.arguments = _rebuild_reply_arguments(
        to=new_to,
        subject=new_subject or "",
        body=new_body,
        in_reply_to=fields["in_reply_to"],
        thread_id=fields["thread_id"],
        cc=fields["cc"],
        source_message_id=fields["source_message_id"],
    )
    await session.commit()
    await session.refresh(request)
    return request


#: Injectable seam so regenerate can produce a new body without a live model in
#: tests. Real implementation calls the Bedrock model via strands (best-effort).
RegenerateBody = Callable[[dict[str, Any]], Awaitable[str] | str]


async def _default_regenerate_body(fields: dict[str, Any]) -> str:
    """Generate a DIFFERENT reply body for the same source email (best-effort).

    Invokes the configured Bedrock model once via the strands SDK with a
    focused "write a different reply" prompt. Raises on any failure so the
    caller can surface an error and leave the approval untouched.
    """
    from strands import Agent  # type: ignore[import-not-found]
    from strands.models import BedrockModel  # type: ignore[import-not-found]

    from app.config import get_settings

    settings = get_settings()
    model = BedrockModel(
        model_id=settings.BEDROCK_MODEL_ID,
        region_name=settings.AWS_REGION,
        streaming=False,
    )
    system_prompt = (
        "You write concise, professional email replies. Given the original "
        "email's recipient and subject and the current draft reply, write a "
        "DIFFERENT reply that still addresses the email appropriately. Return "
        "ONLY the plain-text body of the new reply — no headers, no quotes, no "
        "commentary."
    )
    agent = Agent(model=model, system_prompt=system_prompt, tools=[])
    prompt = (
        f"To: {fields.get('to')}\n"
        f"Subject: {fields.get('subject')}\n\n"
        f"Current draft reply:\n{fields.get('body')}\n\n"
        "Write a different reply body:"
    )
    result = agent(prompt)  # type: ignore[operator]
    text = str(result).strip()
    if not text:
        raise APIError(
            status_code=502,
            code="regenerate_failed",
            message="The model returned an empty reply.",
        )
    return text


async def _rederive_recipient_from_source(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_message_id: str | None,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Re-derive to/subject/in_reply_to from the ORIGINAL message (best-effort).

    Old or mis-addressed approvals may carry the wrong ``to`` (e.g. the mailbox
    owner's own address instead of the sender). When a ``source_message_id`` is
    known we re-fetch the original message's headers and prefer the true sender
    (``Reply-To`` > ``From``), backfilling ``subject``/``in_reply_to`` from the
    original too. On ANY failure (no id, no gmail integration, API error, no
    headers) we return ``fallback`` unchanged so regenerate never blocks.
    """
    if not source_message_id:
        return fallback
    try:
        from app.services import agent_tools
        from app.services.strands_engine import _parse_gmail_headers

        _iid, creds, config = await _resolve_gmail_credentials(
            session, workspace_id=workspace_id
        )
        resp = await agent_tools.call_provider_api(
            provider_name="gmail",
            credentials=creds,
            config=config,
            method="GET",
            path=f"/gmail/v1/users/me/messages/{source_message_id}",
            query={
                "format": "metadata",
                "metadataHeaders": ["From", "Reply-To", "Message-ID", "Subject"],
            },
        )
        headers = _parse_gmail_headers(resp)
    except Exception:  # noqa: BLE001 - best-effort; keep the stored fields
        return fallback
    if not headers:
        return fallback

    derived = dict(fallback)
    sender = (headers.get("reply-to") or "").strip() or (headers.get("from") or "").strip()
    if sender:
        derived["to"] = sender
    msg_id = (headers.get("message-id") or "").strip()
    if msg_id:
        derived["in_reply_to"] = msg_id
    subj = (headers.get("subject") or "").strip()
    if subj and not (derived.get("subject") or "").strip():
        derived["subject"] = subj
    return derived


async def regenerate_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    generate_body: RegenerateBody | None = None,
) -> ApprovalRequest:
    """Regenerate a NEW reply body for a PENDING approval's SAME source email (PART 3).

    Reuses the same To/Subject/threadId/In-Reply-To/source_message_id, asks the
    model (or an injected ``generate_body`` seam) for a fresh body, and rebuilds
    the gated ``arguments`` via the shared clean builder. Stays ``pending``.
    Missing -> 404, already-resolved -> 409. A generation failure raises and the
    approval is left unchanged.
    """
    request = await _load_pending_gmail_reply(session, approval_request_id)
    fields = _decode_reply_fields(request.arguments)

    # Self-correct the recipient: if this reply was built before the
    # server-side recipient fix (or otherwise mis-addressed), re-derive
    # to/subject/in_reply_to from the ORIGINAL message so regenerating also
    # fixes a wrong "To" (best-effort; keeps stored fields on any failure).
    fields = await _rederive_recipient_from_source(
        session,
        workspace_id=request.workspace_id,
        source_message_id=fields.get("source_message_id"),
        fallback=fields,
    )

    gen = generate_body or _default_regenerate_body
    result = gen(fields)
    new_body = await result if _is_awaitable(result) else result
    new_body = (new_body or "").strip()
    if not new_body:
        raise APIError(
            status_code=502,
            code="regenerate_failed",
            message="Could not generate a new reply body.",
        )

    request.arguments = _rebuild_reply_arguments(
        to=fields["to"],
        subject=fields["subject"],
        body=new_body,
        in_reply_to=fields["in_reply_to"],
        thread_id=fields["thread_id"],
        cc=fields["cc"],
        source_message_id=fields["source_message_id"],
    )
    await session.commit()
    await session.refresh(request)
    return request


def _is_awaitable(obj: Any) -> bool:
    import inspect

    return inspect.isawaitable(obj)


# ---------------------------------------------------------------------------
# Real execution: approve + create draft / send (PART 4)
# ---------------------------------------------------------------------------

#: The two explicit execution actions layered over approval (PART 4).
ExecutionAction = Literal["save_to_draft", "send"]


async def _resolve_gmail_credentials(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, dict[str, str], dict[str, str]]:
    """Find the workspace's gmail integration and return (id, fresh_creds, config).

    Decrypts via the Integration_Vault and mints a fresh access token via the
    OAuth refresh service. Raises 404 when no active gmail integration exists.
    Never logs or returns token VALUES to callers beyond the creds map the
    provider-API layer consumes.
    """
    from sqlalchemy import select

    from app.db.models import Integration, IntegrationStatus
    from app.services import integration_vault as _vault
    from app.services import oauth_refresh as _oauth

    integ = await session.scalar(
        select(Integration)
        .where(
            Integration.workspace_id == workspace_id,
            Integration.provider_name == "gmail",
            Integration.status == IntegrationStatus.ACTIVE,
        )
        .order_by(Integration.created_at.asc())
    )
    if integ is None:
        raise APIError(
            status_code=404,
            code="integration_not_found",
            message="No active Gmail integration is connected for this workspace.",
        )
    cred = await _vault.use_credential(session, integration_id=integ.id)
    creds = await _oauth.ensure_access_token(
        "gmail", dict(cred.credentials), dict(cred.config or {})
    )
    return integ.id, creds, dict(cred.config or {})


async def resolve_gmail_credentials(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, dict[str, str], dict[str, str]]:
    """Public wrapper over :func:`_resolve_gmail_credentials` (see it for contract).

    Exposed so other services (e.g. the voice tool layer) can reuse the SAME
    Gmail credential resolution — active integration lookup + vault decrypt +
    fresh-access-token mint — for reads without duplicating the logic. Raises
    404 when no active Gmail integration exists for the workspace.
    """
    return await _resolve_gmail_credentials(session, workspace_id=workspace_id)


def decode_reply_fields(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Public wrapper over :func:`_decode_reply_fields` (see it for contract).

    Decodes a stored ``gmail_reply`` approval's ``arguments`` into readable
    fields ``{to, subject, body, cc, in_reply_to, thread_id, source_message_id}``.
    Exposed so the voice tool layer can read out a pending draft.
    """
    return _decode_reply_fields(arguments)


async def _mark_source_read(
    *,
    credentials: dict[str, str],
    config: dict[str, str],
    source_message_id: str | None,
    call_provider_api: Callable[..., Awaitable[dict]],
) -> None:
    """Best-effort: remove the UNREAD label from the original message (PART 2/4).

    Marking the source email read means the ``is:unread``-scoped poll prompt will
    not surface it again, which is the durable re-propose guard. A failure here
    never fails the already-completed draft/send — the reply was the point.
    """
    if not source_message_id:
        return
    try:
        await call_provider_api(
            provider_name="gmail",
            credentials=credentials,
            config=config,
            method="POST",
            path=f"/gmail/v1/users/me/messages/{source_message_id}/modify",
            body={"removeLabelIds": ["UNREAD"]},
        )
    except Exception:  # noqa: BLE001 - read-marking is best-effort
        return


async def execute_and_approve_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    execution_action: ExecutionAction,
    reviewer_role: MemberRole | None = None,
    call_provider_api: Callable[..., Awaitable[dict]] | None = None,
) -> ApprovalRequest:
    """Approve a PENDING gmail_reply AND actually create the draft or send it (PART 4).

    Execution is resilient: the Gmail API call runs FIRST; only on success is the
    approval marked ``approved`` (via :func:`resolve_request`, which enforces the
    state machine + audit). If the Gmail call fails, the request is left
    ``pending`` and an error is raised. After a successful draft/send the original
    email (``source_message_id``) is marked read (best-effort) so the poller does
    not re-propose it.

    Behaviour:

    - RBAC: enforced by :func:`resolve_request` when ``reviewer_role`` is given
      (Owner/Admin; 403 otherwise).
    - Missing -> 404; already-resolved -> 409 (checked before any side effect).
    - ``save_to_draft`` -> ``POST /gmail/v1/users/me/drafts`` with the stored
      ``body`` (``{"message": {"raw", "threadId?"}}``).
    - ``send`` -> ``POST /gmail/v1/users/me/messages/send`` with
      ``{"raw", "threadId?"}`` derived from the stored draft message.
    - On any non-2xx / transport error from Gmail -> raise 502, leave pending.

    Tokens are never logged. The Gmail call is injectable (``call_provider_api``)
    so tests exercise the flow with a fake and NO real network.
    """
    from app.services import agent_tools

    caller = call_provider_api or agent_tools.call_provider_api

    request = await session.get(ApprovalRequest, approval_request_id)
    if request is None:
        raise APIError(status_code=404, message="The approval request was not found.")
    if request.status is not ApprovalStatus.PENDING:
        raise APIError(
            status_code=409,
            message="This approval request has already been resolved.",
        )

    arguments = request.arguments if isinstance(request.arguments, Mapping) else {}
    body = arguments.get("body") if isinstance(arguments, Mapping) else None
    message = body.get("message") if isinstance(body, Mapping) else None
    raw = message.get("raw") if isinstance(message, Mapping) else None
    thread_id = None
    if isinstance(message, Mapping):
        thread_id = message.get("threadId")
    if not isinstance(raw, str) or not raw:
        raise APIError(
            status_code=422,
            code="invalid_request",
            message="This approval has no draft message to execute.",
        )
    source_message_id = (
        arguments.get("source_message_id") if isinstance(arguments, Mapping) else None
    )

    # Resolve creds up front so a missing integration fails before we approve.
    _integ_id, creds, config = await _resolve_gmail_credentials(
        session, workspace_id=request.workspace_id
    )

    # Perform the Gmail side effect FIRST (approve only on success).
    if execution_action == "save_to_draft":
        gmail_body: dict = {"message": {"raw": raw}}
        if thread_id:
            gmail_body["message"]["threadId"] = thread_id
        result = await caller(
            provider_name="gmail",
            credentials=creds,
            config=config,
            method="POST",
            path="/gmail/v1/users/me/drafts",
            body=gmail_body,
        )
    else:  # send
        send_body: dict = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id
        result = await caller(
            provider_name="gmail",
            credentials=creds,
            config=config,
            method="POST",
            path="/gmail/v1/users/me/messages/send",
            body=send_body,
        )

    if not _gmail_call_ok(result):
        raise APIError(
            status_code=502,
            code="gmail_execution_failed",
            message="The Gmail request failed; the approval is still pending.",
        )

    # Success: mark the original email read so the poller won't re-propose it.
    await _mark_source_read(
        credentials=creds,
        config=config,
        source_message_id=source_message_id if isinstance(source_message_id, str) else None,
        call_provider_api=caller,
    )

    # ALSO record the source message in the durable "already-seen" set (Bedrock
    # cost-control). "Seen" == PROCESSED, independent of read/unread: a message
    # that has received a reply (whether SENT or saved as a DRAFT) must never be
    # handed to another agent run. Marking-read alone is not enough because the
    # cheap poll pre-check now filters on processed_messages (not just Gmail read
    # state), and a draft-only reply may leave the message unread. Best-effort
    # inside the same session/transaction as the approval flip so it commits
    # together; a failure here must not undo the already-successful reply.
    if isinstance(source_message_id, str) and source_message_id:
        try:
            from app.services import gmail_poll as _gmail_poll

            await _gmail_poll.record_seen_one(
                session,
                workspace_id=request.workspace_id,
                integration_id=_integ_id,
                message_id=source_message_id,
            )
        except Exception:  # noqa: BLE001 - recording seen is best-effort
            pass

    # Only now flip the approval to approved (audited, state-machine enforced).
    return await resolve_request(
        session,
        approval_request_id=approval_request_id,
        reviewer_user_id=reviewer_user_id,
        action="approve",
        reviewer_role=reviewer_role,
    )


def _gmail_call_ok(result: Any) -> bool:
    """Whether a call_provider_api result indicates a successful 2xx response."""
    if not isinstance(result, Mapping):
        return False
    if result.get("error"):
        return False
    if "ok" in result:
        return bool(result.get("ok"))
    status = result.get("status_code")
    return isinstance(status, int) and 200 <= status < 300


# ---------------------------------------------------------------------------
# Scheduled sends ("Approve and Schedule") — local cron, catch-up on startup
# ---------------------------------------------------------------------------
#
# Design note (NO DB migration, NO new enum value):
# --------------------------------------------------
# ``ApprovalStatus`` is a Postgres NATIVE enum of exactly pending/approved/
# rejected. A "scheduled" reply is therefore NOT a new status — it is a STILL
# ``pending`` approval whose ``arguments`` carry two extra keys:
#   * ``scheduled_send_at``     — ISO-8601 UTC timestamp string (e.g. ...+00:00)
#   * ``scheduled_by_user_id``  — str(UUID) of the reviewer who scheduled it
# A worker cron (:func:`app.agents.tasks.send_scheduled_replies`) sweeps due
# ones every minute and executes+approves them. This only runs while the worker
# is running: if a scheduled time passes while the app/worker is OFF, the reply
# is sent on the NEXT sweep after startup (CATCH-UP semantics — the startup
# cron runs immediately). It becomes fully "on-time reliable" once the worker
# runs on an always-on server.


def _to_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    Accepts tz-aware datetimes (converted to UTC) or naive datetimes (assumed
    to already be UTC and stamped with ``timezone.utc``). Used so scheduling
    accepts both API-parsed (usually tz-aware) and naive-UTC inputs uniformly.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def schedule_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    scheduled_send_at: datetime,
    reviewer_role: MemberRole | None = None,
) -> ApprovalRequest:
    """Schedule a PENDING reply to be sent automatically at a future time.

    The approval STAYS ``pending``; scheduling metadata is merged into its
    ``arguments`` (``scheduled_send_at`` ISO-8601 UTC string +
    ``scheduled_by_user_id``). A worker cron
    (:func:`app.agents.tasks.send_scheduled_replies`) later sweeps due ones and
    executes+approves them. No DB migration and no new enum value is involved
    (see the module note above). Catch-up-on-startup semantics apply — a time
    that elapsed while the worker was down is honored on the next sweep after
    startup; on-time delivery requires an always-on worker.

    Behaviour:

    - RBAC (optional, safe-by-default): when ``reviewer_role`` is provided it
      must hold :attr:`~app.core.rbac.Capability.RESOLVE_APPROVAL`
      (Owner/Admin) else **403** — mirrors :func:`resolve_request`. The check
      runs before loading state so a forbidden caller learns nothing.
    - **Missing request** -> **404**.
    - **Already resolved** (not pending) -> **409**.
    - **Non-future time** (``<= now`` in UTC) -> **422**.
    - Otherwise merges the schedule into ``arguments`` (as a NEW dict so JSONB
      change tracking fires), keeps ``status`` PENDING, writes an
      ``"approval.scheduled"`` audit row with scrubbed metadata, commits, and
      returns the refreshed request.

    Args:
        session: Active async session; committed on success.
        approval_request_id: The pending request to schedule.
        reviewer_user_id: The scheduling user (recorded + audited).
        scheduled_send_at: When to send; tz-aware or naive-UTC, normalized to
            UTC. Must be in the future.
        reviewer_role: Optional resolved role; enforced against
            ``RESOLVE_APPROVAL`` when given (403 otherwise).

    Returns:
        The updated (still ``pending``) :class:`~app.db.models.ApprovalRequest`.
    """
    # RBAC first (fail-closed): a forbidden caller must not learn any state.
    if reviewer_role is not None and not can(
        reviewer_role, Capability.RESOLVE_APPROVAL
    ):
        raise APIError(
            status_code=403,
            message="Only workspace owners or admins may schedule approvals.",
        )

    request = await session.get(ApprovalRequest, approval_request_id)
    if request is None:
        raise APIError(status_code=404, message="The approval request was not found.")
    if request.status is not ApprovalStatus.PENDING:
        raise APIError(
            status_code=409,
            message="This approval request has already been resolved.",
        )

    normalized = _to_utc(scheduled_send_at)
    now = datetime.now(timezone.utc)
    if normalized <= now:
        raise APIError(
            status_code=422,
            code="invalid_request",
            message="The scheduled send time must be in the future.",
        )

    # Reassign arguments to a NEW dict so SQLAlchemy JSONB change tracking picks
    # it up (in-place mutation of the existing dict would NOT be detected).
    existing = dict(request.arguments) if isinstance(request.arguments, Mapping) else {}
    existing["scheduled_send_at"] = normalized.isoformat()
    existing["scheduled_by_user_id"] = str(reviewer_user_id)
    request.arguments = existing

    audit = SystemAuditLog(
        workspace_id=request.workspace_id,
        user_id=reviewer_user_id,
        action="approval.scheduled",
        log_metadata=scrub(
            {
                "approval_request_id": str(approval_request_id),
                "scheduled_send_at": normalized.isoformat(),
            }
        ),
    )
    session.add(audit)

    await session.commit()
    await session.refresh(request)
    return request


async def list_due_scheduled(
    session: AsyncSession,
    *,
    now: datetime,
) -> list[ApprovalRequest]:
    """Return PENDING approvals whose scheduled send time is due (``<= now``).

    A "due" row is a ``pending`` :class:`~app.db.models.ApprovalRequest` whose
    ``arguments.scheduled_send_at`` is present and parses to a UTC timestamp at
    or before ``now``. Rows without a schedule, future-scheduled rows, and
    already-resolved rows are excluded. Used by the worker cron
    (:func:`app.agents.tasks.send_scheduled_replies`).

    Implementation: filter pending rows that HAVE a ``scheduled_send_at`` key in
    the DB (JSONB), then parse the ISO string in Python and compare to ``now``
    (there are few scheduled approvals, so this is cheap and avoids fragile
    cross-dialect timestamp casting in SQL).
    """
    now_utc = _to_utc(now)
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING)
        .where(ApprovalRequest.arguments["scheduled_send_at"].astext.isnot(None))
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    due: list[ApprovalRequest] = []
    for request in rows:
        args = request.arguments if isinstance(request.arguments, Mapping) else {}
        raw = args.get("scheduled_send_at")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if _to_utc(when) <= now_utc:
            due.append(request)
    return due


async def reject_request(
    session: AsyncSession,
    *,
    approval_request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    reviewer_role: MemberRole | None = None,
) -> ApprovalRequest:
    """Reject a pending request: status -> ``rejected``, reviewer recorded (Req 10.4).

    Thin wrapper over :func:`resolve_request` with ``action="reject"``. See that
    function for the full contract (404/403/409, audit, commit).
    """
    return await resolve_request(
        session,
        approval_request_id=approval_request_id,
        reviewer_user_id=reviewer_user_id,
        action="reject",
        reviewer_role=reviewer_role,
    )


async def reject_all_pending(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    reviewer_role: MemberRole | None = None,
) -> int:
    """Reject EVERY currently-pending approval in a workspace (bulk clear).

    Resolves all rows with ``status == PENDING`` in ``workspace_id`` to
    ``REJECTED`` via the same state machine :func:`resolve_request` uses — this
    is a bulk REJECT that PRESERVES the audit trail, NOT a hard delete
    (ERROR.md Task 3). For each rejected row it sets ``status = REJECTED``,
    records ``reviewed_by_user_id``, and appends an ``approval.rejected``
    :class:`~app.db.models.SystemAuditLog` row with scrubbed metadata (Req 15.3),
    mirroring the single-row reject exactly.

    RBAC (fail-closed): when ``reviewer_role`` is provided it must hold
    :attr:`~app.core.rbac.Capability.RESOLVE_APPROVAL` (Owner/Admin) or a **403**
    is raised before any row is touched (Req 10.5). Tenancy is server-derived:
    only rows in ``workspace_id`` are affected.

    Efficiency: all rows are loaded once and mutated in a single unit of work,
    then committed once — so ~200+ rows clear in one request without a
    per-row round-trip. Idempotent: with no pending rows it commits nothing and
    returns ``0``.

    Returns:
        The count of approvals rejected.
    """
    # RBAC first (fail-closed): a forbidden caller must not touch any row.
    if reviewer_role is not None and not can(
        reviewer_role, Capability.RESOLVE_APPROVAL
    ):
        raise APIError(
            status_code=403,
            message="Only workspace owners or admins may resolve approvals.",
        )

    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.workspace_id == workspace_id)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return 0

    for request in rows:
        request.status = ApprovalStatus.REJECTED
        request.reviewed_by_user_id = reviewer_user_id
        session.add(
            SystemAuditLog(
                workspace_id=request.workspace_id,
                user_id=reviewer_user_id,
                action=_ACTION_AUDIT["reject"],
                log_metadata=scrub(
                    {
                        "approval_request_id": str(request.id),
                        "tool_name": request.tool_name,
                    }
                ),
            )
        )

    await session.commit()
    return len(rows)


__all__ = [
    "DEFAULT_HIGH_IMPACT_MARKERS",
    "is_high_impact",
    "ToolCallDecision",
    "BeforeToolCallHook",
    "make_before_tool_call",
    "ResolutionAction",
    "ResolutionOutcome",
    "resolve_approval_transition",
    "resolve_request",
    "approve_request",
    "reject_request",
    "reject_all_pending",
    "schedule_request",
    "list_due_scheduled",
    "GMAIL_REPLY_TOOL",
    "resolve_gmail_credentials",
    "decode_reply_fields",
    "edit_request",
    "regenerate_request",
    "RegenerateBody",
    "ExecutionAction",
    "execute_and_approve_request",
]
