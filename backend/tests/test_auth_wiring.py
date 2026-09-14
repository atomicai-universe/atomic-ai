"""Wiring-level tests for the auth stack (task 5.6).

This file complements — and deliberately does **not** duplicate —
``test_auth_service.py`` (service internals + DB-backed ``complete_login``) and
``test_auth_router.py`` (router 302s, audit rows, real-DB persistence). It pins
the *seams* between the pure helpers, the service, and the HTTP layer that no
other test locks down, all without Docker/Postgres/Redis:

- **OAuth redirect URL params per provider** (Req 1.1, 1.2): assert the exact
  query-parameter surface of the authorization URL for both providers via the
  pure ``build_authorization_url`` — Google carries the full PKCE + OIDC set,
  GitHub carries the code-flow subset with no ``code_challenge``.
- **Provider-error branch** (Req 1.5): a non-2xx token response makes
  ``complete_login`` raise ``APIError`` and add **no** ``User`` to the session,
  using in-memory ``FakeHTTPClient``/``FakeRedis``/a recording session (no DB).
- **Cookie attributes** (Req 2.6): call ``set_session_cookie`` directly and
  inspect the emitted ``Set-Cookie`` for ``HttpOnly``/``Secure``/``SameSite=Lax``
  and a positive ``Max-Age``.
- **Public-route allowlist vs protected 401** (Req 2.1): ``is_public_path`` for
  ``/auth/...`` and ``/health``; a tiny app whose one route depends on
  ``require_session`` returns 401 with no session, while ``/auth/*`` and
  ``/health`` (which omit the dependency) return 200.
- **GitHub token exchange** (Req 1.2): drive ``complete_login`` for GitHub
  through the ``POST /access_token`` -> ``GET /user`` -> ``GET /user/emails``
  path with a recording session so the primary-verified-email fallback and the
  two-hop profile read are exercised without a DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import (
    SESSION_COOKIE_NAME,
    is_public_path,
    require_session,
    set_session_cookie,
)
from app.core.errors import APIError, install_exception_handlers
from app.db.models import AuthProvider, User
from app.services.auth_service import (
    AuthService,
    build_authorization_url,
    derive_code_challenge,
)


# ===========================================================================
# Shared no-I/O test doubles
# ===========================================================================


class FakeRedis:
    """In-memory async stand-in for the Redis surface the auth path touches."""

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
    """Async HTTP client returning canned responses keyed by URL."""

    def __init__(self, routes: dict) -> None:
        self.routes = {
            k: (v if isinstance(v, list) else [v]) for k, v in routes.items()
        }
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


class RecordingSession:
    """Fake AsyncSession that records added rows; find-or-create finds nothing.

    ``execute`` always resolves to "no existing user", so ``complete_login``
    takes the *create* branch — which lets these tests assert whether a ``User``
    was added without a real database.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, *_args, **_kwargs):
        class _Result:
            def scalar_one_or_none(self_inner):
                return None

        return _Result()

    def users_added(self) -> list[User]:
        return [o for o in self.added if isinstance(o, User)]


def _make_service() -> AuthService:
    """Build an AuthService with an in-memory settings stub (no real secrets)."""

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


async def _seed_state(svc: AuthService, redis: FakeRedis, provider: str) -> str:
    """Run begin_login to populate a valid state/flow, returning the state."""
    result = await svc.begin_login(provider, "https://app.example/cb", redis=redis)
    return result.state


# ===========================================================================
# OAuth redirect URL params per provider (Req 1.1, 1.2)
# ===========================================================================


