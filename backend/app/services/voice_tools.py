"""Voice tool layer — the accessibility capability surface for the voice agent.

This module is the SINGLE, thin capability surface a speech-to-speech voice
agent (Amazon Bedrock Nova Sonic, wired in a later slice) may call. Every tool
here is a *thin wrapper* over an existing service — it duplicates NO business
logic — so the same surface is portable to a hosted agent runtime later.

Design invariants (why this shape):

- **Tenancy is server-derived.** Every call derives the workspace and user from
  the :class:`~app.core.tenancy.RequestContext` + a caller-supplied
  ``workspace_id`` that the dispatcher validates against the caller's
  membership. A client NEVER passes a workspace inside ``tool_input``.
- **High-impact actions go through the Approval_Hub ONLY.** The action tools
  (edit / regenerate / approve+save / approve+send / approve+schedule) delegate
  to :mod:`app.services.approval_service`. The voice layer has NO other path to
  send or draft an email — there is no direct Gmail write here.
- **Never raise for expected failures.** Handlers return a dict carrying a
  ``speak`` string (what the assistant should say) and optionally a structured
  ``action`` (a command for the frontend) and/or ``data``. Expected/handled
  errors return ``{"error": <code>, "speak": <friendly message>}`` — the
  dispatcher also converts any :class:`~app.core.errors.APIError` from the
  wrapped services into that same shape.

The module also exposes :data:`VOICE_TOOL_SPECS` (JSON-schema tool descriptions
suitable to hand to a bidi model's tool configuration) and
:data:`VOICE_TOOL_NAMES` (the set of tool names).
"""

from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.errors import APIError
from app.core.tenancy import RequestContext
from app.db.models import ApprovalRequest, ApprovalStatus
from app.services import agent_tools, approval_service

__all__ = [
    "execute_voice_tool",
    "VOICE_TOOL_SPECS",
    "VOICE_TOOL_NAMES",
]

# ---------------------------------------------------------------------------
# Small helpers (pure)
# ---------------------------------------------------------------------------

#: Allowlist of navigation targets -> frontend route. A client may only ask to
#: navigate to a known place; anything else is refused.
_NAV_TARGETS: dict[str, str] = {
    "integrations": "/dashboard/integrations",
    "approvals": "/dashboard/approvals",
    "rules": "/dashboard/rules",
    "team": "/dashboard/workspace",
    "workspace": "/dashboard/workspace",
    "admin": "/admin",
}

#: Extra spoken synonyms -> allowlist key. Nova Sonic emits natural phrases, so
#: after normalizing away filler words we also map common single-word variants
#: (singular/plural, colloquialisms) onto a canonical target key.
_NAV_SYNONYMS: dict[str, str] = {
    "members": "workspace",
    "approval": "approvals",
    "rule": "rules",
    "integration": "integrations",
    "services": "integrations",
    "service": "integrations",
    "connections": "integrations",
    "connection": "integrations",
}

#: Filler words dropped from a spoken navigation target before matching. These
#: are the words a user naturally appends ("go to the rules menu", "open the
#: approvals page") that carry no routing signal.
_NAV_FILLER_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "go",
        "to",
        "open",
        "my",
        "menu",
        "tab",
        "page",
        "section",
        "screen",
        "view",
        "please",
        "a",
        "an",
    }
)

_NAV_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")


def _normalize_nav_target(target: str) -> str:
    """Lowercase, strip punctuation, and drop filler words from a spoken target.

    Returns the space-joined remainder of meaningful words (pure). Empty string
    when nothing meaningful remains (e.g. the user just said "the page").
    """
    lowered = _NAV_PUNCT_RE.sub(" ", target.lower())
    words = [w for w in lowered.split() if w and w not in _NAV_FILLER_WORDS]
    return " ".join(words)


