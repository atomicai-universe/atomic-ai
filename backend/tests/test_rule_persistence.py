"""DB-backed persistence tests for the Rules_Service (task 10.4).

Where ``test_rules_service.py`` covers the pure resolver plus a single
create/list round-trip, this module focuses squarely on **persistence
fidelity** (Req 8.1): it drives every ``rules_service`` persistence operation
against a *real*, throwaway ``postgres:18.6-alpine`` (on a non-default host
port) with the project's Alembic migration applied, and asserts that **every**
attribute round-trips across the meaningful attribute combinations:

- ``provider_name`` unset (``None``) vs. set;
- ``is_workspace_wide`` ``True`` vs. ``False``;
- ``is_active`` ``True`` vs. ``False``;
- :func:`~app.services.rules_service.update_rule` persists changes to every
  mutable attribute, including *clearing* ``provider_name`` via
  ``update_provider_name=True``;
- :func:`~app.services.rules_service.delete_rule` removes the row so a
  subsequent :func:`~app.services.rules_service.list_rules` no longer returns
  it.

To prove the values truly hit the database (rather than lingering in the
identity map), each round-trip assertion is made against a *fresh* session
bound to the same engine, after committing the writing session and expiring
its state.

The module skips gracefully when Docker is unavailable. It reuses the
container-fixture pattern from ``test_rules_service.py`` but with its own
container name and host port so the two suites never collide.

Requirements: 8.1.
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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.rules_service import (
    create_rule,
    delete_rule,
    list_rules,
    update_rule,
)

# ---------------------------------------------------------------------------
# DB-backed persistence test infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_rule_persistence_test"
_HOST_PORT = 55444  # non-default so we never touch another database
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
                [
                    "docker", "exec", _CONTAINER_NAME,
                    "pg_isready", "-U", _PG_USER, "-d", _PG_DB,
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
    env = {
        **os.environ,
        "DATABASE_URL": _TEST_DSN,
        # Dummy values so app.config's required fields validate.
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
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as sess:
        yield sess


async def _make_user_and_workspace(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a user + workspace + owner membership; return (user_id, workspace_id)."""
    from app.db.models import User
    from app.services.workspace_service import create_workspace

    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
        name="Rule Persistence Tester",
        auth_provider="google",
    )
    session.add(user)
    await session.flush()

    workspace = await create_workspace(
        session, name="Rule Persistence WS", creator_user_id=user.id
    )
    await session.commit()
    return user.id, workspace.id


# ---------------------------------------------------------------------------
# Persistence tests (Req 8.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "is_workspace_wide", "is_active"),
    [
        (None, False, True),
        (None, True, True),
        (None, False, False),
        ("gmail", False, True),
        ("gmail", True, False),
        ("github", True, True),
        ("outlook", False, False),
    ],
)
async def test_create_rule_round_trips_every_attribute(
    session_factory: async_sessionmaker[AsyncSession],
    provider_name: str | None,
    is_workspace_wide: bool,
    is_active: bool,
) -> None:
    """Every attribute of a created rule round-trips through the database (Req 8.1).

    Covers ``provider_name`` unset vs. set, and both boolean flags in both
    states. The read-back happens on a *fresh* session so the assertion is
    against persisted state, not the writing session's identity map.
    """
    async with session_factory() as write_session:
        user_id, workspace_id = await _make_user_and_workspace(write_session)
        created = await create_rule(
            write_session,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            category="email",
            provider_name=provider_name,
            rule_prompt="Keep replies short and professional.",
            is_workspace_wide=is_workspace_wide,
            is_active=is_active,
        )
        await write_session.commit()
        created_id = created.id
        assert created_id is not None
        assert created.created_at is not None

    async with session_factory() as read_session:
        listed = await list_rules(read_session, workspace_id=workspace_id)
        assert len(listed) == 1
        rule = listed[0]
        assert rule.id == created_id
        assert rule.workspace_id == workspace_id
        assert rule.created_by_user_id == user_id
        assert rule.category == "email"
        assert rule.provider_name == provider_name
        assert rule.rule_prompt == "Keep replies short and professional."
        assert rule.is_workspace_wide is is_workspace_wide
        assert rule.is_active is is_active
        assert rule.created_at is not None


