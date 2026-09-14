"""Property-based tests for the workspace lifecycle (task 7.2).

This module implements the four workspace lifecycle properties from the design
(design.md "Correctness Properties"), split along a **pure vs. DB-backed** line:

- **Property 13 (PRIMARY)** — *Generated slugs are unique* (Req 3.2). This is a
  pure, DB-free property over :func:`app.services.workspace_service.unique_slug`,
  so it is expressed as a **generative Hypothesis** test (min 100 examples) over
  arbitrary names and arbitrary finite existing-slug sets. This is the only
  property whose full input space can be swept generatively.

- **Properties 12, 14, 15** — these assert behavior of the async, DB-backed
  ``create_workspace`` / ``delete_workspace`` operations against a real Postgres
  schema (native enums, ``ON DELETE CASCADE`` / ``SET NULL`` foreign keys). A
  fresh throwaway container *per Hypothesis example* is impractical, so these are
  written as **example-based** ``@pytest.mark.asyncio`` tests exercising the
  universal claim over a representative, structurally-complete population:
    - **P12** (Req 3.1): after ``create_workspace`` there is exactly ONE
      ``WorkspaceMember`` for the workspace, with ``role=owner`` and
      ``user_id == creator``.
    - **P14** (Req 3.4): one user creating 5 workspaces holds 5 memberships
      across 5 distinct workspaces (nothing limits a user to one workspace).
    - **P15** (Req 3.5, 20.4): a workspace populated with every dependent kind
      (member, invite, integration, rule, agent_session, approval_request) plus a
      ``system_audit_logs`` row, when deleted by an Owner, leaves zero
      workspace-scoped dependents while the audit row SURVIVES with
      ``workspace_id`` NULL.

The DB-backed tests use a module-scoped, throwaway ``postgres:18.6-alpine`` on a
**non-default** host port (55440) with the project's Alembic migration applied,
and skip gracefully when Docker is unavailable. The container helper/fixture
pattern mirrors ``tests/test_workspace_service.py``.

Requirements: 3.1, 3.2, 3.4, 3.5, 20.4.
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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import MemberRole, User, Workspace, WorkspaceMember
from app.services.workspace_service import create_workspace, delete_workspace, unique_slug

# ---------------------------------------------------------------------------
# Property 13 (PRIMARY): Generated slugs are unique (Req 3.2) — pure, generative
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(name=st.text(), existing=st.sets(st.text(), max_size=50))
def test_property13_unique_slug_never_in_existing(name: str, existing: set[str]) -> None:
    """For any name and any finite existing set, the slug is not in that set.

    **Validates: Requirements 3.2**

    This is the core of Property 13: because the candidate stream has an
    unbounded random tail, a fresh slug always exists for any *finite* existing
    set, so ``unique_slug`` returns a value guaranteed absent from it.
    """
    result = unique_slug(name, existing)
    assert result not in existing


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.data_too_large],
)
@given(name=st.text(), iterations=st.integers(min_value=5, max_value=20))
def test_property13_repeated_from_same_name_are_all_distinct(
    name: str, iterations: int
) -> None:
    """Repeatedly resolving the SAME name, accumulating results, yields distinct slugs.

    **Validates: Requirements 3.2**

    Simulates the collision-retry loop: each produced slug is added to the
    existing set before requesting the next, so N iterations must produce N
    distinct slugs (the invariant that lets two workspaces from one name coexist).
    """
    existing: set[str] = set()
    produced: list[str] = []
    for _ in range(iterations):
        slug = unique_slug(name, existing)
        assert slug not in existing
        existing.add(slug)
        produced.append(slug)

    assert len(produced) == iterations
    assert len(set(produced)) == iterations, "every produced slug must be distinct"


# ---------------------------------------------------------------------------
# DB-backed integration infrastructure (Properties 12, 14, 15)
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_workspace_property_test"
_HOST_PORT = 55440  # non-default so we never touch another database
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
    """A committed-per-test session bound to the throwaway database."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


async def _make_user(session: AsyncSession, email: str | None = None) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex}@x.test",
        name="Test User",
        auth_provider="google",
    )
    session.add(user)
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# Property 12: Workspace creation establishes sole Owner membership (Req 3.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_property12_create_establishes_sole_owner(session: AsyncSession) -> None:
    """After create_workspace, exactly one Owner member exists for the creator.

    **Validates: Requirements 3.1**

    Exercised over several structurally-varied names (each a distinct workspace)
    to stand in for the universal "for all users and valid names" claim.
    """
    names = ["Engineering", "Sales & Ops", "Café Team", "   ", "!!!"]
    for name in names:
        user = await _make_user(session)
        workspace = await create_workspace(session, name=name, creator_user_id=user.id)
        await session.commit()

        members = (
            await session.scalars(
                sa.select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace.id
                )
            )
        ).all()

        assert len(members) == 1, f"exactly one member expected for name={name!r}"
        assert members[0].user_id == user.id
        assert members[0].role is MemberRole.OWNER