def _resolve_nav_target(target: str) -> str | None:
    """Map a spoken navigation phrase onto an allowlist route, forgivingly.

    Resolution order (pure):
      1. Exact match of the normalized remainder against an allowlist key.
      2. Word-substring overlap: a key appears as a whole word in the phrase, or
         the phrase is a single word that is a substring of a key.
      3. Known synonyms (singular/plural, colloquialisms).
    Returns the route path, or ``None`` when nothing matches.
    """
    normalized = _normalize_nav_target(target)
    if not normalized:
        return None

    # 1. Exact key match.
    if normalized in _NAV_TARGETS:
        return _NAV_TARGETS[normalized]

    words = normalized.split()
    word_set = set(words)

    # 2. Key appears as a whole word within the spoken phrase.
    for key, path in _NAV_TARGETS.items():
        if key in word_set:
            return path

    # 3. Synonyms — a synonym word present in the phrase resolves to its key.
    for word in words:
        key = _NAV_SYNONYMS.get(word)
        if key is not None:
            return _NAV_TARGETS[key]

    # 4. Single-word phrase that is a substring of a key (or vice-versa), e.g.
    #    "integ" -> integrations. Guarded to len >= 3 to avoid stray matches.
    if len(words) == 1 and len(words[0]) >= 3:
        w = words[0]
        for key, path in _NAV_TARGETS.items():
            if w in key or key in w:
                return path

    return None

#: Allowlist of form fields the voice agent may type into. Any ``credential:``
#: prefixed field is allowed (dynamic per-provider credential inputs).
_TYPE_FIELDS: frozenset[str] = frozenset(
    {"compose_to", "compose_subject", "compose_body", "search", "rule_prompt"}
)

#: Allowlist of forms the voice agent may submit.
_SUBMIT_FORMS: frozenset[str] = frozenset(
    {"connect_integration", "create_rule", "send_message"}
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def _strip_html(text: str) -> str:
    """Minimal HTML → readable text: drop tags, collapse whitespace (pure).

    A deliberately small fallback for message bodies that arrive as HTML. Not a
    full parser — it removes tags, unescapes a few common entities, and
    collapses runs of blank lines so the result reads naturally aloud.
    """
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    for entity, repl in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ):
        no_tags = no_tags.replace(entity, repl)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in no_tags.splitlines()]
    collapsed = "\n".join(ln for ln in lines if ln)
    return collapsed.strip()


