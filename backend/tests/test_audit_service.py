"""Integration tests for the append-only Audit_Service (task 15.1).

These tests exercise :mod:`app.services.audit_service` against a *real*,
throwaway PostgreSQL instance provisioned with the project's *real* Alembic
migration (``alembic upgrade head``). The migration path is required — not
``Base.metadata.create_all`` — because only the migration installs the
append-only trigger on ``system_audit_logs`` that this suite relies on to prove
DB-level immutability (Req 15.5).

What is asserted:

- :func:`record` appends an entry with the correct ``workspace_id`` /
  ``user_id`` / ``action`` for three representative security-relevant cases:
  a state-changing workspace op (``workspace.created``, Req 15.1), an auth
  event (``auth.logout``, Req 15.2), and an approval resolution
  (``approval.approved``, Req 15.3). The auth case is recorded with NULL
  ``workspace_id`` since auth events are not workspace-scoped.
- Metadata scrubbing (Req 15.4): recording metadata whose keys look like
  secrets (``access_token``, ``client_secret``, ``session_token`` and a nested
  ``api_key``) persists them redacted to
  :data:`app.core.scrubbing.REDACTED` (``"***"``), and none of the raw secret
  strings appear anywhere in the stored ``metadata`` column.
- Append-only application surface (Req 15.5): the module exposes ``record``
  plus read-only helpers only — it has no ``update_*`` / ``delete_*`` function.
- Append-only DB enforcement (Req 15.5): after :func:`record` persists a row,
  a raw ``UPDATE`` and a raw ``DELETE`` against ``system_audit_logs`` both
  raise (the migration's append-only trigger).

Infrastructure:

The module fixture starts a disposable ``postgres:18.6-alpine`` container on a
non-default host port (55454) so it never touches another database, waits for
readiness via ``pg_isready``, injects ``DATABASE_URL`` (plus the other required
config vars) for the alembic subprocess, applies migrations, and always tears
the container down in a ``finally``. The async engine uses ``NullPool`` so no
connections outlive a test. If Docker is unavailable the whole module skips
gracefully so the suite still passes without Docker.

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.scrubbing import REDACTED
from app.services import audit_service

# --- Repo layout / container parameters --------------------------------------
# tests/ -> backend/
_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_audit_service_test"
_HOST_PORT = 55454  # deliberately non-default so we never touch another DB
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105 - throwaway container credential
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}"
    f"@localhost:{_HOST_PORT}/{_PG_DB}"
)


# --- Docker helpers ----------------------------------------------------------

def _docker_available() -> bool:
    """Return True when a usable Docker CLI/daemon is present."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _force_remove_container() -> None:
    """Best-effort removal of the throwaway container; never raises."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", _CONTAINER_NAME],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _wait_until_ready(timeout_s: float = 60.0) -> None:
    """Poll ``pg_isready`` inside the container until it accepts connections."""
    deadline = time.monotonic() + timeout_s
    last_output = ""
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    _CONTAINER_NAME,
                    "pg_isready",
                    "-U",
                    _PG_USER,
                    "-d",
                    _PG_DB,
                ],
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
    """Apply the real Alembic migration via the project's CLI against the DSN."""
    env = {
        **os.environ,
        "DATABASE_URL": _TEST_DSN,
        # Dummy values so app.config's required fields validate; unused here.
        "REDIS_URL": "redis://localhost:6379/0",
        "ENCRYPTION_KEY": "test-encryption-key-value-0123456789",
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


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Start a throwaway Postgres, apply migrations, yield the DSN, tear down.

    Skips the whole module gracefully when Docker is not available so the suite
    still passes in environments without a Docker daemon.
    """
    if not _docker_available():
        pytest.skip("Docker CLI/daemon not available")

    # Clean up any leftover container from a previous interrupted run.
    _force_remove_container()

    try:
        start = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                _CONTAINER_NAME,
                "-e",
                f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e",
                f"POSTGRES_USER={_PG_USER}",
                "-e",
                f"POSTGRES_DB={_PG_DB}",
                "-p",
                f"{_HOST_PORT}:5432",
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
async def engine(migrated_database: str) -> AsyncEngine:
    """A FRESH ``NullPool`` async engine bound to the test DSN.

    ``NullPool`` ensures no connection outlives a test, so the container can be
    removed cleanly at module teardown without dangling connections.
    """
    eng = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    """Async session factory bound to the throwaway engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


# --- FK prerequisite helpers -------------------------------------------------

async def _insert_user(conn: sa.ext.asyncio.AsyncConnection, email: str) -> uuid.UUID:
    """Insert a minimal user row and return its id (FK target for user_id)."""
    uid = uuid.uuid4()
    await conn.execute(
        sa.text(
            "INSERT INTO users (id, email, name, auth_provider, is_superadmin) "
            "VALUES (:id, :email, :name, 'google', false)"
        ),
        {"id": uid, "email": email, "name": "Test User"},
    )
    return uid


async def _insert_workspace(
    conn: sa.ext.asyncio.AsyncConnection, owner_id: uuid.UUID, slug: str
) -> uuid.UUID:
    """Insert a workspace owned by ``owner_id`` (created_by_user_id is NOT NULL)."""
    wid = uuid.uuid4()
    await conn.execute(
        sa.text(
            "INSERT INTO workspaces (id, name, slug, created_by_user_id) "
            "VALUES (:id, :name, :slug, :owner)"
        ),
        {"id": wid, "name": "WS", "slug": slug, "owner": owner_id},
    )
    return wid


# --- record() appends entries for the three representative cases -------------

@pytest.mark.asyncio
async def test_record_appends_workspace_created(
    engine: AsyncEngine, sessionmaker: async_sessionmaker
) -> None:
    """record() appends a state-changing workspace op with the right fields (15.1)."""
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")
        wid = await _insert_workspace(conn, uid, f"ws-{uuid.uuid4().hex[:12]}")

    async with sessionmaker() as session:
        entry = await audit_service.record(
            session,
            workspace_id=wid,
            user_id=uid,
            action=audit_service.ACTION_WORKSPACE_CREATED,
        )
        await session.commit()
        entry_id = entry.id

    # Read back through a fresh connection to confirm durable persistence.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, user_id, action FROM system_audit_logs "
                    "WHERE id = :id"
                ),
                {"id": entry_id},
            )
        ).first()

    assert row is not None
    assert row[0] == wid
    assert row[1] == uid
    assert row[2] == "workspace.created"