# ---------------------------------------------------------------------------
# Property 14: Users may belong to many workspaces (Req 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_property14_user_holds_many_memberships(session: AsyncSession) -> None:
    """One user creating 5 workspaces holds 5 memberships across 5 distinct workspaces.

    **Validates: Requirements 3.4**
    """
    user = await _make_user(session)

    created_ids: set[uuid.UUID] = set()
    for i in range(5):
        workspace = await create_workspace(
            session, name=f"Workspace {i}", creator_user_id=user.id
        )
        created_ids.add(workspace.id)
    await session.commit()

    assert len(created_ids) == 5, "each creation yields a distinct workspace"

    memberships = (
        await session.scalars(
            sa.select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        )
    ).all()
    membership_ws_ids = {m.workspace_id for m in memberships}

    assert len(memberships) == 5, "user simultaneously holds 5 memberships"
    assert membership_ws_ids == created_ids
    assert all(m.role is MemberRole.OWNER for m in memberships)


# ---------------------------------------------------------------------------
# Property 15: Workspace deletion cascades to all dependents (Req 3.5, 20.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_property15_delete_cascades_all_dependents(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Deleting a fully-populated workspace removes every dependent; audit survives.

    **Validates: Requirements 3.5, 20.4**

    Populates the workspace with one row of EVERY dependent kind (member, invite,
    integration, rule, agent_session, approval_request) plus a
    ``system_audit_logs`` row, deletes as Owner, then asserts zero
    workspace-scoped dependents remain while the audit row survives with a NULL
    ``workspace_id`` (Req 20.4). Dependents are inserted via raw SQL so the test
    does not depend on ORM models for tables added by later tasks.
    """
    user = await _make_user(session)
    workspace = await create_workspace(session, name="Populated", creator_user_id=user.id)
    await session.flush()
    workspace_id = workspace.id

    # --- Seed one row of every workspace-scoped dependent kind -------------
    invite_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO workspace_invites "
            "(id, workspace_id, email, role, token, status) "
            "VALUES (:id, :wid, :email, 'member', :tok, 'pending')"
        ),
        {
            "id": invite_id,
            "wid": workspace_id,
            "email": "invitee@x.test",
            "tok": b"token-" + uuid.uuid4().bytes,
        },
    )

    integration_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO integrations "
            "(id, workspace_id, created_by_user_id, category, provider_name, "
            " encrypted_access_token, status) "
            "VALUES (:id, :wid, :uid, 'email', 'gmail', :tok, 'active')"
        ),
        {
            "id": integration_id,
            "wid": workspace_id,
            "uid": user.id,
            "tok": b"cipher-" + uuid.uuid4().bytes,
        },
    )

    rule_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO rules "
            "(id, workspace_id, created_by_user_id, category, rule_prompt) "
            "VALUES (:id, :wid, :uid, 'email', 'always be concise')"
        ),
        {"id": rule_id, "wid": workspace_id, "uid": user.id},
    )

    agent_session_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO agent_sessions "
            "(id, workspace_id, triggered_by_user_id, thread_id, status) "
            "VALUES (:id, :wid, :uid, :thread, 'running')"
        ),
        {
            "id": agent_session_id,
            "wid": workspace_id,
            "uid": user.id,
            "thread": f"thread-{uuid.uuid4().hex}",
        },
    )

    approval_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO approval_requests "
            "(id, workspace_id, agent_session_id, triggered_by_user_id, "
            " tool_name, status) "
            "VALUES (:id, :wid, :sid, :uid, 'send_email', 'pending')"
        ),
        {
            "id": approval_id,
            "wid": workspace_id,
            "sid": agent_session_id,
            "uid": user.id,
        },
    )

    audit_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO system_audit_logs (id, workspace_id, user_id, action) "
            "VALUES (:id, :wid, :uid, 'workspace.created')"
        ),
        {"id": audit_id, "wid": workspace_id, "uid": user.id},
    )
    await session.commit()

    # --- Delete as Owner ---------------------------------------------------
    await delete_workspace(
        session, workspace_id=workspace_id, actor_role=MemberRole.OWNER
    )
    await session.commit()

    # --- Assert: zero dependents remain; audit survives with NULL fk -------
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.text("SELECT count(*) FROM workspaces WHERE id = :id"),
                {"id": workspace_id},
            )
            == 0
        )

        dependent_counts = {}
        for table, id_val in (
            ("workspace_members", None),
            ("workspace_invites", invite_id),
            ("integrations", integration_id),
            ("rules", rule_id),
            ("agent_sessions", agent_session_id),
            ("approval_requests", approval_id),
        ):
            count = await conn.scalar(
                sa.text(
                    f"SELECT count(*) FROM {table} WHERE workspace_id = :wid"  # noqa: S608
                ),
                {"wid": workspace_id},
            )
            dependent_counts[table] = count

        assert all(c == 0 for c in dependent_counts.values()), (
            f"all workspace-scoped dependents must cascade-delete: {dependent_counts}"
        )

        audit_row = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, action FROM system_audit_logs WHERE id = :id"
                ),
                {"id": audit_id},
            )
        ).first()
        assert audit_row is not None, "audit row must survive workspace deletion"
        assert audit_row[0] is None, "surviving audit row must have NULL workspace_id"
        assert audit_row[1] == "workspace.created"
