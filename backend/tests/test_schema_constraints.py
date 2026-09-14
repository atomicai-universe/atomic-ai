"""Integration tests for schema, cascade, and constraints (task 2.4).

These tests run the *real* Alembic migration
(``0001_initial_schema``) against a *real*, throwaway PostgreSQL instance and
assert the resulting schema and its enforced invariants. They deliberately do
NOT use ``Base.metadata.create_all`` because that path would not create the
append-only ``system_audit_logs`` trigger, which only the migration emits.

What is asserted:

- Full schema materialization: all ten tables and all eight native ``ENUM``
  types exist after ``alembic upgrade head`` on an empty database
  (Req 1.6, 4.1, 5.7, 7.2, 10.6, 20.1, 20.2).
- Enum rejection: inserting a label outside a native enum's domain raises
  (Req 1.6, 4.1, 5.7, 10.6).
- Referential integrity: a ``workspace_members`` row referencing a nonexistent
  ``workspace_id``/``user_id`` is rejected (Req 20.3).
- ``ON DELETE CASCADE``: deleting a workspace removes its members, invites,
  integrations, rules, agent_sessions, and approval_requests, while a
  ``system_audit_logs`` row for that workspace survives with ``workspace_id``
  set to NULL (Req 20.3, 20.4).
- Append-only trigger: ``UPDATE`` and ``DELETE`` on ``system_audit_logs`` both
  raise (Req 15.5).

Infrastructure:

The module fixture starts a disposable ``postgres:18.6-alpine`` container on a
non-default host port (55434) so it never touches any other database, waits for
readiness via ``pg_isready``, points ``DATABASE_URL`` (plus the other required
config vars) at it, applies migrations with the project's ``alembic`` CLI, and
always tears the container down. If Docker is unavailable the whole module
skips gracefully so the suite still passes without Docker.

Requirements: 1.6, 4.1, 5.7, 7.2, 10.6, 15.5, 20.1, 20.2, 20.3.
"""

from __future__ import annotations

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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# --- Repo layout / container parameters --------------------------------------
# tests/ -> backend/ -> project root
_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_schema_test"
_HOST_PORT = 55434  # deliberately non-default so we never touch another DB
_PG_IMAGE = "postgres:18.6-alpine"
_PG_USER = "test"
_PG_PASSWORD = "test"  # noqa: S105 - throwaway container credential
_PG_DB = "atomic_test"

_TEST_DSN = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}"
    f"@localhost:{_HOST_PORT}/{_PG_DB}"
)

# The 10 tables and 8 enum types the migration must materialize.
_EXPECTED_TABLES = frozenset(
    {
        "users",
        "sessions",
        "workspaces",
        "workspace_members",
        "workspace_invites",
        "integrations",
        "rules",
        "agent_sessions",
        "approval_requests",
        "system_audit_logs",
    }
)
_EXPECTED_ENUMS = frozenset(
    {
        "auth_provider",
        "member_role",
        "invite_role",
        "invite_status",
        "integration_category",
        "integration_status",
        "agent_session_status",
        "approval_status",
    }
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
    """Apply the real Alembic migration via the project's CLI against the DSN.

    ``DATABASE_URL`` plus the other required config vars are injected so
    ``app.config`` loads inside the alembic subprocess; env.py reads the DSN
    from settings at runtime.
    """
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
    """A FRESH async engine bound to the test DSN.

    Built directly with ``create_async_engine`` rather than the lru_cached
    ``app.db.session.get_engine`` so it targets the throwaway container and is
    disposed cleanly per test.
    """
    eng = create_async_engine(migrated_database, future=True, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


# --- Helpers to build a connected fixture graph ------------------------------

async def _insert_user(conn: sa.ext.asyncio.AsyncConnection, email: str) -> uuid.UUID:
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
    wid = uuid.uuid4()
    await conn.execute(
        sa.text(
            "INSERT INTO workspaces (id, name, slug, created_by_user_id) "
            "VALUES (:id, :name, :slug, :owner)"
        ),
        {"id": wid, "name": "WS", "slug": slug, "owner": owner_id},
    )
    return wid


# --- Schema materialization (Req 20.1, 20.2, and enum-domain reqs) -----------

@pytest.mark.asyncio
async def test_migration_creates_all_ten_tables(engine: AsyncEngine) -> None:
    """The migration materializes exactly the 10 designed tables (Req 20.1)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        tables = {r[0] for r in rows}

    # Exclude alembic's bookkeeping table from the comparison.
    tables.discard("alembic_version")
    assert tables == _EXPECTED_TABLES, (
        f"missing: {_EXPECTED_TABLES - tables}; extra: {tables - _EXPECTED_TABLES}"
    )


@pytest.mark.asyncio
async def test_migration_creates_all_eight_enum_types(engine: AsyncEngine) -> None:
    """The migration creates the 8 native Postgres ENUM types (Req 20.2)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT typname FROM pg_type WHERE typtype = 'e'"
            )
        )
        enums = {r[0] for r in rows}

    assert _EXPECTED_ENUMS.issubset(enums), f"missing enums: {_EXPECTED_ENUMS - enums}"