@pytest.mark.asyncio
async def test_record_appends_auth_logout_with_null_workspace(
    engine: AsyncEngine, sessionmaker: async_sessionmaker
) -> None:
    """record() appends an auth event; workspace_id may be NULL (15.2)."""
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")

    async with sessionmaker() as session:
        entry = await audit_service.record(
            session,
            workspace_id=None,  # auth events are not workspace-scoped
            user_id=uid,
            action=audit_service.ACTION_AUTH_LOGOUT,
        )
        await session.commit()
        entry_id = entry.id

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, user_id, action FROM system_audit_logs "
                    "WHERE id = :id"
                ),
                {"id": entry_id},
            )
        ).first()

    assert row is not None
    assert row[0] is None
    assert row[1] == uid
    assert row[2] == "auth.logout"


@pytest.mark.asyncio
async def test_record_appends_approval_approved(
    engine: AsyncEngine, sessionmaker: async_sessionmaker
) -> None:
    """record() appends an approval resolution with the right fields (15.3)."""
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")
        wid = await _insert_workspace(conn, uid, f"ws-{uuid.uuid4().hex[:12]}")

    async with sessionmaker() as session:
        entry = await audit_service.record(
            session,
            workspace_id=wid,
            user_id=uid,
            action=audit_service.ACTION_APPROVAL_APPROVED,
        )
        await session.commit()
        entry_id = entry.id

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, user_id, action FROM system_audit_logs "
                    "WHERE id = :id"
                ),
                {"id": entry_id},
            )
        ).first()

    assert row is not None
    assert row[0] == wid
    assert row[1] == uid
    assert row[2] == "approval.approved"


# --- Metadata scrubbing (Req 15.4) -------------------------------------------

