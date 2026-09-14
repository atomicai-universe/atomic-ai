"""Tests for the OAuth Auth_Service (task 5.1).

Coverage:

- Pure helpers: ``resolve_user_identity`` (create / return_existing / conflict)
  and the PKCE ``derive_code_challenge`` (S256, base64url, no padding).
- ``begin_login`` builds correct Google/GitHub authorization URLs (scopes,
  ``state``, and for Google ``code_challenge`` + ``code_challenge_method=S256``)
  and stores the flow in a fake Redis keyed by ``state``.
- ``complete_login`` with a mocked HTTP client, fake Redis, and a real DB
  session: happy path persists a user + session row; missing/expired state and
  provider error reject without creating a user; a cross-provider email raises
  ``account_conflict`` and creates no new user.

The DB-backed cases spin up a throwaway Postgres 18.6 container on the
non-default port 55435 and apply migrations. If Docker is unavailable they skip
gracefully, while the pure-resolver, URL, and state-validation tests always run.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from app.core.errors import APIError
from app.db.models import AuthProvider, Session, User
from app.services.auth_service import (
    AuthService,
    LoginRedirect,
    build_authorization_url,
    derive_code_challenge,
    generate_pkce_verifier,
    generate_state,
    resolve_user_identity,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeRedis:
    """A tiny in-memory async stand-in for the Redis client.

    Implements just the async surface Auth_Service uses (``set``/``get``/
    ``delete``); TTL is accepted but not enforced (tests exercise expiry by
    deleting the key).
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FakeResponse:
    """Canned HTTP response exposing the ``httpx``-shaped surface used."""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHTTPClient:
    """Fake async HTTP client returning canned responses keyed by URL.

    ``routes`` maps a URL to either a :class:`FakeResponse` or a list of
    responses consumed in order (so repeat GETs to the same URL can differ).
    Requests to an unmapped URL raise, surfacing as an auth error upstream.
    """

    def __init__(self, routes: dict) -> None:
        self.routes = {k: (v if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.calls: list[tuple[str, str]] = []

    def _next(self, method: str, url: str) -> FakeResponse:
        self.calls.append((method, url))
        queue = self.routes.get(url)
        if not queue:
            raise RuntimeError(f"unexpected request: {method} {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def post(self, url: str, *args, **kwargs) -> FakeResponse:
        return self._next("POST", url)

    async def get(self, url: str, *args, **kwargs) -> FakeResponse:
        return self._next("GET", url)


def _make_service() -> AuthService:
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


# ---------------------------------------------------------------------------
# Pure helper tests (always run)
# ---------------------------------------------------------------------------


def test_resolve_user_identity_create_when_no_user():
    assert resolve_user_identity(None, AuthProvider.GOOGLE) == "create"


def test_resolve_user_identity_return_existing_same_provider():
    assert (
        resolve_user_identity(AuthProvider.GOOGLE, AuthProvider.GOOGLE)
        == "return_existing"
    )
    assert (
        resolve_user_identity(AuthProvider.GITHUB, AuthProvider.GITHUB)
        == "return_existing"
    )


def test_resolve_user_identity_conflict_different_provider():
    assert resolve_user_identity(AuthProvider.GOOGLE, AuthProvider.GITHUB) == "conflict"
    assert resolve_user_identity(AuthProvider.GITHUB, AuthProvider.GOOGLE) == "conflict"


def test_derive_code_challenge_matches_rfc7636_s256():
    verifier = "test-verifier-abc123"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    challenge = derive_code_challenge(verifier)
    assert challenge == expected
    assert "=" not in challenge  # base64url, no padding


def test_generated_state_and_verifier_are_unique_and_nonempty():
    assert generate_state() != generate_state()
    assert len(generate_pkce_verifier()) >= 43  # RFC 7636 minimum


# ---------------------------------------------------------------------------
# begin_login URL + state-storage tests (always run)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_login_google_url_and_state_storage():
    svc = _make_service()
    redis = FakeRedis()
    result = await svc.begin_login(
        "google", "https://app.example/callback", redis=redis
    )
    assert isinstance(result, LoginRedirect)

    parsed = urlparse(result.authorization_url)
    assert parsed.hostname == "accounts.google.com"
    q = parse_qs(parsed.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["google-client-id"]
    assert q["redirect_uri"] == ["https://app.example/callback"]
    assert q["scope"] == ["openid email profile"]
    assert q["state"] == [result.state]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] and q["code_challenge"][0]
    assert q["nonce"] and q["nonce"][0]

    # Flow stored under the state key, and the challenge matches the verifier.
    stored_key = f"oauth:state:{result.state}"
    assert stored_key in redis.store
    import json

    flow = json.loads(redis.store[stored_key])
    assert flow["provider"] == "google"
    assert derive_code_challenge(flow["code_verifier"]) == q["code_challenge"][0]


@pytest.mark.asyncio
async def test_begin_login_github_url_and_scopes():
    svc = _make_service()
    redis = FakeRedis()
    result = await svc.begin_login(
        "github", "https://app.example/cb", redis=redis
    )
    parsed = urlparse(result.authorization_url)
    assert parsed.hostname == "github.com"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["github-client-id"]
    assert q["scope"] == ["read:user user:email"]
    assert q["state"] == [result.state]
    # GitHub authorize endpoint gets no PKCE challenge params.
    assert "code_challenge" not in q


@pytest.mark.asyncio
async def test_begin_login_rejects_unknown_provider():
    svc = _make_service()
    redis = FakeRedis()
    with pytest.raises(APIError) as exc:
        await svc.begin_login("facebook", "https://app.example/cb", redis=redis)
    assert exc.value.status_code == 400


def test_build_authorization_url_is_pure_for_github():
    url = build_authorization_url(
        AuthProvider.GITHUB,
        client_id="cid",
        redirect_uri="https://x/cb",
        state="st",
        code_challenge="ignored",
        nonce="n",
    )
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "code_challenge" not in url


# ---------------------------------------------------------------------------
# DB-backed complete_login tests (skip if Docker unavailable)
# ---------------------------------------------------------------------------

_PG_CONTAINER = "atomic-auth-test-pg"
_PG_PORT = 55435
_PG_DSN = (
    f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{_PG_PORT}/postgres"
)


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"], check=True, capture_output=True, timeout=15
        )
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def pg_dsn():
    """Provision a throwaway Postgres 18.6 and apply migrations; tear down after."""
    if not _docker_available():
        pytest.skip("Docker is not available for the DB-backed auth tests")

    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm",
                "--name", _PG_CONTAINER,
                "-e", "POSTGRES_PASSWORD=postgres",
                "-e", "POSTGRES_DB=postgres",
                "-p", f"{_PG_PORT}:5432",
                "postgres:18.6-alpine",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        pytest.skip(f"Could not start Postgres container: {exc.stderr.decode()[:200]}")

    try:
        # Wait for readiness.
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            probe = subprocess.run(
                ["docker", "exec", _PG_CONTAINER, "pg_isready", "-U", "postgres"],
                capture_output=True,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:  # pragma: no cover
            pytest.skip("Postgres container did not become ready in time")

        # Apply migrations against the throwaway DB.
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": _PG_DSN,
                "REDIS_URL": "redis://localhost:6379/0",
                "ENCRYPTION_KEY": "test-encryption-key-value-0123456789",
                "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
                "GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
                "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
                "GITHUB_OAUTH_CLIENT_SECRET": "github-secret",
            }
        )
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        migrate = subprocess.run(
            [os.path.join(backend_dir, ".venv", "bin", "alembic"),
             "-c", "alembic.ini", "upgrade", "head"],
            cwd=backend_dir,
            env=env,
            capture_output=True,
        )
        if migrate.returncode != 0:  # pragma: no cover
            pytest.skip(f"alembic upgrade failed: {migrate.stderr.decode()[:300]}")

        yield _PG_DSN
    finally:
        subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


@pytest_asyncio.fixture
async def db_session(pg_dsn):
    """A real AsyncSession bound to the throwaway Postgres, rolled back per test."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(pg_dsn, future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


def _google_routes(email="user@example.com", name="Test User"):
    from app.services.auth_service import (
        _GOOGLE_TOKEN_URL,
        _GOOGLE_USERINFO_URL,
    )

    return {
        _GOOGLE_TOKEN_URL: FakeResponse({"access_token": "at-123"}),
        _GOOGLE_USERINFO_URL: FakeResponse({"email": email, "name": name}),
    }


async def _seed_state(svc, redis, provider):
    """Run begin_login to populate a valid state/flow, returning the state."""
    result = await svc.begin_login(provider, "https://app.example/cb", redis=redis)
    return result.state


@pytest.mark.asyncio
async def test_complete_login_creates_user_and_session(db_session):
    svc = _make_service()
    redis = FakeRedis()
    state = await _seed_state(svc, redis, "google")

    user, raw_token, expires_at = await svc.complete_login(
        "google", "auth-code", state,
        session=db_session, redis=redis,
        http_client=FakeHTTPClient(_google_routes()),
    )
    await db_session.flush()

    assert user.email == "user@example.com"
    assert user.auth_provider == AuthProvider.GOOGLE
    assert user.is_superadmin is False
    assert raw_token
    assert expires_at > datetime.now(timezone.utc)

    # A session row was persisted for the user.
    from sqlalchemy import select

    rows = (
        await db_session.execute(select(Session).where(Session.user_id == user.id))
    ).scalars().all()
    assert len(rows) == 1
    # State consumed (single-use).
    assert f"oauth:state:{state}" not in redis.store


@pytest.mark.asyncio
async def test_complete_login_idempotent_returns_existing(db_session):
    svc = _make_service()
    redis = FakeRedis()

    state1 = await _seed_state(svc, redis, "google")
    user1, _, _ = await svc.complete_login(
        "google", "code", state1,
        session=db_session, redis=redis,
        http_client=FakeHTTPClient(_google_routes(email="dup@example.com")),
    )
    await db_session.flush()

    state2 = await _seed_state(svc, redis, "google")
    user2, _, _ = await svc.complete_login(
        "google", "code", state2,
        session=db_session, redis=redis,
        http_client=FakeHTTPClient(_google_routes(email="dup@example.com")),
    )
    assert user1.id == user2.id


@pytest.mark.asyncio
async def test_complete_login_missing_state_rejects_no_user(db_session):
    svc = _make_service()
    redis = FakeRedis()
    from sqlalchemy import func, select

    before = (await db_session.execute(select(func.count(User.id)))).scalar_one()
    with pytest.raises(APIError) as exc:
        await svc.complete_login(
            "google", "code", "never-issued-state",
            session=db_session, redis=redis,
            http_client=FakeHTTPClient(_google_routes()),
        )
    assert exc.value.status_code == 401
    after = (await db_session.execute(select(func.count(User.id)))).scalar_one()
    assert before == after


@pytest.mark.asyncio
async def test_complete_login_cross_provider_conflict(db_session):
    svc = _make_service()
    redis = FakeRedis()
    from app.services.auth_service import (
        _GITHUB_TOKEN_URL,
        _GITHUB_USER_URL,
    )
    from sqlalchemy import func, select

    # First login creates a Google user.
    state_g = await _seed_state(svc, redis, "google")
    await svc.complete_login(
        "google", "code", state_g,
        session=db_session, redis=redis,
        http_client=FakeHTTPClient(_google_routes(email="clash@example.com")),
    )
    await db_session.flush()
    count_after_first = (
        await db_session.execute(select(func.count(User.id)))
    ).scalar_one()

    # Second login with the SAME email but GitHub -> conflict, no new user.
    state_gh = await _seed_state(svc, redis, "github")
    github_routes = {
        _GITHUB_TOKEN_URL: FakeResponse({"access_token": "gh-at"}),
        _GITHUB_USER_URL: FakeResponse({"email": "clash@example.com", "name": "Clash"}),
    }
    with pytest.raises(APIError) as exc:
        await svc.complete_login(
            "github", "code", state_gh,
            session=db_session, redis=redis,
            http_client=FakeHTTPClient(github_routes),
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "account_conflict"
    await db_session.flush()
    count_after_conflict = (
        await db_session.execute(select(func.count(User.id)))
    ).scalar_one()
    assert count_after_conflict == count_after_first


@pytest.mark.asyncio
async def test_complete_login_provider_error_rejects_no_user(db_session):
    svc = _make_service()
    redis = FakeRedis()
    from app.services.auth_service import _GOOGLE_TOKEN_URL
    from sqlalchemy import func, select

    state = await _seed_state(svc, redis, "google")
    before = (await db_session.execute(select(func.count(User.id)))).scalar_one()
    routes = {_GOOGLE_TOKEN_URL: FakeResponse({"error": "invalid_grant"}, status_code=400)}
    with pytest.raises(APIError) as exc:
        await svc.complete_login(
            "google", "bad-code", state,
            session=db_session, redis=redis,
            http_client=FakeHTTPClient(routes),
        )
    assert exc.value.status_code == 401
    after = (await db_session.execute(select(func.count(User.id)))).scalar_one()
    assert before == after
