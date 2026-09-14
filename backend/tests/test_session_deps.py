"""Tests for the session dependency, cookies, and logout (task 5.3).

These exercise :mod:`app.api.deps` through a tiny FastAPI app with one protected
route (``GET /me`` depending on :func:`require_session`), a ``POST /login`` that
sets the session cookie, and a ``POST /logout`` that invalidates the current
session. The DB/Redis seams are faked (an in-memory session store and a fake
Redis) and injected by overriding the ``get_session`` dependency and
monkeypatching ``load_session``/``revoke_session``, so no Postgres/Redis is
needed.

Covers:
- No token -> 401; invalid token -> 401; expired session -> 401 (Req 2.2, 2.4).
- Valid session -> 200 and the RequestContext carries the right ``user_id``.
- Public-route allowlist (Req 2.1): ``/auth`` and ``/health`` are public.
- Cookie attributes: Set-Cookie has HttpOnly, Secure, SameSite=Lax (Req 2.6).
- Logout invalidates the session (subsequent request -> 401) and clears the
  cookie (Req 2.5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    is_public_path,
    logout,
    require_session,
    set_session_cookie,
)
from app.core.errors import install_exception_handlers
from app.core.security import hash_token
from app.core.tenancy import RequestContext
from app.db.models import MemberRole


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeSessionRow:
    """Minimal stand-in for the ``Session`` ORM row used by the deps."""

    user_id: uuid.UUID
    revoked: bool
    expires_at: datetime


@dataclass
class FakeUser:
    """Minimal stand-in for the ``User`` ORM row used by the deps."""

    id: uuid.UUID
    is_superadmin: bool = False


class FakeDBSession:
    """In-memory async session covering just what the deps touch.

    Holds session rows keyed by ``sha256(raw_token)`` (mirroring how the real
    ``Session.token`` column is stored) plus a user directory and a membership
    map. ``get(User, id)`` and ``execute(select(...))`` are implemented enough
    for ``require_session``/``logout`` to resolve a context.
    """

    def __init__(self) -> None:
        self.rows: dict[bytes, FakeSessionRow] = {}
        self.users: dict[uuid.UUID, FakeUser] = {}
        # {user_id: {workspace_id: role}}
        self.memberships: dict[uuid.UUID, dict[uuid.UUID, MemberRole]] = {}

    # -- helpers used by the fake load/revoke functions -------------------
    def add_session(self, raw_token: str, row: FakeSessionRow) -> None:
        self.rows[hash_token(raw_token)] = row

    def get_row(self, raw_token: str) -> FakeSessionRow | None:
        return self.rows.get(hash_token(raw_token))

    # -- AsyncSession surface --------------------------------------------
    async def get(self, model, pk):  # noqa: ANN001 - mirrors AsyncSession.get
        return self.users.get(pk)

    async def execute(self, stmt):  # noqa: ANN001 - membership SELECT only
        return _FakeResult(self)


class _FakeResult:
    """Result of the membership SELECT: yields ``(workspace_id, role)`` rows."""

    def __init__(self, db: FakeDBSession) -> None:
        self._db = db
        self._user_id = _CURRENT_USER_ID.get()

    def all(self):
        mapping = self._db.memberships.get(self._user_id, {})
        return [(ws_id, role) for ws_id, role in mapping.items()]


# The membership SELECT in deps filters by user_id, but our fake result doesn't
# parse the statement; instead the resolving code sets the "current user" via a
# ContextVar-free shim. We keep it simple: only one user authenticates per
# request in these tests, tracked on the fake DB.
class _CurrentUser:
    def __init__(self) -> None:
        self._value: uuid.UUID | None = None

    def set(self, value: uuid.UUID | None) -> None:
        self._value = value

    def get(self) -> uuid.UUID | None:
        return self._value


_CURRENT_USER_ID = _CurrentUser()


class FakeRedis:
    """In-memory async Redis stand-in exposing just ``delete``/``exists``."""

    def __init__(self) -> None:
        self.store: set[str] = set()

    async def delete(self, key: str) -> None:
        self.store.discard(key)

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


# ---------------------------------------------------------------------------
# App + fixtures
# ---------------------------------------------------------------------------


def _make_app(db: FakeDBSession, redis: FakeRedis) -> FastAPI:
    """Build a tiny app wiring require_session/login/logout against the fakes."""
    app = FastAPI()
    # Install the central handlers so a raised APIError becomes the 401 envelope
    # (matching how the real app renders it), rather than propagating.
    install_exception_handlers(app)

    async def _get_session_override():
        yield db

    # Route uses the real require_session; its get_session dependency is
    # overridden to the fake below.
    @app.get("/me")
    async def me(ctx: RequestContext = Depends(require_session)) -> dict:
        return {"user_id": str(ctx.user_id), "is_superadmin": ctx.is_superadmin}

    @app.post("/login")
    async def login(response: Response) -> dict:
        # A stand-in "login": mint a token, store a live session, set cookie.
        raw_token = "raw-token-abc"
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        db.add_session(
            raw_token, FakeSessionRow(user_id=_LOGIN_USER_ID, revoked=False, expires_at=expires_at)
        )
        db.users[_LOGIN_USER_ID] = FakeUser(id=_LOGIN_USER_ID)
        set_session_cookie(response, raw_token, expires_at)
        return {"ok": True}

    @app.post("/logout")
    async def logout_route(request: Request, response: Response) -> dict:
        revoked = await logout(request, response, session=db, redis=redis)
        return {"revoked": revoked}

    # Public routes that omit require_session entirely (Req 2.1).
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/auth/login")
    async def auth_login() -> dict:
        return {"public": True}

    app.dependency_overrides[deps.get_session] = _get_session_override
    return app


_LOGIN_USER_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _patch_seams(monkeypatch: pytest.MonkeyPatch):
    """Route load_session/revoke_session at the fake DB and track current user."""

    async def _fake_load_session(session, raw_token):
        row = session.get_row(raw_token)
        if row is not None:
            _CURRENT_USER_ID.set(row.user_id)
        return row

    async def _fake_revoke_session(session, raw_token):
        row = session.get_row(raw_token)
        if row is None:
            return False
        row.revoked = True
        return True

    monkeypatch.setattr(deps, "load_session", _fake_load_session)
    monkeypatch.setattr(deps, "revoke_session", _fake_revoke_session)
    yield


@pytest.fixture
def db() -> FakeDBSession:
    return FakeDBSession()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def client(db: FakeDBSession, redis: FakeRedis) -> TestClient:
    return TestClient(_make_app(db, redis))


# ---------------------------------------------------------------------------
# 401 cases: missing / invalid / expired (Req 2.2, 2.4)
# ---------------------------------------------------------------------------


def test_no_token_returns_401(client: TestClient):
    resp = client.get("/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_invalid_token_returns_401(client: TestClient):
    resp = client.get("/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_invalid_cookie_token_returns_401(client: TestClient):
    client.cookies.set(SESSION_COOKIE_NAME, "bogus")
    resp = client.get("/me")
    assert resp.status_code == 401


def test_expired_session_returns_401(client: TestClient, db: FakeDBSession):
    user_id = uuid.uuid4()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.add_session("expired-token", FakeSessionRow(user_id=user_id, revoked=False, expires_at=past))
    db.users[user_id] = FakeUser(id=user_id)
    resp = client.get("/me", headers={"Authorization": "Bearer expired-token"})
    assert resp.status_code == 401


def test_revoked_session_returns_401(client: TestClient, db: FakeDBSession):
    user_id = uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    db.add_session("revoked-token", FakeSessionRow(user_id=user_id, revoked=True, expires_at=future))
    db.users[user_id] = FakeUser(id=user_id)
    resp = client.get("/me", headers={"Authorization": "Bearer revoked-token"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Valid session (Req 2.2) + context content
# ---------------------------------------------------------------------------


def test_valid_session_returns_200_with_right_user(client: TestClient, db: FakeDBSession):
    user_id = uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    db.add_session("good-token", FakeSessionRow(user_id=user_id, revoked=False, expires_at=future))
    db.users[user_id] = FakeUser(id=user_id, is_superadmin=True)

    resp = client.get("/me", headers={"Authorization": "Bearer good-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(user_id)
    assert body["is_superadmin"] is True


def test_valid_session_via_cookie(client: TestClient, db: FakeDBSession):
    user_id = uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    db.add_session("cookie-token", FakeSessionRow(user_id=user_id, revoked=False, expires_at=future))
    db.users[user_id] = FakeUser(id=user_id)

    client.cookies.set(SESSION_COOKIE_NAME, "cookie-token")
    resp = client.get("/me")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == str(user_id)


def test_active_workspace_resolved_from_header_when_member(
    client: TestClient, db: FakeDBSession
):
    user_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    db.add_session("ws-token", FakeSessionRow(user_id=user_id, revoked=False, expires_at=future))
    db.users[user_id] = FakeUser(id=user_id)
    db.memberships[user_id] = {ws_id: MemberRole.OWNER}

    # Member of the workspace -> accepted; the /me route doesn't echo it but the
    # request must still succeed (a foreign id would be ignored, not rejected).
    resp = client.get(
        "/me",
        headers={"Authorization": "Bearer ws-token", "X-Workspace-Id": str(ws_id)},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Public-route allowlist (Req 2.1)
# ---------------------------------------------------------------------------


def test_public_routes_do_not_require_session(client: TestClient):
    assert client.get("/health").status_code == 200
    assert client.get("/auth/login").status_code == 200


def test_is_public_path_allowlist():
    assert is_public_path("/health") is True
    assert is_public_path("/auth") is True
    assert is_public_path("/auth/login/google") is True
    assert is_public_path("/me") is False
    assert is_public_path("/workspaces") is False
    # A path that merely starts with the same characters is not public.
    assert is_public_path("/authorize") is False
    assert is_public_path("/healthz") is False


# ---------------------------------------------------------------------------
# Cookie attributes (Req 2.6)
# ---------------------------------------------------------------------------


def test_login_sets_secure_httponly_samesite_cookie(client: TestClient):
    resp = client.post("/login")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "max-age=" in lowered


def test_set_session_cookie_clamps_past_expiry_to_zero():
    resp = Response()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    set_session_cookie(resp, "tok", past)
    header = resp.headers.get("set-cookie", "").lower()
    assert "max-age=0" in header


def test_clear_session_cookie_expires_it():
    resp = Response()
    clear_session_cookie(resp)
    header = resp.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE_NAME in header
    # An expiring delete-cookie sets max-age=0 (and an epoch expires).
    assert "max-age=0" in header


# ---------------------------------------------------------------------------
# Logout invalidation (Req 2.5)
# ---------------------------------------------------------------------------


def test_logout_invalidates_session_and_clears_cookie(
    client: TestClient, db: FakeDBSession
):
    user_id = uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    db.add_session("logout-token", FakeSessionRow(user_id=user_id, revoked=False, expires_at=future))
    db.users[user_id] = FakeUser(id=user_id)

    auth = {"Authorization": "Bearer logout-token"}

    # The session is valid before logout.
    assert client.get("/me", headers=auth).status_code == 200

    # Logout revokes the session and clears the cookie.
    logout_resp = client.post("/logout", headers=auth)
    assert logout_resp.status_code == 200
    assert logout_resp.json()["revoked"] is True
    cleared = logout_resp.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE_NAME in cleared
    assert "max-age=0" in cleared

    # A subsequent request with the same token is now rejected (Req 2.5).
    assert client.get("/me", headers=auth).status_code == 401


def test_logout_without_token_clears_cookie_and_reports_false(client: TestClient):
    resp = client.post("/logout")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is False
    assert SESSION_COOKIE_NAME in resp.headers.get("set-cookie", "").lower()
