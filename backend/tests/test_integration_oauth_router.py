"""Router tests for the integrations OAuth flow (task 5.3).

These pin the HTTP seam in :mod:`app.api.integrations_oauth` — session/tenancy/
RBAC on the authorize endpoint and the always-redirect behavior of the public
callback endpoint — WITHOUT Docker/Postgres/Redis and WITHOUT any live provider
round-trip (Req 13.3). The router only touches ``session.get(Integration, id)``,
``ctx.member_role(...)``, ``can(role, ...)``, and delegates the flow logic to
:class:`~app.services.integration_oauth.IntegrationOAuthService`; so a tiny
in-memory fake session plus an injectable fake service exercise every branch
the task requires. Provider network I/O, when it is reached at all, is only ever
exercised through the injected fake ``AsyncHTTPClient`` used by the
service-level tests — never here.

A FastAPI app is built with the ``integrations_oauth`` router mounted and its
dependencies overridden:

- ``require_session`` → a chosen :class:`~app.core.tenancy.RequestContext`
  (caller/role), or *omitted* to prove a missing session is rejected (Req 2.1).
- ``get_session`` → a fake async session serving a seeded ``Integration`` by id.
- ``get_redis`` → a ``FakeRedis`` (so a "no Redis record" assertion is real).
- ``_oauth_service`` → a fake ``IntegrationOAuthService`` that either records the
  ``begin_authorize`` call and returns a canned authorization URL, or raises the
  service's own :class:`~app.core.errors.APIError` (unsupported provider,
  invalid state, etc.) so the router's mapping is what is under test.

Properties covered (from design.md "Correctness Properties"):

- **Property 4** — foreign or absent integrations are indistinguishable (404).
  Validates: Requirements 2.2.
- **Property 5** — callers without authority are rejected (403).
  Validates: Requirements 2.5.
- **Property 6** — non-OAuth-family providers cannot begin a flow (400, no Redis
  record). Validates: Requirements 2.6, 9.4.
- **Property 9** — every callback outcome redirects to the frontend with a
  status (302 with ``oauth=success|error``; success carries ``integration_id``).
  Validates: Requirements 3.5, 3.6.

Also asserted directly: authorize rejects a missing session (Req 2.1), the
callback requires no session (Req 3.1), and no token material appears in any
response (Req 7.2).

Validates: Requirements 2.1, 2.2, 2.5, 2.6, 3.1, 3.5, 3.6, 7.2, 9.4.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.api import integrations_oauth
from app.api.deps import get_redis, require_session
from app.api.integrations_oauth import _oauth_service, router as oauth_router
from app.core.errors import APIError, install_exception_handlers
from app.core.tenancy import RequestContext
from app.db.models import (
    Integration,
    IntegrationCategory,
    IntegrationStatus,
    MemberRole,
)
from app.db.session import get_session
from app.services.integration_oauth import AuthorizeRedirect, CompletedAuthorization


# ===========================================================================
# No-I/O test doubles
# ===========================================================================


class FakeRedis:
    """In-memory async stand-in for the Redis surface the router passes through."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class _FakeDBSession:
    """Minimal async-session stand-in: ``get(Integration, id)`` returns a seed.

    The authorize route only ever calls ``session.get(Integration, id)`` before
    delegating to the (faked) service, so a dict keyed by id is enough.
    """

    def __init__(self, rows: dict[uuid.UUID, Integration] | None = None) -> None:
        self._rows: dict[uuid.UUID, Integration] = dict(rows or {})

    async def get(self, _model: type, ident: uuid.UUID) -> Integration | None:
        return self._rows.get(ident)


class _FakeAuthorizeService:
    """Fake service whose ``begin_authorize`` returns a canned redirect.

    Records the call so tests can assert the router reached the service only
    after the tenancy/RBAC gates passed.
    """

    def __init__(self, authorization_url: str = "https://provider.example/auth?x=1"):
        self._url = authorization_url
        self.begin_calls: list[dict] = []

    async def begin_authorize(self, *, integration, user_id, session, redis):
        self.begin_calls.append(
            {
                "integration": integration,
                "user_id": user_id,
                "session": session,
                "redis": redis,
            }
        )
        return AuthorizeRedirect(authorization_url=self._url, state="state-token")


