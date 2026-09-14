"""Tests for the shared Gmail message builder (:mod:`app.services.gmail_message`).

DB-free, network-free unit + property tests proving the builder produces a
CLEAN base64url RFC 2822 message:

- the decoded body has NO trailing "=" quoted-printable soft breaks (the bug
  this fixes) — the CTE is base64 and a round-trip decode returns the exact
  original body;
- the Subject decodes to a human-readable string (a ``Re:`` prefix is added for
  replies, idempotently) even for non-ASCII;
- ``In-Reply-To``/``References`` and the ``threadId`` on the payload are set
  correctly.
"""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser

from hypothesis import given
from hypothesis import strategies as st

from app.services import gmail_message


def _decode(raw: str):
    padded = raw + "=" * (-len(raw) % 4)
    data = base64.urlsafe_b64decode(padded)
    return BytesParser(policy=policy.default).parsebytes(data)


def test_build_reply_raw_has_no_qp_soft_breaks_and_readable_subject() -> None:
    long_body = (
        "Hi there,\n\n"
        + ("This is a very long single line that quoted-printable would soft "
           "wrap with a trailing equals sign once it passes seventy six columns "
           "on one line for sure.\n")
        + "\nBest,\nAtomic"
    )
    payload = gmail_message.build_draft_payload(
        to="a@b.com",
        subject="Café ☕ déjà vu",
        body=long_body,
        in_reply_to="<msgid@x>",
        thread_id="T1",
    )
    message = payload["message"]
    assert message["threadId"] == "T1"

    parsed = _decode(message["raw"])
    # Base64 CTE — never QP — so no "=" soft breaks in the decoded body.
    assert parsed["Content-Transfer-Encoding"].lower() == "base64"
    decoded_body = parsed.get_content()
    for line in decoded_body.splitlines():
        assert not line.endswith("="), f"QP soft break leaked: {line!r}"
    # Round-trip exact.
    assert decoded_body.rstrip("\n") == long_body

    # Subject is human-readable (Re: added) and headers are set.
    assert parsed["Subject"] == "Re: Café ☕ déjà vu"
    assert parsed["To"] == "a@b.com"
    assert parsed["In-Reply-To"] == "<msgid@x>"
    assert parsed["References"] == "<msgid@x>"


def test_no_reprefix_when_already_re_and_no_thread_without_id() -> None:
    payload = gmail_message.build_draft_payload(
        to="a@b.com", subject="Re: Hello", body="ok", in_reply_to="<m>"
    )
    parsed = _decode(payload["message"]["raw"])
    assert parsed["Subject"] == "Re: Hello"
    # No threadId given -> not present on the payload.
    assert "threadId" not in payload["message"]


def test_standalone_has_no_re_prefix_or_in_reply_to() -> None:
    payload = gmail_message.build_draft_payload(
        to="a@b.com", subject="Standalone", body="ok"
    )
    parsed = _decode(payload["message"]["raw"])
    assert parsed["Subject"] == "Standalone"
    assert parsed["In-Reply-To"] is None


def test_cc_is_included() -> None:
    payload = gmail_message.build_draft_payload(
        to="a@b.com", subject="Hi", body="ok", cc="c@d.com"
    )
    parsed = _decode(payload["message"]["raw"])
    assert parsed["Cc"] == "c@d.com"


@given(
    # Exclude control/whitespace-only edge cases that the RFC header/body
    # normalization legitimately rewrites (folding, trailing CR/LF stripping),
    # which are not what this property is about.
    body=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs"), max_codepoint=0x2FFF),
        min_size=1,
        max_size=800,
    ),
    subject=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs"), max_codepoint=0x2FFF),
        max_size=200,
    ),
    to=st.emails(),
)
def test_property_round_trip_body_and_no_soft_breaks(body, subject, to) -> None:
    """For any body/subject, decoding returns the exact body with no QP "=" breaks.

    Validates: Requirements PART 1 (readable email preview).
    """
    payload = gmail_message.build_draft_payload(to=to, subject=subject, body=body)
    parsed = _decode(payload["message"]["raw"])
    # The body is base64-encoded on the wire, so quoted-printable "=" soft line
    # breaks can never be injected into the payload in the first place.
    assert parsed["Content-Transfer-Encoding"].lower() == "base64"
    decoded = parsed.get_content()
    # The definitive no-soft-break proof is an EXACT body round-trip: if QP had
    # soft-wrapped the body, the decoded text would differ from the original.
    # (A literal "=" in the body is preserved exactly precisely because base64,
    # not quoted-printable, is used — so a per-line "endswith('=')" heuristic
    # would be a false positive here and is intentionally NOT used.)
    assert decoded.rstrip("\n") == body.rstrip("\n")
    # Subject decodes to a HUMAN-READABLE string: never a raw RFC 2047
    # encoded-word (that is the "=?utf-8?b?...?=" bug this fixes).
    subj = parsed["Subject"] or ""
    assert "=?" not in subj


# ---------------------------------------------------------------------------
# is_no_reply_address — never auto-reply to a do-not-reply mailbox
# ---------------------------------------------------------------------------


import pytest as _pytest


@_pytest.mark.parametrize(
    "address",
    [
        "no-reply@example.com",
        "noreply@example.com",
        "NoReply@Example.com",
        "no_reply@example.com",
        "no.reply@example.com",
        "donotreply@example.com",
        "do-not-reply@example.com",
        "Google <noreply-accounts@google.com>",
        "\"Support\" <noreply@sendersrv.com>",
        "DoNotReply@corp.example.org",
    ],
)
def test_is_no_reply_address_detects_no_reply(address) -> None:
    assert gmail_message.is_no_reply_address(address) is True


@_pytest.mark.parametrize(
    "address",
    [
        "ignas@payrogen.com",
        "hello@payrogen.com",
        "Jane Doe <jane@company.com>",
        "support@company.com",
        "replies@company.com",     # "replies" is not a no-reply prefix
        "reply@company.com",       # "reply" alone (not no-/do-not-) is a person
        "",
        None,
        "not-an-email",
    ],
)
def test_is_no_reply_address_allows_real_senders(address) -> None:
    assert gmail_message.is_no_reply_address(address) is False


def test_build_reply_raw_rejects_bare_display_name_recipient() -> None:
    # Regression: a scheduled reply drafted with To="Atomic AI" (a bare display
    # name, no <addr>) was rejected by Gmail with 400 "Invalid To header" and
    # then retried forever by the scheduled-send cron. The builder must refuse
    # to construct such a message up front.
    import pytest

    with pytest.raises(ValueError):
        gmail_message.build_reply_raw(to="Atomic AI", subject="Hi", body="Hello")


def test_build_reply_raw_accepts_name_and_address() -> None:
    # A proper "Name <addr>" recipient is preserved verbatim in the To header.
    raw = gmail_message.build_reply_raw(
        to="AtomicAI <atomicai.com@gmail.com>",
        subject="PayRogen Support Request",
        body="Hi there, sure.",
        in_reply_to="<CAE27VN7@mail.gmail.com>",
    )
    parsed = _decode(raw)
    assert parsed["To"] == "AtomicAI <atomicai.com@gmail.com>"
    assert parsed["Subject"].startswith("Re:")


def test_build_reply_raw_accepts_bare_address() -> None:
    # A bare address (no display name) is also valid.
    raw = gmail_message.build_reply_raw(
        to="atomicai.com@gmail.com", subject="Re: x", body="ok"
    )
    parsed = _decode(raw)
    assert parsed["To"] == "atomicai.com@gmail.com"