@pytest.mark.asyncio
async def test_update_rule_persists_changes_to_every_attribute(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """update_rule persists changes to every mutable attribute (Req 8.1).

    Starts from a provider-scoped, inactive, non-workspace-wide rule and flips
    each mutable attribute, verifying the new values survive a commit and a
    fresh-session read-back.
    """
    async with session_factory() as write_session:
        user_id, workspace_id = await _make_user_and_workspace(write_session)
        created = await create_rule(
            write_session,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            category="email",
            provider_name="gmail",
            rule_prompt="Original prompt.",
            is_workspace_wide=False,
            is_active=False,
        )
        await write_session.commit()
        rule_id = created.id

    async with session_factory() as update_session:
        updated = await update_rule(
            update_session,
            rule_id=rule_id,
            workspace_id=workspace_id,
            category="calendar",
            provider_name="google",
            update_provider_name=True,
            rule_prompt="Updated prompt.",
            is_workspace_wide=True,
            is_active=True,
        )
        await update_session.commit()
        assert updated.id == rule_id

    async with session_factory() as read_session:
        listed = await list_rules(read_session, workspace_id=workspace_id)
        assert len(listed) == 1
        rule = listed[0]
        assert rule.category == "calendar"
        assert rule.provider_name == "google"
        assert rule.rule_prompt == "Updated prompt."
        assert rule.is_workspace_wide is True
        assert rule.is_active is True


@pytest.mark.asyncio
async def test_update_rule_clears_provider_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """update_provider_name=True with provider_name=None clears the scope (Req 8.1)."""
    async with session_factory() as write_session:
        user_id, workspace_id = await _make_user_and_workspace(write_session)
        created = await create_rule(
            write_session,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            category="email",
            provider_name="gmail",
            rule_prompt="Provider-scoped prompt.",
        )
        await write_session.commit()
        rule_id = created.id

    async with session_factory() as update_session:
        await update_rule(
            update_session,
            rule_id=rule_id,
            workspace_id=workspace_id,
            provider_name=None,
            update_provider_name=True,
        )
        await update_session.commit()

    async with session_factory() as read_session:
        listed = await list_rules(read_session, workspace_id=workspace_id)
        assert len(listed) == 1
        assert listed[0].provider_name is None


@pytest.mark.asyncio
async def test_update_rule_leaves_provider_name_untouched_when_not_flagged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without update_provider_name, provider_name is left unchanged (Req 8.1)."""
    async with session_factory() as write_session:
        user_id, workspace_id = await _make_user_and_workspace(write_session)
        created = await create_rule(
            write_session,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            category="email",
            provider_name="gmail",
            rule_prompt="Provider-scoped prompt.",
        )
        await write_session.commit()
        rule_id = created.id

    async with session_factory() as update_session:
        await update_rule(
            update_session,
            rule_id=rule_id,
            workspace_id=workspace_id,
            rule_prompt="Changed only the prompt.",
        )
        await update_session.commit()

    async with session_factory() as read_session:
        listed = await list_rules(read_session, workspace_id=workspace_id)
        assert len(listed) == 1
        assert listed[0].provider_name == "gmail"
        assert listed[0].rule_prompt == "Changed only the prompt."


@pytest.mark.asyncio
async def test_delete_rule_removes_the_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delete_rule removes the row so list_rules no longer returns it (Req 8.1)."""
    async with session_factory() as write_session:
        user_id, workspace_id = await _make_user_and_workspace(write_session)
        created = await create_rule(
            write_session,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            category="email",
            rule_prompt="Doomed rule.",
        )
        await write_session.commit()
        rule_id = created.id

    async with session_factory() as delete_session:
        await delete_rule(
            delete_session, rule_id=rule_id, workspace_id=workspace_id
        )
        await delete_session.commit()

    async with session_factory() as read_session:
        listed = await list_rules(read_session, workspace_id=workspace_id)
        assert listed == []
