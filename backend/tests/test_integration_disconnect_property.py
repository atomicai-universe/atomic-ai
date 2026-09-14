"""Property-based test for Integration_Vault disconnect (task 9.2).

Property 20: **Integration disconnect removes credentials** — after
``disconnect``, the integration row (and therefore its encrypted access/refresh
ciphertext) no longer exists in the datastore, and any dependent operation that
tries to ``use_credential`` on the disconnected id is rejected with a 404. This
holds across arbitrary token contents, optional refresh tokens, every
integration category, and arbitrary provider names.

The property is exercised without a database: it reuses the in-memory
``_FakeSession`` pattern from ``test_integration_vault.py`` (add/flush/get/delete
keyed by row id) and a real Fernet-backed ``EncryptionService``. A fresh session
and encryption service are constructed per example so examples are fully
isolated, and the async store/use/disconnect calls are driven with
``asyncio.run``.

Validates: Requirements 7.7.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from cryptography.fernet import Fernet
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.encryption import EncryptionService
from app.core.errors import APIError
from app.db.models import Integration, IntegrationCategory, IntegrationStatus
from app.services import integration_vault


class _FakeSession:
    """Minimal async-session stand-in supporting add/flush/get/delete.

    Rows are keyed by their ``id``; enough to drive the vault's
    store/use/disconnect logic without a database (mirrors the helper in
    ``test_integration_vault.py``).
    """

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, Integration] = {}

    def add(self, obj: Integration) -> None:
        if obj.id is None:
            obj.id = uuid.uuid4()
        if obj.status is None:
            obj.status = IntegrationStatus.ACTIVE
        self._rows[obj.id] = obj

    async def flush(self) -> None:  # ids assigned eagerly in add()
        return None

    async def get(self, _model: type, ident: uuid.UUID) -> Integration | None:
        return self._rows.get(ident)

    async def scalar(self, _stmt: object) -> Integration | None:
        # No SQL execution in this fake; dedupe SELECT reports "no existing row".
        return None

    async def delete(self, obj: Integration) -> None:
        self._rows.pop(obj.id, None)


async def _store_use_disconnect(
    *,
    category: IntegrationCategory,
    provider_name: str,
    access_token: str,
    refresh_token: str | None,
) -> None:
    """Drive the full store -> use -> disconnect -> use lifecycle for one example."""
    enc = EncryptionService(Fernet.generate_key())
    session = _FakeSession()

    integration = await integration_vault.store(
        session,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        category=category,
        provider_name=provider_name,
        access_token=access_token,
        refresh_token=refresh_token,
        encryption_service=enc,
    )
    integration_id = integration.id

    # Before disconnect the credential round-trips to the original plaintext.
    cred = await integration_vault.use_credential(
        session,  # type: ignore[arg-type]
        integration_id=integration_id,
        encryption_service=enc,
    )
    assert cred.access_token == access_token
    assert cred.refresh_token == refresh_token

    # Disconnect removes all trace of the encrypted credentials (Req 7.7).
    await integration_vault.disconnect(
        session,  # type: ignore[arg-type]
        integration_id=integration_id,
    )

    assert await session.get(Integration, integration_id) is None

    # Any dependent op on the disconnected id is now rejected with a 404.
    with pytest.raises(APIError) as excinfo:
        await integration_vault.use_credential(
            session,  # type: ignore[arg-type]
            integration_id=integration_id,
            encryption_service=enc,
        )
    assert excinfo.value.status_code == 404


@settings(max_examples=200)
@given(
    access_token=st.text(),
    refresh_token=st.one_of(st.none(), st.text()),
    category=st.sampled_from(list(IntegrationCategory)),
    provider_name=st.text(min_size=1),
)
def test_disconnect_removes_credentials(
    access_token: str,
    refresh_token: str | None,
    category: IntegrationCategory,
    provider_name: str,
) -> None:
    """Property 20: disconnect removes the encrypted credentials (Req 7.7).

    For arbitrary token contents (including empty and unicode), an optional
    refresh token, any of the twelve categories, and any non-empty provider
    name: after ``store`` the credential is usable, and after ``disconnect`` the
    row is gone and ``use_credential`` raises a 404.

    Validates: Requirements 7.7.
    """
    asyncio.run(
        _store_use_disconnect(
            category=category,
            provider_name=provider_name,
            access_token=access_token,
            refresh_token=refresh_token,
        )
    )


@settings(max_examples=200)
@given(
    access_token=st.text(),
    refresh_token=st.one_of(st.none(), st.text()),
    category=st.sampled_from(list(IntegrationCategory)),
    provider_name=st.text(min_size=1),
)
def test_disconnect_is_idempotent(
    access_token: str,
    refresh_token: str | None,
    category: IntegrationCategory,
    provider_name: str,
) -> None:
    """Disconnecting an already-absent integration is a no-op (Req 7.7).

    A second ``disconnect`` on the same (now removed) id must not raise, so the
    operation is safely idempotent.

    Validates: Requirements 7.7.
    """

    async def _run() -> None:
        enc = EncryptionService(Fernet.generate_key())
        session = _FakeSession()

        integration = await integration_vault.store(
            session,  # type: ignore[arg-type]
            workspace_id=uuid.uuid4(),
            created_by_user_id=uuid.uuid4(),
            category=category,
            provider_name=provider_name,
            access_token=access_token,
            refresh_token=refresh_token,
            encryption_service=enc,
        )
        integration_id = integration.id

        await integration_vault.disconnect(
            session,  # type: ignore[arg-type]
            integration_id=integration_id,
        )
        # Second disconnect on the now-absent id must not raise.
        await integration_vault.disconnect(
            session,  # type: ignore[arg-type]
            integration_id=integration_id,
        )
        assert await session.get(Integration, integration_id) is None

    asyncio.run(_run())