@pytest.mark.asyncio
async def test_record_scrubs_secret_metadata(
    engine: AsyncEngine, sessionmaker: async_sessionmaker
) -> None:
    """Secret-looking keys are redacted before persistence; raw values never land."""
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")
        wid = await _insert_workspace(conn, uid, f"ws-{uuid.uuid4().hex[:12]}")

    # Distinct, easily-searchable secret values so we can prove they never land.
    secret_access = f"ACCESS-{uuid.uuid4().hex}"
    secret_client = f"CLIENT-{uuid.uuid4().hex}"
    secret_session = f"SESSION-{uuid.uuid4().hex}"
    secret_api = f"APIKEY-{uuid.uuid4().hex}"

    metadata = {
        "access_token": secret_access,
        "client_secret": secret_client,
        "session_token": secret_session,
        "provider": "google",  # non-sensitive, should survive unchanged
        "nested": {"api_key": secret_api, "region": "us-east-1"},
    }

    async with sessionmaker() as session:
        entry = await audit_service.record(
            session,
            workspace_id=wid,
            user_id=uid,
            action="integration.connected",
            metadata=metadata,
        )
        await session.commit()
        entry_id = entry.id

    # Read the raw JSONB back as text so we can assert on the exact stored bytes.
    async with engine.connect() as conn:
        stored_text = await conn.scalar(
            sa.text(
                "SELECT metadata::text FROM system_audit_logs WHERE id = :id"
            ),
            {"id": entry_id},
        )
        stored = json.loads(stored_text)

    # Sensitive keys stored redacted to the module's REDACTED sentinel.
    assert stored["access_token"] == REDACTED
    assert stored["client_secret"] == REDACTED
    assert stored["session_token"] == REDACTED
    assert stored["nested"]["api_key"] == REDACTED
    # Non-sensitive values survive.
    assert stored["provider"] == "google"
    assert stored["nested"]["region"] == "us-east-1"

    # None of the raw secret strings appear anywhere in the persisted column.
    for secret in (secret_access, secret_client, secret_session, secret_api):
        assert secret not in stored_text


# --- Append-only application surface (Req 15.5) ------------------------------

def test_audit_service_exposes_no_mutation_functions() -> None:
    """The module surface is record + read helpers only; no update/delete path."""
    public_names = {name for name in dir(audit_service) if not name.startswith("_")}

    # No update_* / delete_* / remove_* mutation entry points exist anywhere on
    # the module surface — there is no supported code path to mutate a row.
    for name in public_names:
        assert not name.startswith("update_"), f"unexpected mutation fn: {name}"
        assert not name.startswith("delete_"), f"unexpected mutation fn: {name}"
        assert not name.startswith("remove_"), f"unexpected mutation fn: {name}"
        assert not name.startswith("purge_"), f"unexpected mutation fn: {name}"
    for banned in (
        "update_audit_log",
        "delete_audit_log",
        "update",
        "delete",
        "remove",
        "purge",
    ):
        assert not hasattr(audit_service, banned), f"unexpected attr: {banned}"

    # The module's own defined public API (``__all__``) is the write path plus
    # read-only helpers and action-name constants — nothing that mutates.
    exported_fns = {
        name
        for name in audit_service.__all__
        if callable(getattr(audit_service, name))
    }
    assert exported_fns == {
        "record",
        "record_for_context",
        "get_audit_log",
        "list_audit_logs",
    }


# --- Append-only DB enforcement (Req 15.5) -----------------------------------

@pytest.mark.asyncio
async def test_recorded_row_cannot_be_updated(
    engine: AsyncEngine, sessionmaker: async_sessionmaker
) -> None:
    """After record() persists a row, a raw UPDATE is rejected by the trigger."""
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")

    async with sessionmaker() as session:
        entry = await audit_service.record(
            session,
            workspace_id=None,
            user_id=uid,
            action=audit_service.ACTION_AUTH_LOGIN,
        )
        await session.commit()
        entry_id = entry.id

    async with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text(
                    "UPDATE system_audit_logs SET action = 'tampered' WHERE id = :id"
                ),
                {"id": entry_id},
            )
    assert "append-only" in str(excinfo.value).lower()

    # The original row is intact and unchanged.
    async with engine.connect() as conn:
        action = await conn.scalar(
            sa.text("SELECT action FROM system_audit_logs WHERE id = :id"),
            {"id": entry_id},
        )
    assert action == "auth.login"


@pytest.mark.asyncio
async def test_recorded_row_cannot_be_deleted(
    engine: AsyncEngine, sessionmaker: async_sessionmaker
) -> None:
    """After record() persists a row, a raw DELETE is rejected by the trigger."""
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")

    async with sessionmaker() as session:
        entry = await audit_service.record(
            session,
            workspace_id=None,
            user_id=uid,
            action=audit_service.ACTION_AUTH_LOGOUT,
        )
        await session.commit()
        entry_id = entry.id

    async with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text("DELETE FROM system_audit_logs WHERE id = :id"),
                {"id": entry_id},
            )
    assert "append-only" in str(excinfo.value).lower()

    # The row survives the blocked delete.
    async with engine.connect() as conn:
        count = await conn.scalar(
            sa.text("SELECT count(*) FROM system_audit_logs WHERE id = :id"),
            {"id": entry_id},
        )
    assert count == 1
