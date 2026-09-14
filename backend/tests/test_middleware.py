"""Tests for the middleware stack and central exception handler (Task 4.7).

Covers transport-security and request-guard behavior wired by
``app.core.middleware.install_middleware`` and the safe error envelope from
``app.core.errors.install_exception_handlers``:

- Oversized request body -> 413 with the error envelope (Req 17.5).
- Security headers present on every response, including /health (Req 21.4).
- An endpoint that raises -> generic 500 envelope, no traceback/paths (Req 17.4).
- CORS allowlist echoes allowed origins and omits disallowed ones (Req 21.1).
- Forwarded-proto (X-Forwarded-Proto) is honored so the request scheme is
  https behind a proxy (Req 21.3).
- The Redis-backed auth rate limiter allows up to the limit then 429s, and
  fails open when Redis errors (Req 21.2).
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import APIError, install_exception_handlers
from app.core.middleware import check_auth_rate_limit, install_middleware


# --- Fakes -------------------------------------------------------------------

class FakeRedis:
    """Minimal async fake exposing the incr/expire surface the limiter uses."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


class BrokenRedis:
    """Async fake that raises on every operation (simulates an outage)."""

    async def incr(self, key: str) -> int:
        raise ConnectionError("redis down")

    async def expire(self, key: str, seconds: int) -> None:
        raise ConnectionError("redis down")


# --- App builders ------------------------------------------------------------

def _base_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/atomic",
        "REDIS_URL": "redis://redis:6379/0",
        "ENCRYPTION_KEY": "test-encryption-key-0123456789",
        "GOOGLE_OAUTH_CLIENT_ID": "gid",
        "GOOGLE_OAUTH_CLIENT_SECRET": "gsecret",
        "GITHUB_OAUTH_CLIENT_ID": "hid",
        "GITHUB_OAUTH_CLIENT_SECRET": "hsecret",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _build_app(settings: Settings, redis_factory=None) -> FastAPI:
    """Build a small app with the middleware + handlers and a few test routes."""
    app = FastAPI()
    install_middleware(app, settings, redis_factory=redis_factory)
    install_exception_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/echo")
    async def echo(payload: dict) -> dict:
        return {"received": True}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("secret-internal-detail /abs/path/to/file.py")

    return app


# --- Body-size limit (Req 17.5) ----------------------------------------------

def test_oversized_body_returns_413_envelope() -> None:
    settings = _base_settings(MAX_BODY_SIZE_BYTES=100)
    client = TestClient(_build_app(settings))

    big = {"data": "x" * 500}
    resp = client.post("/echo", json=big)

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "payload_too_large"
    assert "message" in body["error"]


def test_body_within_limit_is_accepted() -> None:
    settings = _base_settings(MAX_BODY_SIZE_BYTES=1_000_000)
    client = TestClient(_build_app(settings))

    resp = client.post("/echo", json={"data": "small"})
    assert resp.status_code == 200


def test_oversized_content_length_header_rejected() -> None:
    settings = _base_settings(MAX_BODY_SIZE_BYTES=10)
    client = TestClient(_build_app(settings))

    resp = client.post(
        "/echo",
        content=b"x" * 50,
        headers={"Content-Type": "application/json", "Content-Length": "50"},
    )
    assert resp.status_code == 413


# --- Security headers (Req 21.4) ---------------------------------------------

def test_security_headers_present_on_health() -> None:
    settings = _base_settings()
    client = TestClient(_build_app(settings))

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_security_headers_present_on_error_response() -> None:
    settings = _base_settings()
    client = TestClient(_build_app(settings), raise_server_exceptions=False)

    resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


# --- Central exception handler (Req 17.4) ------------------------------------

def test_unhandled_exception_returns_generic_500_without_internals() -> None:
    settings = _base_settings()
    client = TestClient(_build_app(settings), raise_server_exceptions=False)

    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body == {"error": {"code": "internal_error", "message": "An internal error occurred."}}
    # No traceback, file path, or the raised message leaked.
    text = resp.text
    assert "Traceback" not in text
    assert "/abs/path/to/file.py" not in text
    assert "secret-internal-detail" not in text


def test_api_error_maps_to_envelope() -> None:
    settings = _base_settings()
    app = _build_app(settings)

    @app.get("/conflict")
    async def conflict() -> dict:
        raise APIError(status_code=409, code="account_conflict", message="Email in use")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/conflict")
    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "account_conflict", "message": "Email in use"}}


def test_validation_error_names_fields_and_scrubs() -> None:
    settings = _base_settings()
    client = TestClient(_build_app(settings), raise_server_exceptions=False)

    # /echo expects a JSON object body; send a non-object to trigger 422.
    resp = client.post("/echo", json=123)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "fields" in body["error"]