def test_google_authorization_url_carries_full_pkce_and_oidc_params():
    """Google's authorize URL has the complete PKCE + OIDC parameter set."""
    url = build_authorization_url(
        AuthProvider.GOOGLE,
        client_id="google-client-id",
        redirect_uri="https://app.example/callback",
        state="state-abc",
        code_challenge="challenge-xyz",
        nonce="nonce-123",
    )
    parsed = urlparse(url)
    assert parsed.hostname == "accounts.google.com"
    q = parse_qs(parsed.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["google-client-id"]
    assert q["redirect_uri"] == ["https://app.example/callback"]
    assert q["scope"] == ["openid email profile"]
    assert q["state"] == ["state-abc"]
    assert q["code_challenge"] == ["challenge-xyz"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["nonce"] == ["nonce-123"]


def test_github_authorization_url_has_subset_scopes_and_no_pkce():
    """GitHub's authorize URL carries its scopes/state but no PKCE challenge."""
    url = build_authorization_url(
        AuthProvider.GITHUB,
        client_id="github-client-id",
        redirect_uri="https://app.example/callback",
        state="state-def",
        code_challenge="ignored-by-github",
        nonce="ignored-nonce",
    )
    parsed = urlparse(url)
    assert parsed.hostname == "github.com"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["github-client-id"]
    assert q["redirect_uri"] == ["https://app.example/callback"]
    assert q["scope"] == ["read:user user:email"]
    assert q["state"] == ["state-def"]
    assert "code_challenge" not in q
    assert "code_challenge_method" not in q
    assert "nonce" not in q


# ===========================================================================
# Provider-error branch: no user created (Req 1.5)
# ===========================================================================


@pytest.mark.asyncio
async def test_provider_token_error_raises_and_creates_no_user():
    """A non-2xx token response rejects the login and adds no User (no DB)."""
    from app.services.auth_service import _GOOGLE_TOKEN_URL

    svc = _make_service()
    redis = FakeRedis()
    session = RecordingSession()
    state = await _seed_state(svc, redis, "google")

    routes = {
        _GOOGLE_TOKEN_URL: FakeResponse({"error": "invalid_grant"}, status_code=400)
    }
    with pytest.raises(APIError) as exc:
        await svc.complete_login(
            "google",
            "bad-code",
            state,
            session=session,
            redis=redis,
            http_client=FakeHTTPClient(routes),
        )

    assert exc.value.status_code == 401
    assert exc.value.code == "oauth_exchange_failed"
    assert session.users_added() == []
    # State was consumed even though the exchange failed (single-use).
    assert f"oauth:state:{state}" not in redis.store


@pytest.mark.asyncio
async def test_redirect_uri_mismatch_maps_to_actionable_message():
    """Google's redirect_uri_mismatch surfaces a clear, fix-oriented message."""
    from app.services.auth_service import _GOOGLE_TOKEN_URL

    svc = _make_service()
    redis = FakeRedis()
    session = RecordingSession()
    state = await _seed_state(svc, redis, "google")

    routes = {
        _GOOGLE_TOKEN_URL: FakeResponse(
            {"error": "redirect_uri_mismatch",
             "error_description": "redirect_uri mismatch"},
            status_code=400,
        )
    }
    with pytest.raises(APIError) as exc:
        await svc.complete_login(
            "google",
            "some-code",
            state,
            session=session,
            redis=redis,
            http_client=FakeHTTPClient(routes),
        )

    assert exc.value.code == "oauth_exchange_failed"
    assert "redirect URI" in exc.value.message
    assert session.users_added() == []



# ===========================================================================
# Cookie attributes (Req 2.6)
# ===========================================================================


def test_set_session_cookie_marks_httponly_secure_samesite_and_max_age():
    """set_session_cookie emits a hardened, session-scoped cookie."""
    response = Response()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    set_session_cookie(response, "raw-token-value", expires_at)

    header = response.headers["set-cookie"]
    parsed = SimpleCookie()
    parsed.load(header)
    morsel = parsed[SESSION_COOKIE_NAME]

    assert morsel.value == "raw-token-value"
    assert morsel["httponly"]
    assert morsel["secure"]
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"
    # ~1h remaining; allow a small clock skew.
    assert 3000 <= int(morsel["max-age"]) <= 3600


def test_set_session_cookie_clamps_expired_to_zero_max_age():
    """An already-past expiry clamps Max-Age to 0 (immediately expiring)."""
    response = Response()
    expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    set_session_cookie(response, "raw-token-value", expires_at)

    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    assert int(parsed[SESSION_COOKIE_NAME]["max-age"]) == 0


# ===========================================================================
# Public-route allowlist vs protected 401 (Req 2.1)
# ===========================================================================


@pytest.mark.parametrize(
    "path",
    ["/auth", "/auth/login/google", "/auth/callback/github", "/health"],
)
def test_is_public_path_true_for_auth_and_health(path: str):
    assert is_public_path(path) is True


@pytest.mark.parametrize(
    "path",
    ["/workspaces", "/authorize", "/healthz", "/api/auth", "/"],
)
def test_is_public_path_false_for_protected_and_lookalike_paths(path: str):
    assert is_public_path(path) is False


@pytest.fixture
def allowlist_client() -> TestClient:
    """A tiny app: one protected route (require_session) + two public routes.

    The protected route must 401 with no session (require_session raises the
    central ``unauthorized`` APIError); the public routes omit the dependency
    and answer 200 — mirroring how real routers opt in/out per Req 2.1.
    """
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/protected")
    async def _protected(ctx=Depends(require_session)) -> dict:  # pragma: no cover
        return {"user_id": str(ctx.user_id)}

    @app.get("/auth/ping")
    async def _auth_ping() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def _health() -> dict:
        return {"status": "ok"}

    # require_session depends on get_session; a protected request should be
    # rejected before it's ever used, but override it defensively so an
    # accidental DB touch can't reach a real engine.
    async def _no_session():  # pragma: no cover - defensive
        yield None

    from app.db.session import get_session

    app.dependency_overrides[get_session] = _no_session
    return TestClient(app)


def test_protected_route_401_without_session(allowlist_client: TestClient):
    resp = allowlist_client.get("/protected")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_public_routes_answer_without_session(allowlist_client: TestClient):
    assert allowlist_client.get("/auth/ping").status_code == 200
    assert allowlist_client.get("/health").status_code == 200


# ===========================================================================
# GitHub token exchange with mocked provider endpoints (Req 1.2)
# ===========================================================================


@pytest.mark.asyncio
async def test_github_exchange_reads_user_then_emails_and_creates_user():
    """GitHub login hits token -> /user -> /user/emails and creates a User."""
    from app.services.auth_service import (
        _GITHUB_TOKEN_URL,
        _GITHUB_USER_EMAILS_URL,
        _GITHUB_USER_URL,
    )

    svc = _make_service()
    redis = FakeRedis()
    session = RecordingSession()
    state = await _seed_state(svc, redis, "github")

    routes = {
        _GITHUB_TOKEN_URL: FakeResponse({"access_token": "gh-at"}),
        # /user has no top-level email, forcing the /user/emails fallback.
        _GITHUB_USER_URL: FakeResponse({"login": "octocat", "email": None}),
        _GITHUB_USER_EMAILS_URL: FakeResponse(
            [
                {"email": "secondary@example.com", "primary": False, "verified": True},
                {"email": "primary@example.com", "primary": True, "verified": True},
            ]
        ),
    }
    http_client = FakeHTTPClient(routes)

    user, raw_token, expires_at = await svc.complete_login(
        "github",
        "auth-code",
        state,
        session=session,
        redis=redis,
        http_client=http_client,
    )

    # Two-hop profile read happened in order: token, /user, /user/emails.
    assert http_client.calls == [
        ("POST", _GITHUB_TOKEN_URL),
        ("GET", _GITHUB_USER_URL),
        ("GET", _GITHUB_USER_EMAILS_URL),
    ]
    # Primary+verified email won the fallback selection.
    assert user.email == "primary@example.com"
    assert user.name == "octocat"
    assert user.auth_provider == AuthProvider.GITHUB
    assert user.is_superadmin is False
    assert raw_token
    assert expires_at > datetime.now(timezone.utc)
    assert len(session.users_added()) == 1


@pytest.mark.asyncio
async def test_github_exchange_uses_top_level_email_without_emails_call():
    """When /user already carries an email, /user/emails is not requested."""
    from app.services.auth_service import _GITHUB_TOKEN_URL, _GITHUB_USER_URL

    svc = _make_service()
    redis = FakeRedis()
    session = RecordingSession()
    state = await _seed_state(svc, redis, "github")

    routes = {
        _GITHUB_TOKEN_URL: FakeResponse({"access_token": "gh-at"}),
        _GITHUB_USER_URL: FakeResponse(
            {"login": "octo", "name": "Octo Cat", "email": "octo@example.com"}
        ),
    }
    http_client = FakeHTTPClient(routes)

    user, _, _ = await svc.complete_login(
        "github",
        "auth-code",
        state,
        session=session,
        redis=redis,
        http_client=http_client,
    )

    assert user.email == "octo@example.com"
    assert user.name == "Octo Cat"
    # No third call to /user/emails.
    assert [c[1] for c in http_client.calls] == [_GITHUB_TOKEN_URL, _GITHUB_USER_URL]
