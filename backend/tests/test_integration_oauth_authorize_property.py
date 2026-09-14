"""Property-based tests for ``IntegrationOAuthService.begin_authorize`` (task 2.2).

These exercise the authorization-URL construction and the pending Redis
Flow_Record produced by ``begin_authorize`` for every OAuth-family provider in
``app.services.integration_oauth.OAUTH_PROVIDERS``. No real Redis, database, or
provider round-trip is used: a ``FakeRedis`` (the pattern from
``tests/test_auth_wiring.py``, extended to record TTL) captures the flow record,
and a tiny ``Integration``-like stand-in supplies ``id`` / ``workspace_id`` /
``provider_name``. The provider network is never contacted (Req 13.3).

Feature: integration-oauth-flow, Property 1: Authorization URL is well-formed
for every OAuth-family provider — the query carries the provider scopes, an
opaque ``state``, and the constructed ``redirect_uri``.

Feature: integration-oauth-flow, Property 2: PKCE S256 is present wherever the
provider supports it — a non-empty ``code_challenge`` plus
``code_challenge_method=S256`` for every ``uses_pkce`` provider.

Feature: integration-oauth-flow, Property 3: Beginning authorization stores a
complete, TTL-bound flow record — ``oauth:integration:{state}`` holds all six
fields (``integration_id``, ``provider``, ``workspace_id``, ``user_id``,
``code_verifier``, ``redirect_uri``) with a TTL of 600 seconds.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 8.1, 8.2.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import parse_qs, urlparse

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.auth_service import OAUTH_FLOW_TTL_SECONDS
from app.services.integration_oauth import (
    OAUTH_PROVIDERS,
    AuthorizeRedirect,
    IntegrationOAuthService,
)

# The redirect base the fake settings expose; ``begin_authorize`` builds the
# per-provider ``redirect_uri`` from it (Req 8.1).
_REDIRECT_BASE = "https://backend.example"
# The client_id the fake credential vault returns for every integration.
_TEST_CLIENT_ID = "test-client-id-1234567890"
_CALLBACK_PATH = "/api/v1/integrations/oauth/callback/{provider}"

# The six fields every pending Flow_Record must carry (Req 1.3, 8.2).
_FLOW_FIELDS = frozenset(
    {
        "integration_id",
        "provider",
        "workspace_id",
        "user_id",
        "code_verifier",
        "redirect_uri",
    }
)


# ===========================================================================
# No-I/O test doubles
# ===========================================================================


class FakeRedis:
    """In-memory async Redis stand-in that also records the TTL per key.

    Mirrors the ``FakeRedis`` from ``tests/test_auth_wiring.py`` but captures the
    ``ex`` argument passed to ``set`` so tests can assert the 600-second TTL on
    the flow record (Property 3 / Req 1.4).
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttl[key] = ex

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)
        self.ttl.pop(key, None)


class FakeIntegration:
    """Minimal ``Integration``-shaped object: the attributes begin_authorize reads.

    ``begin_authorize`` only touches ``id``, ``workspace_id``, and
    ``provider_name``, so a lightweight stand-in avoids constructing a real ORM
    row (no database).
    """

    def __init__(self, *, provider_name: str) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.provider_name = provider_name


class _FakeSettings:
    """Settings surface ``begin_authorize`` reads (just the redirect base)."""

    OAUTH_REDIRECT_BASE_URL = _REDIRECT_BASE


def _make_service() -> IntegrationOAuthService:
    """Build a service with in-memory settings (no real config / secrets)."""
    return IntegrationOAuthService(settings=_FakeSettings())


class _FakeSession:
    """No-op async session stand-in; begin_authorize only passes it through to
    the (monkeypatched) vault ``use_credential`` and never touches it directly."""


class _FakeCredential:
    """Minimal DecryptedCredential-like object exposing ``.get`` for client_id."""

    def __init__(self, mapping: dict) -> None:
        self._m = dict(mapping)
        self.credentials = dict(mapping)
        self.config = {}

    def get(self, key, default=None):
        return self._m.get(key, default)


async def _fake_use_credential(session, *, integration_id):
    """Stub for integration_vault.use_credential: returns stored client_id."""
    return _FakeCredential({"client_id": _TEST_CLIENT_ID, "client_secret": "s"})