def _b64url_decode(data: str) -> bytes:
    """Decode a base64url string, tolerating missing padding."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _gmail_headers_map(message_body: Any) -> dict[str, str]:
    """Lower-cased header map from a Gmail message ``payload.headers`` list."""
    out: dict[str, str] = {}
    if not isinstance(message_body, dict):
        return out
    payload = message_body.get("payload")
    if not isinstance(payload, dict):
        return out
    for h in payload.get("headers") or []:
        if isinstance(h, dict):
            name = h.get("name")
            value = h.get("value")
            if isinstance(name, str) and value is not None:
                out[name.lower()] = str(value)
    return out


def _gmail_plaintext_body(message_body: Any) -> str:
    """Extract a readable plain-text body from a Gmail messages.get payload.

    Walks the MIME parts preferring ``text/plain``; falls back to ``text/html``
    (stripped) then the message ``snippet``. Returns ``""`` when nothing usable
    is present. Never raises — malformed shapes yield the best available text.
    """
    if not isinstance(message_body, dict):
        return ""
    payload = message_body.get("payload")

    def _decode_part_data(part: dict) -> str:
        body = part.get("body")
        if not isinstance(body, dict):
            return ""
        data = body.get("data")
        if not isinstance(data, str) or not data:
            return ""
        try:
            return _b64url_decode(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort decode
            return ""

    plain = ""
    html = ""

    def _walk(part: Any) -> None:
        nonlocal plain, html
        if not isinstance(part, dict):
            return
        mime = (part.get("mimeType") or "").lower()
        if mime == "text/plain" and not plain:
            plain = _decode_part_data(part)
        elif mime == "text/html" and not html:
            html = _decode_part_data(part)
        for child in part.get("parts") or []:
            _walk(child)

    if isinstance(payload, dict):
        _walk(payload)

    if plain.strip():
        return plain.strip()
    if html.strip():
        return _strip_html(html)
    snippet = message_body.get("snippet")
    return str(snippet).strip() if isinstance(snippet, str) else ""


def _friendly_api_error(exc: APIError) -> dict[str, Any]:
    """Convert an APIError into the voice error envelope ``{error, speak}``."""
    return {"error": exc.code or "error", "speak": exc.message}


def _forbidden() -> dict[str, Any]:
    return {"error": "forbidden", "speak": "You don't have access to that workspace."}


# ---------------------------------------------------------------------------
# READ / NAVIGATE handlers
# ---------------------------------------------------------------------------


async def _list_unread_emails(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    raw_max = tool_input.get("max_results", 5)
    try:
        max_results = int(raw_max)
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(max_results, 10))

    try:
        _iid, creds, config = await approval_service.resolve_gmail_credentials(
            session, workspace_id=workspace_id
        )
    except APIError as exc:
        if exc.status_code == 404:
            return {
                "error": "integration_not_found",
                "speak": "You don't have Gmail connected yet.",
            }
        return _friendly_api_error(exc)

    listing = await agent_tools.call_provider_api(
        provider_name="gmail",
        credentials=creds,
        config=config,
        method="GET",
        path="/gmail/v1/users/me/messages",
        query={"q": "is:unread", "maxResults": max_results},
    )
    if not _resp_ok(listing):
        return {"error": "gmail_error", "speak": "I couldn't read your inbox right now."}

    messages = (listing.get("body") or {}).get("messages") or []
    if not messages:
        return {"speak": "You have no unread emails.", "data": []}

    data: list[dict[str, Any]] = []
    for entry in messages[:max_results]:
        msg_id = entry.get("id") if isinstance(entry, dict) else None
        if not msg_id:
            continue
        detail = await agent_tools.call_provider_api(
            provider_name="gmail",
            credentials=creds,
            config=config,
            method="GET",
            path=f"/gmail/v1/users/me/messages/{msg_id}",
            query={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
        )
        body = detail.get("body") if _resp_ok(detail) else None
        headers = _gmail_headers_map(body)
        data.append(
            {
                "id": msg_id,
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "snippet": (body or {}).get("snippet", "") if isinstance(body, dict) else "",
            }
        )

    count = len(data)
    plural = "email" if count == 1 else "emails"
    parts = [f"You have {count} unread {plural}."]
    for i, m in enumerate(data, start=1):
        sender = m["from"] or "an unknown sender"
        subject = m["subject"] or "no subject"
        parts.append(f"{i}. From {sender}, subject {subject}.")
    return {"speak": " ".join(parts), "data": data}


async def _read_email(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    message_id = tool_input.get("message_id")
    if not isinstance(message_id, str) or not message_id.strip():
        return {"error": "invalid_request", "speak": "I need to know which email to read."}

    try:
        _iid, creds, config = await approval_service.resolve_gmail_credentials(
            session, workspace_id=workspace_id
        )
    except APIError as exc:
        if exc.status_code == 404:
            return {
                "error": "integration_not_found",
                "speak": "You don't have Gmail connected yet.",
            }
        return _friendly_api_error(exc)

    detail = await agent_tools.call_provider_api(
        provider_name="gmail",
        credentials=creds,
        config=config,
        method="GET",
        path=f"/gmail/v1/users/me/messages/{message_id}",
        query={"format": "full"},
    )
    if not _resp_ok(detail):
        return {"error": "gmail_error", "speak": "I couldn't open that email."}

    body = detail.get("body")
    headers = _gmail_headers_map(body)
    sender = headers.get("from", "an unknown sender")
    subject = headers.get("subject", "no subject")
    text = _gmail_plaintext_body(body)
    speak = f"{subject}. From {sender}. {text}".strip()
    return {
        "speak": speak,
        "data": {"id": message_id, "from": sender, "subject": subject, "body": text},
    }


async def _list_pending_approvals(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.workspace_id == workspace_id)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING)
        .order_by(ApprovalRequest.created_at.desc())
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return {"speak": "You have no replies awaiting approval.", "data": []}

    data: list[dict[str, Any]] = []
    for req in rows:
        fields = approval_service.decode_reply_fields(req.arguments)
        data.append(
            {
                "id": str(req.id),
                "to": fields.get("to", ""),
                "subject": fields.get("subject", ""),
            }
        )

    count = len(data)
    plural = "reply" if count == 1 else "replies"
    parts = [f"You have {count} {plural} awaiting approval."]
    for i, d in enumerate(data, start=1):
        to = d["to"] or "an unknown recipient"
        subject = d["subject"] or "no subject"
        parts.append(f"{i}. To {to} re {subject}.")
    return {"speak": " ".join(parts), "data": data}


async def _read_approval(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    request = await _load_pending_in_workspace(tool_input, session=session, workspace_id=workspace_id)
    if isinstance(request, dict):
        return request  # error envelope

    fields = approval_service.decode_reply_fields(request.arguments)
    to = fields.get("to", "") or "an unknown recipient"
    subject = fields.get("subject", "") or "no subject"
    body = fields.get("body", "") or ""
    speak = f"Reply to {to}, subject {subject}. {body}".strip()
    return {
        "speak": speak,
        "data": {"id": str(request.id), "to": fields.get("to", ""),
                 "subject": fields.get("subject", ""), "body": body},
    }


async def _navigate(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    target = tool_input.get("target")
    if not isinstance(target, str) or not target.strip():
        return {"error": "unknown_target", "speak": "I can't navigate there."}
    path = _resolve_nav_target(target)
    if path is None:
        return {"error": "unknown_target", "speak": "I can't navigate there."}
    return {
        "speak": f"Opening {target.strip()}.",
        "action": {"type": "navigate", "path": path},
    }


# ---------------------------------------------------------------------------
# TYPING / FORM-FILL handlers (structured commands for the frontend)
# ---------------------------------------------------------------------------


async def _type_text(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    field = tool_input.get("field")
    text = tool_input.get("text", "")
    if not isinstance(field, str) or not (
        field in _TYPE_FIELDS or field.startswith("credential:")
    ):
        return {"error": "unknown_field", "speak": "I can't type into that field."}
    return {
        "speak": "Typed.",
        "action": {"type": "type", "field": field, "text": str(text)},
    }


async def _submit_form(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    form = tool_input.get("form")
    if not isinstance(form, str) or form not in _SUBMIT_FORMS:
        return {"error": "unknown_form", "speak": "I can't submit that form."}
    return {
        "speak": f"Submitting {form}.",
        "action": {"type": "submit", "form": form},
    }


# ---------------------------------------------------------------------------
# ACTION handlers (HIGH-IMPACT — route ONLY through approval_service)
# ---------------------------------------------------------------------------


def _approval_id(tool_input: dict) -> uuid.UUID | dict[str, Any]:
    """Parse ``approval_id`` from input; return an error envelope on failure."""
    raw = tool_input.get("approval_id")
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "invalid_request", "speak": "I need to know which reply."}
    try:
        return uuid.UUID(raw)
    except ValueError:
        return {"error": "invalid_request", "speak": "That reply reference isn't valid."}


async def _load_pending_in_workspace(
    tool_input: dict, *, session, workspace_id: uuid.UUID
) -> ApprovalRequest | dict[str, Any]:
    """Load a PENDING approval that belongs to ``workspace_id`` (tenant-scoped).

    Returns an error envelope dict when the id is bad, missing, in another
    workspace, or already resolved — so a voice caller can never read or act on
    another tenant's approval.
    """
    parsed = _approval_id(tool_input)
    if isinstance(parsed, dict):
        return parsed
    request = await session.get(ApprovalRequest, parsed)
    if request is None or request.workspace_id != workspace_id:
        return {"error": "not_found", "speak": "I couldn't find that reply."}
    if request.status is not ApprovalStatus.PENDING:
        return {"error": "conflict", "speak": "That reply has already been handled."}
    return request


async def _edit_reply(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    request = await _load_pending_in_workspace(tool_input, session=session, workspace_id=workspace_id)
    if isinstance(request, dict):
        return request
    try:
        await approval_service.edit_request(
            session,
            approval_request_id=request.id,
            subject=tool_input.get("subject"),
            body=tool_input.get("body"),
            to=tool_input.get("to"),
        )
    except APIError as exc:
        return _friendly_api_error(exc)
    return {"speak": "I've updated the reply. It's still awaiting your approval."}


async def _regenerate_reply(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    request = await _load_pending_in_workspace(tool_input, session=session, workspace_id=workspace_id)
    if isinstance(request, dict):
        return request
    try:
        await approval_service.regenerate_request(
            session, approval_request_id=request.id
        )
    except APIError as exc:
        return _friendly_api_error(exc)
    return {"speak": "I've written a new version of the reply for your review."}


async def _approve_and_save_draft(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    request = await _load_pending_in_workspace(tool_input, session=session, workspace_id=workspace_id)
    if isinstance(request, dict):
        return request
    try:
        await approval_service.execute_and_approve_request(
            session,
            approval_request_id=request.id,
            reviewer_user_id=ctx.user_id,
            execution_action="save_to_draft",
            reviewer_role=ctx.member_role(workspace_id),
        )
    except APIError as exc:
        return _friendly_api_error(exc)
    return {"speak": "I've saved the reply to your drafts."}


async def _approve_and_send(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    request = await _load_pending_in_workspace(tool_input, session=session, workspace_id=workspace_id)
    if isinstance(request, dict):
        return request
    try:
        await approval_service.execute_and_approve_request(
            session,
            approval_request_id=request.id,
            reviewer_user_id=ctx.user_id,
            execution_action="send",
            reviewer_role=ctx.member_role(workspace_id),
        )
    except APIError as exc:
        return _friendly_api_error(exc)
    return {"speak": "I've sent the reply."}


async def _approve_and_schedule(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    request = await _load_pending_in_workspace(tool_input, session=session, workspace_id=workspace_id)
    if isinstance(request, dict):
        return request

    when_iso = tool_input.get("when_iso")
    if not isinstance(when_iso, str) or not when_iso.strip():
        return {"error": "invalid_request", "speak": "I need to know when to send it."}
    try:
        when = datetime.fromisoformat(when_iso)
    except ValueError:
        return {"error": "invalid_request", "speak": "I couldn't understand that time."}
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    try:
        await approval_service.schedule_request(
            session,
            approval_request_id=request.id,
            reviewer_user_id=ctx.user_id,
            scheduled_send_at=when,
            reviewer_role=ctx.member_role(workspace_id),
        )
    except APIError as exc:
        return _friendly_api_error(exc)
    return {"speak": "I've scheduled the reply to send later."}


async def _clear_pending(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Bulk-clear (REJECT) every pending reply in the workspace (ERROR.md Task 3).

    Rejects ALL currently-pending approvals in ``workspace_id`` via
    :func:`approval_service.reject_all_pending` — a bulk REJECT that PRESERVES
    the audit trail, never a hard delete. This is a resolver action, so the
    caller must hold ``RESOLVE_APPROVAL`` (Owner/Admin); a Viewer/Member is told
    they don't have permission and NO row is touched (the membership floor is
    already enforced by :func:`execute_voice_tool`). Tenancy is server-derived
    from ``workspace_id`` / ``ctx`` — the model supplies nothing.

    Speaks a confirmation naming the count cleared; carries an ``action`` so live
    reviewers refetch.
    """
    from app.core.rbac import Capability, can

    role = ctx.member_role(workspace_id)
    if role is None or not can(role, Capability.RESOLVE_APPROVAL):
        return {
            "error": "forbidden",
            "speak": "Only workspace owners or admins can clear pending replies.",
        }

    try:
        cleared = await approval_service.reject_all_pending(
            session,
            workspace_id=workspace_id,
            reviewer_user_id=ctx.user_id,
            reviewer_role=role,
        )
    except APIError as exc:
        return _friendly_api_error(exc)

    if cleared == 0:
        return {
            "speak": "You have no pending replies to clear.",
            "data": {"cleared": 0},
            "action": {"type": "approvals_cleared", "cleared": 0},
        }
    plural = "reply" if cleared == 1 else "replies"
    return {
        "speak": f"I've cleared {cleared} pending {plural}.",
        "data": {"cleared": cleared},
        "action": {"type": "approvals_cleared", "cleared": cleared},
    }