class _RaisingBeginService:
    """Fake service whose ``begin_authorize`` raises a service ``APIError``.

    Used for the non-OAuth-family case (Property 6): the real service raises a
    400 ``unsupported_provider`` from ``lookup`` inside ``begin_authorize``; the
    router must surface that as a 400 and never write a Redis record.
    """

    def __init__(self, error: APIError) -> None:
        self._error = error
        self.begin_calls = 0

    async def begin_authorize(self, *, integration, user_id, session, redis):
        self.begin_calls += 1
        raise self._error


class _CallbackService:
    """Fake service whose ``complete_authorize`` returns a canned success."""

    def __init__(self, completed: CompletedAuthorization) -> None:
        self._completed = completed
        self.complete_calls = 0

    async def complete_authorize(self, **_kwargs):
        self.complete_calls += 1
        return self._completed


class _RaisingCallbackService:
    """Fake service whose ``complete_authorize`` raises a service ``APIError``."""

    def __init__(self, error: APIError) -> None:
        self._error = error
        self.complete_calls = 0

    async def complete_authorize(self, **_kwargs):
        self.complete_calls += 1
        raise self._error


# ===========================================================================
# Fixtures / helpers
# ===========================================================================

_FRONTEND_ORIGIN = "http://frontend.example:3000"


@pytest.fixture(autouse=True)
def _frontend_settings(monkeypatch: pytest.MonkeyPatch):
    """Stub ``get_settings`` in the router so the callback builds a stable URL.

    The callback derives the frontend origin from ``POST_LOGIN_REDIRECT_URL``.
    Pinning it makes the redirect ``Location`` assertions deterministic without
    depending on the process-wide settings singleton (conftest strips env).
    """

    class _Settings:
        POST_LOGIN_REDIRECT_URL = _FRONTEND_ORIGIN

    monkeypatch.setattr(integrations_oauth, "get_settings", lambda: _Settings())


def _make_integration(
    *,
    integration_id: uuid.UUID,
    workspace_id: uuid.UUID,
    creator_id: uuid.UUID,
    provider_name: str = "gmail",
) -> Integration:
    integration = Integration(
        workspace_id=workspace_id,
        created_by_user_id=creator_id,
        category=IntegrationCategory.EMAIL,
        provider_name=provider_name,
        is_shared_with_workspace=False,
        status=IntegrationStatus.ERROR,
        encrypted_credentials=b"ciphertext-not-a-token",
    )
    integration.id = integration_id
    return integration


def _make_context(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID, role: MemberRole
) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        active_workspace_id=workspace_id,
        is_superadmin=False,
        roles={workspace_id: role},
    )


def _build_app(
    *,
    session: _FakeDBSession,
    redis: FakeRedis,
    service: object,
    ctx: RequestContext | None,
) -> FastAPI:
    """Mount the oauth router with all dependencies overridden.

    When ``ctx`` is None, ``require_session`` is left un-overridden so the real
    dependency runs and rejects the request as unauthenticated (Req 2.1).
    """
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(oauth_router)

    async def _get_session_override():
        yield session

    async def _get_redis_override():
        yield redis

    def _service_override():
        return service

    app.dependency_overrides[get_session] = _get_session_override
    app.dependency_overrides[get_redis] = _get_redis_override
    app.dependency_overrides[_oauth_service] = _service_override
    if ctx is not None:
        app.dependency_overrides[require_session] = lambda: ctx
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    )


# Recognizable token-shaped material that must never appear in any response.
_TOKEN_MATERIAL = ("access_token", "refresh_token", "client_secret", "code_verifier")


# ===========================================================================
# Authorize — missing session is rejected (Req 2.1)
# ===========================================================================


