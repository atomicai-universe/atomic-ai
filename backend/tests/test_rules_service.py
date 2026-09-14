"""Tests for the Rules_Service (task 10.1).

Two layers of coverage:

1. **Pure resolver unit tests** (no DB, primary): exercise
   :func:`~app.services.rules_service.applicable_rules` /
   :func:`~app.services.rules_service.rule_matches` against a lightweight
   in-memory Rule-like fake, covering every branch of the resolver semantics
   (Req 8.2, 8.3, 8.4):

   - an active workspace-wide rule is always included;
   - an inactive rule is excluded even when workspace-wide;
   - a category-matching rule is included; a category-mismatching one excluded;
   - a provider-scoped rule is included only for its provider, excluded for
     another provider;
   - a rule with ``provider_name is None`` applies to any provider within its
     category.

   (Task 10.2 layers the Hypothesis Property 21 test on top of these examples.)

2. **DB-backed persistence test** against a *real*, throwaway
   ``postgres:18.6-alpine`` on a non-default host port (55439) with the
   project's Alembic migration applied, so ``create_rule`` persistence and
   ``list_rules`` retrieval are exercised for real (Req 8.1). The module skips
   gracefully when Docker is unavailable.

Requirements: 8.1, 8.2, 8.3, 8.4.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
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
    RuleContext,
    applicable_rules,
    create_rule,
    list_rules,
    rule_matches,
)


# ---------------------------------------------------------------------------
# Pure resolver unit tests (no DB) — always run
# ---------------------------------------------------------------------------


@dataclass
class FakeRule:
    """Minimal Rule-like object exposing exactly what the resolver reads."""

    category: str
    provider_name: str | None = None
    is_workspace_wide: bool = False
    is_active: bool = True


def test_active_workspace_wide_rule_always_included() -> None:
    """An active workspace-wide rule applies regardless of category/provider (Req 8.2)."""
    rule = FakeRule(category="email", is_workspace_wide=True, is_active=True)
    # Context category/provider differ from the rule's — still applies.
    ctx = RuleContext(category="calendar", provider_name="google")
    assert rule_matches(rule, ctx) is True
    assert applicable_rules([rule], ctx) == [rule]


def test_workspace_wide_rule_with_category_still_included() -> None:
    """Workspace-wide wins even when the rule also carries a category/provider (Req 8.2)."""
    rule = FakeRule(
        category="email",
        provider_name="gmail",
        is_workspace_wide=True,
        is_active=True,
    )
    ctx = RuleContext(category="chat", provider_name="slack")
    assert applicable_rules([rule], ctx) == [rule]


def test_inactive_rule_excluded_even_if_workspace_wide() -> None:
    """An inactive rule never applies, even when workspace-wide (Req 8.3)."""
    rule = FakeRule(category="email", is_workspace_wide=True, is_active=False)
    ctx = RuleContext(category="email", provider_name="gmail")
    assert rule_matches(rule, ctx) is False
    assert applicable_rules([rule], ctx) == []


def test_category_match_included() -> None:
    """An active category-scoped rule applies when the category matches (Req 8.4)."""
    rule = FakeRule(category="email", is_active=True)
    ctx = RuleContext(category="email", provider_name="gmail")
    assert applicable_rules([rule], ctx) == [rule]


def test_category_mismatch_excluded() -> None:
    """A category-scoped rule does not apply to a different category (Req 8.4)."""
    rule = FakeRule(category="email", is_active=True)
    ctx = RuleContext(category="calendar", provider_name="google")
    assert rule_matches(rule, ctx) is False
    assert applicable_rules([rule], ctx) == []


def test_provider_specific_rule_included_for_matching_provider() -> None:
    """A provider-scoped rule applies only for its provider (Req 8.4)."""
    rule = FakeRule(category="email", provider_name="gmail", is_active=True)
    ctx = RuleContext(category="email", provider_name="gmail")
    assert applicable_rules([rule], ctx) == [rule]


def test_provider_specific_rule_excluded_for_other_provider() -> None:
    """A provider-scoped rule does not apply to a different provider (Req 8.4)."""
    rule = FakeRule(category="email", provider_name="gmail", is_active=True)
    ctx = RuleContext(category="email", provider_name="outlook")
    assert rule_matches(rule, ctx) is False
    assert applicable_rules([rule], ctx) == []


def test_provider_none_rule_included_for_any_provider_in_category() -> None:
    """A rule with provider_name None applies to any provider in its category (Req 8.4)."""
    rule = FakeRule(category="email", provider_name=None, is_active=True)
    for provider in ("gmail", "outlook", None):
        ctx = RuleContext(category="email", provider_name=provider)
        assert applicable_rules([rule], ctx) == [rule]


def test_resolver_selects_only_applicable_and_preserves_order() -> None:
    """Across a mixed set, only applicable rules are returned, in input order."""
    ws_wide = FakeRule(category="chat", is_workspace_wide=True, is_active=True)
    inactive_ws_wide = FakeRule(
        category="email", is_workspace_wide=True, is_active=False
    )
    cat_match = FakeRule(category="email", is_active=True)
    cat_miss = FakeRule(category="calendar", is_active=True)
    prov_match = FakeRule(category="email", provider_name="gmail", is_active=True)
    prov_miss = FakeRule(category="email", provider_name="outlook", is_active=True)

    rules = [ws_wide, inactive_ws_wide, cat_match, cat_miss, prov_match, prov_miss]
    ctx = RuleContext(category="email", provider_name="gmail")

    # Included: ws_wide (workspace-wide), cat_match (category match, no provider),
    # prov_match (provider match). Order preserved.
    assert applicable_rules(rules, ctx) == [ws_wide, cat_match, prov_match]


# ---------------------------------------------------------------------------
# DB-backed persistence test infrastructure
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_rules_service_test"
_HOST_PORT = 55439  # non-default so we never touch another database
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
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as sess:
        yield sess


async def _make_user_and_workspace(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a user + workspace + owner membership; return (user_id, workspace_id)."""
    # Imported here so the pure tests never touch the ORM/DB models.
    from app.db.models import User
    from app.services.workspace_service import create_workspace

    user = User(
        email=f"{uuid.uuid4().hex}@x.test",
        name="Rules Tester",
        auth_provider="google",
    )
    session.add(user)
    await session.flush()

    workspace = await create_workspace(
        session, name="Rules WS", creator_user_id=user.id
    )
    await session.commit()
    return user.id, workspace.id


@pytest.mark.asyncio
async def test_create_rule_persists_all_attributes_and_list_returns_it(
    session: AsyncSession,
) -> None:
    """create_rule persists every attribute; list_rules returns it (Req 8.1)."""
    user_id, workspace_id = await _make_user_and_workspace(session)

    created = await create_rule(
        session,
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        category="email",
        provider_name="gmail",
        rule_prompt="Always be concise and never send before 9am.",
        is_workspace_wide=True,
        is_active=True,
    )
    await session.commit()

    assert created.id is not None
    assert created.created_at is not None

    listed = await list_rules(session, workspace_id=workspace_id)
    assert len(listed) == 1
    rule = listed[0]
    assert rule.id == created.id
    assert rule.workspace_id == workspace_id
    assert rule.created_by_user_id == user_id
    assert rule.category == "email"
    assert rule.provider_name == "gmail"
    assert rule.rule_prompt == "Always be concise and never send before 9am."
    assert rule.is_workspace_wide is True
    assert rule.is_active is True
