"""Tests for the backend configuration loader and fail-fast startup.

Covers:
- Req 18.1: configuration is read from environment variables.
- Req 18.3: a missing required secret aborts startup naming the variable.
- Req 18.4: startup logs and error output exclude secret values.
"""

from __future__ import annotations

import logging

import pytest

from app.config import (
    REQUIRED_ENV_VARS,
    SECRET_ENV_VARS,
    ConfigurationError,
    Settings,
    load_settings,
)
from app.main import load_startup_settings


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# --- Req 18.1: reads configuration from the environment ----------------------

def test_load_settings_reads_all_values_from_environment(monkeypatch, valid_env):
    valid_env["CORS_ALLOW_ORIGINS"] = "https://a.example, https://b.example"
    valid_env["MAX_BODY_SIZE_BYTES"] = "2048"
    valid_env["AUTH_RATE_LIMIT_PER_MINUTE"] = "5"
    _apply(monkeypatch, valid_env)

    settings = load_settings()

    assert settings.DATABASE_URL.get_secret_value() == valid_env["DATABASE_URL"]
    assert settings.REDIS_URL.get_secret_value() == valid_env["REDIS_URL"]
    assert settings.ENCRYPTION_KEY.get_secret_value() == valid_env["ENCRYPTION_KEY"]
    assert settings.GOOGLE_OAUTH_CLIENT_ID == valid_env["GOOGLE_OAUTH_CLIENT_ID"]
    assert (
        settings.GOOGLE_OAUTH_CLIENT_SECRET.get_secret_value()
        == valid_env["GOOGLE_OAUTH_CLIENT_SECRET"]
    )
    assert settings.GITHUB_OAUTH_CLIENT_ID == valid_env["GITHUB_OAUTH_CLIENT_ID"]
    assert (
        settings.GITHUB_OAUTH_CLIENT_SECRET.get_secret_value()
        == valid_env["GITHUB_OAUTH_CLIENT_SECRET"]
    )


def test_cors_allowlist_parses_comma_separated(monkeypatch, valid_env):
    valid_env["CORS_ALLOW_ORIGINS"] = "https://a.example, https://b.example"
    _apply(monkeypatch, valid_env)

    settings = load_settings()

    assert settings.CORS_ALLOW_ORIGINS == ["https://a.example", "https://b.example"]


def test_body_size_and_rate_limit_have_defaults(monkeypatch, valid_env):
    _apply(monkeypatch, valid_env)

    settings = load_settings()

    assert settings.MAX_BODY_SIZE_BYTES == 1_048_576
    assert settings.AUTH_RATE_LIMIT_PER_MINUTE == 10


# --- Req 18.3: fail fast naming the missing variable(s) ----------------------

@pytest.mark.parametrize("missing_var", REQUIRED_ENV_VARS)
def test_missing_single_required_var_names_that_variable(monkeypatch, valid_env, missing_var):
    del valid_env[missing_var]
    _apply(monkeypatch, valid_env)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert missing_var in excinfo.value.missing
    assert missing_var in str(excinfo.value)


def test_missing_multiple_required_vars_names_all_of_them(monkeypatch, valid_env):
    del valid_env["DATABASE_URL"]
    del valid_env["ENCRYPTION_KEY"]
    _apply(monkeypatch, valid_env)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert "DATABASE_URL" in str(excinfo.value)
    assert "ENCRYPTION_KEY" in str(excinfo.value)


def test_startup_exits_nonzero_when_required_var_missing(monkeypatch, valid_env):
    del valid_env["REDIS_URL"]
    _apply(monkeypatch, valid_env)

    with pytest.raises(SystemExit) as excinfo:
        load_startup_settings()

    assert excinfo.value.code != 0


def test_startup_succeeds_with_full_environment(monkeypatch, valid_env):
    _apply(monkeypatch, valid_env)

    settings = load_startup_settings()

    assert isinstance(settings, Settings)


# --- Req 18.4: secret values excluded from logs and error output -------------

def test_configuration_error_message_excludes_secret_values(monkeypatch, valid_env):
    # Keep the secret present but corrupt an unrelated required var so the
    # error path runs; the message must never contain a secret value.
    secret_value = valid_env["ENCRYPTION_KEY"]
    del valid_env["DATABASE_URL"]
    _apply(monkeypatch, valid_env)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert secret_value not in str(excinfo.value)


def test_safe_dump_redacts_all_secret_fields(monkeypatch, valid_env):
    _apply(monkeypatch, valid_env)
    settings = load_settings()

    dumped = settings.safe_dump()

    for secret_name in SECRET_ENV_VARS:
        assert dumped[secret_name] == "***"
    # Non-secret values are preserved.
    assert dumped["GOOGLE_OAUTH_CLIENT_ID"] == valid_env["GOOGLE_OAUTH_CLIENT_ID"]

    # No raw secret value appears anywhere in the dumped view.
    rendered = repr(dumped)
    for secret_name in SECRET_ENV_VARS:
        assert valid_env[secret_name] not in rendered


def test_startup_logs_exclude_secret_values(monkeypatch, valid_env, caplog):
    _apply(monkeypatch, valid_env)

    with caplog.at_level(logging.INFO, logger="atomic_ai.startup"):
        load_startup_settings()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    for secret_name in SECRET_ENV_VARS:
        assert valid_env[secret_name] not in log_text


def test_str_of_settings_does_not_leak_secrets(monkeypatch, valid_env):
    _apply(monkeypatch, valid_env)
    settings = load_settings()

    rendered = str(settings) + repr(settings)

    assert valid_env["ENCRYPTION_KEY"] not in rendered
    assert valid_env["GOOGLE_OAUTH_CLIENT_SECRET"] not in rendered
    assert valid_env["DATABASE_URL"] not in rendered
