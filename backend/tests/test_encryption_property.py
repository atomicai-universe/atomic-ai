"""Property-based tests for the Encryption_Service credential round-trip.

Implements **Property 18: Credential encryption round-trip** from the design.

**Validates: Requirements 6.1, 6.2, 6.3**

- Req 6.1: OAuth tokens are encrypted before persistence.
- Req 6.2: the vault persists tokens only in encrypted form (ciphertext differs
  from plaintext and is opaque ``bytes``).
- Req 6.3: an authorized execution can decrypt the credential in memory,
  recovering exactly the original secret (round-trip fidelity).

The properties exercise :class:`app.core.encryption.EncryptionService`, which
wraps authenticated symmetric encryption (Fernet). Each test builds its own
service from a freshly generated key so the tests never depend on process
configuration or the ``ENCRYPTION_KEY`` environment variable.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.encryption import DecryptionError, EncryptionService


def _service() -> EncryptionService:
    """Build an EncryptionService from a fresh, valid Fernet key."""
    return EncryptionService(Fernet.generate_key())


# A shared settings profile: the design mandates a minimum of 100 examples for
# property tests, and these generators are cheap so we can afford more.
_PBT = settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# --- Property 18: text round-trip -------------------------------------------

@_PBT
@given(plaintext=st.text())
def test_text_round_trip_recovers_original(plaintext: str) -> None:
    """decrypt(encrypt(x)) == x for arbitrary text (Req 6.1, 6.3)."""
    service = _service()
    ciphertext = service.encrypt(plaintext)
    assert service.decrypt(ciphertext) == plaintext


# --- Property 18: bytes round-trip ------------------------------------------

@_PBT
@given(plaintext=st.binary())
def test_bytes_round_trip_recovers_original(plaintext: bytes) -> None:
    """decrypt_bytes(encrypt(x)) == x for arbitrary bytes (Req 6.1, 6.3)."""
    service = _service()
    ciphertext = service.encrypt(plaintext)
    assert service.decrypt_bytes(ciphertext) == plaintext


# --- Property 18: ciphertext is opaque bytes, never the plaintext -----------

@_PBT
@given(plaintext=st.text(min_size=1))
def test_text_ciphertext_is_opaque_bytes(plaintext: str) -> None:
    """Ciphertext is ``bytes`` and never equals the plaintext (Req 6.2)."""
    service = _service()
    ciphertext = service.encrypt(plaintext)
    assert isinstance(ciphertext, bytes)
    assert ciphertext != plaintext.encode("utf-8")
    assert ciphertext != plaintext


@_PBT
@given(plaintext=st.binary(min_size=1))
def test_bytes_ciphertext_is_opaque_bytes(plaintext: bytes) -> None:
    """Ciphertext is ``bytes`` and never equals the raw plaintext (Req 6.2)."""
    service = _service()
    ciphertext = service.encrypt(plaintext)
    assert isinstance(ciphertext, bytes)
    assert ciphertext != plaintext


# --- Property 18: nondeterministic encryption still round-trips -------------

@_PBT
@given(plaintext=st.text())
def test_two_encryptions_differ_but_both_decrypt(plaintext: str) -> None:
    """Fernet's random IV/timestamp makes repeated encryptions differ, yet
    both must decrypt back to the original (Req 6.2, 6.3)."""
    service = _service()
    first = service.encrypt(plaintext)
    second = service.encrypt(plaintext)
    assert first != second
    assert service.decrypt(first) == plaintext
    assert service.decrypt(second) == plaintext


# --- Property 18: tampering breaks authentication ---------------------------

@_PBT
@given(
    plaintext=st.text(),
    data=st.data(),
)
def test_tampered_ciphertext_raises_decryption_error(plaintext: str, data) -> None:
    """Flipping a byte of the RAW (base64-decoded) Fernet token must fail
    authentication and raise :class:`DecryptionError` (Req 6.3).

    A Fernet token is urlsafe-base64 *text*; mutating a byte of that encoded
    form can, for some positions, decode to the same underlying token (base64
    is not injective at the byte level, and padding bits are ignored). To make
    the tamper deterministic we decode the token to its raw bytes, flip one of
    those bytes to a guaranteed-different value, then re-encode. Any change to
    the raw IV, ciphertext body, or HMAC tag invalidates the authentication
    tag, so decrypt() must raise DecryptionError.
    """
    import base64

    service = _service()
    token = service.encrypt(plaintext)

    raw = bytearray(base64.urlsafe_b64decode(token))
    index = data.draw(st.integers(min_value=0, max_value=len(raw) - 1))
    original = raw[index]
    replacement = data.draw(
        st.integers(min_value=0, max_value=255).filter(lambda b: b != original)
    )
    raw[index] = replacement
    tampered = base64.urlsafe_b64encode(bytes(raw))

    try:
        service.decrypt(tampered)
    except DecryptionError:
        pass
    else:
        raise AssertionError("tampered ciphertext was accepted by decrypt()")


# --- Property 18: truncation breaks authentication --------------------------

@_PBT
@given(plaintext=st.text(min_size=1))
def test_truncated_ciphertext_raises_decryption_error(plaintext: str) -> None:
    """Dropping the final byte of a token must raise :class:`DecryptionError`
    rather than returning corrupted plaintext (Req 6.3)."""
    service = _service()
    ciphertext = service.encrypt(plaintext)
    truncated = ciphertext[:-1]

    try:
        service.decrypt(truncated)
    except DecryptionError:
        pass
    else:
        raise AssertionError("truncated ciphertext was accepted by decrypt()")
