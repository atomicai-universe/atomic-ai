"""Tests for the dedicated, structured ``gmail_reply`` tool (Strands_Engine).

These tests build the tools exactly the way the run loop does — via
:func:`app.services.strands_engine._make_provider_tools` — using a minimal
:class:`~app.services.strands_engine.RunLoopContext` whose
``resolved_credentials`` include a ``gmail`` integration and a FAKE
``before_tool_call`` hook that records the arguments and returns a "do not
proceed" decision. No DB, no network, no real Gmail.

We assert that:

- ``gmail_reply`` builds a correct base64url RFC 2822 draft: the hook receives
  ``{"method": "POST", "path": ".../drafts", "body": {"message": {"raw", "threadId"}}}``
  whose ``raw`` decodes to a message with ``To``, ``Subject: Re: ...``,
  ``In-Reply-To``, and the plain-text body.
- Empty ``to`` or ``body`` returns the ``invalid_reply`` error WITHOUT gating.
- When gated, the tool returns the ``{"gated": True, ...}`` shape.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from email.parser import BytesParser
from email import policy

import pytest

pytest.importorskip("strands", reason="Strands SDK not installed")

from app.services import strands_engine
from app.services.strands_engine import RunLoopContext


@dataclass
class _Decision:
    """Minimal ToolCallDecision-shaped object the hook returns."""

    proceed: bool
    approval_request_id: uuid.UUID | None = None


class _RecordingHook:
    """Fake ``before_tool_call`` that records args and refuses to proceed."""

    def __init__(self, proceed: bool = False) -> None:
        self.proceed = proceed
        self.calls: list[tuple[str, dict]] = []
        self.request_id = uuid.uuid4()

    def __call__(self, tool_name: str, arguments):
        self.calls.append((tool_name, arguments))
        return _Decision(proceed=self.proceed, approval_request_id=self.request_id)


def _make_ctx(hook) -> RunLoopContext:
    return RunLoopContext(
        agent_session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        triggered_by_user_id=uuid.uuid4(),
        thread_id="t",
        prompt="reply to emails",
        rules=[],
        tool_servers=[],
        before_tool_call=hook,
        resolved_credentials={
            str(uuid.uuid4()): {
                "provider": "gmail",
                "credentials": {"access_token": "tok"},
                "config": {},
            }
        },
    )


def _get_gmail_reply_tool(tools):
    for t in tools:
        name = getattr(t, "tool_name", None) or getattr(t, "__name__", None)
        spec = getattr(t, "tool_spec", None)
        if spec is not None and spec.get("name") == "gmail_reply":
            return t
        if name == "gmail_reply" or name == "_gmail_reply_tool":
            return t
    return None


def _call(tool, **kwargs):
    """Invoke a Strands @tool wrapper directly, tolerating wrapper shapes."""
    for attr in ("original_function", "_original_function", "func", "__wrapped__"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn(**kwargs)
    return tool(**kwargs)


def _decode_raw(raw: str):
    padded = raw + "=" * (-len(raw) % 4)
    data = base64.urlsafe_b64decode(padded)
    return BytesParser(policy=policy.default).parsebytes(data)


def test_gmail_reply_builds_well_formed_draft_and_gates() -> None:
    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)
    assert tool is not None, "gmail_reply tool was not registered"

    result = _call(
        tool,
        to="a@b.com",
        subject="Hello",
        body="Thanks!",
        in_reply_to="<msgid>",
        thread_id="T1",
    )

    # The hook was invoked once with the gated draft arguments.
    assert len(hook.calls) == 1
    tool_name, args = hook.calls[0]
    assert tool_name == "gmail_reply"
    assert args["method"] == "POST"
    assert args["path"] == "/gmail/v1/users/me/drafts"
    message = args["body"]["message"]
    assert message["threadId"] == "T1"

    msg = _decode_raw(message["raw"])
    assert msg["To"] == "a@b.com"
    assert msg["Subject"] == "Re: Hello"
    assert msg["In-Reply-To"] == "<msgid>"
    assert msg["References"] == "<msgid>"
    assert msg.get_content().strip() == "Thanks!"

    # The gated result shape is returned.
    assert result["gated"] is True
    assert result["approval_request_id"] == str(hook.request_id)
    assert "pending" in result["message"].lower()


def test_gmail_reply_no_reprefix_when_subject_already_has_re() -> None:
    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)

    _call(tool, to="a@b.com", subject="Re: Hello", body="Body", in_reply_to="<m>")
    _tool_name, args = hook.calls[0]
    msg = _decode_raw(args["body"]["message"]["raw"])
    assert msg["Subject"] == "Re: Hello"


def test_gmail_reply_without_reply_context_has_no_re_prefix_or_threadid() -> None:
    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)

    _call(tool, to="a@b.com", subject="Standalone", body="Body")
    _tool_name, args = hook.calls[0]
    message = args["body"]["message"]
    assert "threadId" not in message
    msg = _decode_raw(message["raw"])
    assert msg["Subject"] == "Standalone"
    assert msg["In-Reply-To"] is None


@pytest.mark.parametrize(
    ("to", "body"),
    [("", "hi"), ("   ", "hi"), ("a@b.com", ""), ("a@b.com", "   ")],
)
def test_gmail_reply_rejects_empty_to_or_body_without_gating(to, body) -> None:
    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)

    result = _call(tool, to=to, subject="S", body=body)
    assert result["error"] == "invalid_reply"
    assert hook.calls == []  # never gated, never created


def test_gmail_reply_includes_cc_when_provided() -> None:
    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)

    _call(tool, to="a@b.com", subject="Hi", body="Body", cc="c@d.com")
    _tool_name, args = hook.calls[0]
    msg = _decode_raw(args["body"]["message"]["raw"])
    assert msg["Cc"] == "c@d.com"


def test_gmail_reply_derives_recipient_from_original_sender(monkeypatch) -> None:
    """The reply recipient is derived SERVER-SIDE from the original message.

    The model wrongly supplies its own address as ``to`` (the classic From/To
    confusion). With ``source_message_id`` set, the tool GETs the original
    message metadata and OVERRIDES ``to`` with the true sender (From), and
    backfills In-Reply-To (Message-ID) and Subject when the model omitted them.
    No network: we patch ``call_provider_api`` — it handles the GET lookup; the
    POST is intercepted by the gating hook (returns not-proceed).
    """

    async def _fake_call_provider_api(*, provider_name, credentials, config,
                                      method, path, query=None, body=None):
        # Only the recipient-lookup GET should reach the provider API; the
        # POST draft is gated by the hook before it is ever executed.
        assert method == "GET"
        assert path == "/gmail/v1/users/me/messages/m1"
        assert query["format"] == "metadata"
        return {
            "status_code": 200,
            "ok": True,
            "body": {
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Camilla <newsletter@sender.net>"},
                        {"name": "Message-ID", "value": "<abc@x>"},
                        {"name": "Subject", "value": "Marketing"},
                    ]
                }
            },
        }

    monkeypatch.setattr(
        "app.services.agent_tools.call_provider_api",
        _fake_call_provider_api,
    )

    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)

    _call(
        tool,
        to="hello@payrogen.com",  # WRONG (the mailbox owner's own address)
        subject="",
        body="Thanks for reaching out.",
        in_reply_to=None,
        source_message_id="m1",
    )

    assert len(hook.calls) == 1
    _tool_name, args = hook.calls[0]
    assert args["source_message_id"] == "m1"
    msg = _decode_raw(args["body"]["message"]["raw"])
    # Recipient overridden to the ORIGINAL SENDER, not the model's wrong value.
    assert msg["To"] == "Camilla <newsletter@sender.net>"
    assert msg["To"] != "hello@payrogen.com"
    # Backfilled from the original message headers.
    assert msg["In-Reply-To"] == "<abc@x>"
    assert msg["Subject"].startswith("Re:")


def test_gmail_reply_falls_back_to_model_to_when_lookup_fails(monkeypatch) -> None:
    """If the recipient lookup GET fails, fall back to the model-provided ``to``."""

    async def _fake_call_provider_api(*, provider_name, credentials, config,
                                      method, path, query=None, body=None):
        assert method == "GET"
        return {"status_code": 404, "ok": False, "body": {}}

    monkeypatch.setattr(
        "app.services.agent_tools.call_provider_api",
        _fake_call_provider_api,
    )

    hook = _RecordingHook(proceed=False)
    tools = strands_engine._make_provider_tools(_make_ctx(hook))
    tool = _get_gmail_reply_tool(tools)

    _call(tool, to="fallback@x.com", subject="Hi", body="Body",
          source_message_id="missing")

    _tool_name, args = hook.calls[0]
    msg = _decode_raw(args["body"]["message"]["raw"])
    assert msg["To"] == "fallback@x.com"
