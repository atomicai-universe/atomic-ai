"""Property + example tests for ``IntegrationOAuthService.complete_authorize`` (task 3.4).

These tests exercise the *callback* half of the per-integration OAuth flow with
NO live provider round-trip (Req 13.3): the provider token endpoint is faked via
the ``FakeHTTPClient``/``FakeResponse`` doubles (same shape as
``tests/test_auth_wiring.py``), the flow state is held in an in-memory
``FakeRedis``, and the integration row is seeded through a real Fernet-backed
``integration_vault.store`` on an in-memory ``_FakeDBSession`` so
``use_credential`` decrypts the stored ``client_id``/``client_secret`` for real.

A process-wide :class:`~app.core.encryption.EncryptionService` built from a known
Fernet key is installed for the module (via ``set_encryption_service``) so the
service-internal ``integration_vault`` calls — which use the process-wide
encryption service — round-trip deterministically without depending on the
``ENCRYPTION_KEY`` environment configuration.

Properties covered (from design.md "Correctness Properties"):

- **Property 7** — state is single-use (replay-proof).
- **Property 8** — invalid/expired/consumed/provider-mismatch state blocks the
  exchange (401, zero exchange calls).
- **Property 10** — code exchange is a single server-side POST to the provider
  ``token_url`` carrying the integration's own ``client_id``/``client_secret``.
- **Property 11** — exchange ``redirect_uri`` and ``code_verifier`` round-trip
  from authorize.
- **Property 13** — a missing refresh token fails without activation
  (``reason=no_refresh_token``, status not ACTIVE).
- **Property 14** — success persists both tokens and activates in place.
- **Property 15** — re-authorization rotates the stored tokens.
- **Property 16 / 17** — secret hygiene: no ``client_secret``/``code_verifier``/
  ``access_token``/``refresh_token`` in logs/responses/audit; failed exchange
  logs only ``error``/``error_description``.
- **Property 19** — exchange failure returns 401 and does not activate.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from cryptography.fernet import Fernet

from app.core import encryption as encryption_module
from app.core.encryption import EncryptionService, set_encryption_service
from app.core.errors import APIError
from app.db.models import Integration, IntegrationCategory, IntegrationStatus
from app.services import integration_vault
from app.services.integration_oauth import (
    AUDIT_ACTION_AUTHORIZED,
    OAUTH_PROVIDERS,
    IntegrationOAuthService,
    _FLOW_STATE_PREFIX,
)


# ===========================================================================
# No-I/O test doubles (mirror tests/test_auth_wiring.py + test_integration_vault.py)
# ===========================================================================


class FakeRedis:
    """In-memory async stand-in for the Redis surface the flow touches."""

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
    """Async HTTP client returning a canned token response and recording POSTs.

    Records every POST as ``(url, data)`` so tests can assert exactly one
    server-to-server exchange, the target ``token_url``, and the posted
    ``client_id``/``client_secret``/``redirect_uri``/``code_verifier``.
    """

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *args, data=None, **kwargs) -> FakeResponse:
        self.posts.append((url, dict(data or {})))
        return self._response

    async def aclose(self) -> None:  # pragma: no cover - never owned here
        return None


class _FakeDBSession:
    """Minimal async-session stand-in supporting add/flush/get/scalar/commit.

    Rows are keyed by their ``id`` (mirrors the fake in
    ``tests/test_integration_vault.py``) so ``integration_vault.store`` /
    ``use_credential`` drive the real encryption round-trip without Postgres.
    ``scalar`` implements the vault's dedupe SELECT by matching on
    (workspace, creator, category, provider) so re-authorization updates the
    existing row in place (Property 15) rather than inserting a new one.
    """

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, Integration] = {}
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)
        if isinstance(obj, Integration):
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.status is None:
                obj.status = IntegrationStatus.ACTIVE
            self._rows[obj.id] = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, _model: type, ident: uuid.UUID) -> Integration | None:
        return self._rows.get(ident)

    async def scalar(self, _stmt: object) -> Integration | None:
        # The vault's dedupe lookup runs a SELECT that this fake can't execute;
        # return the single seeded integration row (there is only ever one in a
        # test), so store() takes the update-in-place branch on re-authorize.
        for row in self._rows.values():
            return row
        return None

    def audit_rows(self):
        from app.db.models import SystemAuditLog

        return [o for o in self.added if isinstance(o, SystemAuditLog)]


# ===========================================================================
# Fixtures / helpers
# ===========================================================================


@pytest.fixture(autouse=True)
def _known_encryption_service():
    """Install a real Fernet-keyed EncryptionService process-wide for each test.

    ``complete_authorize`` reaches ``integration_vault`` without an explicit
    ``encryption_service``, so it uses the process-wide instance; pinning a
    known key makes the seed + persist + decrypt round-trip deterministic and
    independent of ``ENCRYPTION_KEY`` (which conftest strips).
    """
    set_encryption_service(EncryptionService(Fernet.generate_key()))
    try:
        yield
    finally:
        encryption_module.reset_encryption_service()


def _make_settings():
    class _Settings:
        OAUTH_REDIRECT_BASE_URL = "https://app.example"

    return _Settings()


def _service() -> IntegrationOAuthService:
    return IntegrationOAuthService(settings=_make_settings())


async def _seed_integration(
    session: _FakeDBSession,
    *,
    provider_slug: str,
    client_id: str = "the-client-id",
    client_secret: str = "the-client-secret",
) -> Integration:
    """Seed an OAuth-family integration row with encrypted client credentials."""
    return await integration_vault.store(
        session,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        created_by_user_id=uuid.uuid4(),
        category=IntegrationCategory.EMAIL,
        provider_name=provider_slug,
        credentials={"client_id": client_id, "client_secret": client_secret},
    )


def _seed_flow(
    redis: FakeRedis,
    *,
    integration: Integration,
    provider_slug: str,
    state: str = "state-abc",
    code_verifier: str = "verifier-xyz",
    redirect_uri: str = "https://app.example/api/v1/integrations/oauth/callback/gmail",
) -> str:
    """Write a valid pending flow record into fake Redis, returning the state."""
    flow = {
        "integration_id": str(integration.id),
        "provider": provider_slug,
        "workspace_id": str(integration.workspace_id),
        "user_id": str(integration.created_by_user_id),
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    redis.store[f"{_FLOW_STATE_PREFIX}{state}"] = json.dumps(flow)
    return state


def _decrypt_credentials(integration: Integration) -> dict:
    """Decrypt the stored credential blob with the installed process key."""
    enc = encryption_module.get_encryption_service()
    return json.loads(enc.decrypt(integration.encrypted_credentials))


# ===========================================================================
# Property 7: State is single-use (replay-proof) — Validates Req 3.2
# ===========================================================================


@pytest.mark.asyncio
async def test_property7_state_is_single_use_replay_rejected():
    """Feature: integration-oauth-flow, Property 7: State is single-use (replay-proof).

    The first callback consumes (reads + deletes) its flow record; a second
    callback presenting the same state is rejected.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")

    ok = FakeHTTPClient(
        FakeResponse({"access_token": "at-1", "refresh_token": "rt-1"})
    )
    result = await svc.complete_authorize(
        provider_slug="gmail",
        code="the-code",
        state=state,
        session=session,
        redis=redis,
        http_client=ok,
    )
    assert result.integration_id == integration.id
    # State was consumed on first use.
    assert f"{_FLOW_STATE_PREFIX}{state}" not in redis.store

    # Replaying the same state is rejected, with no further exchange.
    replay = FakeHTTPClient(
        FakeResponse({"access_token": "at-2", "refresh_token": "rt-2"})
    )
    with pytest.raises(APIError) as exc:
        await svc.complete_authorize(
            provider_slug="gmail",
            code="the-code",
            state=state,
            session=session,
            redis=redis,
            http_client=replay,
        )
    assert exc.value.status_code == 401
    assert replay.posts == []


