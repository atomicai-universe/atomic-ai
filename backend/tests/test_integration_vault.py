"""Tests for the Integration_Vault store/use/disconnect operations (task 9.1).

Two layers of coverage:

1. **Fast pure-ish tests** (no DB, always run): drive the vault functions with
   an in-memory fake session so encryption round-trip and the corrupted-cipher
   error path are validated cheaply without Postgres.

2. **DB-backed integration tests** against a *real*, throwaway
   ``postgres:18.6-alpine`` on a non-default host port (55438) with the
   project's Alembic migration applied, so the ``bytea`` ciphertext columns,
   status transitions, and row deletion are exercised for real. The module
   skips gracefully when Docker is unavailable.

Asserted behaviors (Req 6.3, 6.5, 7.1, 7.2, 7.7):

- ``store`` persists a row whose ``encrypted_access_token`` is CIPHERTEXT (not
  the plaintext bytes) bound to the workspace + creating user (Req 7.1, 7.2).
- ``use_credential`` round-trips: returns the original plaintext token (Req 6.3).
- Corrupted ciphertext -> ``use_credential`` raises an APIError AND sets the
  integration ``status = error`` (Req 6.5, 7.7).
- ``disconnect`` deletes the row; a subsequent load returns ``None`` (Req 7.7).

Requirements: 6.3, 6.5, 7.1, 7.2, 7.7.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.encryption import EncryptionService
from app.core.errors import APIError
from app.db.models import (
    Integration,
    IntegrationCategory,
    IntegrationStatus,
    User,
    Workspace,
    WorkspaceMember,
    MemberRole,
)
from app.services import integration_vault

# A real Fernet key for the tests' encryption service (never a fixed secret).
_TEST_FERNET_KEY = Fernet.generate_key()


def _enc() -> EncryptionService:
    return EncryptionService(_TEST_FERNET_KEY)


# ---------------------------------------------------------------------------
# Fast tests with an in-memory fake session (no DB) — always run
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal async-session stand-in supporting add/flush/get/delete.

    Enough to drive the vault's encryption/decryption logic and status
    transitions without a database. Rows are keyed by their ``id``.
    """

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, Integration] = {}

    def add(self, obj: Integration) -> None:
        if obj.id is None:
            obj.id = uuid.uuid4()
        # Mirror server defaults the ORM would apply.
        if obj.status is None:
            obj.status = IntegrationStatus.ACTIVE
        self._rows[obj.id] = obj

    async def flush(self) -> None:  # no-op; ids assigned in add()
        return None

    async def get(self, _model: type, ident: uuid.UUID) -> Integration | None:
        return self._rows.get(ident)

    async def scalar(self, _stmt: object) -> Integration | None:
        # The vault's dedupe lookup runs a SELECT; this fake doesn't execute
        # SQL, so report "no existing row" -> store() takes the create path.
        return None

    async def delete(self, obj: Integration) -> None:
        self._rows.pop(obj.id, None)


@pytest.mark.asyncio
async def test_store_encrypts_and_use_round_trips_in_memory() -> None:
    """store() persists ciphertext; use_credential() recovers the plaintext."""
    enc = _enc()
    session = _FakeSession()
    plaintext = "ya29.super-secret-access-token"

    integration = await integration_vault.store(
        session,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        access_token=plaintext,
        refresh_token="refresh-xyz",
        encryption_service=enc,
    )

    # Never store plaintext (Req 6.1, 6.2).
    assert integration.encrypted_access_token != plaintext.encode()
    assert integration.encrypted_refresh_token is not None
    assert integration.status is IntegrationStatus.ACTIVE

    cred = await integration_vault.use_credential(
        session,  # type: ignore[arg-type]
        integration_id=integration.id,
        encryption_service=enc,
    )
    assert cred.access_token == plaintext
    assert cred.refresh_token == "refresh-xyz"


@pytest.mark.asyncio
async def test_use_credential_corrupted_cipher_marks_error_in_memory() -> None:
    """Corrupted ciphertext raises APIError and marks status=error (Req 6.5)."""
    enc = _enc()
    session = _FakeSession()
    integration = await integration_vault.store(
        session,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        category=IntegrationCategory.DEVELOPER,
        provider_name="github",
        access_token="tok",
        encryption_service=enc,
    )
    # Tamper with the stored ciphertext.
    integration.encrypted_access_token = b"not-valid-ciphertext"

    with pytest.raises(APIError) as excinfo:
        await integration_vault.use_credential(
            session,  # type: ignore[arg-type]
            integration_id=integration.id,
            encryption_service=enc,
        )
    assert excinfo.value.status_code == 502
    assert integration.status is IntegrationStatus.ERROR