@pytest.mark.asyncio
async def test_authorize_without_session_is_unauthenticated():
    """Req 2.1: the authorize endpoint rejects a request with no Session_Token.

    ``require_session`` is left un-overridden; with no session cookie the real
    dependency raises the central ``unauthorized`` APIError (401).
    """
    integration_id = uuid.uuid4()
    session = _FakeDBSession()
    redis = FakeRedis()
    service = _FakeAuthorizeService()
    app = _build_app(session=session, redis=redis, service=service, ctx=None)

    # get_session must be safe to touch even though require_session should reject
    # first; the override yields the fake session regardless.
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/integrations/{integration_id}/oauth/authorize")

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    # The service was never reached.
    assert service.begin_calls == []


# ===========================================================================
# Property 4: Foreign or absent integrations are indistinguishable (404)
#             — Validates Req 2.2
# ===========================================================================


@pytest.mark.asyncio
async def test_property4_absent_and_foreign_integrations_return_identical_404():
    """Feature: integration-oauth-flow, Property 4: Foreign or absent integrations are indistinguishable (404).

    An id that does not exist and an id that exists in a workspace the caller is
    not a member of both yield an identical 404 body — existence is not
    disclosed.
    """
    caller_ws = uuid.uuid4()
    caller_id = uuid.uuid4()
    ctx = _make_context(user_id=caller_id, workspace_id=caller_ws, role=MemberRole.OWNER)

    # --- Absent: the id is not in the session at all. ---
    absent_id = uuid.uuid4()
    absent_service = _FakeAuthorizeService()
    absent_app = _build_app(
        session=_FakeDBSession(),
        redis=FakeRedis(),
        service=absent_service,
        ctx=ctx,
    )
    async with _client(absent_app) as client:
        absent_resp = await client.get(
            f"/api/v1/integrations/{absent_id}/oauth/authorize"
        )

    # --- Foreign: the id exists but belongs to a workspace the caller is not in. ---
    foreign_ws = uuid.uuid4()
    foreign_id = uuid.uuid4()
    foreign_integration = _make_integration(
        integration_id=foreign_id,
        workspace_id=foreign_ws,  # caller has no role here
        creator_id=uuid.uuid4(),
    )
    foreign_service = _FakeAuthorizeService()
    foreign_app = _build_app(
        session=_FakeDBSession({foreign_id: foreign_integration}),
        redis=FakeRedis(),
        service=foreign_service,
        ctx=ctx,
    )
    async with _client(foreign_app) as client:
        foreign_resp = await client.get(
            f"/api/v1/integrations/{foreign_id}/oauth/authorize"
        )

    assert absent_resp.status_code == 404
    assert foreign_resp.status_code == 404
    # Identical body: existence of a foreign integration is not disclosed.
    assert absent_resp.json() == foreign_resp.json()
    assert absent_resp.json()["error"]["code"] == "not_found"
    # Neither reached the service.
    assert absent_service.begin_calls == []
    assert foreign_service.begin_calls == []


# ===========================================================================
# Authorize — creator and manage-capability holders are permitted (Req 2.3/2.4)
# ===========================================================================


@pytest.mark.asyncio
async def test_authorize_creator_is_permitted_and_redirects_to_provider():
    """Req 2.3 + 1.1: the integration creator may authorize; router 302s to the URL."""
    ws = uuid.uuid4()
    creator_id = uuid.uuid4()
    integration_id = uuid.uuid4()
    integration = _make_integration(
        integration_id=integration_id, workspace_id=ws, creator_id=creator_id
    )
    # Creator is only a VIEWER (no MANAGE_INTEGRATIONS) — creator rule still permits.
    ctx = _make_context(user_id=creator_id, workspace_id=ws, role=MemberRole.VIEWER)
    service = _FakeAuthorizeService(authorization_url="https://provider.example/auth?ok=1")
    app = _build_app(
        session=_FakeDBSession({integration_id: integration}),
        redis=FakeRedis(),
        service=service,
        ctx=ctx,
    )
    async with _client(app) as client:
        resp = await client.get(
            f"/api/v1/integrations/{integration_id}/oauth/authorize"
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://provider.example/auth?ok=1"
    assert len(service.begin_calls) == 1
    assert service.begin_calls[0]["user_id"] == creator_id
    # Req 7.2 — no token material in the (empty) redirect response body.
    for token in _TOKEN_MATERIAL:
        assert token not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.ADMIN])