# --- CORS allowlist (Req 21.1) -----------------------------------------------

def test_cors_allows_configured_origin() -> None:
    settings = _base_settings(CORS_ALLOW_ORIGINS=["https://app.example.com"])
    client = TestClient(_build_app(settings))

    resp = client.get("/health", headers={"Origin": "https://app.example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_omits_disallowed_origin() -> None:
    settings = _base_settings(CORS_ALLOW_ORIGINS=["https://app.example.com"])
    client = TestClient(_build_app(settings))

    resp = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example.com"


# --- Forwarded proto (Req 21.3) ----------------------------------------------

def test_forwarded_proto_is_honored() -> None:
    """ProxyHeadersMiddleware maps X-Forwarded-Proto: https onto the ASGI scope.

    Asserted at the ASGI scope level (via a minimal probe app mounted under the
    real middleware stack) rather than through a FastAPI route, so the check
    targets the middleware behavior directly and is not affected by FastAPI
    route/dependant analysis.
    """
    captured: dict[str, str] = {}

    async def probe(scope, receive, send):
        # ProxyHeadersMiddleware rewrites scope["scheme"] from X-Forwarded-Proto.
        captured["scheme"] = scope.get("scheme", "")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok"})

    settings = _base_settings()
    app = FastAPI()
    install_middleware(app, settings)
    install_exception_handlers(app)
    # Mount the raw ASGI probe so the request bypasses FastAPI route validation
    # but still traverses the full middleware stack (incl. ProxyHeaders).
    app.mount("/scheme-check", probe)

    client = TestClient(app)
    resp = client.get("/scheme-check", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    assert captured["scheme"] == "https"


# --- Rate-limit function unit tests (Req 21.2) -------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_allows_up_to_limit_then_blocks() -> None:
    redis = FakeRedis()
    now = 1000.0  # fixed instant, same window for all calls
    allowed_flags = []
    for _ in range(3):
        allowed, _retry = await check_auth_rate_limit(redis, "1.2.3.4", limit=3, now=now)
        allowed_flags.append(allowed)
    # 4th request in the same window exceeds the limit.
    blocked, retry_after = await check_auth_rate_limit(redis, "1.2.3.4", limit=3, now=now)

    assert allowed_flags == [True, True, True]
    assert blocked is False
    assert retry_after >= 1


@pytest.mark.asyncio
async def test_rate_limiter_windows_are_per_client() -> None:
    redis = FakeRedis()
    now = 500.0
    a1, _ = await check_auth_rate_limit(redis, "a", limit=1, now=now)
    b1, _ = await check_auth_rate_limit(redis, "b", limit=1, now=now)
    a2, _ = await check_auth_rate_limit(redis, "a", limit=1, now=now)

    assert a1 is True
    assert b1 is True  # separate client, own window
    assert a2 is False  # a exceeded its own limit


@pytest.mark.asyncio
async def test_rate_limiter_new_window_resets() -> None:
    redis = FakeRedis()
    now = 60.0
    first, _ = await check_auth_rate_limit(redis, "c", limit=1, now=now)
    second, _ = await check_auth_rate_limit(redis, "c", limit=1, now=now)
    # Advance into the next fixed window.
    third, _ = await check_auth_rate_limit(redis, "c", limit=1, now=now + 60)

    assert first is True
    assert second is False
    assert third is True


@pytest.mark.asyncio
async def test_rate_limiter_fails_open_when_redis_down() -> None:
    allowed, retry = await check_auth_rate_limit(BrokenRedis(), "x", limit=1, now=1.0)
    assert allowed is True
    assert retry == 0


# --- Rate-limit middleware integration (Req 21.2) ----------------------------

def test_auth_rate_limit_middleware_returns_429() -> None:
    settings = _base_settings(AUTH_RATE_LIMIT_PER_MINUTE=2)
    shared = FakeRedis()
    app = _build_app(settings, redis_factory=lambda: shared)

    @app.get("/auth/ping")
    async def auth_ping() -> dict:
        return {"ok": True}

    client = TestClient(app)
    codes = [client.get("/auth/ping").status_code for _ in range(3)]
    assert codes[0] == 200
    assert codes[1] == 200
    assert codes[2] == 429
    last = client.get("/auth/ping")
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in last.headers


def test_non_auth_path_is_not_rate_limited() -> None:
    settings = _base_settings(AUTH_RATE_LIMIT_PER_MINUTE=1)
    shared = FakeRedis()
    app = _build_app(settings, redis_factory=lambda: shared)
    client = TestClient(app)

    # /health is not under /auth, so repeated calls are never limited.
    codes = [client.get("/health").status_code for _ in range(5)]
    assert all(code == 200 for code in codes)