@pytest.mark.asyncio
async def test_use_credential_missing_raises_404_in_memory() -> None:
    session = _FakeSession()
    with pytest.raises(APIError) as excinfo:
        await integration_vault.use_credential(
            session,  # type: ignore[arg-type]
            integration_id=uuid.uuid4(),
            encryption_service=_enc(),
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_disconnect_removes_row_in_memory() -> None:
    enc = _enc()
    session = _FakeSession()
    integration = await integration_vault.store(
        session,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        category=IntegrationCategory.CRM,
        provider_name="salesforce",
        access_token="tok",
        encryption_service=enc,
    )
    await integration_vault.disconnect(
        session,  # type: ignore[arg-type]
        integration_id=integration.id,
    )
    assert await session.get(Integration, integration.id) is None


# ---------------------------------------------------------------------------
# DB-backed integration test infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_integration_vault_test"
_HOST_PORT = 55438  # non-default so we never touch another database
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105 - throwaway container credential
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}"
    f"@localhost:{_HOST_PORT}/{_PG_DB}"
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _force_remove_container() -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _wait_until_ready(timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_output = ""
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            last_output = str(exc)
            time.sleep(1.0)
            continue
        if result.returncode == 0:
            return
        last_output = (result.stdout or "") + (result.stderr or "")
        time.sleep(1.0)
    raise RuntimeError(
        f"Postgres container did not become ready in {timeout_s}s: {last_output!r}"
    )


def _run_migrations() -> None:
    env = {
        **os.environ,
        "DATABASE_URL": _TEST_DSN,
        "REDIS_URL": "redis://localhost:6379/0",
        "ENCRYPTION_KEY": _TEST_FERNET_KEY.decode(),
        "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret-value",
        "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret-value",
    }
    result = subprocess.run(
        [".venv/bin/alembic", "-c", "alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_BACKEND_DIR),
        env=env,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Start a throwaway Postgres, apply migrations, yield the DSN, tear down."""
    if not _docker_available():
        pytest.skip("Docker CLI/daemon not available")

    _force_remove_container()
    try:
        start = subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", _CONTAINER_NAME,
                "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e", f"POSTGRES_USER={_PG_USER}",
                "-e", f"POSTGRES_DB={_PG_DB}",
                "-p", f"{_HOST_PORT}:5432",
                _PG_IMAGE,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if start.returncode != 0:
            pytest.skip(
                f"could not start Postgres container: {start.stderr.strip()[:300]}"
            )
        _wait_until_ready()
        _run_migrations()
        yield _TEST_DSN
    finally:
        _force_remove_container()


@pytest_asyncio.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, future=True, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


async def _make_workspace_and_user(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a user + workspace + owner membership; return (workspace_id, user_id)."""
    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
        name="Vault User",
        auth_provider="google",
    )
    session.add(user)
    await session.flush()

    workspace = Workspace(
        name="Vault WS",
        slug=f"vault-{uuid.uuid4().hex[:8]}",
        created_by_user_id=user.id,
    )
    session.add(workspace)
    await session.flush()

    session.add(
        WorkspaceMember(
            workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER
        )
    )
    await session.flush()
    return workspace.id, user.id


# ---------------------------------------------------------------------------
# store (Req 7.1, 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_persists_ciphertext_bound_to_workspace_and_user(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """store() persists ciphertext bound to workspace + creator (Req 7.1, 7.2)."""
    workspace_id, user_id = await _make_workspace_and_user(session)
    plaintext = "access-token-plaintext-value"

    integration = await integration_vault.store(
        session,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        category=IntegrationCategory.EMAIL,
        provider_name="gmail",
        access_token=plaintext,
        refresh_token="refresh-plaintext",
        is_shared_with_workspace=True,
        encryption_service=_enc(),
    )
    await session.commit()
    integration_id = integration.id

    # Read the raw bytea back on a fresh connection: it must be ciphertext, not
    # the plaintext bytes, and bound to the workspace + creating user.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, created_by_user_id, encrypted_access_token, "
                    "encrypted_refresh_token, category, status "
                    "FROM integrations WHERE id = :id"
                ),
                {"id": integration_id},
            )
        ).first()

    assert row is not None
    assert row[0] == workspace_id
    assert row[1] == user_id
    stored_access = bytes(row[2])
    assert stored_access != plaintext.encode(), "access token must be ciphertext"
    assert bytes(row[3]) != b"refresh-plaintext"
    assert row[4] == IntegrationCategory.EMAIL.value
    assert row[5] == IntegrationStatus.ACTIVE.value

    # The ciphertext must still decrypt back to the original plaintext.
    assert _enc().decrypt(stored_access) == plaintext


