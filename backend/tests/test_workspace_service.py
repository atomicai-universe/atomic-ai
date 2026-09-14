"""Tests for the Workspace_Service lifecycle operations (task 7.1).

Two layers of coverage:

1. **Pure unit tests** (no DB, always run): exercise :func:`slugify` and the
   candidate/uniqueness helpers, covering the empty/punctuation fallback,
   unicode transliteration, and the "same name -> different unique slug"
   behavior that underpins Req 3.2.

2. **DB-backed integration tests** against a *real*, throwaway
   ``postgres:18.6-alpine`` on a non-default host port (55437) with the project's
   Alembic migration applied, so ``ON DELETE CASCADE`` and audit-log survival are
   exercised for real (Req 3.5, 20.4). The module skips gracefully when Docker is
   unavailable.

Asserted behaviors:

- ``create_workspace`` persists the workspace AND exactly one Owner membership
  for the creator (Req 3.1).
- Two workspaces created from the SAME name receive DIFFERENT unique slugs
  (Req 3.2).
- A single user may own/join multiple workspaces (Req 3.4).
- ``delete_workspace`` by a non-Owner raises 403; by an Owner it deletes and
  cascades (a dependent integration row is gone) while an audit row survives with
  NULL ``workspace_id`` (Req 3.5, 3.6, 20.4).
- ``switch_active_workspace`` to a workspace the user is NOT a member of raises
  404; to one they belong to returns it (Req 3.3, 3.7).

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 20.4.
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.errors import APIError
from app.db.models import (
    IntegrationStatus,
    MemberRole,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services.workspace_service import (
    create_workspace,
    delete_workspace,
    generate_slug_candidates,
    slugify,
    switch_active_workspace,
    unique_slug,
)

# ---------------------------------------------------------------------------
# Pure unit tests for slug generation (no DB) — always run
# ---------------------------------------------------------------------------


def test_slugify_basic_lowercases_and_hyphenates() -> None:
    assert slugify("My Cool Workspace") == "my-cool-workspace"


def test_slugify_collapses_punctuation_and_trims() -> None:
    assert slugify("  Hello, World!!  ") == "hello-world"
    assert slugify("a---b___c") == "a-b-c"


def test_slugify_transliterates_unicode_to_ascii() -> None:
    assert slugify("Café Déjà Vu") == "cafe-deja-vu"


def test_slugify_empty_or_punctuation_falls_back() -> None:
    # Empty, whitespace, and all-punctuation inputs must still yield a valid slug.
    assert slugify("") == "workspace"
    assert slugify("   ") == "workspace"
    assert slugify("!!!") == "workspace"
    # A name that transliterates to nothing (no ASCII representation).
    assert slugify("日本語") == "workspace"


def test_generate_slug_candidates_are_distinct_and_start_with_base() -> None:
    gen = generate_slug_candidates("Sales Team")
    first = next(gen)
    assert first == "sales-team"
    # Next few are readable numeric suffixes and all distinct.
    following = [next(gen) for _ in range(5)]
    assert following[0] == "sales-team-2"
    assert len({first, *following}) == 6


def test_unique_slug_avoids_existing_set() -> None:
    # Property 13 basis: the produced slug is never already present.
    existing = {"sales-team", "sales-team-2", "sales-team-3"}
    result = unique_slug("Sales Team", existing)
    assert result not in existing
    assert result == "sales-team-4"


def test_unique_slug_same_name_differs_from_first() -> None:
    # Two workspaces from the same name resolve to different slugs (Req 3.2).
    first = unique_slug("Acme", set())
    second = unique_slug("Acme", {first})
    assert first != second
    assert first == "acme"


# ---------------------------------------------------------------------------
# DB-backed integration test infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_workspace_service_test"
_HOST_PORT = 55437  # non-default so we never touch another database
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
# create_workspace (Req 3.1, 3.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_establishes_sole_owner(session: AsyncSession) -> None:
    """create_workspace persists the workspace and exactly one Owner member (Req 3.1)."""
    user = await _make_user(session)

    workspace = await create_workspace(
        session, name="Engineering", creator_user_id=user.id
    )
    await session.commit()

    assert workspace.id is not None
    assert workspace.slug == "engineering"

    members = (
        await session.scalars(
            sa.select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id
            )
        )
    ).all()
    assert len(members) == 1
    assert members[0].user_id == user.id
    assert members[0].role is MemberRole.OWNER


@pytest.mark.asyncio
async def test_same_name_gets_distinct_unique_slugs(session: AsyncSession) -> None:
    """Two workspaces from the same name receive different unique slugs (Req 3.2)."""
    user = await _make_user(session)

    ws1 = await create_workspace(session, name="Marketing", creator_user_id=user.id)
    await session.commit()
    ws2 = await create_workspace(session, name="Marketing", creator_user_id=user.id)
    await session.commit()

    assert ws1.slug != ws2.slug
    assert ws1.slug == "marketing"
    # Unique constraint holds: both slugs are distinct rows in the DB.
    slugs = (await session.scalars(sa.select(Workspace.slug))).all()
    assert len(slugs) == len(set(slugs))


# ---------------------------------------------------------------------------
# Multi-workspace membership (Req 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_may_own_multiple_workspaces(session: AsyncSession) -> None:
    """A single user can be Owner of many workspaces (Req 3.4)."""
    user = await _make_user(session)

    ws1 = await create_workspace(session, name="Alpha", creator_user_id=user.id)
    ws2 = await create_workspace(session, name="Beta", creator_user_id=user.id)
    ws3 = await create_workspace(session, name="Gamma", creator_user_id=user.id)
    await session.commit()

    memberships = (
        await session.scalars(
            sa.select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        )
    ).all()
    owned_ids = {m.workspace_id for m in memberships}
    assert owned_ids == {ws1.id, ws2.id, ws3.id}
    assert all(m.role is MemberRole.OWNER for m in memberships)


# ---------------------------------------------------------------------------
# delete_workspace (Req 3.5, 3.6, 20.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_workspace_by_non_owner_raises_403(session: AsyncSession) -> None:
    """A non-Owner deletion attempt is rejected with 403 (Req 3.6)."""
    user = await _make_user(session)
    workspace = await create_workspace(session, name="Ops", creator_user_id=user.id)
    await session.commit()

    with pytest.raises(APIError) as excinfo:
        await delete_workspace(
            session, workspace_id=workspace.id, actor_role=MemberRole.ADMIN
        )
    assert excinfo.value.status_code == 403

    # Workspace still exists — nothing was deleted.
    still_there = await session.get(Workspace, workspace.id)
    assert still_there is not None


@pytest.mark.asyncio
async def test_delete_workspace_by_owner_cascades_and_audit_survives(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Owner deletion cascades to dependents; audit row survives with NULL fk."""
    user = await _make_user(session)
    workspace = await create_workspace(session, name="Support", creator_user_id=user.id)
    await session.flush()

    # A dependent integration row that must be cascade-deleted (Req 3.5, 20.4).
    integration_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO integrations "
            "(id, workspace_id, created_by_user_id, category, provider_name, "
            " encrypted_access_token, status) "
            "VALUES (:id, :wid, :uid, 'email', 'gmail', :tok, :status)"
        ),
        {
            "id": integration_id,
            "wid": workspace.id,
            "uid": user.id,
            "tok": b"cipher-" + uuid.uuid4().bytes,
            "status": IntegrationStatus.ACTIVE.value,
        },
    )
    # An audit row that must SURVIVE with workspace_id set to NULL (Req 20.4).
    audit_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO system_audit_logs (id, workspace_id, user_id, action) "
            "VALUES (:id, :wid, :uid, 'workspace.created')"
        ),
        {"id": audit_id, "wid": workspace.id, "uid": user.id},
    )
    await session.commit()

    workspace_id = workspace.id
    await delete_workspace(
        session, workspace_id=workspace_id, actor_role=MemberRole.OWNER
    )
    await session.commit()

    # Verify cascade + survival on a fresh connection.
    async with engine.connect() as conn:
        ws_count = await conn.scalar(
            sa.text("SELECT count(*) FROM workspaces WHERE id = :id"),
            {"id": workspace_id},
        )
        assert ws_count == 0

        integ_count = await conn.scalar(
            sa.text("SELECT count(*) FROM integrations WHERE id = :id"),
            {"id": integration_id},
        )
        assert integ_count == 0, "dependent integration must cascade-delete"

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