# ===========================================================================
# Property 8: Invalid or mismatched state blocks the exchange — Req 3.3, 3.4
# ===========================================================================


@pytest.mark.asyncio
async def test_property8_absent_state_blocks_exchange():
    """Feature: integration-oauth-flow, Property 8: Invalid or mismatched state blocks the exchange.

    An absent state -> 401 with zero token-exchange requests.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    client = FakeHTTPClient(FakeResponse({"refresh_token": "rt"}))

    with pytest.raises(APIError) as exc:
        await svc.complete_authorize(
            provider_slug="gmail",
            code="c",
            state="no-such-state",
            session=session,
            redis=redis,
            http_client=client,
        )
    assert exc.value.status_code == 401
    assert client.posts == []


@pytest.mark.asyncio
async def test_property8_consumed_state_blocks_exchange():
    """Feature: integration-oauth-flow, Property 8: Invalid or mismatched state blocks the exchange.

    An already-consumed state (deleted from Redis) -> 401, zero exchanges.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")
    # Simulate prior consumption.
    del redis.store[f"{_FLOW_STATE_PREFIX}{state}"]

    client = FakeHTTPClient(FakeResponse({"refresh_token": "rt"}))
    with pytest.raises(APIError) as exc:
        await svc.complete_authorize(
            provider_slug="gmail",
            code="c",
            state=state,
            session=session,
            redis=redis,
            http_client=client,
        )
    assert exc.value.status_code == 401
    assert client.posts == []