@pytest.mark.asyncio
async def test_store_supports_all_twelve_categories(session: AsyncSession) -> None:
    """A row can be stored under any of the twelve categories (Req 7.2)."""
    workspace_id, user_id = await _make_workspace_and_user(session)
    enc = _enc()
    for category in IntegrationCategory:
        integration = await integration_vault.store(
            session,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            category=category,
            provider_name=f"provider-{category.value}",
            access_token=f"tok-{category.value}",
            encryption_service=enc,
        )
        assert integration.category is category
    await session.commit()


# ---------------------------------------------------------------------------
# use_credential round-trip (Req 6.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_credential_round_trips(session: AsyncSession) -> None:
    """use_credential() returns the original plaintext token(s) (Req 6.3)."""
    workspace_id, user_id = await _make_workspace_and_user(session)
    enc = _enc()
    integration = await integration_vault.store(
        session,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        category=IntegrationCategory.CALENDAR,
        provider_name="google-calendar",
        access_token="the-access-token",
        refresh_token="the-refresh-token",
        encryption_service=enc,
    )
    await session.commit()

    cred = await integration_vault.use_credential(
        session, integration_id=integration.id, encryption_service=enc
    )
    assert cred.integration_id == integration.id
    assert cred.access_token == "the-access-token"
    assert cred.refresh_token == "the-refresh-token"


# ---------------------------------------------------------------------------
# corrupted ciphertext -> error + status=error (Req 6.5, 7.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupted_ciphertext_rejects_and_marks_error(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Garbage ciphertext -> APIError raised AND status=error persisted (Req 6.5)."""
    workspace_id, user_id = await _make_workspace_and_user(session)
    enc = _enc()
    integration = await integration_vault.store(
        session,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        category=IntegrationCategory.SUPPORT,
        provider_name="zendesk",
        access_token="valid-token",
        encryption_service=enc,
    )
    await session.commit()
    integration_id = integration.id

    # Manually overwrite the stored ciphertext with garbage bytes.
    await session.execute(
        sa.text(
            "UPDATE integrations SET encrypted_access_token = :garbage WHERE id = :id"
        ),
        {"garbage": b"\x00\x01\x02-not-a-fernet-token", "id": integration_id},
    )
    await session.commit()
    session.expire_all()

    with pytest.raises(APIError) as excinfo:
        await integration_vault.use_credential(
            session, integration_id=integration_id, encryption_service=enc
        )
    assert excinfo.value.status_code == 502
    await session.commit()

    # status=error must be persisted (checked on a fresh connection).
    async with engine.connect() as conn:
        status_value = await conn.scalar(
            sa.text("SELECT status FROM integrations WHERE id = :id"),
            {"id": integration_id},
        )
    assert status_value == IntegrationStatus.ERROR.value


# ---------------------------------------------------------------------------
# disconnect deletes the row (Req 7.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_deletes_the_row(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """disconnect() deletes the integration; subsequent load returns None (Req 7.7)."""
    workspace_id, user_id = await _make_workspace_and_user(session)
    integration = await integration_vault.store(
        session,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        category=IntegrationCategory.CLOUD,
        provider_name="aws",
        access_token="tok",
        encryption_service=_enc(),
    )
    await session.commit()
    integration_id = integration.id

    await integration_vault.disconnect(session, integration_id=integration_id)
    await session.commit()

    assert await session.get(Integration, integration_id) is None
    async with engine.connect() as conn:
        count = await conn.scalar(
            sa.text("SELECT count(*) FROM integrations WHERE id = :id"),
            {"id": integration_id},
        )
    assert count == 0