@pytest.mark.asyncio
async def test_delete_missing_workspace_raises_404(session: AsyncSession) -> None:
    """Deleting a nonexistent workspace as Owner surfaces 404."""
    with pytest.raises(APIError) as excinfo:
        await delete_workspace(
            session, workspace_id=uuid.uuid4(), actor_role=MemberRole.OWNER
        )
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# switch_active_workspace (Req 3.3, 3.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_to_member_workspace_returns_it(session: AsyncSession) -> None:
    """Switching to a workspace the caller belongs to returns it (Req 3.3, 3.7)."""
    user = await _make_user(session)
    workspace = await create_workspace(session, name="Design", creator_user_id=user.id)
    await session.commit()

    resolved = await switch_active_workspace(session, user.id, workspace.id)
    assert resolved.id == workspace.id


@pytest.mark.asyncio
async def test_switch_to_non_member_workspace_raises(session: AsyncSession) -> None:
    """Switching to a workspace the caller is not a member of raises 404 (Req 3.3)."""
    owner = await _make_user(session)
    outsider = await _make_user(session)
    workspace = await create_workspace(session, name="Private", creator_user_id=owner.id)
    await session.commit()

    with pytest.raises(APIError) as excinfo:
        await switch_active_workspace(session, outsider.id, workspace.id)
    assert excinfo.value.status_code == 404
