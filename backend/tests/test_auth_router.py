"""Tests for the auth router: OAuth login/callback + logout (task 5.5).

Two layers of coverage:

1. **Mocked / no-DB tests (always run).** They build a FastAPI app with the auth
   router mounted, override ``get_redis`` with an in-memory fake and
   ``_auth_service`` with a fake ``AuthService`` (so no provider/network I/O),
   and override ``get_session`` with a fake session that just captures the audit
   rows added to it. These assert:
   - ``GET /auth/login/google`` issues a 302 to ``accounts.google.com`` with the
     ``state`` in the URL, and the begin_login flow persisted that state in the
     (fake) Redis.
   - ``GET /auth/callback/google`` sets a session cookie with
     ``HttpOnly``/``Secure``/``SameSite=Lax`` and adds an ``auth.login`` audit
     row for the acting user.
   - ``POST /auth/logout`` clears the cookie and adds an ``auth.logout`` audit
     row; a request with no session still returns 200 and clears the cookie.
   - The ``/auth/*`` routes are on the public allowlist (Req 2.1) — they do not
     require a prior session.

2. **Real-Postgres audit test (skips without Docker).** Starts a throwaway
   ``postgres:18.6-alpine`` on port 55443, applies migrations, and runs the
   callback + logout against a *real* session so the ``auth.login`` /
   ``auth.logout`` rows are truly persisted to ``system_audit_logs`` and read
   back (Req 15.2). The provider exchange and Redis are still faked so the test
   needs no network.

Requirements: 2.1, 15.2 (also 1.1, 2.6).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import auth as auth_module
from app.api import deps
from app.api.auth import (
    AUDIT_ACTION_LOGIN,
    AUDIT_ACTION_LOGOUT,
    router as auth_router,
)
from app.api.deps import SESSION_COOKIE_NAME
from app.core.errors import install_exception_handlers
from app.core.security import hash_token
from app.db.models import AuthProvider, SystemAuditLog, User
from app.db.session import get_session
from app.services.auth_service import LoginRedirect


# ===========================================================================
# Shared fakes
# ===========================================================================


class FakeRedis:
    """In-memory async Redis exposing just the methods the auth path touches."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None:  # pragma: no cover - cleanup no-op
        return None


@dataclass
class FakeSessionRow:
    """Minimal ``Session`` stand-in returned by the faked ``load_session``."""

    user_id: uuid.UUID
    revoked: bool = False
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    )