async def test_authorize_non_creator_with_manage_capability_is_permitted(role):
    """Req 2.4: a non-creating Owner/Admin holding MANAGE_INTEGRATIONS may authorize."""
    ws = uuid.uuid4()
    integration_id = uuid.uuid4()
    integration = _make_integration(
        integration_id=integration_id,
        workspace_id=ws,
        creator_id=uuid.uuid4(),  # someone else created it
    )
    caller_id = uuid.uuid4()
    ctx = _make_context(user_id=caller_id, workspace_id=ws, role=role)
    service = _FakeAuthorizeService()
    app = _build_app(
        session=_FakeDBSession({integration_id: integration}),
        redis=FakeRedis(),
        service=service,
        ctx=ctx,
    )
    async with _client(app) as client:
        resp = await client.get(
            f"/api/v1/integrations/{integration_id}/oauth/authorize"
        )

    assert resp.status_code == 302
    assert len(service.begin_calls) == 1


# ===========================================================================
# Property 5: Callers without authority are rejected (403) — Validates Req 2.5
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MemberRole.MEMBER, MemberRole.VIEWER])
async def test_property5_non_creator_without_capability_is_forbidden(role):
    """Feature: integration-oauth-flow, Property 5: Callers without authority are rejected (403).

    A caller who is neither the creator nor a holder of MANAGE_INTEGRATIONS
    (a non-creating Member/Viewer) is rejected with 403, and the service is
    never reached.
    """
    ws = uuid.uuid4()
    integration_id = uuid.uuid4()
    integration = _make_integration(
        integration_id=integration_id,
        workspace_id=ws,
        creator_id=uuid.uuid4(),  # created by someone else
    )
    caller_id = uuid.uuid4()
    ctx = _make_context(user_id=caller_id, workspace_id=ws, role=role)
    service = _FakeAuthorizeService()
    app = _build_app(
        session=_FakeDBSession({integration_id: integration}),
        redis=FakeRedis(),
        service=service,
        ctx=ctx,
    )
    async with _client(app) as client:
        resp = await client.get(
            f"/api/v1/integrations/{integration_id}/oauth/authorize"
        )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert service.begin_calls == []


# ===========================================================================
# Property 6: Non-OAuth-family providers cannot begin a flow (400, no Redis)
#             — Validates Req 2.6, 9.4
# ===========================================================================


