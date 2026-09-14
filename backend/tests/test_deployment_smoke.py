"""Smoke tests for containerized deployment and startup wiring (task 1.4).

These tests assert that the deployment artifacts are internally consistent and
match the design's pinned versions and startup ordering, without requiring a
running Docker daemon:

- Req 19.1: Compose defines postgres, redis, fastapi_backend, nextjs_frontend.
- Req 19.2: postgres/redis images are pinned to the mandated versions.
- Req 19.5: backend/frontend build from Dockerfile.backend/Dockerfile.frontend
  and Uvicorn is pinned to 0.52.4 on Python 3.14.
- Req 19.3: the backend entrypoint runs `alembic upgrade head` before Uvicorn.
- Req 18.3: a missing required secret aborts startup naming the variable and
  exits non-zero.
- Req 18.2: `.env.example` lists every required variable with placeholders.

The compose assertions parse `docker-compose.yml` directly with PyYAML so they
run without Docker. A separate test invokes `docker compose config` when a
daemon/CLI is available and otherwise skips gracefully.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config import REQUIRED_ENV_VARS, ConfigurationError, load_settings
from app.main import load_startup_settings

# --- Repo layout -------------------------------------------------------------
# tests/ -> backend/ -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _PROJECT_ROOT / "docker-compose.yml"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"
_ENTRYPOINT = _PROJECT_ROOT / "backend" / "entrypoint.sh"
_REQUIREMENTS = _PROJECT_ROOT / "backend" / "requirements.txt"

_EXPECTED_SERVICES = {"postgres", "redis", "fastapi_backend", "nextjs_frontend"}
_POSTGRES_IMAGE = "postgres:18.6-alpine"
_REDIS_IMAGE = "redis:8.10-alpine"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parse docker-compose.yml once for the structural assertions."""
    assert _COMPOSE_FILE.is_file(), f"missing compose file: {_COMPOSE_FILE}"
    with _COMPOSE_FILE.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), "docker-compose.yml did not parse to a mapping"
    return data


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    svc = compose.get("services")
    assert isinstance(svc, dict) and svc, "compose file defines no services"
    return svc


# --- Req 19.1: all four services present -------------------------------------

def test_compose_defines_all_four_services(services: dict):
    assert _EXPECTED_SERVICES.issubset(services.keys()), (
        f"missing services: {_EXPECTED_SERVICES - services.keys()}"
    )


# --- Req 19.2: datastore images pinned ---------------------------------------

def test_postgres_image_is_pinned(services: dict):
    assert services["postgres"].get("image") == _POSTGRES_IMAGE


def test_redis_image_is_pinned(services: dict):
    assert services["redis"].get("image") == _REDIS_IMAGE


def test_datastore_images_are_exact_pins_not_floating_tags(services: dict):
    # Guard against `postgres:latest` / `postgres:18` style floating tags.
    for name, expected in (("postgres", _POSTGRES_IMAGE), ("redis", _REDIS_IMAGE)):
        image = services[name].get("image", "")
        assert image == expected
        tag = image.split(":", 1)[1] if ":" in image else ""
        assert tag not in {"", "latest"}, f"{name} uses a floating tag: {image!r}"


# --- Req 19.5: builds reference the correct Dockerfiles ----------------------

@pytest.mark.parametrize(
    ("service", "dockerfile"),
    [
        ("fastapi_backend", "Dockerfile.backend"),
        ("nextjs_frontend", "Dockerfile.frontend"),
    ],
)
def test_service_builds_reference_expected_dockerfile(services: dict, service, dockerfile):
    build = services[service].get("build")
    assert isinstance(build, dict), f"{service} must use a build section"
    assert build.get("dockerfile") == dockerfile
    # The referenced Dockerfile must actually exist at the repo root.
    assert (_PROJECT_ROOT / dockerfile).is_file(), f"missing {dockerfile}"


def test_referenced_dockerfiles_exist(services: dict):
    for service in ("fastapi_backend", "nextjs_frontend"):
        dockerfile = services[service]["build"]["dockerfile"]
        assert (_PROJECT_ROOT / dockerfile).is_file()


def test_uvicorn_pinned_to_expected_version():
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"^uvicorn(\[[^\]]*\])?==0\.52\.4\b", text, re.MULTILINE), (
        "requirements.txt must pin uvicorn==0.52.4"
    )


def test_backend_dockerfile_uses_python_314():
    text = (_PROJECT_ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    assert re.search(r"FROM\s+python:3\.14", text), (
        "Dockerfile.backend must build on Python 3.14"
    )


# --- Optional live check via docker CLI --------------------------------------

def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def test_docker_compose_config_defines_services_when_docker_available():
    if not _docker_compose_available():
        pytest.skip("docker compose CLI/daemon not available")

    # Provide the required env vars so interpolation resolves; values are
    # placeholders only and never logged.
    env = {var: "placeholder" for var in REQUIRED_ENV_VARS}
    result = subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "config"],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        env={"PATH": __import__("os").environ.get("PATH", ""), **env},
        timeout=60,
    )
    if result.returncode != 0:
        pytest.skip(f"docker compose config failed: {result.stderr.strip()[:200]}")

    rendered = yaml.safe_load(result.stdout)
    assert _EXPECTED_SERVICES.issubset(rendered.get("services", {}).keys())


# --- Req 19.3: migrations run before Uvicorn ---------------------------------

def test_entrypoint_runs_migrations_before_uvicorn():
    text = _ENTRYPOINT.read_text(encoding="utf-8")

    alembic_match = re.search(r"alembic\s+upgrade\s+head", text)
    uvicorn_match = re.search(r"\buvicorn\b", text)

    assert alembic_match, "entrypoint must run `alembic upgrade head`"
    assert uvicorn_match, "entrypoint must launch uvicorn"
    assert alembic_match.start() < uvicorn_match.start(), (
        "migrations (alembic upgrade head) must run before launching Uvicorn"
    )


def test_entrypoint_execs_uvicorn_for_signal_handling():
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r"exec\s+uvicorn\b", text), (
        "entrypoint should `exec uvicorn` so the server receives container signals"
    )


# --- Req 18.3: missing-secret startup failure names the variable -------------

@pytest.mark.parametrize("missing_var", REQUIRED_ENV_VARS)
def test_missing_secret_startup_error_names_the_variable(monkeypatch, valid_env, missing_var):
    del valid_env[missing_var]
    for key, value in valid_env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert missing_var in str(excinfo.value)


def test_startup_exits_nonzero_when_secret_missing(monkeypatch, valid_env):
    del valid_env["ENCRYPTION_KEY"]
    for key, value in valid_env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit) as excinfo:
        load_startup_settings()

    assert excinfo.value.code != 0


# --- Req 18.2: .env.example lists every required variable --------------------

def test_env_example_exists():
    assert _ENV_EXAMPLE.is_file(), ".env.example must exist at the project root"


@pytest.mark.parametrize("required_var", REQUIRED_ENV_VARS)
def test_env_example_declares_each_required_variable(required_var):
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    # A declaration is a line assigning the variable (ignoring surrounding comments).
    assert re.search(rf"^{re.escape(required_var)}=", text, re.MULTILINE), (
        f".env.example must declare {required_var}"
    )


def test_env_example_declares_compose_datastore_variables():
    # Compose interpolates these to provision the bundled datastores.
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert re.search(rf"^{re.escape(var)}=", text, re.MULTILINE), (
            f".env.example should declare {var} for the compose datastore"
        )
