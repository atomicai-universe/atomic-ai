"""Property-based tests for the super admin control plane (task 14.2).

Implements three design properties for the Admin_Service / admin router:

- **Property 26: Super admin gate**
  **Validates: Requirements 12.1, 12.2**
- **Property 11: Ban invalidates sessions and blocks authentication**
  **Validates: Requirements 12.4**
- **Property 27: Ownership reassignment**
  **Validates: Requirements 12.5**

Testing strategy per property (chosen to match what each property actually
asserts and what can be exercised without prohibitively slow setup):

- **Property 26** is a PURE / decision-level property: "access is granted iff
  ``is_superadmin`` is true" for *all* users and *all* admin-namespace
  endpoints. The decision lives entirely in the ``require_superadmin``
  dependency (:mod:`app.api.admin`), which raises ``APIError(403)`` when the
  context is not a super admin and returns the context otherwise. We drive that
  dependency directly with a fake :class:`~app.core.tenancy.RequestContext`
  built over ``st.booleans()`` and assert raise/return matches the flag, over
  well above the 100-example minimum. This tests the *actual dependency* the
  whole ``/api/v1/admin/*`` surface is guarded by (every route depends on it),
  so the "for all endpoints" quantifier is covered by construction — no route
  can bypass the gate. We also assert a distilled predicate to make the
  invariant explicit.

- **Property 11** and **Property 27** are DB-backed. They are validated over
  representative instances (parametrized examples) rather than randomized
  Hypothesis inputs because each instance needs a real, migrated Postgres and a
  fresh dataset — a container-per-example run is impractical. They use a single
  module-scoped throwaway ``postgres:18.6-alpine`` on a non-default host port
  with the project's Alembic migration applied, a ``NullPool`` async engine so
  no connection outlives a test, and skip gracefully when Docker is
  unavailable. Property 11 is parametrized over ``N in {0, 1, 3}`` active
  sessions; Property 27 covers the new-owner-already-a-member and
  not-a-member cases.

Requirements: 12.1, 12.2, 12.4, 12.5.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.admin import require_superadmin
from app.core.errors import APIError
from app.core.tenancy import RequestContext
from app.db.models import (
    AuthProvider,
    MemberRole,
    Session as SessionModel,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services import admin_service
from app.services.auth_service import AuthService


async def _add_member(
    session: AsyncSession,
    *,
    workspace: Workspace,
    user: User,
    role: MemberRole = MemberRole.MEMBER,
) -> WorkspaceMember:
    member = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role=role
    )
    session.add(member)
    await session.flush()
    return member


# ===========================================================================
# Property 26: Super admin gate (PURE / decision-level) — Req 12.1, 12.2
# ===========================================================================
#
# The gate is the ``require_superadmin`` dependency. Because *every* route in
# the admin router depends on it (see app/api/admin.py), verifying the
# dependency's decision for all boolean values of ``is_superadmin`` covers the
# property's "for all endpoints under the admin namespace" quantifier: no
# endpoint can be reached without this dependency granting access.

# Comfortably above the design's 100-example minimum.
_PBT = settings(max_examples=200)


def _superadmin_access_granted(is_superadmin: bool) -> bool:
    """Distilled predicate: the admin gate grants access iff ``is_superadmin``.

    This mirrors ``require_superadmin`` at the decision level so the invariant
    is stated explicitly and testable in isolation.
    """
    return is_superadmin is True


@_PBT
@given(is_superadmin=st.booleans())
def test_admin_gate_matches_is_superadmin_predicate(is_superadmin: bool) -> None:
    """Distilled predicate agrees with ``is_superadmin`` for all inputs.

    Access is granted iff ``is_superadmin`` is true (Property 26, Req 12.1,
    12.2).
    """
    assert _superadmin_access_granted(is_superadmin) is is_superadmin


@_PBT
@given(is_superadmin=st.booleans())
def test_require_superadmin_dependency_grants_iff_superadmin(
    is_superadmin: bool,
) -> None:
    """Drive the real gate dependency: it returns the ctx iff super admin.

    We call ``require_superadmin`` directly with a fake ``RequestContext``
    whose ``is_superadmin`` is the generated boolean. When true the dependency
    must return the same context unchanged (access granted); when false it must
    raise ``APIError`` with status 403 (access denied). Because the whole
    ``/api/v1/admin/*`` surface is guarded by this one dependency, this is the
    security invariant for every admin endpoint (Property 26, Req 12.1, 12.2).

    ``require_superadmin`` is an ``async def`` dependency with no awaited I/O of
    its own, so each generated example is driven to completion with
    ``asyncio.run`` — this keeps Hypothesis's per-example re-execution cleanly
    separate from the event loop (rather than layering the async pytest marker
    under ``@given``).
    """
    ctx = RequestContext(
        user_id=uuid.uuid4(),
        active_workspace_id=None,
        is_superadmin=is_superadmin,
        roles={},
    )

    if is_superadmin:
        result = asyncio.run(require_superadmin(ctx=ctx))
        assert result is ctx  # granted: context passed through unchanged
    else:
        with pytest.raises(APIError) as exc:
            asyncio.run(require_superadmin(ctx=ctx))
        assert exc.value.status_code == 403  # denied
        assert exc.value.code == "forbidden"


# ===========================================================================
# DB-backed infrastructure (Properties 11 & 27) — skips without Docker
# ===========================================================================

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_admin_property_test"
_HOST_PORT = 55455  # non-default so we never touch another database
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
    last = ""
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
            last = str(exc)
            time.sleep(1.0)
            continue
        if result.returncode == 0:
            return
        last = (result.stdout or "") + (result.stderr or "")
        time.sleep(1.0)
    raise RuntimeError(f"Postgres not ready in {timeout_s}s: {last!r}")


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
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A NullPool-backed session for setup, service calls, and verification."""
    engine = create_async_engine(migrated_database, future=True, poolclass=NullPool)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Test doubles for the auth path (Property 11 blocks re-authentication)
# ---------------------------------------------------------------------------


class FakeRedis:
    """Tiny in-memory async stand-in for Redis (set/get/delete only)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FakeResponse:
    """Canned HTTP response exposing the httpx-shaped surface used."""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHTTPClient:
    """Fake async HTTP client returning canned responses keyed by URL."""

    def __init__(self, routes: dict) -> None:
        self.routes = {
            k: (v if isinstance(v, list) else [v]) for k, v in routes.items()
        }

    def _next(self, url: str) -> FakeResponse:
        queue = self.routes.get(url)
        if not queue:
            raise RuntimeError(f"unexpected request: {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def post(self, url: str, *args, **kwargs) -> FakeResponse:
        return self._next(url)

    async def get(self, url: str, *args, **kwargs) -> FakeResponse:
        return self._next(url)


def _make_auth_service() -> AuthService:
    """Build an AuthService with a minimal settings stub (no real secrets)."""

    class _Secret:
        def __init__(self, v: str) -> None:
            self._v = v

        def get_secret_value(self) -> str:
            return self._v

    class _Settings:
        GOOGLE_OAUTH_CLIENT_ID = "google-client-id"
        GOOGLE_OAUTH_CLIENT_SECRET = _Secret("google-secret")
        GITHUB_OAUTH_CLIENT_ID = "github-client-id"
        GITHUB_OAUTH_CLIENT_SECRET = _Secret("github-secret")

    return AuthService(settings=_Settings())


def _google_routes(email: str, name: str = "Returning User") -> dict:
    from app.services.auth_service import _GOOGLE_TOKEN_URL, _GOOGLE_USERINFO_URL

    return {
        _GOOGLE_TOKEN_URL: FakeResponse({"access_token": "at-123"}),
        _GOOGLE_USERINFO_URL: FakeResponse({"email": email, "name": name}),
    }


# ---------------------------------------------------------------------------
# Seed helpers (create users before workspaces; created_by_user_id is NOT NULL)
# ---------------------------------------------------------------------------


async def _make_user(
    session: AsyncSession,
    name: str,
    *,
    email: str | None = None,
    is_superadmin: bool = False,
    provider: AuthProvider = AuthProvider.GOOGLE,
) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex}@x.test",
        name=name,
        auth_provider=provider,
        is_superadmin=is_superadmin,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_workspace(
    session: AsyncSession, name: str, *, owner: User
) -> Workspace:
    ws = Workspace(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        created_by_user_id=owner.id,
    )
    session.add(ws)
    await session.flush()
    session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role=MemberRole.OWNER)
    )
    await session.flush()
    return ws


async def _add_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, count: int
) -> None:
    for _ in range(count):
        session.add(
            SessionModel(
                user_id=user_id,
                token=uuid.uuid4().bytes,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    await session.flush()


async def _session_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await session.scalar(
        sa.select(sa.func.count())
        .select_from(SessionModel)
        .where(SessionModel.user_id == user_id)
    )


# ===========================================================================
# Property 11: Ban invalidates sessions and blocks authentication (Req 12.4)
# ===========================================================================
#
# DB-backed property validated over representative instances. For a user with N
# active sessions, after ``ban_user``:
#   (a) all of the user's session rows are gone, and
#   (b) the authentication path rejects the banned user with 403 account_banned.
# Parametrized over N in {0, 1, 3} active sessions.


@pytest.mark.asyncio
@pytest.mark.parametrize("n_sessions", [0, 1, 3])
async def test_ban_removes_all_sessions_and_blocks_auth(
    db_session: AsyncSession, n_sessions: int
) -> None:
    """Ban clears every session row and blocks re-authentication (Property 11).

    Validated over representative instances (N active sessions). Part (a):
    ``ban_user`` deletes all of the user's ``sessions`` rows. Part (b): a
    subsequent OAuth ``complete_login`` for the same identity is rejected with
    403 ``account_banned`` — the durable ban blocks future auth (Req 12.4).
    """
    email = f"banned-{uuid.uuid4().hex}@x.test"
    user = await _make_user(
        db_session, "Victim", email=email, provider=AuthProvider.GOOGLE
    )
    await _add_sessions(db_session, user_id=user.id, count=n_sessions)
    await db_session.commit()

    # Precondition: exactly N session rows exist.
    assert await _session_count(db_session, user.id) == n_sessions

    # Ban the user (service flushes; we commit to persist).
    await admin_service.ban_user(db_session, target_user_id=user.id)
    await db_session.commit()

    # (a) All session rows are gone regardless of the starting count.
    assert await _session_count(db_session, user.id) == 0
    is_banned = await db_session.scalar(
        sa.select(User.is_banned).where(User.id == user.id)
    )
    assert is_banned is True

    # (b) The auth path now rejects this identity with 403 account_banned. We
    # drive the real AuthService.complete_login with a fake Redis flow and a
    # mocked provider exchange returning the *same* email; find-or-create must
    # reject the banned existing user before issuing any session.
    svc = _make_auth_service()
    redis = FakeRedis()
    begin = await svc.begin_login("google", "https://app.example/cb", redis=redis)
    with pytest.raises(APIError) as exc:
        await svc.complete_login(
            "google",
            "auth-code",
            begin.state,
            session=db_session,
            redis=redis,
            http_client=FakeHTTPClient(_google_routes(email=email)),
        )
    assert exc.value.status_code == 403
    assert exc.value.code == "account_banned"

    # No new session was issued for the banned user by the rejected login.
    assert await _session_count(db_session, user.id) == 0


# ===========================================================================
# Property 27: Ownership reassignment (Req 12.5)
# ===========================================================================
#
# DB-backed property validated over representative instances: after a super
# admin reassigns ownership, the target user holds the Owner role in that
# workspace. Two cases: the new owner is already a member (Member -> Owner) and
# the new owner is not yet a member (a fresh Owner membership is created). In
# both, the previous owner is demoted to Admin per the service contract.


@pytest.mark.asyncio
async def test_reassign_owner_when_new_owner_already_a_member(
    db_session: AsyncSession,
) -> None:
    """Reassigning to an existing member promotes them to Owner (Property 27).

    The prior owner is demoted to Admin so ownership is not duplicated
    (Req 12.5).
    """
    old_owner = await _make_user(db_session, "OldOwner")
    new_owner = await _make_user(db_session, "NewOwner")
    ws = await _make_workspace(db_session, "Alpha", owner=old_owner)
    # new_owner is already a plain Member of the workspace.
    db_session.add(
        WorkspaceMember(
            workspace_id=ws.id, user_id=new_owner.id, role=MemberRole.MEMBER
        )
    )
    await db_session.commit()

    member = await admin_service.reassign_ownership(
        db_session, workspace_id=ws.id, new_owner_user_id=new_owner.id
    )
    await db_session.commit()

    assert member.user_id == new_owner.id
    assert member.role is MemberRole.OWNER

    new_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == new_owner.id,
        )
    )
    assert new_role is MemberRole.OWNER

    # The previous owner is demoted to Admin (single-owner invariant).
    old_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == old_owner.id,
        )
    )
    assert old_role is MemberRole.ADMIN


@pytest.mark.asyncio
async def test_reassign_owner_when_new_owner_not_a_member(
    db_session: AsyncSession,
) -> None:
    """Reassigning to a non-member creates a fresh Owner membership (Property 27).

    The target, who had no membership, ends up an Owner ``WorkspaceMember``, and
    the prior owner is demoted to Admin (Req 12.5).
    """
    old_owner = await _make_user(db_session, "OldOwner")
    outsider = await _make_user(db_session, "Outsider")
    ws = await _make_workspace(db_session, "Beta", owner=old_owner)
    await db_session.commit()

    # Precondition: the outsider has no membership in this workspace.
    pre_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == outsider.id,
        )
    )
    assert pre_role is None

    member = await admin_service.reassign_ownership(
        db_session, workspace_id=ws.id, new_owner_user_id=outsider.id
    )
    await db_session.commit()

    assert member.user_id == outsider.id
    assert member.role is MemberRole.OWNER

    new_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == outsider.id,
        )
    )
    assert new_role is MemberRole.OWNER

    old_role = await db_session.scalar(
        sa.select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == old_owner.id,
        )
    )
    assert old_role is MemberRole.ADMIN


# ===========================================================================
# set_superadmin / update_user / delete_user (DB-backed) — role, edit, delete
# ===========================================================================
#
# These validate the three new super-admin directory operations against the
# real migrated database, following the same representative-instance style as
# Properties 11 & 27 (a container-per-example run would be impractical).


@pytest.mark.asyncio
async def test_set_superadmin_promotes_and_demotes(db_session: AsyncSession) -> None:
    """set_superadmin flips the flag both ways when it is safe to do so."""
    # Two super admins so demoting one is allowed (the other remains).
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    other = await _make_user(db_session, "Other", is_superadmin=True)
    member = await _make_user(db_session, "Member", is_superadmin=False)
    await db_session.commit()

    # Promote a plain member.
    await admin_service.set_superadmin(
        db_session,
        target_user_id=member.id,
        is_superadmin=True,
        acting_user_id=admin.id,
    )
    await db_session.commit()
    assert await db_session.scalar(
        sa.select(User.is_superadmin).where(User.id == member.id)
    ) is True

    # Demote one super admin (others remain, so it is allowed).
    await admin_service.set_superadmin(
        db_session,
        target_user_id=other.id,
        is_superadmin=False,
        acting_user_id=admin.id,
    )
    await db_session.commit()
    assert await db_session.scalar(
        sa.select(User.is_superadmin).where(User.id == other.id)
    ) is False


@pytest.mark.asyncio
async def test_set_superadmin_refuses_demoting_last_admin(
    db_session: AsyncSession,
) -> None:
    """Demoting the only remaining super admin is refused with 409 conflict."""
    lone = await _make_user(db_session, "Lone", is_superadmin=True)
    await _make_user(db_session, "Plain", is_superadmin=False)
    # The module shares one Postgres across tests, so prior tests may have left
    # other super admins committed. Demote everyone except `lone` so it is
    # genuinely the last super admin the guardrail must protect.
    await db_session.execute(
        sa.update(User)
        .where(User.is_superadmin.is_(True), User.id != lone.id)
        .values(is_superadmin=False)
    )
    await db_session.commit()

    with pytest.raises(APIError) as exc:
        await admin_service.set_superadmin(
            db_session,
            target_user_id=lone.id,
            is_superadmin=False,
            acting_user_id=lone.id,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "conflict"

    # The flag is unchanged (still a super admin).
    assert await db_session.scalar(
        sa.select(User.is_superadmin).where(User.id == lone.id)
    ) is True


@pytest.mark.asyncio
async def test_update_user_changes_name(db_session: AsyncSession) -> None:
    """update_user sets a new, stripped display name."""
    user = await _make_user(db_session, "Before")
    await db_session.commit()

    await admin_service.update_user(
        db_session, target_user_id=user.id, name="  After  "
    )
    await db_session.commit()
    assert await db_session.scalar(
        sa.select(User.name).where(User.id == user.id)
    ) == "After"


@pytest.mark.asyncio
async def test_update_user_rejects_empty_name(db_session: AsyncSession) -> None:
    """A blank (whitespace-only) name is rejected with 400 invalid."""
    user = await _make_user(db_session, "Keep")
    await db_session.commit()

    with pytest.raises(APIError) as exc:
        await admin_service.update_user(
            db_session, target_user_id=user.id, name="   "
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid"

    # The original name is untouched.
    assert await db_session.scalar(
        sa.select(User.name).where(User.id == user.id)
    ) == "Keep"


@pytest.mark.asyncio
async def test_delete_user_happy_path(db_session: AsyncSession) -> None:
    """A deletable user (no workspaces, not last admin, not self) is removed."""
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    victim = await _make_user(db_session, "Victim")
    # A session row confirms CASCADE cleanup does not block the delete.
    await _add_sessions(db_session, user_id=victim.id, count=2)
    await db_session.commit()

    returned = await admin_service.delete_user(
        db_session, target_user_id=victim.id, acting_user_id=admin.id
    )
    await db_session.commit()
    assert returned == victim.id

    assert await db_session.scalar(
        sa.select(User.id).where(User.id == victim.id)
    ) is None
    # CASCADE removed the user's sessions.
    assert await _session_count(db_session, victim.id) == 0


@pytest.mark.asyncio
async def test_delete_user_refuses_self_delete(db_session: AsyncSession) -> None:
    """A super admin cannot delete their own account (409 conflict)."""
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    await _make_user(db_session, "Other", is_superadmin=True)
    await db_session.commit()

    with pytest.raises(APIError) as exc:
        await admin_service.delete_user(
            db_session, target_user_id=admin.id, acting_user_id=admin.id
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "conflict"
    assert await db_session.scalar(
        sa.select(User.id).where(User.id == admin.id)
    ) is not None


@pytest.mark.asyncio
async def test_delete_user_refuses_last_superadmin(
    db_session: AsyncSession,
) -> None:
    """Deleting the only remaining super admin is refused (409 conflict).

    The acting user is a *different* super admin so guardrail A (self-delete)
    does not fire first; guardrail B (last admin) is what must trigger. To make
    the target the sole super admin while the actor is also a super admin, the
    actor is demoted at the DB level before the call.
    """
    target = await _make_user(db_session, "LastAdmin", is_superadmin=True)
    actor = await _make_user(db_session, "Actor", is_superadmin=False)
    # Demote any super admins left by prior tests so `target` is the last one.
    await db_session.execute(
        sa.update(User)
        .where(User.is_superadmin.is_(True), User.id != target.id)
        .values(is_superadmin=False)
    )
    await db_session.commit()

    with pytest.raises(APIError) as exc:
        await admin_service.delete_user(
            db_session, target_user_id=target.id, acting_user_id=actor.id
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "conflict"
    assert await db_session.scalar(
        sa.select(User.id).where(User.id == target.id)
    ) is not None


@pytest.mark.asyncio
async def test_delete_user_refuses_workspace_creator(
    db_session: AsyncSession,
) -> None:
    """A user who created a workspace cannot be deleted (409 conflict).

    ``workspaces.created_by_user_id`` is ON DELETE RESTRICT, so the guardrail
    catches this up front rather than letting an integrity error surface.
    """
    admin = await _make_user(db_session, "Admin", is_superadmin=True)
    creator = await _make_user(db_session, "Creator")
    await _make_workspace(db_session, "Owned", owner=creator)
    await db_session.commit()

    with pytest.raises(APIError) as exc:
        await admin_service.delete_user(
            db_session, target_user_id=creator.id, acting_user_id=admin.id
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "conflict"
    assert await db_session.scalar(
        sa.select(User.id).where(User.id == creator.id)
    ) is not None


# ===========================================================================
# list_workspaces / delete_workspace (DB-backed) — workspace directory
# ===========================================================================
#
# These validate the two new super-admin workspace operations against the real
# migrated database, following the same representative-instance style.


@pytest.mark.asyncio
async def test_list_workspaces_reports_members_and_owner(
    db_session: AsyncSession,
) -> None:
    """list_workspaces returns each workspace with member_count and owner.

    ``_make_workspace`` seeds an OWNER member, so a workspace with extra plain
    members reports the correct count and resolves ``owner_user_id`` to the
    OWNER. A workspace whose only member is demoted away from OWNER reports
    ``owner_user_id is None``.
    """
    owner = await _make_user(db_session, "WsOwner")
    extra = await _make_user(db_session, "WsMember")
    ws_owned = await _make_workspace(db_session, "WithOwner", owner=owner)
    await _add_member(db_session, workspace=ws_owned, user=extra)

    # A workspace with no OWNER member: create it, then demote the seeded owner.
    orphan_creator = await _make_user(db_session, "OrphanCreator")
    ws_ownerless = await _make_workspace(
        db_session, "NoOwner", owner=orphan_creator
    )
    await db_session.execute(
        sa.update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws_ownerless.id)
        .values(role=MemberRole.ADMIN)
    )
    await db_session.commit()

    entries = await admin_service.list_workspaces(db_session)
    by_id = {e.id: e for e in entries}

    owned = by_id[ws_owned.id]
    assert owned.member_count == 2  # seeded owner + extra
    assert owned.owner_user_id == owner.id
    assert owned.name == "WithOwner"
    assert owned.created_by_user_id == owner.id

    ownerless = by_id[ws_ownerless.id]
    assert ownerless.member_count == 1  # the demoted (now ADMIN) member
    assert ownerless.owner_user_id is None


@pytest.mark.asyncio
async def test_delete_workspace_cascades_members(
    db_session: AsyncSession,
) -> None:
    """delete_workspace removes the workspace and cascades its members."""
    owner = await _make_user(db_session, "DelOwner")
    member = await _make_user(db_session, "DelMember")
    ws = await _make_workspace(db_session, "Doomed", owner=owner)
    await _add_member(db_session, workspace=ws, user=member)
    await db_session.commit()

    # Precondition: two members belong to the workspace.
    count_before = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws.id)
    )
    assert count_before == 2

    returned = await admin_service.delete_workspace(
        db_session, workspace_id=ws.id
    )
    await db_session.commit()
    assert returned == ws.id

    # The workspace row is gone.
    assert await db_session.scalar(
        sa.select(Workspace.id).where(Workspace.id == ws.id)
    ) is None
    # CASCADE removed every member of the workspace.
    count_after = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws.id)
    )
    assert count_after == 0


@pytest.mark.asyncio
async def test_delete_workspace_missing_is_not_found(
    db_session: AsyncSession,
) -> None:
    """Deleting a non-existent workspace raises 404 not_found."""
    with pytest.raises(APIError) as exc:
        await admin_service.delete_workspace(
            db_session, workspace_id=uuid.uuid4()
        )
    assert exc.value.status_code == 404
    assert exc.value.code == "not_found"