class CapturingSession:
    """Fake AsyncSession that captures added rows and no-ops commit/flush.

    Enough surface for the auth router: ``add`` records ``SystemAuditLog`` rows,
    ``commit``/``flush`` are no-ops. ``load_session`` and ``revoke_session`` are
    monkeypatched at the module seams (they receive this object as ``session``),
    so this fake never needs to run SQL.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def audit_rows(self) -> list[SystemAuditLog]:
        return [o for o in self.added if isinstance(o, SystemAuditLog)]


class FakeAuthService:
    """Fake ``AuthService`` with deterministic begin/complete behavior."""

    def __init__(self, user: User | None = None) -> None:
        self._user = user or _make_user()
        self.raw_token = "issued-raw-token"
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        self.last_state = "state-xyz"

    async def begin_login(self, provider, redirect_uri, *, redis) -> LoginRedirect:
        # Mirror the real service: stash a flow keyed by state, return the URL.
        state = self.last_state
        await redis.set(f"oauth:state:{state}", "{}", ex=600)
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?response_type=code&client_id=cid&state={state}"
        )
        return LoginRedirect(authorization_url=url, state=state)

    async def complete_login(
        self, provider, code, state, *, session, redis, http_client=None
    ):
        return self._user, self.raw_token, self.expires_at


def _make_user(email: str | None = None) -> User:
    u = User(
        id=uuid.uuid4(),
        email=email or f"{uuid.uuid4().hex}@x.test",
        name="Test User",
        auth_provider=AuthProvider.GOOGLE,
        is_superadmin=False,
    )
    return u


# ===========================================================================
# Layer 1 — mocked app (always run)
# ===========================================================================


def _make_app(
    *,
    fake_redis: FakeRedis,
    fake_session: CapturingSession,
    fake_service: FakeAuthService,
) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)

    async def _get_redis_override():
        yield fake_redis

    async def _get_session_override():
        yield fake_session

    app.dependency_overrides[deps.get_redis] = _get_redis_override
    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[auth_module._auth_service] = lambda: fake_service
    return app


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def fake_session() -> CapturingSession:
    return CapturingSession()


@pytest.fixture
def fake_service() -> FakeAuthService:
    return FakeAuthService()


@pytest.fixture
def _patch_session_seams(monkeypatch: pytest.MonkeyPatch):
    """Route load_session/revoke_session (used by logout) at the fake session.

    The logout path resolves the acting user via ``load_session`` and revokes via
    ``deps.logout`` -> ``revoke_session``. Both are monkeypatched to consult a
    per-fake in-memory token registry so no SQL runs.
    """
    registry: dict[bytes, FakeSessionRow] = {}

    async def _fake_load_session(session, raw_token):
        return registry.get(hash_token(raw_token))

    async def _fake_revoke_session(session, raw_token):
        row = registry.get(hash_token(raw_token))
        if row is None:
            return False
        row.revoked = True
        return True

    # auth.py imports load_session directly; deps.py holds revoke_session.
    monkeypatch.setattr(auth_module, "load_session", _fake_load_session)
    monkeypatch.setattr(deps, "revoke_session", _fake_revoke_session)
    monkeypatch.setattr(deps, "load_session", _fake_load_session)
    yield registry


@pytest.fixture
def token_registry(_patch_session_seams) -> dict[bytes, FakeSessionRow]:
    return _patch_session_seams


@dataclass
class _StubSettings:
    """Minimal settings surface the auth router reads (redirect URLs)."""

    OAUTH_REDIRECT_BASE_URL: str = "http://localhost:8000"
    POST_LOGIN_REDIRECT_URL: str = "/"


@pytest.fixture
def _patch_settings(monkeypatch: pytest.MonkeyPatch):
    """Provide settings to the router without a full environment.

    The conftest strips all config env vars before each test, so
    ``get_settings()`` would raise. The router only needs the two redirect URLs,
    so stub them.
    """
    monkeypatch.setattr(auth_module, "get_settings", lambda: _StubSettings())
    yield


@pytest.fixture
def client(
    _patch_session_seams,
    _patch_settings,
    fake_redis: FakeRedis,
    fake_session: CapturingSession,
    fake_service: FakeAuthService,
) -> TestClient:
    app = _make_app(
        fake_redis=fake_redis, fake_session=fake_session, fake_service=fake_service
    )
    # Do not auto-follow the provider redirect (it points at a real host).
    return TestClient(app, follow_redirects=False)


# --- login begin (Req 1.1) --------------------------------------------------


def test_login_redirects_to_google_with_state(
    client: TestClient, fake_redis: FakeRedis, fake_service: FakeAuthService
):
    resp = client.get("/auth/login/google")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "accounts.google.com" in location
    assert f"state={fake_service.last_state}" in location
    # The flow state was stashed in (fake) Redis by begin_login.
    assert f"oauth:state:{fake_service.last_state}" in fake_redis.store


def test_login_rejects_unknown_provider(client: TestClient):
    resp = client.get("/auth/login/twitter")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_provider"


# --- callback (Req 2.6, 15.2) ----------------------------------------------


def test_callback_sets_secure_cookie_and_writes_login_audit(
    client: TestClient, fake_session: CapturingSession, fake_service: FakeAuthService
):
    resp = client.get(
        "/auth/callback/google", params={"code": "auth-code", "state": "state-xyz"}
    )
    assert resp.status_code == 302

    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE_NAME in set_cookie
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie

    audits = fake_session.audit_rows()
    assert len(audits) == 1
    assert audits[0].action == AUDIT_ACTION_LOGIN
    assert audits[0].user_id == fake_service._user.id
    assert audits[0].workspace_id is None
    # Metadata records the provider and never a token.
    assert audits[0].log_metadata == {"provider": "google"}


def test_callback_rejects_unknown_provider(client: TestClient):
    resp = client.get(
        "/auth/callback/twitter", params={"code": "c", "state": "s"}
    )
    assert resp.status_code == 400


# --- logout (Req 2.5, 15.2) -------------------------------------------------


def test_logout_with_session_clears_cookie_and_writes_audit(
    client: TestClient,
    fake_session: CapturingSession,
    token_registry: dict,
):
    user_id = uuid.uuid4()
    token_registry[hash_token("live-token")] = FakeSessionRow(user_id=user_id)

    resp = client.post("/auth/logout", headers={"Authorization": "Bearer live-token"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    cleared = resp.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE_NAME in cleared
    assert "max-age=0" in cleared

    audits = fake_session.audit_rows()
    assert len(audits) == 1
    assert audits[0].action == AUDIT_ACTION_LOGOUT
    assert audits[0].user_id == user_id


def test_logout_without_session_still_200_and_clears_cookie(
    client: TestClient, fake_session: CapturingSession
):
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    cleared = resp.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE_NAME in cleared
    assert "max-age=0" in cleared
    # An audit row is still written, attributed to no user.
    audits = fake_session.audit_rows()
    assert len(audits) == 1
    assert audits[0].action == AUDIT_ACTION_LOGOUT
    assert audits[0].user_id is None


# --- public allowlist (Req 2.1) --------------------------------------------


def test_auth_routes_are_public():
    """The router's paths are all under the /auth public prefix (Req 2.1)."""
    from app.api.deps import is_public_path

    paths = {route.path for route in auth_router.routes}
    assert paths  # sanity: the router mounted some routes
    for path in paths:
        assert is_public_path(path), f"{path} should be public"


