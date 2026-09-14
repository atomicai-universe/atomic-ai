"""Integration_Vault — store / use / disconnect encrypted OAuth credentials (task 9.1).

Implements the credential-lifecycle operations of the ``Integration_Vault``
component from the design (see design.md "Integration_Vault and
Encryption_Service"). It is the only path through which OAuth access/refresh
tokens enter, are used by, or leave the platform's datastore.

Operations (module-level async functions; the caller owns the transaction and
is responsible for ``commit``):

- :func:`store` — encrypts the ``access_token`` (and ``refresh_token`` when
  provided) via the process-wide :class:`~app.core.encryption.EncryptionService`
  and persists an :class:`~app.db.models.Integration` row holding **only the
  ciphertext** in ``encrypted_access_token`` / ``encrypted_refresh_token``
  (``bytea``), bound to ``workspace_id`` and the creating user across any of the
  twelve :class:`~app.db.models.IntegrationCategory` values. The plaintext token
  is never persisted (Req 6.1, 6.2, 7.1, 7.2).
- :func:`use_credential` — loads the row and decrypts the stored ciphertext
  **in memory only** for the duration of the caller's tool call, returning the
  plaintext token(s) (Req 6.3). The plaintext is never logged, never written
  back, and never attached to a serializable model. If decryption fails
  (tampered/garbage ciphertext -> :class:`~app.core.encryption.DecryptionError`),
  the integration's ``status`` is set to
  :attr:`~app.db.models.IntegrationStatus.ERROR` and the dependent operation is
  rejected by raising an :class:`~app.core.errors.APIError` — the caller's
  operation must not proceed (Req 6.5, 7.7).
- :func:`disconnect` — deletes the integration row entirely, removing its
  encrypted credentials from the datastore (Req 7.7).

This module intentionally does **not** implement the sharing-scope resolver /
toggle authorization (task 9.3) or the HTTP router (task 9.5); it stays focused
on the encrypted store/use/disconnect primitives.

Requirements: 6.3, 6.5, 7.1, 7.2, 7.7.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import (
    DecryptionError,
    EncryptionService,
    get_encryption_service,
)
from app.core.errors import APIError
from app.db.models import (
    Integration,
    IntegrationCategory,
    IntegrationStatus,
    MemberRole,
)


class _SharingScope(Protocol):
    """Structural view of an Integration for sharing-scope decisions.

    Both the ORM :class:`~app.db.models.Integration` and lightweight test
    fakes satisfy this protocol by exposing the two attributes the pure
    resolver functions read.
    """

    is_shared_with_workspace: bool
    created_by_user_id: uuid.UUID


@dataclass(frozen=True)
class DecryptedCredential:
    """In-memory plaintext credential(s) for a single tool call (Req 6.3).

    Held only for the scope of the caller's tool call and never persisted,
    logged, or serialized. The ``integration_id`` is included for correlation
    (it is not sensitive).

    ``credentials`` is the flexible per-provider secret map (e.g. ``bot_token``,
    ``client_id``/``client_secret``, ``api_key``, ``access_key_id`` …). ``config``
    holds non-secret operational values (region, host, base_url …). The
    ``access_token`` / ``refresh_token`` attributes remain for backward
    compatibility and are populated from the legacy columns or from the
    corresponding keys in ``credentials`` when present.
    """

    integration_id: uuid.UUID
    access_token: str | None = None
    refresh_token: str | None = None
    credentials: dict[str, str] = None  # type: ignore[assignment]
    config: dict[str, str] | None = None

    def __post_init__(self) -> None:
        # dataclass is frozen; use object.__setattr__ to default the map.
        if self.credentials is None:
            object.__setattr__(self, "credentials", {})

    def get(self, key: str, default: str | None = None) -> str | None:
        """Convenience accessor for a single secret field."""
        return self.credentials.get(key, default)


async def store(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    category: IntegrationCategory,
    provider_name: str,
    access_token: str | None = None,
    refresh_token: str | None = None,
    credentials: dict[str, str] | None = None,
    config: dict[str, str] | None = None,
    is_shared_with_workspace: bool = False,
    encryption_service: EncryptionService | None = None,
) -> Integration:
    """Encrypt and persist OAuth credentials for an integration (Req 7.1, 7.2).

    The ``access_token`` (and ``refresh_token`` when supplied) are encrypted with
    the authenticated :class:`~app.core.encryption.EncryptionService` and the
    resulting **ciphertext** is stored in ``encrypted_access_token`` /
    ``encrypted_refresh_token``. Plaintext tokens are never written to the
    database (Req 6.1, 6.2). The row is bound to ``workspace_id`` and
    ``created_by_user_id`` and may use any of the twelve
    :class:`~app.db.models.IntegrationCategory` values; ``status`` defaults to
    :attr:`~app.db.models.IntegrationStatus.ACTIVE`.

    The caller owns the surrounding transaction: this flushes to populate the
    generated id but does not ``commit``.

    Args:
        session: The active async session/transaction.
        workspace_id: Workspace the integration belongs to (tenant binding).
        created_by_user_id: User who connected the integration (creator binding).
        category: One of the twelve platform integration categories.
        provider_name: Human-readable provider label (e.g. ``"gmail"``).
        access_token: Plaintext OAuth access token to encrypt at rest.
        refresh_token: Optional plaintext OAuth refresh token to encrypt at rest.
        is_shared_with_workspace: Whether the integration is shared workspace-wide.
        encryption_service: Optional service override (defaults to the
            process-wide instance from :func:`get_encryption_service`).

    Returns:
        The persisted :class:`~app.db.models.Integration` with its generated id.
    """
    enc = encryption_service or get_encryption_service()

    # Assemble the full secret map. Callers may pass a flexible `credentials`
    # map (preferred) and/or the legacy access/refresh tokens; both are merged
    # so the whole secret set is encrypted as one JSON blob at rest (Req 6.1/6.2).
    secret_map: dict[str, str] = {}
    if credentials:
        secret_map.update({k: v for k, v in credentials.items() if v is not None})
    if access_token is not None:
        secret_map.setdefault("access_token", access_token)
    if refresh_token is not None:
        secret_map.setdefault("refresh_token", refresh_token)

    if not secret_map:
        raise APIError(
            status_code=422,
            code="invalid_request",
            message="At least one credential value is required to connect.",
        )

    encrypted_credentials = enc.encrypt(json.dumps(secret_map, separators=(",", ":")))

    # Preserve the legacy columns when an access/refresh token is present so any
    # older reader keeps working; new-shape secrets live only in the blob.
    encrypted_access_token = (
        enc.encrypt(access_token) if access_token is not None else None
    )
    encrypted_refresh_token = (
        enc.encrypt(refresh_token) if refresh_token is not None else None
    )

    # Dedupe: if this user already connected the same provider in this
    # workspace, update that row in place instead of creating a duplicate
    # (reconnecting a provider replaces its credentials). Matches on
    # (workspace, creator, category, provider_name).
    existing = await session.scalar(
        select(Integration).where(
            Integration.workspace_id == workspace_id,
            Integration.created_by_user_id == created_by_user_id,
            Integration.category == category,
            Integration.provider_name == provider_name,
        )
    )
    if existing is not None:
        existing.encrypted_access_token = encrypted_access_token
        existing.encrypted_refresh_token = encrypted_refresh_token
        existing.encrypted_credentials = encrypted_credentials
        existing.config = dict(config) if config else None
        existing.status = IntegrationStatus.ACTIVE
        # Preserve the existing sharing scope unless the caller changes it.
        existing.is_shared_with_workspace = is_shared_with_workspace
        await session.flush()
        return existing

    integration = Integration(
        workspace_id=workspace_id,
        created_by_user_id=created_by_user_id,
        category=category,
        provider_name=provider_name,
        is_shared_with_workspace=is_shared_with_workspace,
        encrypted_access_token=encrypted_access_token,
        encrypted_refresh_token=encrypted_refresh_token,
        encrypted_credentials=encrypted_credentials,
        config=dict(config) if config else None,
        status=IntegrationStatus.ACTIVE,
    )
    session.add(integration)
    await session.flush()
    return integration


async def use_credential(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID,
    encryption_service: EncryptionService | None = None,
) -> DecryptedCredential:
    """Decrypt an integration's credentials in memory for a tool call (Req 6.3).

    Loads the :class:`~app.db.models.Integration`, decrypts its stored
    ciphertext, and returns the plaintext token(s) as an in-memory
    :class:`DecryptedCredential`. The plaintext is confined to the caller's
    tool-call scope: it is never logged, persisted, or attached to a
    serializable model (Req 6.4 alignment).

    On a decryption/authentication-tag failure (tampered, truncated, garbage, or
    wrong-key ciphertext), the integration's ``status`` is set to
    :attr:`~app.db.models.IntegrationStatus.ERROR` and the dependent operation is
    rejected with an :class:`~app.core.errors.APIError` so it does not proceed
    (Req 6.5, 7.7 partial).

    The caller owns the surrounding transaction; the ``status = error`` mutation
    is flushed but not committed here so it participates in the caller's tx.

    Args:
        session: The active async session/transaction.
        integration_id: Identifier of the integration whose credentials to use.
        encryption_service: Optional service override (defaults to the
            process-wide instance from :func:`get_encryption_service`).

    Returns:
        A :class:`DecryptedCredential` holding the recovered plaintext token(s).

    Raises:
        APIError: 404 if the integration does not exist.
        APIError: 502 if the stored ciphertext cannot be decrypted; the
            integration is marked ``status = error`` first.
    """
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Integration not found.",
        )

    enc = encryption_service or get_encryption_service()

    try:
        credentials: dict[str, str] = {}
        # Preferred: the flexible encrypted credential blob (JSON of all secrets).
        if integration.encrypted_credentials is not None:
            raw = enc.decrypt(integration.encrypted_credentials)
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                credentials = {str(k): str(v) for k, v in loaded.items()}
        # Legacy fallback / merge: single-token columns from older rows.
        if integration.encrypted_access_token is not None:
            credentials.setdefault(
                "access_token", enc.decrypt(integration.encrypted_access_token)
            )
        if integration.encrypted_refresh_token is not None:
            credentials.setdefault(
                "refresh_token", enc.decrypt(integration.encrypted_refresh_token)
            )
    except (DecryptionError, ValueError) as exc:
        # Authentication-tag failure (or a corrupt JSON blob): mark the
        # integration errored and reject the dependent operation so it cannot
        # proceed with a bad credential (Req 6.5). No key material/plaintext in
        # the error message.
        integration.status = IntegrationStatus.ERROR
        await session.flush()
        raise APIError(
            status_code=502,
            code="integration_error",
            message="The integration credential could not be decrypted; the "
            "integration has been marked as errored.",
        ) from exc

    return DecryptedCredential(
        integration_id=integration.id,
        access_token=credentials.get("access_token"),
        refresh_token=credentials.get("refresh_token"),
        credentials=credentials,
        config=dict(integration.config) if integration.config else None,
    )


async def disconnect(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID,
) -> None:
    """Delete an integration, removing its encrypted credentials (Req 7.7).

    Deletes the :class:`~app.db.models.Integration` row entirely so its
    ``encrypted_access_token`` / ``encrypted_refresh_token`` ciphertext no longer
    exists in the datastore. A subsequent load of the same id returns ``None``.

    The caller owns the surrounding transaction (no ``commit`` here). Deleting a
    nonexistent integration is a no-op (idempotent disconnect).

    Args:
        session: The active async session/transaction.
        integration_id: Identifier of the integration to disconnect.
    """
    integration = await session.get(Integration, integration_id)
    if integration is None:
        return
    await session.delete(integration)
    await session.flush()


async def list_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
) -> list[Integration]:
    """Return all integrations for a workspace (token-free rows for listing).

    Read-only. Returns the ORM rows scoped to ``workspace_id`` ordered by
    creation time (newest first). Callers project these onto a token-free view
    for responses — the ciphertext columns are never serialized (Req 6.4). The
    caller owns any surrounding transaction.
    """
    result = await session.execute(
        select(Integration)
        .where(Integration.workspace_id == workspace_id)
        .order_by(Integration.created_at.desc())
    )
    return list(result.scalars().all())


def can_use_integration(
    integration: _SharingScope,
    user_id: uuid.UUID,
    role: MemberRole,
) -> bool:
    """Decide whether ``user_id`` (holding ``role``) may USE ``integration``.

    Pure decision function (no I/O) — the sole authority for integration-use
    authorization, property-tested by task 9.4 (Property 3).

    Rules (Req 7.3, 7.4, 7.5):

    - **Creator** — a User whose id equals ``integration.created_by_user_id``
      may ALWAYS use the integration, whether it is personal or shared, and
      regardless of role.
    - **Shared_Integration** (``is_shared_with_workspace`` is ``True``) — a
      non-creating Workspace_Member may use it only if their role is
      :attr:`~app.db.models.MemberRole.OWNER`,
      :attr:`~app.db.models.MemberRole.ADMIN`, or
      :attr:`~app.db.models.MemberRole.MEMBER`. The
      :attr:`~app.db.models.MemberRole.VIEWER` role may NOT use it (Req 7.3,
      7.5).
    - **Personal_Integration** (``is_shared_with_workspace`` is ``False``) —
      usable ONLY by its creating User; every non-creator is denied regardless
      of role (Req 7.4, 7.5).

    Args:
        integration: The integration (ORM row or any object exposing
            ``is_shared_with_workspace`` and ``created_by_user_id``).
        user_id: The id of the acting Workspace_Member.
        role: The acting member's workspace role.

    Returns:
        ``True`` if use is permitted, ``False`` otherwise.
    """
    if integration.created_by_user_id == user_id:
        return True
    if integration.is_shared_with_workspace:
        return role in (MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MEMBER)
    return False


def can_toggle_sharing(
    integration: _SharingScope,
    user_id: uuid.UUID,
    role: MemberRole,
) -> bool:
    """Decide whether ``user_id`` (holding ``role``) may TOGGLE sharing scope.

    Pure decision function (no I/O) — the sole authority for sharing-toggle
    authorization, property-tested by task 9.4 (Property 4).

    Rules (Req 7.6):

    - The **creating User** (id equals ``integration.created_by_user_id``) may
      always toggle the sharing scope of their own integration.
    - A non-creating Workspace_Member may toggle only if they hold the
      :attr:`~app.db.models.MemberRole.OWNER` or
      :attr:`~app.db.models.MemberRole.ADMIN` role.
    - A non-creating :attr:`~app.db.models.MemberRole.MEMBER` or
      :attr:`~app.db.models.MemberRole.VIEWER` is denied.

    Args:
        integration: The integration (ORM row or any object exposing
            ``is_shared_with_workspace`` and ``created_by_user_id``).
        user_id: The id of the acting Workspace_Member.
        role: The acting member's workspace role.

    Returns:
        ``True`` if toggling is permitted, ``False`` otherwise.
    """
    if integration.created_by_user_id == user_id:
        return True
    return role in (MemberRole.OWNER, MemberRole.ADMIN)


async def set_sharing(
    session: AsyncSession,
    *,
    integration_id: uuid.UUID,
    user_id: uuid.UUID,
    role: MemberRole,
    shared: bool,
) -> Integration:
    """Set an integration's sharing scope, enforcing :func:`can_toggle_sharing`.

    Loads the :class:`~app.db.models.Integration`, checks toggle authorization
    with the pure :func:`can_toggle_sharing` rule, and — when permitted — sets
    ``is_shared_with_workspace`` to ``shared`` (Req 7.6). The caller owns the
    surrounding transaction (this flushes but does not ``commit``).

    Args:
        session: The active async session/transaction.
        integration_id: Identifier of the integration to update.
        user_id: The id of the acting Workspace_Member.
        role: The acting member's workspace role.
        shared: Desired value of ``is_shared_with_workspace``.

    Returns:
        The updated :class:`~app.db.models.Integration`.

    Raises:
        APIError: 404 if the integration does not exist.
        APIError: 403 if the acting user is not permitted to toggle sharing.
    """
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Integration not found.",
        )

    if not can_toggle_sharing(integration, user_id, role):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="You are not permitted to change the sharing scope of this "
            "integration.",
        )

    integration.is_shared_with_workspace = shared
    await session.flush()
    return integration


__all__ = [
    "DecryptedCredential",
    "store",
    "use_credential",
    "disconnect",
    "list_for_workspace",
    "can_use_integration",
    "can_toggle_sharing",
    "set_sharing",
]
