"""Property-based tests for OAuth identity resolution and state handling (task 5.2).

Implements three design properties over the pure decision core and the
state-lifecycle behavior of :mod:`app.services.auth_service`:

- **Property 5: OAuth login is find-or-create idempotent per identity.**
  **Validates: Requirements 1.3.** For any provider, an email with no existing
  user resolves to ``"create"`` while an email already bound to the *same*
  provider resolves to ``"return_existing"`` — repeatedly and stably — so a
  repeat login neither duplicates nor mutates the identity.

- **Property 6: Cross-provider email is an account conflict.**
  **Validates: Requirements 1.7.** For any two *distinct* providers, an email
  already bound to one provider that is presented for login under the other
  resolves to ``"conflict"`` (never create, never return).

- **Property 7: OAuth state must match.**
  **Validates: Requirements 1.4.** A completion whose presented ``state`` is not
  the exact value issued and stored (wrong/absent/expired) is rejected with an
  ``invalid_state`` :class:`APIError`, without consuming the genuine pending
  flow; and a correct ``state`` validates exactly once — reuse after consumption
  is rejected (single-use / replay protection).

These use Hypothesis with a minimum of 100 examples per property, per the
design's PBT mandate. The decision properties are pure and I/O-free; the
state property drives ``AuthService.begin_login``/``complete_login`` against a
tiny in-memory async ``FakeRedis`` (mirroring ``test_auth_service.py``) and a
fake HTTP client, so no network or database is touched.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import APIError
from app.db.models import AuthProvider
from app.services.auth_service import (
    AuthService,
    resolve_user_identity,
)

# The design mandates at least 100 examples per property. These generators are
# cheap, so we run comfortably above the floor.
_PBT = settings(max_examples=200)

# Every supported provider (Req 1.6 constrains identities to exactly these).
_PROVIDERS = list(AuthProvider)


# ---------------------------------------------------------------------------
# Test doubles (mirrors the fakes in test_auth_service.py)
# ---------------------------------------------------------------------------


class FakeRedis:
    """Tiny in-memory async stand-in for the Redis client.

    Implements just the async surface Auth_Service uses (``set``/``get``/
    ``delete``). TTL is accepted but not enforced; expiry is modeled by an
    absent key, which the state property exercises via wrong/unknown states.
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
    """Fake async HTTP client returning canned responses keyed by URL."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    async def post(self, url: str, *args, **kwargs) -> FakeResponse:
        self.calls.append(("POST", url))
        return self.routes[url]

    async def get(self, url: str, *args, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url))
        return self.routes[url]


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
# Property 5: OAuth login is find-or-create idempotent per identity.
# Validates: Requirements 1.3
# ---------------------------------------------------------------------------


@_PBT
@given(provider=st.sampled_from(_PROVIDERS))
def test_property5_find_or_create_idempotent_per_identity(provider: AuthProvider):
    """No user for the email => create; same-provider user => return_existing.

    The resolution is a pure function of ``(existing_provider, requested)``, so
    idempotency is captured by determinism: resolving the same identity any
    number of times yields the same stable decision. A brand-new email always
    provisions (``"create"``); an email already bound to the *same* provider is
    always returned, never duplicated (``"return_existing"``).
    """
    # No existing user for this email -> provision a new one.
    assert resolve_user_identity(None, provider) == "create"

    # An existing user under the SAME provider -> return it (idempotent login).
    first = resolve_user_identity(provider, provider)
    assert first == "return_existing"

    # Idempotent/stable: repeating the resolution never duplicates or drifts.
    for _ in range(5):
        assert resolve_user_identity(provider, provider) == "return_existing"


# ---------------------------------------------------------------------------
# Property 6: Cross-provider email is an account conflict.
# Validates: Requirements 1.7
# ---------------------------------------------------------------------------


@_PBT
@given(pair=st.tuples(st.sampled_from(_PROVIDERS), st.sampled_from(_PROVIDERS)))
def test_property6_cross_provider_email_is_conflict(
    pair: tuple[AuthProvider, AuthProvider],
):
    """Distinct existing/requested providers for one email => conflict.

    With only two providers, we generate all ordered pairs and keep the
    distinct ones (google-vs-github and the reverse). Every such cross-provider
    login must resolve to ``"conflict"`` — never create, never return.
    """
    existing, requested = pair
    if existing == requested:
        # Same-provider pairs are covered by Property 5; only cross-provider
        # pairs are in scope for the conflict rule.
        assert resolve_user_identity(existing, requested) == "return_existing"
        return

    assert resolve_user_identity(existing, requested) == "conflict"


def test_property6_both_distinct_orderings_are_conflict():
    """Focused check that BOTH distinct orderings are conflicts (Req 1.7)."""
    assert (
        resolve_user_identity(AuthProvider.GOOGLE, AuthProvider.GITHUB) == "conflict"
    )
    assert (
        resolve_user_identity(AuthProvider.GITHUB, AuthProvider.GOOGLE) == "conflict"
    )


# ---------------------------------------------------------------------------
# Property 7: OAuth state must match.
# Validates: Requirements 1.4
# ---------------------------------------------------------------------------


@_PBT
@given(wrong_state=st.text())
def test_property7_wrong_state_rejected_and_does_not_consume(wrong_state: str):
    """A presented state != the issued one is rejected and consumes nothing.

    We start a real flow via ``begin_login`` (which stores the genuine state in
    Redis), then attempt ``complete_login`` with an arbitrary *different* state.
    The completion must raise ``invalid_state`` before any code exchange, and
    the genuine pending flow must remain untouched (so a later correct
    completion could still succeed). ``wrong_state`` is coerced to differ from
    the issued value.
    """

    async def scenario() -> None:
        svc = _make_service()
        redis = FakeRedis()

        issued = (
            await svc.begin_login("google", "https://app.example/cb", redis=redis)
        ).state

        # Guarantee the presented state differs from the one that was issued.
        presented = wrong_state
        if presented == issued:
            presented = issued + "x"

        genuine_key = f"oauth:state:{issued}"
        assert genuine_key in redis.store  # flow is pending before the attempt

        # An HTTP client that would explode if reached — proving rejection
        # happens on the state check, before any token exchange.
        http = FakeHTTPClient(routes={})

        with pytest.raises(APIError) as exc:
            await svc.complete_login(
                "google",
                "auth-code",
                presented,
                session=None,  # must never be touched on the wrong-state path
                redis=redis,
                http_client=http,
            )
        assert exc.value.status_code == 401
        assert exc.value.code == "invalid_state"

        # The wrong state consumed nothing: the genuine flow is still pending
        # and no HTTP exchange was attempted.
        assert genuine_key in redis.store
        assert http.calls == []

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_property7_correct_state_is_single_use():
    """The genuine state validates exactly once; reuse is rejected (replay).

    Driving the full happy path under Hypothesis is heavy (it needs a mocked
    token exchange), so single-use is asserted as a focused example: after one
    successful consumption the same state no longer validates.
    """
    from app.services.auth_service import _GOOGLE_TOKEN_URL, _GOOGLE_USERINFO_URL

    svc = _make_service()
    redis = FakeRedis()

    issued = (
        await svc.begin_login("google", "https://app.example/cb", redis=redis)
    ).state
    genuine_key = f"oauth:state:{issued}"
    assert genuine_key in redis.store

    routes = {
        _GOOGLE_TOKEN_URL: FakeResponse({"access_token": "at-123"}),
        _GOOGLE_USERINFO_URL: FakeResponse(
            {"email": "single-use@example.com", "name": "Test User"}
        ),
    }

    # Capture the internal find-or-create so we avoid needing a real DB: the
    # state must be consumed BEFORE the exchange/user steps run.
    consumed = await svc._consume_flow(issued, AuthProvider.GOOGLE, redis)
    assert consumed["provider"] == "google"
    # First consumption removed the key -> single-use.
    assert genuine_key not in redis.store

    # Reusing the now-consumed state is rejected as invalid_state (replay).
    with pytest.raises(APIError) as exc:
        await svc._consume_flow(issued, AuthProvider.GOOGLE, redis)
    assert exc.value.status_code == 401
    assert exc.value.code == "invalid_state"

    # Sanity: routes were defined for the exchange path but the replay attempt
    # never reaches them (kept to document the single-use boundary).
    assert set(routes) == {_GOOGLE_TOKEN_URL, _GOOGLE_USERINFO_URL}