def test_login_and_logout_need_no_prior_session(client: TestClient):
    """No Authorization/cookie is presented, yet the routes work (Req 2.1)."""
    assert client.get("/auth/login/github").status_code == 302
    assert client.post("/auth/logout").status_code == 200


# ===========================================================================
# Layer 2 — real Postgres audit persistence (skips without Docker)
# ===========================================================================

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_CONTAINER_NAME = "atomic_auth_router_test"
_HOST_PORT = 55443  # non-default so we never touch another database
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
                ["docker", "exec", _CONTAINER_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB],
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
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, future=True, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(engine: AsyncEngine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def _real_db_app(
    db_sessionmaker,
    *,
    fake_redis: FakeRedis,
    fake_service: FakeAuthService,
) -> FastAPI:
    """Build the auth app backed by the real DB session, faking redis+service."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)

    async def _get_redis_override():
        yield fake_redis

    async def _get_session_override():
        async with db_sessionmaker() as sess:
            yield sess

    app.dependency_overrides[deps.get_redis] = _get_redis_override
    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[auth_module._auth_service] = lambda: fake_service
    return app


@pytest.mark.asyncio
async def test_callback_persists_login_audit_row(
    db_sessionmaker, monkeypatch: pytest.MonkeyPatch
):
    """A real callback writes an ``auth.login`` row to system_audit_logs (Req 15.2)."""
    # Seed a real user the fake service will "return" from complete_login.
    async with db_sessionmaker() as sess:
        user = User(
            email=f"{uuid.uuid4().hex}@x.test",
            name="Login User",
            auth_provider=AuthProvider.GOOGLE,
            is_superadmin=False,
        )
        sess.add(user)
        await sess.commit()
        user_id = user.id

    monkeypatch.setattr(auth_module, "get_settings", lambda: _StubSettings())
    service = FakeAuthService(user=user)
    fake_redis = FakeRedis()
    app = _real_db_app(db_sessionmaker, fake_redis=fake_redis, fake_service=service)

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.get(
            "/auth/callback/google", params={"code": "code", "state": "s"}
        )
        assert resp.status_code == 302

    async with db_sessionmaker() as sess:
        rows = (
            await sess.scalars(
                sa.select(SystemAuditLog).where(
                    SystemAuditLog.action == AUDIT_ACTION_LOGIN,
                    SystemAuditLog.user_id == user_id,
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].workspace_id is None
    assert rows[0].log_metadata == {"provider": "google"}


@pytest.mark.asyncio
async def test_logout_persists_logout_audit_row(
    db_sessionmaker, monkeypatch: pytest.MonkeyPatch
):
    """A real logout writes an ``auth.logout`` row to system_audit_logs (Req 15.2)."""
    from app.core.security import persist_session

    async with db_sessionmaker() as sess:
        user = User(
            email=f"{uuid.uuid4().hex}@x.test",
            name="Logout User",
            auth_provider=AuthProvider.GOOGLE,
            is_superadmin=False,
        )
        sess.add(user)
        await sess.flush()
        user_id = user.id
        raw_token = "real-logout-token"
        await persist_session(
            sess,
            user_id=user_id,
            raw_token=raw_token,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await sess.commit()

    fake_redis = FakeRedis()
    app = _real_db_app(
        db_sessionmaker, fake_redis=fake_redis, fake_service=FakeAuthService()
    )

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        resp = await client.post(
            "/auth/logout", headers={"Authorization": f"Bearer {raw_token}"}
        )
        assert resp.status_code == 200

    async with db_sessionmaker() as sess:
        rows = (
            await sess.scalars(
                sa.select(SystemAuditLog).where(
                    SystemAuditLog.action == AUDIT_ACTION_LOGOUT,
                    SystemAuditLog.user_id == user_id,
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].workspace_id is None