@pytest.mark.asyncio
async def test_property8_provider_mismatch_blocks_exchange():
    """Feature: integration-oauth-flow, Property 8: Invalid or mismatched state blocks the exchange.

    A callback path provider differing from the flow record's provider -> 401,
    zero exchanges.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    # Flow recorded provider=gmail, but callback arrives on the salesforce path.
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")

    client = FakeHTTPClient(FakeResponse({"refresh_token": "rt"}))
    with pytest.raises(APIError) as exc:
        await svc.complete_authorize(
            provider_slug="salesforce",
            code="c",
            state=state,
            session=session,
            redis=redis,
            http_client=client,
        )
    assert exc.value.status_code == 401
    assert client.posts == []


# ===========================================================================
# Property 10: Server-side exchange uses the integration's own credentials
#              — Validates Req 4.1, 4.2
# ===========================================================================


@pytest.mark.asyncio
async def test_property10_single_post_to_token_url_with_own_credentials():
    """Feature: integration-oauth-flow, Property 10: Code exchange is server-side with the integration's own credentials.

    Exactly one POST is made to the provider ``token_url`` carrying the stored
    ``client_id`` and ``client_secret``.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(
        session,
        provider_slug="gmail",
        client_id="stored-client-id",
        client_secret="stored-client-secret",
    )
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")

    client = FakeHTTPClient(
        FakeResponse({"access_token": "at", "refresh_token": "rt"})
    )
    await svc.complete_authorize(
        provider_slug="gmail",
        code="the-code",
        state=state,
        session=session,
        redis=redis,
        http_client=client,
    )

    assert len(client.posts) == 1
    url, data = client.posts[0]
    assert url == OAUTH_PROVIDERS["gmail"].token_url
    assert data["client_id"] == "stored-client-id"
    assert data["client_secret"] == "stored-client-secret"
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"


# ===========================================================================
# Property 11: redirect_uri and code_verifier round-trip from authorize
#              — Validates Req 4.3, 4.4, 8.1, 8.2
# ===========================================================================