def _run_begin_authorize(
    provider_slug: str,
) -> tuple[FakeIntegration, uuid.UUID, FakeRedis, AuthorizeRedirect]:
    """Drive ``begin_authorize`` for one provider and return the pieces to assert."""
    import app.services.integration_oauth as _mod

    svc = _make_service()
    redis = FakeRedis()
    integration = FakeIntegration(provider_name=provider_slug)
    user_id = uuid.uuid4()

    # Patch the vault so begin_authorize can read a stored client_id without a DB.
    _orig = _mod.integration_vault.use_credential
    _mod.integration_vault.use_credential = _fake_use_credential
    try:
        result = asyncio.run(
            svc.begin_authorize(
                integration=integration,
                user_id=user_id,
                session=_FakeSession(),
                redis=redis,
            )
        )
    finally:
        _mod.integration_vault.use_credential = _orig
    return integration, user_id, redis, result


# All OAuth-family slugs, and the subset that supports PKCE.
_ALL_SLUGS = tuple(OAUTH_PROVIDERS.keys())
_PKCE_SLUGS = tuple(s for s, p in OAUTH_PROVIDERS.items() if p.uses_pkce)


# ===========================================================================
# Property 1: authorization URL is well-formed for every OAuth-family provider
# ===========================================================================


@settings(max_examples=200, deadline=None)
@given(slug=st.sampled_from(_ALL_SLUGS))
def test_authorization_url_is_well_formed(slug: str) -> None:
    """Property 1: the authorize URL carries scopes, opaque state, redirect_uri.

    The URL targets the provider's registered ``authorize_url`` host, and its
    query contains the joined provider scopes, the same opaque ``state`` returned
    to the caller, and the ``redirect_uri`` built from the callback base — with
    the ``integration_id`` never appearing in the URL (Req 8.2).

    Feature: integration-oauth-flow, Property 1: Authorization URL is well-formed
    for every OAuth-family provider.

    Validates: Requirements 1.1, 1.2, 8.1.
    """
    integration, _user_id, _redis, result = _run_begin_authorize(slug)
    entry = OAUTH_PROVIDERS[slug]

    parsed = urlparse(result.authorization_url)
    # The URL points at the provider's authorize endpoint (scheme + host + path).
    assert result.authorization_url.startswith(entry.authorize_url + "?")
    assert parsed.query != ""

    q = parse_qs(parsed.query)

    # Opaque state round-trips into the query and matches the returned handle.
    assert q["state"] == [result.state]
    assert result.state

    # Scopes are the joined, space-separated provider scopes.
    expected_scope = " ".join(entry.scopes)
    assert q["scope"] == [expected_scope]
    for scope in entry.scopes:
        assert scope in q["scope"][0]

    # redirect_uri is the fixed, provider-keyed callback (no integration_id).
    expected_redirect = _REDIRECT_BASE + _CALLBACK_PATH.format(provider=slug)
    assert q["redirect_uri"] == [expected_redirect]
    assert str(integration.id) not in result.authorization_url

    # Authorization-code flow.
    assert q["response_type"] == ["code"]

    # The OAuth client_id is present in the URL (required by the provider).
    assert q["client_id"] == [_TEST_CLIENT_ID]

    # Any provider-specific refresh-token params are present in the URL.
    for key, value in entry.extra_authorize_params.items():
        assert q[key] == [value]


# ===========================================================================
# Property 2: PKCE S256 is present wherever the provider supports it
# ===========================================================================


@settings(max_examples=200, deadline=None)
@given(slug=st.sampled_from(_PKCE_SLUGS))
def test_pkce_s256_present_for_pkce_providers(slug: str) -> None:
    """Property 2: PKCE providers carry a non-empty S256 challenge in the URL.

    Feature: integration-oauth-flow, Property 2: PKCE S256 is present wherever
    the provider supports it.

    Validates: Requirements 1.2.
    """
    assert OAUTH_PROVIDERS[slug].uses_pkce is True

    _integration, _user_id, redis, result = _run_begin_authorize(slug)

    q = parse_qs(urlparse(result.authorization_url).query)

    # Non-empty code_challenge + S256 method.
    assert "code_challenge" in q
    assert len(q["code_challenge"]) == 1
    assert q["code_challenge"][0].strip() != ""
    assert q["code_challenge_method"] == ["S256"]

    # The challenge is not the raw stored verifier (S256 derivation, not plain).
    (flow_json,) = list(redis.store.values())
    flow = json.loads(flow_json)
    assert q["code_challenge"][0] != flow["code_verifier"]


