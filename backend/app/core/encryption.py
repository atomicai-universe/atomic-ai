"""Encryption_Service — authenticated symmetric encryption for credentials.

Provides the :class:`EncryptionService` used by the Integration_Vault to encrypt
OAuth access/refresh tokens before they are persisted and to decrypt them
in-memory for the duration of a tool call. Ciphertext is returned as ``bytes``
suitable for the ``bytea`` (``LargeBinary``) columns in ``db/models.py`` such as
``Integration.encrypted_access_token``.

Design decisions (see design.md "Integration_Vault and Encryption_Service"):

- Uses **Fernet** (AES-128-CBC with an HMAC-SHA256 authentication tag), an
  authenticated symmetric scheme. Tampered or truncated ciphertext fails the
  authentication check and raises a distinct :class:`DecryptionError` rather
  than returning corrupted plaintext (Req 6.3, 6.5 support).
- The key is read **only** from the ``ENCRYPTION_KEY`` environment configuration
  (via :func:`app.config.get_settings`) and is never written to the database
  (Req 6.6). The plaintext token is never persisted — only ciphertext is
  returned for storage (Req 6.1, 6.2).

Requirements: 6.1, 6.2, 6.3, 6.6.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class EncryptionError(RuntimeError):
    """Base class for Encryption_Service failures."""


class DecryptionError(EncryptionError):
    """Raised when ciphertext cannot be authenticated or decrypted.

    A distinct error type so callers can catch authentication-tag failures
    (tampered, truncated, or garbage ciphertext, or ciphertext produced under a
    different key) and mark the dependent Integration ``status = error`` while
    rejecting the operation (Req 6.5). The message never includes key material
    or plaintext.
    """


class EncryptionError_InvalidKey(EncryptionError):
    """Raised when the configured ``ENCRYPTION_KEY`` is not a valid Fernet key."""


class EncryptionService:
    """Authenticated symmetric encryption bound to a single key.

    The key is supplied at construction only (Req 6.6) — this class does not
    read the environment itself; use :func:`get_encryption_service` for the
    lazily-configured process-wide instance.
    """

    def __init__(self, key: bytes | str) -> None:
        """Create a service from a urlsafe-base64 Fernet key.

        Args:
            key: A 32-byte urlsafe-base64-encoded Fernet key, as ``bytes`` or
                ``str`` (for example the value produced by
                ``Fernet.generate_key()``).

        Raises:
            EncryptionError_InvalidKey: if ``key`` is not a valid Fernet key.
        """
        if isinstance(key, str):
            key = key.encode("utf-8")
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            # Do not echo the key value; only report that it is malformed.
            raise EncryptionError_InvalidKey(
                "ENCRYPTION_KEY is not a valid Fernet key "
                "(expected 32 url-safe base64-encoded bytes)."
            ) from exc

    def encrypt(self, plaintext: str | bytes) -> bytes:
        """Encrypt ``plaintext`` and return authenticated ciphertext bytes.

        The returned bytes are safe to store in a ``bytea``/``LargeBinary``
        column and are never equal to the plaintext bytes.

        Args:
            plaintext: The secret to encrypt, as ``str`` (UTF-8 encoded) or
                raw ``bytes``.

        Returns:
            The Fernet token as ``bytes``.
        """
        if isinstance(plaintext, str):
            data = plaintext.encode("utf-8")
        else:
            data = plaintext
        return self._fernet.encrypt(data)

    def decrypt(self, ciphertext: bytes | str) -> str:
        """Decrypt ``ciphertext`` produced by :meth:`encrypt`.

        Args:
            ciphertext: The Fernet token bytes previously returned by
                :meth:`encrypt` (``str`` is accepted and UTF-8 encoded).

        Returns:
            The recovered plaintext decoded as a UTF-8 ``str``.

        Raises:
            DecryptionError: if the ciphertext fails authentication (tampered,
                truncated, garbage, or produced under a different key), or if
                the recovered plaintext is not valid UTF-8.
        """
        if isinstance(ciphertext, str):
            token = ciphertext.encode("utf-8")
        else:
            token = ciphertext
        try:
            data = self._fernet.decrypt(token)
        except (InvalidToken, TypeError, ValueError) as exc:
            raise DecryptionError(
                "Failed to decrypt credential: ciphertext is invalid or has "
                "been tampered with."
            ) from exc
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecryptionError(
                "Decrypted credential is not valid UTF-8 text."
            ) from exc

    def decrypt_bytes(self, ciphertext: bytes | str) -> bytes:
        """Decrypt ``ciphertext`` and return the raw plaintext ``bytes``.

        Use when the stored secret is binary rather than UTF-8 text.

        Raises:
            DecryptionError: if the ciphertext fails authentication.
        """
        if isinstance(ciphertext, str):
            token = ciphertext.encode("utf-8")
        else:
            token = ciphertext
        try:
            return self._fernet.decrypt(token)
        except (InvalidToken, TypeError, ValueError) as exc:
            raise DecryptionError(
                "Failed to decrypt credential: ciphertext is invalid or has "
                "been tampered with."
            ) from exc


_service: EncryptionService | None = None


def get_encryption_service() -> EncryptionService:
    """Return the process-wide Encryption_Service, constructing it on first use.

    The key is read lazily from ``ENCRYPTION_KEY`` via
    :func:`app.config.get_settings` (``SecretStr.get_secret_value``) so the key
    is sourced only from environment configuration and never from the database
    (Req 6.6).
    """
    global _service
    if _service is None:
        key = get_settings().ENCRYPTION_KEY.get_secret_value()
        _service = EncryptionService(key)
    return _service


def reset_encryption_service() -> None:
    """Clear the cached Encryption_Service (used by tests after key changes)."""
    global _service
    _service = None


def set_encryption_service(service: EncryptionService | None) -> None:
    """Install a specific process-wide Encryption_Service instance.

    Primarily a testing seam: it lets a test inject an ``EncryptionService``
    built from a known Fernet key so encrypted fixtures round-trip
    deterministically without depending on the ``ENCRYPTION_KEY`` environment
    configuration. Passing ``None`` clears the cached instance, equivalent to
    :func:`reset_encryption_service`.
    """
    global _service
    _service = service


__all__ = [
    "EncryptionService",
    "EncryptionError",
    "DecryptionError",
    "EncryptionError_InvalidKey",
    "get_encryption_service",
    "reset_encryption_service",
    "set_encryption_service",
]