@pytest.mark.asyncio
async def test_property11_redirect_uri_and_code_verifier_round_trip():
    """Feature: integration-oauth-flow, Property 11: Exchange redirect_uri and code_verifier round-trip from authorize.

    The exchange ``redirect_uri`` and (PKCE) ``code_verifier`` equal the values
    recorded in the flow at authorize time.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    flow_redirect = "https://app.example/api/v1/integrations/oauth/callback/gmail"
    flow_verifier = "the-flow-verifier-value"
    state = _seed_flow(
        redis,
        integration=integration,
        provider_slug="gmail",
        code_verifier=flow_verifier,
        redirect_uri=flow_redirect,
    )

    client = FakeHTTPClient(
        FakeResponse({"access_token": "at", "refresh_token": "rt"})
    )
    await svc.complete_authorize(
        provider_slug="gmail",
        code="the-code",
        state=state,
        session=session,
        redis=redis,
        http_client=client,
    )

    _url, data = client.posts[0]
    assert data["redirect_uri"] == flow_redirect
    assert data["code_verifier"] == flow_verifier


# ===========================================================================
# Property 13: A missing refresh token fails without activation — Req 5.4, 5.5
# ===========================================================================


@pytest.mark.asyncio
async def test_property13_missing_refresh_token_fails_without_activation():
    """Feature: integration-oauth-flow, Property 13: A missing refresh token fails without activation.

    A token response lacking ``refresh_token`` raises an error carrying
    ``reason=no_refresh_token`` and does not mark the integration ACTIVE.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    # Seed a non-ACTIVE starting state to prove activation never happens.
    integration.status = IntegrationStatus.ERROR
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")

    client = FakeHTTPClient(FakeResponse({"access_token": "at-only"}))
    with pytest.raises(APIError) as exc:
        await svc.complete_authorize(
            provider_slug="gmail",
            code="the-code",
            state=state,
            session=session,
            redis=redis,
            http_client=client,
        )

    assert exc.value.status_code == 401
    assert (exc.value.fields or {}).get("reason") == "no_refresh_token"
    assert integration.status is not IntegrationStatus.ACTIVE


# ===========================================================================
# Property 14: Success persists both tokens and activates in place
#              — Validates Req 5.4, 6.1, 6.2, 6.3
# ===========================================================================


@pytest.mark.asyncio
async def test_property14_success_persists_tokens_and_activates_in_place():
    """Feature: integration-oauth-flow, Property 14: Successful authorization persists both tokens and activates in place.

    Decrypting the stored credentials yields the response tokens, the same row
    is updated (no new row), and status becomes ACTIVE.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    original_id = integration.id
    rows_before = len(session._rows)
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")

    client = FakeHTTPClient(
        FakeResponse({"access_token": "the-access", "refresh_token": "the-refresh"})
    )
    result = await svc.complete_authorize(
        provider_slug="gmail",
        code="the-code",
        state=state,
        session=session,
        redis=redis,
        http_client=client,
    )

    assert result.integration_id == original_id
    # No new row was created (same count, same id).
    assert len(session._rows) == rows_before
    stored = await session.get(Integration, original_id)
    assert stored is integration
    assert stored.status is IntegrationStatus.ACTIVE

    creds = _decrypt_credentials(stored)
    assert creds["access_token"] == "the-access"
    assert creds["refresh_token"] == "the-refresh"
    # Client credentials survive the merge.
    assert creds["client_id"] == "the-client-id"
    assert creds["client_secret"] == "the-client-secret"


# ===========================================================================
# Property 15: Re-authorization rotates the stored tokens — Validates Req 6.5
# ===========================================================================


@pytest.mark.asyncio
async def test_property15_reauthorization_rotates_tokens():
    """Feature: integration-oauth-flow, Property 15: Re-authorization rotates the stored tokens.

    A second successful authorization replaces the stored refresh/access tokens.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    original_id = integration.id

    # First authorization.
    state1 = _seed_flow(
        redis, integration=integration, provider_slug="gmail", state="state-1"
    )
    await svc.complete_authorize(
        provider_slug="gmail",
        code="code-1",
        state=state1,
        session=session,
        redis=redis,
        http_client=FakeHTTPClient(
            FakeResponse({"access_token": "at-1", "refresh_token": "rt-1"})
        ),
    )
    creds1 = _decrypt_credentials(await session.get(Integration, original_id))
    assert creds1["access_token"] == "at-1"
    assert creds1["refresh_token"] == "rt-1"

    # Second authorization rotates both tokens on the SAME row.
    state2 = _seed_flow(
        redis, integration=integration, provider_slug="gmail", state="state-2"
    )
    await svc.complete_authorize(
        provider_slug="gmail",
        code="code-2",
        state=state2,
        session=session,
        redis=redis,
        http_client=FakeHTTPClient(
            FakeResponse({"access_token": "at-2", "refresh_token": "rt-2"})
        ),
    )

    assert len(session._rows) == 1
    creds2 = _decrypt_credentials(await session.get(Integration, original_id))
    assert creds2["access_token"] == "at-2"
    assert creds2["refresh_token"] == "rt-2"