# ===========================================================================
# Property 3: begin_authorize stores a complete, TTL-bound flow record
# ===========================================================================


@settings(max_examples=200, deadline=None)
@given(slug=st.sampled_from(_ALL_SLUGS))
def test_flow_record_is_complete_and_ttl_bound(slug: str) -> None:
    """Property 3: oauth:integration:{state} holds all six fields with TTL 600.

    The Redis key is namespaced by the opaque ``state``, its JSON value carries
    exactly the six flow fields with the correct values (integration id,
    provider slug, workspace id, user id, a non-empty code verifier, and the same
    redirect_uri as the URL), and the entry is written with a 600-second TTL.

    Feature: integration-oauth-flow, Property 3: Beginning authorization stores a
    complete, TTL-bound flow record.

    Validates: Requirements 1.3, 1.4, 8.2.
    """
    integration, user_id, redis, result = _run_begin_authorize(slug)

    key = f"oauth:integration:{result.state}"
    assert key in redis.store
    # Exactly one flow record was written.
    assert len(redis.store) == 1

    flow = json.loads(redis.store[key])

    # All six fields present — no more, no less.
    assert set(flow.keys()) == _FLOW_FIELDS

    # Field values match the inputs / provider.
    assert flow["integration_id"] == str(integration.id)
    assert flow["provider"] == slug
    assert flow["workspace_id"] == str(integration.workspace_id)
    assert flow["user_id"] == str(user_id)
    assert flow["code_verifier"]
    assert flow["code_verifier"].strip() != ""

    expected_redirect = _REDIRECT_BASE + _CALLBACK_PATH.format(provider=slug)
    assert flow["redirect_uri"] == expected_redirect
    # The redirect_uri in the record matches the one in the authorize URL.
    q = parse_qs(urlparse(result.authorization_url).query)
    assert q["redirect_uri"] == [flow["redirect_uri"]]

    # TTL is exactly the 600-second flow window (Req 1.4).
    assert redis.ttl[key] == OAUTH_FLOW_TTL_SECONDS
    assert redis.ttl[key] == 600


# ===========================================================================
# Example test: distinct flows get distinct state / verifier (opaque + fresh)
# ===========================================================================


def test_two_flows_for_same_provider_get_distinct_state_and_verifier() -> None:
    """Two authorizations for the same provider mint independent state/verifier.

    Confirms ``state`` is freshly generated per call (opaque, single-use keying)
    and the stored ``code_verifier`` differs between flows.

    Validates: Requirements 1.3, 1.4.
    """
    slug = "gmail"
    _i1, _u1, r1, res1 = _run_begin_authorize(slug)
    _i2, _u2, r2, res2 = _run_begin_authorize(slug)

    assert res1.state != res2.state

    flow1 = json.loads(next(iter(r1.store.values())))
    flow2 = json.loads(next(iter(r2.store.values())))
    assert flow1["code_verifier"] != flow2["code_verifier"]


# ===========================================================================
# Guardrail: an integration without a stored client_id cannot begin a flow
# ===========================================================================


def test_missing_client_id_rejected() -> None:
    """begin_authorize rejects an integration with no client_id (400).

    The provider authorize endpoint requires client_id; if the integration was
    connected without one, we fail fast with a clear 400 rather than emitting a
    broken URL that the provider rejects with "Missing required parameter".

    Validates: Requirements 4.1, 12.2.
    """
    import app.services.integration_oauth as _mod
    from app.core.errors import APIError

    async def _no_client_id(session, *, integration_id):
        return _FakeCredential({"client_secret": "s"})  # no client_id

    svc = _make_service()
    redis = FakeRedis()
    integration = FakeIntegration(provider_name="gmail")

    _orig = _mod.integration_vault.use_credential
    _mod.integration_vault.use_credential = _no_client_id
    try:
        raised = False
        try:
            asyncio.run(
                svc.begin_authorize(
                    integration=integration,
                    user_id=uuid.uuid4(),
                    session=_FakeSession(),
                    redis=redis,
                )
            )
        except APIError as exc:
            raised = True
            assert exc.status_code == 400
        assert raised, "expected APIError(400) for missing client_id"
        # No flow record should have been written.
        assert redis.store == {}
    finally:
        _mod.integration_vault.use_credential = _orig