async def _reject_reply(
    tool_input: dict, *, ctx: RequestContext, session, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Reject (decline) a single PENDING reply — the voice 'Reject' button.

    Loads the pending approval tenant-scoped, checks the caller holds
    ``RESOLVE_APPROVAL`` (Owner/Admin) fail-closed, then rejects it via
    :func:`approval_service.reject_request` (status -> rejected, audit preserved
    — never a hard delete). Speaks a confirmation and carries an ``action`` so a
    live reviewer's queue refreshes. Tenancy is server-derived.
    """
    from app.core.rbac import Capability, can

    request = await _load_pending_in_workspace(
        tool_input, session=session, workspace_id=workspace_id
    )
    if isinstance(request, dict):
        return request

    role = ctx.member_role(workspace_id)
    if role is None or not can(role, Capability.RESOLVE_APPROVAL):
        return {
            "error": "forbidden",
            "speak": "Only workspace owners or admins can reject replies.",
        }

    try:
        await approval_service.reject_request(
            session,
            approval_request_id=request.id,
            reviewer_user_id=ctx.user_id,
            reviewer_role=role,
        )
    except APIError as exc:
        return _friendly_api_error(exc)
    return {
        "speak": "I've rejected that reply.",
        "action": {"type": "approvals_cleared", "cleared": 1},
    }


def _resp_ok(result: Any) -> bool:
    """Whether a call_provider_api result is a successful 2xx response."""
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    if "ok" in result:
        return bool(result.get("ok"))
    status = result.get("status_code")
    return isinstance(status, int) and 200 <= status < 300


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLERS = {
    "list_unread_emails": _list_unread_emails,
    "read_email": _read_email,
    "list_pending_approvals": _list_pending_approvals,
    "read_approval": _read_approval,
    "navigate": _navigate,
    "type_text": _type_text,
    "submit_form": _submit_form,
    "edit_reply": _edit_reply,
    "regenerate_reply": _regenerate_reply,
    "approve_and_save_draft": _approve_and_save_draft,
    "approve_and_send": _approve_and_send,
    "approve_and_schedule": _approve_and_schedule,
    "reject_reply": _reject_reply,
    "clear_pending": _clear_pending,
}


async def execute_voice_tool(
    name: str,
    tool_input: dict,
    *,
    ctx: RequestContext,
    session,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    """Validate membership, dispatch a voice tool call, and return a speak envelope.

    Tenancy is server-derived: the caller's role in ``workspace_id`` is resolved
    from ``ctx`` (a non-member -> ``forbidden``). ``tool_input`` never carries a
    workspace. Unknown tool names return an ``unknown_tool`` envelope. Any
    :class:`~app.core.errors.APIError` escaping a handler is converted to the
    ``{error, speak}`` envelope rather than raised.

    Returns a dict with a ``speak`` string and optionally ``action``/``data``,
    or ``{"error", "speak"}`` on a handled failure.
    """
    if ctx.member_role(workspace_id) is None:
        return _forbidden()

    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": "unknown_tool", "speak": "I don't know how to do that."}

    tool_input = tool_input if isinstance(tool_input, dict) else {}
    try:
        return await handler(
            tool_input, ctx=ctx, session=session, workspace_id=workspace_id
        )
    except APIError as exc:
        return _friendly_api_error(exc)


# ---------------------------------------------------------------------------
# Tool specifications for the bidi model
# ---------------------------------------------------------------------------

VOICE_TOOL_SPECS: list[dict] = [
    {
        "name": "list_unread_emails",
        "description": "Read out the user's unread emails (sender and subject).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "How many unread emails to summarize (default 5).",
                }
            },
        },
    },
    {
        "name": "read_email",
        "description": "Read one email aloud: subject, sender, and body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Gmail message id to read.",
                }
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "list_pending_approvals",
        "description": "List replies that are waiting for the user's approval.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_approval",
        "description": "Read a pending reply draft aloud: recipient, subject, body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {
                    "type": "string",
                    "description": "The approval request id to read.",
                }
            },
            "required": ["approval_id"],
        },
    },
    {
        "name": "navigate",
        "description": (
            "Open a page in the app. Accepts natural spoken phrases such as "
            "'go to the rules menu', 'open the approvals page', 'integrations', "
            "or 'team members' — filler words are ignored. Known places: "
            "integrations, approvals, rules, workspace/team, admin."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Where to navigate, as a natural phrase (e.g. 'rules "
                        "menu', 'the approvals page', 'integrations', 'team "
                        "members'). One of the known places: integrations, "
                        "approvals, rules, workspace, team, admin."
                    ),
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into an allowed form field on the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": (
                        "The field to type into: one of compose_to, "
                        "compose_subject, compose_body, search, rule_prompt, or "
                        "a credential:<name> field."
                    ),
                },
                "text": {"type": "string", "description": "The text to enter."},
            },
            "required": ["field", "text"],
        },
    },
    {
        "name": "submit_form",
        "description": "Submit an allowed form: connect_integration, create_rule, or send_message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form": {
                    "type": "string",
                    "enum": sorted(_SUBMIT_FORMS),
                    "description": "Which form to submit.",
                }
            },
            "required": ["form"],
        },
    },
    {
        "name": "edit_reply",
        "description": "Change the recipient, subject, or body of a pending reply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["approval_id"],
        },
    },
    {
        "name": "regenerate_reply",
        "description": "Write a fresh version of a pending reply's body.",
        "inputSchema": {
            "type": "object",
            "properties": {"approval_id": {"type": "string"}},
            "required": ["approval_id"],
        },
    },
    {
        "name": "approve_and_save_draft",
        "description": "Approve a pending reply and save it to drafts.",
        "inputSchema": {
            "type": "object",
            "properties": {"approval_id": {"type": "string"}},
            "required": ["approval_id"],
        },
    },
    {
        "name": "approve_and_send",
        "description": "Approve a pending reply and send it now.",
        "inputSchema": {
            "type": "object",
            "properties": {"approval_id": {"type": "string"}},
            "required": ["approval_id"],
        },
    },
    {
        "name": "approve_and_schedule",
        "description": "Approve a pending reply and schedule it to send at a future time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "when_iso": {
                    "type": "string",
                    "description": "ISO-8601 time to send (future).",
                },
            },
            "required": ["approval_id", "when_iso"],
        },
    },
    {
        "name": "reject_reply",
        "description": (
            "Reject (decline) a single pending reply, keeping an audit trail "
            "(not a permanent delete). Owner/Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"approval_id": {"type": "string"}},
            "required": ["approval_id"],
        },
    },
    {
        "name": "clear_pending",
        "description": (
            "Clear ALL pending replies in the workspace by rejecting them "
            "(keeps an audit trail; not a permanent delete). Owner/Admin only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

#: The set of tool names the voice agent may call.
VOICE_TOOL_NAMES: set[str] = {spec["name"] for spec in VOICE_TOOL_SPECS}