# ===========================================================================
# Property 16 / 17: Secret hygiene — Validates Req 7.1, 7.3, 7.4
# ===========================================================================

_SECRET_MATERIAL = (
    "the-client-secret",
    "the-flow-verifier-value",
    "secret-access-token",
    "secret-refresh-token",
)


@pytest.mark.asyncio
async def test_property16_success_leaks_no_secrets_in_logs_or_audit(caplog):
    """Feature: integration-oauth-flow, Property 16: Secrets and tokens never appear in logs, responses, or audit.

    On a successful flow, captured logs and the written audit record contain no
    client_secret/code_verifier/access_token/refresh_token material.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(
        session, provider_slug="gmail", client_secret="the-client-secret"
    )
    state = _seed_flow(
        redis,
        integration=integration,
        provider_slug="gmail",
        code_verifier="the-flow-verifier-value",
    )

    client = FakeHTTPClient(
        FakeResponse(
            {"access_token": "secret-access-token", "refresh_token": "secret-refresh-token"}
        )
    )
    with caplog.at_level(logging.DEBUG):
        result = await svc.complete_authorize(
            provider_slug="gmail",
            code="the-code",
            state=state,
            session=session,
            redis=redis,
            http_client=client,
        )

    log_text = caplog.text
    for secret in _SECRET_MATERIAL:
        assert secret not in log_text

    # The returned outcome carries only non-secret identifiers.
    result_text = f"{result.integration_id}{result.workspace_id}"
    for secret in _SECRET_MATERIAL:
        assert secret not in result_text

    # Audit metadata is scrubbed and holds no token/secret material.
    audit_rows = session.audit_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == AUDIT_ACTION_AUTHORIZED
    audit_text = json.dumps(audit_rows[0].log_metadata)
    for secret in _SECRET_MATERIAL:
        assert secret not in audit_text


@pytest.mark.asyncio
async def test_property17_failed_exchange_logs_only_error_fields(caplog):
    """Feature: integration-oauth-flow, Property 17: Failed exchanges log only non-secret OAuth error fields.

    A non-2xx token response logs only ``error``/``error_description`` and no
    secret/token material.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(
        session, provider_slug="gmail", client_secret="the-client-secret"
    )
    state = _seed_flow(
        redis,
        integration=integration,
        provider_slug="gmail",
        code_verifier="the-flow-verifier-value",
    )

    client = FakeHTTPClient(
        FakeResponse(
            {"error": "invalid_grant", "error_description": "code expired"},
            status_code=400,
        )
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(APIError):
            await svc.complete_authorize(
                provider_slug="gmail",
                code="the-code",
                state=state,
                session=session,
                redis=redis,
                http_client=client,
            )

    log_text = caplog.text
    # Non-secret provider error fields are allowed and present.
    assert "invalid_grant" in log_text
    # No secret material leaks.
    assert "the-client-secret" not in log_text
    assert "the-flow-verifier-value" not in log_text


# ===========================================================================
# Property 19: Exchange failure returns 401 and does not activate — Req 10.1, 10.2
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    ["invalid_grant", "redirect_uri_mismatch", "invalid_client"],
)
async def test_property19_exchange_failure_401_no_activation(error):
    """Feature: integration-oauth-flow, Property 19: Exchange failure returns 401 and does not activate.

    A non-2xx token response -> 401 ``oauth_exchange_failed`` and the
    integration is not marked ACTIVE.
    """
    svc = _service()
    redis = FakeRedis()
    session = _FakeDBSession()
    integration = await _seed_integration(session, provider_slug="gmail")
    integration.status = IntegrationStatus.ERROR  # prove no activation happens
    state = _seed_flow(redis, integration=integration, provider_slug="gmail")

    client = FakeHTTPClient(FakeResponse({"error": error}, status_code=400))
    with pytest.raises(APIError) as exc:
        await svc.complete_authorize(
            provider_slug="gmail",
            code="the-code",
            state=state,
            session=session,
            redis=redis,
            http_client=client,
        )

    assert exc.value.status_code == 401
    assert exc.value.code == "oauth_exchange_failed"
    assert integration.status is not IntegrationStatus.ACTIVE
    assert session.commits == 0