@pytest.mark.asyncio
async def test_property6_non_oauth_family_provider_is_400_with_no_redis_record():
    """Feature: integration-oauth-flow, Property 6: Non-OAuth-family providers cannot begin a flow.

    An integration whose provider is not OAuth-family surfaces the service's
    400 ``unsupported_provider`` (raised by ``lookup`` inside ``begin_authorize``)
    and no Redis flow record is written.
    """
    ws = uuid.uuid4()
    creator_id = uuid.uuid4()
    integration_id = uuid.uuid4()
    # "slack" is a bot-token provider — deliberately NOT in OAUTH_PROVIDERS.
    integration = _make_integration(
        integration_id=integration_id,
        workspace_id=ws,
        creator_id=creator_id,
        provider_name="slack",
    )
    ctx = _make_context(user_id=creator_id, workspace_id=ws, role=MemberRole.OWNER)
    redis = FakeRedis()
    service = _RaisingBeginService(
        APIError(
            status_code=400,
            code="unsupported_provider",
            message="This provider does not support in-app OAuth authorization.",
        )
    )
    app = _build_app(
        session=_FakeDBSession({integration_id: integration}),
        redis=redis,
        service=service,
        ctx=ctx,
    )
    async with _client(app) as client:
        resp = await client.get(
            f"/api/v1/integrations/{integration_id}/oauth/authorize"
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_provider"
    # begin_authorize was reached (gates passed) but raised; no Redis record.
    assert service.begin_calls == 1
    assert redis.store == {}


# ===========================================================================
# Property 9: Every callback outcome redirects to the frontend with a status
#             — Validates Req 3.5, 3.6 (callback is public — Req 3.1)
# ===========================================================================


def _callback_app(service: object) -> FastAPI:
    """Build an app for the PUBLIC callback (no require_session override)."""
    return _build_app(
        session=_FakeDBSession(), redis=FakeRedis(), service=service, ctx=None
    )


@pytest.mark.asyncio
async def test_property9_callback_success_redirects_with_status_and_integration_id():
    """Feature: integration-oauth-flow, Property 9: Every callback outcome redirects to the frontend with a status.

    A successful callback → 302 to the frontend integrations page carrying
    ``oauth=success`` and the ``integration_id`` (Req 3.5, 3.6). No session is
    required (Req 3.1). No token material leaks (Req 7.2).
    """
    integration_id = uuid.uuid4()
    service = _CallbackService(
        CompletedAuthorization(integration_id=integration_id, workspace_id=uuid.uuid4())
    )
    app = _callback_app(service)

    async with _client(app) as client:
        resp = await client.get(
            "/api/v1/integrations/oauth/callback/gmail",
            params={"code": "the-code", "state": "the-state"},
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    parts = urlsplit(location)
    # Redirected back to the frontend integrations page (public callback, Req 3.1).
    assert parts.netloc == urlsplit(_FRONTEND_ORIGIN).netloc
    assert parts.path == "/dashboard/integrations"
    query = parse_qs(parts.query)
    assert query["oauth"] == ["success"]
    assert query["integration"] == [str(integration_id)]  # Req 3.6
    assert service.complete_calls == 1
    for token in _TOKEN_MATERIAL:
        assert token not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (
            APIError(
                status_code=401,
                code="invalid_state",
                message="The OAuth state is invalid or has expired.",
            ),
            "state",
        ),
        (
            APIError(
                status_code=401,
                code="oauth_exchange_failed",
                message="The authorization code could not be exchanged.",
            ),
            "exchange",
        ),
        (
            APIError(
                status_code=401,
                code="oauth_no_refresh_token",
                message="The provider did not return a refresh token.",
                fields={"reason": "no_refresh_token"},
            ),
            "no_refresh_token",
        ),
    ],
)
async def test_property9_callback_failure_redirects_with_error_status(
    error, expected_reason
):
    """Feature: integration-oauth-flow, Property 9: Every callback outcome redirects to the frontend with a status.

    Every failure outcome (invalid state, failed exchange, missing refresh
    token) → 302 to the frontend page carrying ``oauth=error`` and a mapped
    ``reason`` — never a raw 4xx (Req 3.5). No token material leaks (Req 7.2).
    """
    service = _RaisingCallbackService(error)
    app = _callback_app(service)

    async with _client(app) as client:
        resp = await client.get(
            "/api/v1/integrations/oauth/callback/gmail",
            params={"code": "the-code", "state": "the-state"},
        )

    assert resp.status_code == 302
    parts = urlsplit(resp.headers["location"])
    assert parts.path == "/dashboard/integrations"
    query = parse_qs(parts.query)
    assert query["oauth"] == ["error"]
    assert query["reason"] == [expected_reason]
    # No integration id is disclosed on failure.
    assert "integration" not in query
    for token in _TOKEN_MATERIAL:
        assert token not in resp.text


@pytest.mark.asyncio
async def test_callback_requires_no_session():
    """Req 3.1: the callback carries no session dependency and answers without one.

    The app is built WITHOUT a ``require_session`` override; a successful
    callback still returns a 302 (it would 401 if the route required a session).
    """
    integration_id = uuid.uuid4()
    service = _CallbackService(
        CompletedAuthorization(integration_id=integration_id, workspace_id=uuid.uuid4())
    )
    app = _callback_app(service)

    async with _client(app) as client:
        resp = await client.get(
            "/api/v1/integrations/oauth/callback/gmail",
            params={"code": "c", "state": "s"},
        )

    assert resp.status_code == 302
    assert service.complete_calls == 1
