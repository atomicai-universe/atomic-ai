"""Edge-case unit tests for the middleware stack, error envelope, and startup
log scrubbing (Task 4.10).

The happy paths and several edges are already covered by ``test_middleware.py``.
This module adds *boundary* and *edge* cases that complement, rather than
duplicate, those tests:

- Body-size BOUNDARY: a body exactly at the limit is accepted, one byte over is
  rejected — for both the Content-Length path and the streamed / no
  Content-Length path counted by ``MaxBodySizeMiddleware`` (Req 17.5 / 21.*).
- Auth rate-limit BOUNDARY: exactly ``limit`` requests allowed, the next
  blocked with a positive ``retry_after``; independent per-client buckets; a
  new time window resets; fail-open when Redis raises (Req 21.2).
- CORS allowlist: allowlisted origin echoed, non-allowlisted omitted (Req 21.1).
- Forwarded-proto HTTPS at the ASGI-scope level via a mounted probe (Req 21.3).
- Security headers present on 200, 404, and 500 responses (Req 21.4).
- Error-response scrubbing: generic 500 leaks no internals; an ``APIError``
  whose ``fields`` carry a secret-looking key has that value redacted while a
  benign field is preserved (Req 17.4).
- Malformed-payload field naming: an ``extra="forbid"`` request model names the
  offending field in the 422 envelope (Req 17.2).
- Startup log scrubbing: ``load_startup_settings`` logs the safe_dump and never
  the raw ``ENCRYPTION_KEY`` value at INFO (Req 18.4).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import APIError, install_exception_handlers
from app.core.middleware import (
    MaxBodySizeMiddleware,
    check_auth_rate_limit,
    install_middleware,
)
from app.schemas.base import BaseRequest


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


class IncrRaisesRedis:
    """Async fake whose ``incr`` raises, to exercise the fail-open path."""

    async def incr(self, key: str) -> int:
        raise ConnectionError("redis down")

    async def expire(self, key: str, seconds: int) -> None:  # pragma: no cover
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


async def _echo_bytes_probe(scope, receive, send) -> None:
    """Raw-ASGI app that drains the request body and returns its length.

    Mounted (rather than added as a FastAPI ``request: Request`` route) so the
    request traverses the full middleware stack — including MaxBodySizeMiddleware
    — without tripping FastAPI's route/dependant analysis, which can 422 an
    otherwise-valid raw-body request under the test client.
    """
    total = 0
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":  # pragma: no cover
            return
        total += len(message.get("body", b""))
        more = message.get("more_body", False)
    body = str(total).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _build_app(settings: Settings, redis_factory=None) -> FastAPI:
    """Build an app with the middleware + handlers, a /health route, and a
    mounted raw-ASGI /echo-bytes probe for exact body-size boundary checks."""
    app = FastAPI()
    install_middleware(app, settings, redis_factory=redis_factory)
    install_exception_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/echo-bytes", _echo_bytes_probe)

    return app


# --- Body-size BOUNDARY: Content-Length path (Req 17.5 / 21.*) ---------------

def test_body_exactly_at_limit_is_accepted_content_length() -> None:
    limit = 32
    settings = _base_settings(MAX_BODY_SIZE_BYTES=limit)
    client = TestClient(_build_app(settings))

    resp = client.post("/echo-bytes", content=b"x" * limit)
    assert resp.status_code == 200
    assert resp.text == str(limit)


def test_body_one_byte_over_limit_is_rejected_content_length() -> None:
    limit = 32
    settings = _base_settings(MAX_BODY_SIZE_BYTES=limit)
    client = TestClient(_build_app(settings))

    resp = client.post("/echo-bytes", content=b"x" * (limit + 1))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


# --- Body-size BOUNDARY: streamed / no Content-Length path (Req 17.5) --------
#
# TestClient always sets Content-Length for a bytes body, so to exercise the
# chunked/no-length branch of MaxBodySizeMiddleware we drive its ASGI interface
# directly with a hand-built receive stream that omits Content-Length.

@pytest.mark.asyncio
async def test_streamed_body_exactly_at_limit_is_accepted_no_content_length() -> None:
    limit = 16
    downstream_len: dict[str, int] = {}

    async def downstream(scope, receive, send):
        # Drain the (replayed) body and record its size.
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        downstream_len["total"] = total
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = MaxBodySizeMiddleware(downstream, max_body_size=limit)
    scope = {"type": "http", "headers": [], "path": "/x", "method": "POST"}

    # Two chunks summing exactly to the limit, no Content-Length header.
    chunks = [
        {"type": "http.request", "body": b"a" * 10, "more_body": True},
        {"type": "http.request", "body": b"b" * 6, "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)

    # Reached downstream with the full body; no 413 emitted.
    assert downstream_len["total"] == limit
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_streamed_body_one_over_limit_rejected_no_content_length() -> None:
    limit = 16
    reached_downstream = {"hit": False}

    async def downstream(scope, receive, send):  # pragma: no cover - must not run
        reached_downstream["hit"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = MaxBodySizeMiddleware(downstream, max_body_size=limit)
    scope = {"type": "http", "headers": [], "path": "/x", "method": "POST"}

    chunks = [
        {"type": "http.request", "body": b"a" * 10, "more_body": True},
        {"type": "http.request", "body": b"b" * 7, "more_body": False},  # 17 > 16
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)

    assert reached_downstream["hit"] is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


# --- Auth rate-limit BOUNDARY (Req 21.2) -------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_exact_boundary_allows_limit_blocks_next() -> None:
    redis = FakeRedis()
    now = 2000.0
    limit = 5

    # Exactly ``limit`` requests in the window are allowed.
    for _ in range(limit):
        allowed, retry = await check_auth_rate_limit(
            redis, "10.0.0.1", limit=limit, now=now
        )
        assert allowed is True
        assert retry == 0

    # The (limit + 1)th is blocked with a positive retry_after.
    blocked, retry_after = await check_auth_rate_limit(
        redis, "10.0.0.1", limit=limit, now=now
    )
    assert blocked is False
    assert retry_after > 0


@pytest.mark.asyncio
async def test_rate_limit_buckets_are_independent_per_client() -> None:
    redis = FakeRedis()
    now = 2000.0

    # Exhaust client A's single-request budget.
    a_first, _ = await check_auth_rate_limit(redis, "client-a", limit=1, now=now)
    a_second, _ = await check_auth_rate_limit(redis, "client-a", limit=1, now=now)
    # A different client is entirely unaffected.
    b_first, _ = await check_auth_rate_limit(redis, "client-b", limit=1, now=now)

    assert a_first is True
    assert a_second is False
    assert b_first is True


@pytest.mark.asyncio
async def test_rate_limit_resets_in_new_window() -> None:
    redis = FakeRedis()
    now = 3000.0

    first, _ = await check_auth_rate_limit(redis, "client-c", limit=1, now=now)
    blocked, _ = await check_auth_rate_limit(redis, "client-c", limit=1, now=now)
    # Advance a full window (60s): the fixed-window bucket rolls over.
    reset, reset_retry = await check_auth_rate_limit(
        redis, "client-c", limit=1, now=now + 60
    )

    assert first is True
    assert blocked is False
    assert reset is True
    assert reset_retry == 0


@pytest.mark.asyncio
async def test_rate_limit_fails_open_when_incr_raises() -> None:
    allowed, retry = await check_auth_rate_limit(
        IncrRaisesRedis(), "client-x", limit=1, now=1.0
    )
    assert allowed is True
    assert retry == 0


# --- CORS allowlist (Req 21.1) -----------------------------------------------

def test_cors_echoes_allowlisted_origin_and_omits_others() -> None:
    settings = _base_settings(CORS_ALLOW_ORIGINS=["https://good.example.com"])
    client = TestClient(_build_app(settings))

    allowed = client.get("/health", headers={"Origin": "https://good.example.com"})
    assert allowed.status_code == 200
    assert (
        allowed.headers.get("access-control-allow-origin")
        == "https://good.example.com"
    )

    denied = client.get("/health", headers={"Origin": "https://bad.example.com"})
    assert denied.status_code == 200
    assert denied.headers.get("access-control-allow-origin") != "https://bad.example.com"


# --- Forwarded-proto HTTPS (Req 21.3) ----------------------------------------

def test_forwarded_proto_https_sets_scope_scheme() -> None:
    """X-Forwarded-Proto: https rewrites the ASGI scope scheme to https.

    Asserted at the scope level via a mounted raw-ASGI probe so the check
    targets ProxyHeadersMiddleware directly and avoids FastAPI route analysis.
    """
    captured: dict[str, str] = {}

    async def probe(scope, receive, send):
        captured["scheme"] = scope.get("scheme", "")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    settings = _base_settings()
    app = FastAPI()
    install_middleware(app, settings)
    install_exception_handlers(app)
    app.mount("/scheme", probe)

    client = TestClient(app)
    resp = client.get("/scheme", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    assert captured["scheme"] == "https"


# --- Security headers on 200 / 404 / 500 (Req 21.4) --------------------------

def _assert_security_headers(headers) -> None:
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_security_headers_on_200_404_and_500() -> None:
    settings = _base_settings()
    app = _build_app(settings)

    @app.get("/kaboom")
    async def kaboom() -> dict:
        raise RuntimeError("internal /secret/path detail")

    client = TestClient(app, raise_server_exceptions=False)

    ok = client.get("/health")
    assert ok.status_code == 200
    _assert_security_headers(ok.headers)

    missing = client.get("/does-not-exist")
    assert missing.status_code == 404
    _assert_security_headers(missing.headers)

    error = client.get("/kaboom")
    assert error.status_code == 500
    _assert_security_headers(error.headers)


# --- Error-response scrubbing / no internals (Req 17.4) ----------------------

def test_generic_500_reveals_no_internals() -> None:
    settings = _base_settings()
    app = _build_app(settings)

    @app.get("/explode")
    async def explode() -> dict:
        raise RuntimeError("db://user:hunter2@host leaked /etc/passwd path")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/explode")

    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }
    text = resp.text
    assert "Traceback" not in text
    assert "hunter2" not in text
    assert "/etc/passwd" not in text
    assert "/explode" not in text


def test_api_error_fields_scrub_secret_key_and_preserve_benign() -> None:
    settings = _base_settings()
    app = _build_app(settings)

    @app.get("/api-error")
    async def api_error() -> dict:
        raise APIError(
            status_code=400,
            code="bad_request",
            message="Invalid input.",
            fields={"access_token": "abc", "name": "ok"},
        )

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api-error")

    assert resp.status_code == 400
    fields = resp.json()["error"]["fields"]
    assert fields["access_token"] == "***"
    assert "abc" not in resp.text
    assert fields["name"] == "ok"


# --- Malformed-payload field naming (Req 17.2) -------------------------------

class _WidgetRequest(BaseRequest):
    """extra="forbid" model used to exercise 422 field naming."""

    name: str
    quantity: int


def test_malformed_payload_names_offending_field_in_422() -> None:
    settings = _base_settings()
    app = _build_app(settings)

    @app.post("/widgets")
    async def create_widget(payload: _WidgetRequest) -> dict:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)

    # Wrong type for a declared field -> the field is named in the envelope.
    resp = client.post("/widgets", json={"name": "gadget", "quantity": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "quantity" in body["error"]["fields"]


def test_extra_forbidden_field_is_named_in_422() -> None:
    settings = _base_settings()
    app = _build_app(settings)

    @app.post("/widgets2")
    async def create_widget2(payload: _WidgetRequest) -> dict:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)

    # An undeclared field is rejected by extra="forbid" and named.
    resp = client.post(
        "/widgets2",
        json={"name": "g", "quantity": 1, "surprise": "x"},
    )
    assert resp.status_code == 422
    fields = resp.json()["error"]["fields"]
    assert any("surprise" in key for key in fields)


# --- Startup log scrubbing (Req 18.4) ----------------------------------------

def test_startup_logs_safe_dump_and_never_raw_encryption_key(
    monkeypatch, valid_env, caplog
) -> None:
    from app.main import load_startup_settings

    for key, value in valid_env.items():
        monkeypatch.setenv(key, value)

    with caplog.at_level(logging.INFO, logger="atomic_ai.startup"):
        load_startup_settings()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    # The safe_dump view is logged (redaction marker present) ...
    assert "***" in log_text
    # ... and the raw ENCRYPTION_KEY value never appears at INFO.
    assert valid_env["ENCRYPTION_KEY"] not in log_text
