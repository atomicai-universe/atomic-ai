"""Pytest configuration and shared fixtures for backend tests."""

from __future__ import annotations

import pytest

# The complete set of required environment variables with placeholder values.
# Used to build a valid environment that tests can selectively mutate.
_VALID_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@db:5432/atomic",
    "REDIS_URL": "redis://redis:6379/0",
    "ENCRYPTION_KEY": "test-encryption-key-value-0123456789",
    "GOOGLE_OAUTH_CLIENT_ID": "google-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "google-client-secret-value",
    "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret-value",
}


@pytest.fixture
def valid_env() -> dict[str, str]:
    """Return a fresh copy of a complete, valid environment mapping."""
    return dict(_VALID_ENV)


@pytest.fixture(autouse=True)
def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure config env vars from the host don't leak into tests.

    Every known config variable (required and optional) is removed before each
    test so tests control the environment explicitly.
    """
    for key in (
        *_VALID_ENV.keys(),
        "CORS_ALLOW_ORIGINS",
        "MAX_BODY_SIZE_BYTES",
        "AUTH_RATE_LIMIT_PER_MINUTE",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)
