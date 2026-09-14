"""Gmail message builder — one clean RFC 2822 → base64url builder for replies.

This module is the SINGLE source of truth for turning simple reply fields
(``to``, ``subject``, ``body``, ``in_reply_to``, ``thread_id``, ``cc``) into the
base64url-encoded RFC 2822 message Gmail's ``drafts``/``messages.send`` endpoints
expect. It is shared by:

- the agent's ``gmail_reply`` tool (:mod:`app.services.strands_engine`),
- the approval EDIT path (rebuild the draft from user-edited fields), and
- the approval REGENERATE path (rebuild with a fresh body),

so all three produce identically clean, well-formed messages.

Why a dedicated builder — the "=" / "=?utf-8?b?...?=" problem
------------------------------------------------------------
Python's :class:`email.message.EmailMessage` defaults to serializing a text body
as **quoted-printable**: long lines are soft-wrapped with a trailing ``=`` and
non-ASCII bytes become ``=XX`` escapes, so a decoder that doesn't undo QP shows
stray ``=`` characters. Likewise a non-ASCII ``Subject`` is emitted as an
RFC 2047 *encoded-word* (``=?utf-8?b?...?=``), which is unreadable raw.

To guarantee the DECODED body has **no** trailing ``=`` soft breaks and the
Subject is human-readable, we serialize the text body with a **base64**
Content-Transfer-Encoding (via :class:`email.mime.text.MIMEText` with charset
``utf-8``). Base64 CTE never injects ``=`` soft line breaks into the payload
(its only ``=`` are proper base64 padding, which base64-decoding consumes), so a
round-trip decode returns the exact original body. The Subject is still an
RFC 2047 encoded-word when it contains non-ASCII (that is required by the wire
format), but the frontend now decodes encoded-words, and for ASCII subjects the
header is plain and readable. See ``tests/test_gmail_message.py`` for the
round-trip proof.
"""

from __future__ import annotations

import base64
import re
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr  # noqa: F401  (formataddr kept for future From support)

__all__ = ["build_reply_raw", "build_draft_payload", "is_no_reply_address"]


#: Local-part patterns that identify a "do not reply" mailbox. Matched against
#: the address local-part (before ``@``), case-insensitively, after stripping
#: non-alphanumerics — so "no-reply", "noreply", "no_reply", "do-not-reply",
#: "donotreply", "no.reply", "noreply-accounts" all match. Kept broad but
#: anchored to the START of the local-part so a real person like
#: "noreplyman@x.com" is... still matched (intentionally conservative: we would
#: rather NOT auto-reply to a borderline address than spam a no-reply box).
_NO_REPLY_LOCALPART_RE = re.compile(r"^(?:donotreply|noreply)")


def is_no_reply_address(address: str | None) -> bool:
    """Return whether ``address`` is a no-reply / do-not-reply mailbox (pure).

    Accepts a bare address ("noreply@example.com") or a display-name form
    ("Google <noreply-accounts@google.com>"); the address is extracted with
    :func:`email.utils.parseaddr`. The local-part (before ``@``) is lowercased
    and stripped of non-alphanumerics, then matched against ``no reply`` /
    ``do not reply`` prefixes. Returns ``False`` for empty/malformed input so a
    missing recipient never accidentally blocks a legitimate reply here (the
    tool separately requires a non-empty ``to``).

    Auto-replying to a no-reply sender is pointless (it bounces / is ignored) and
    wastes model credits, so the reply pipeline refuses these senders.
    """
    if not address or not isinstance(address, str):
        return False
    _name, email_addr = parseaddr(address)
    email_addr = (email_addr or address).strip().lower()
    local = email_addr.split("@", 1)[0] if "@" in email_addr else email_addr
    # Collapse separators so "no-reply" / "no_reply" / "no.reply" all normalize.
    normalized = re.sub(r"[^a-z0-9]", "", local)
    return bool(_NO_REPLY_LOCALPART_RE.match(normalized))



def _apply_re_prefix(subject: str, in_reply_to: str | None) -> str:
    """Add a single ``Re:`` prefix for replies, never doubling an existing one."""
    subj = subject or ""
    if in_reply_to and not subj.strip().lower().startswith("re:"):
        subj = f"Re: {subj}" if subj else "Re:"
    return subj


def build_reply_raw(
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    thread_id: str | None = None,  # noqa: ARG001 - threadId lives in the payload, not the raw
    cc: str | None = None,
) -> str:
    """Build a clean base64url-encoded RFC 2822 message from simple fields.

    The body is encoded with a **base64** Content-Transfer-Encoding so a decoded
    copy is byte-for-byte the original — no quoted-printable ``=`` soft breaks.
    A ``Re:`` prefix is added for replies (idempotently). ``From`` is never set:
    Gmail fills the authenticated sender. ``thread_id`` is intentionally NOT part
    of the raw message — it belongs on the Gmail request payload (see
    :func:`build_draft_payload`).

    Args:
        to: Recipient address(es).
        subject: Original subject; a ``Re:`` prefix is added for replies.
        body: The reply prose (plain text, UTF-8).
        in_reply_to: Optional original ``Message-ID`` to thread the reply.
        thread_id: Accepted for signature symmetry; not written into the raw.
        cc: Optional Cc address(es).

    Returns:
        The base64url (RFC 4648, ``-``/``_`` alphabet) string of the serialized
        message, ready for Gmail's ``message.raw``.
    """
    # Guard: the recipient MUST contain a real email address. A bare display
    # name ("Atomic AI" with no <addr>) makes Gmail reject the send with 400
    # "Invalid To header". parseaddr returns ("", "") or a name-only tuple when
    # there is no address; require an "@" in the extracted address.
    _to_addr = parseaddr(to or "")[1]
    if "@" not in _to_addr:
        raise ValueError(
            "gmail reply recipient has no valid email address (got a bare "
            "display name); refusing to build a message Gmail will reject."
        )

    # base64 CTE (MIMEText default for utf-8) — no QP "=" soft breaks in the body.
    msg = MIMEText(body or "", "plain", "utf-8")
    # MIMEText also sets a MIME-Version + Content-Type; we only add addressing.
    msg["To"] = to
    subj = _apply_re_prefix(subject, in_reply_to)
    msg["Subject"] = subj
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")


def build_draft_payload(
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    thread_id: str | None = None,
    cc: str | None = None,
) -> dict:
    """Build the Gmail drafts request payload ``{"message": {"raw", "threadId?"}}``.

    Wraps :func:`build_reply_raw` and attaches ``threadId`` on the message object
    (where Gmail expects it) when a ``thread_id`` is given. This is the exact
    shape stored in the gated approval ``arguments.body`` and later sent to the
    ``drafts`` endpoint.
    """
    raw = build_reply_raw(
        to=to,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        thread_id=thread_id,
        cc=cc,
    )
    message_obj: dict = {"raw": raw}
    if thread_id:
        message_obj["threadId"] = thread_id
    return {"message": message_obj}