@pytest.mark.asyncio
async def test_append_only_trigger_exists(engine: AsyncEngine) -> None:
    """The append-only trigger is installed on system_audit_logs (Req 15.5)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'system_audit_logs' AND NOT t.tgisinternal"
            )
        )
        triggers = {r[0] for r in rows}
    assert "trg_system_audit_logs_append_only" in triggers


# --- Enum rejection (Req 1.6, 4.1, 5.7, 10.6) --------------------------------

@pytest.mark.asyncio
async def test_invalid_enum_label_is_rejected(engine: AsyncEngine) -> None:
    """An out-of-domain enum label (auth_provider) is rejected by Postgres."""
    async with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text(
                    "INSERT INTO users (id, email, name, auth_provider) "
                    "VALUES (:id, :email, :name, 'facebook')"
                ),
                {
                    "id": uuid.uuid4(),
                    "email": f"{uuid.uuid4()}@x.test",
                    "name": "Bad Provider",
                },
            )
        # asyncpg surfaces an invalid enum label as an InvalidTextRepresentation.
        assert "facebook" in str(excinfo.value) or "invalid input value" in str(
            excinfo.value
        )


@pytest.mark.asyncio
async def test_invalid_member_role_enum_is_rejected(engine: AsyncEngine) -> None:
    """A workspace_members row with an invalid role label is rejected."""
    async with engine.connect() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")
        wid = await _insert_workspace(conn, uid, f"ws-{uuid.uuid4().hex[:12]}")
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text(
                    "INSERT INTO workspace_members (id, workspace_id, user_id, role) "
                    "VALUES (:id, :wid, :uid, 'superuser')"
                ),
                {"id": uuid.uuid4(), "wid": wid, "uid": uid},
            )
        assert "superuser" in str(excinfo.value) or "invalid input value" in str(
            excinfo.value
        )


# --- Referential integrity (Req 20.3) ----------------------------------------

@pytest.mark.asyncio
async def test_member_with_nonexistent_refs_is_rejected(engine: AsyncEngine) -> None:
    """A member row referencing missing workspace/user ids violates FKs."""
    async with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text(
                    "INSERT INTO workspace_members (id, workspace_id, user_id, role) "
                    "VALUES (:id, :wid, :uid, 'member')"
                ),
                {
                    "id": uuid.uuid4(),
                    "wid": uuid.uuid4(),  # nonexistent workspace
                    "uid": uuid.uuid4(),  # nonexistent user
                },
            )
        msg = str(excinfo.value).lower()
        assert "foreign key" in msg or "violates foreign key" in msg


# --- ON DELETE CASCADE + audit-log survival (Req 20.3, 20.4) -----------------

@pytest.mark.asyncio
async def test_workspace_delete_cascades_and_audit_survives(
    engine: AsyncEngine,
) -> None:
    """Deleting a workspace removes dependents; an audit row survives w/ NULL fk.

    Builds user + workspace + owner member + integration + rule + agent_session
    + approval_request + a system_audit_logs row for the workspace, deletes the
    workspace, then asserts every dependent is gone while the audit row remains
    with ``workspace_id`` set to NULL (Req 20.4).
    """
    async with engine.begin() as conn:
        uid = await _insert_user(conn, f"{uuid.uuid4()}@x.test")
        wid = await _insert_workspace(conn, uid, f"ws-{uuid.uuid4().hex[:12]}")

        member_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO workspace_members (id, workspace_id, user_id, role) "
                "VALUES (:id, :wid, :uid, 'owner')"
            ),
            {"id": member_id, "wid": wid, "uid": uid},
        )

        invite_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO workspace_invites "
                "(id, workspace_id, email, role, token, status) "
                "VALUES (:id, :wid, :email, 'member', :token, 'pending')"
            ),
            {
                "id": invite_id,
                "wid": wid,
                "email": "invitee@x.test",
                "token": b"tok-" + uuid.uuid4().bytes,
            },
        )

        integration_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO integrations "
                "(id, workspace_id, created_by_user_id, category, provider_name, "
                " encrypted_access_token, status) "
                "VALUES (:id, :wid, :uid, 'email', 'gmail', :tok, 'active')"
            ),
            {
                "id": integration_id,
                "wid": wid,
                "uid": uid,
                "tok": b"cipher-" + uuid.uuid4().bytes,
            },
        )

        rule_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO rules "
                "(id, workspace_id, created_by_user_id, category, rule_prompt, "
                " is_workspace_wide, is_active) "
                "VALUES (:id, :wid, :uid, 'email', 'be concise', true, true)"
            ),
            {"id": rule_id, "wid": wid, "uid": uid},
        )

        session_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO agent_sessions "
                "(id, workspace_id, triggered_by_user_id, thread_id, status) "
                "VALUES (:id, :wid, :uid, 'thread-1', 'running')"
            ),
            {"id": session_id, "wid": wid, "uid": uid},
        )

        approval_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO approval_requests "
                "(id, workspace_id, agent_session_id, triggered_by_user_id, "
                " tool_name, status) "
                "VALUES (:id, :wid, :sid, :uid, 'send_email', 'pending')"
            ),
            {"id": approval_id, "wid": wid, "sid": session_id, "uid": uid},
        )

        audit_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO system_audit_logs (id, workspace_id, user_id, action) "
                "VALUES (:id, :wid, :uid, 'workspace.created')"
            ),
            {"id": audit_id, "wid": wid, "uid": uid},
        )

    # Delete the workspace; cascades should fire.
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("DELETE FROM workspaces WHERE id = :wid"), {"wid": wid}
        )

    async with engine.connect() as conn:
        # Every workspace-scoped dependent is gone.
        for table, row_id in (
            ("workspace_members", member_id),
            ("workspace_invites", invite_id),
            ("integrations", integration_id),
            ("rules", rule_id),
            ("agent_sessions", session_id),
            ("approval_requests", approval_id),
        ):
            count = await conn.scalar(
                sa.text(f"SELECT count(*) FROM {table} WHERE id = :id"),
                {"id": row_id},
            )
            assert count == 0, f"{table} row survived workspace deletion"

        # The audit row survives with workspace_id nulled out (Req 20.4).
        audit_row = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, action FROM system_audit_logs "
                    "WHERE id = :id"
                ),
                {"id": audit_id},
            )
        ).first()
        assert audit_row is not None, "audit log must survive workspace deletion"
        assert audit_row[0] is None, "surviving audit row must have NULL workspace_id"
        assert audit_row[1] == "workspace.created"


# --- Append-only trigger (Req 15.5) ------------------------------------------

@pytest.mark.asyncio
async def test_audit_log_update_is_rejected(engine: AsyncEngine) -> None:
    """UPDATE on system_audit_logs is blocked by the append-only trigger."""
    audit_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO system_audit_logs (id, action) VALUES (:id, 'seed')"
            ),
            {"id": audit_id},
        )

    async with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text(
                    "UPDATE system_audit_logs SET action = 'tampered' WHERE id = :id"
                ),
                {"id": audit_id},
            )
        assert "append-only" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_audit_log_delete_is_rejected(engine: AsyncEngine) -> None:
    """DELETE on system_audit_logs is blocked by the append-only trigger."""
    audit_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO system_audit_logs (id, action) VALUES (:id, 'seed2')"
            ),
            {"id": audit_id},
        )

    async with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            await conn.execute(
                sa.text("DELETE FROM system_audit_logs WHERE id = :id"),
                {"id": audit_id},
            )
        assert "append-only" in str(excinfo.value).lower()
