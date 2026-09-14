"""Backend entrypoint and FastAPI application wiring.

This module owns two responsibilities:

1. **Fail-fast configuration loading** at process startup. If any required
   secret is absent, the process logs a configuration error naming the missing
   variable(s) and exits with a non-zero status (Req 18.3). Secret values are
   never written to logs or error output (Req 18.4).

2. **The FastAPI application**. The app is exposed at module level as ``app``
   so ``uvicorn app.main:app`` (and the container entrypoint, which runs
   ``alembic upgrade head`` before Uvicorn per Req 19.3) can import it. A
   lifespan handler runs the configuration gate at startup so a missing secret
   still fails fast, and a public ``/health`` endpoint reports PostgreSQL and
   Redis reachability (Req 14.3).

Routers are mounted incrementally by later tasks via :func:`register_routers`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from sqlalchemy import text

from app.config import ConfigurationError, Settings, get_settings, load_settings
from app.core.errors import install_exception_handlers
from app.core.middleware import install_middleware

logger = logging.getLogger("atomic_ai.startup")


def load_startup_settings() -> Settings:
    """Load settings for startup, exiting non-zero on configuration failure.

    On success, logs a redacted, secret-free view of the effective settings and
    returns them. On :class:`ConfigurationError`, logs the (secret-free) error
    message and terminates the process with a non-zero exit code (Req 18.3, 18.4).
    """
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        # The message names the missing variable(s) and contains no secrets.
        logger.critical("Startup aborted: %s", exc)
        sys.exit(1)

    logger.info("Configuration loaded: %s", settings.safe_dump())
    return settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: run the fail-fast config gate at startup.

    Calling :func:`load_startup_settings` here means the process still aborts
    with a non-zero exit code if a required secret is missing when the ASGI
    server boots the app (Req 18.3), while never logging secret values (Req
    18.4). Settings are cached by :func:`app.config.get_settings`, so this does
    not re-read the environment on every request.
    """
    load_startup_settings()
    yield


# --- Dependency health probes -------------------------------------------------

async def _check_postgres() -> bool:
    """Return True if the PostgreSQL database answers a trivial query.

    Runs ``SELECT 1`` over the shared async engine. Any exception (unreachable
    database, auth failure, timeout) is treated as "not reachable" and never
    surfaces the DSN or other secrets to the caller.
    """
    # Imported lazily so importing this module never eagerly builds the engine
    # (which would require DATABASE_URL to be present at import time).
    from app.db.session import get_engine

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - health must not propagate dependency errors
        logger.warning("Health check: PostgreSQL is not reachable")
        return False


async def _check_redis() -> bool:
    """Return True if Redis answers a PING.

    Builds a short-lived client from ``REDIS_URL`` and pings it. Any failure is
    reported as "not reachable"; the connection URL is never logged.
    """
    import redis.asyncio as redis_asyncio

    client = None
    try:
        settings = get_settings()
        client = redis_asyncio.from_url(
            settings.REDIS_URL.get_secret_value(),
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        return bool(await client.ping())
    except Exception:  # noqa: BLE001 - health must not propagate dependency errors
        logger.warning("Health check: Redis is not reachable")
        return False
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def register_routers(app: FastAPI) -> None:
    """Mount API routers on the application.

    The auth router (public OAuth login/callback + logout under ``/auth``, Req
    2.1) is mounted here. Later feature routers (workspaces, sessions, approvals,
    admin, ...) are added by subsequent tasks by importing their router module
    and calling ``app.include_router(...)`` here.
    """
    from app.agents.router import router as agents_router
    from app.api.admin import router as admin_router
    from app.api.approvals import router as approvals_router
    from app.api.auth import router as auth_router
    from app.api.integrations import router as integrations_router
    from app.api.integrations_oauth import router as integrations_oauth_router
    from app.api.profile import router as profile_router
    from app.api.webhooks import router as webhooks_router
    from app.api.rules import router as rules_router
    from app.api.workspaces import router as workspaces_router
    from app.api.voice import register_voice_routes
    from app.api.ws import register_ws_routes

    # The router already carries its own ``/auth`` prefix, matching the public
    # allowlist (``PUBLIC_PATH_PREFIXES``) so these routes need no Session_Token.
    app.include_router(auth_router)
    # Integration vault router (connect / sharing toggle / disconnect) under
    # ``/api/v1/integrations`` (task 9.5); each route requires a Session_Token
    # and is RBAC-guarded, and never returns or logs decrypted tokens.
    app.include_router(integrations_router)
    # Per-integration OAuth authorize/callback router under
    # ``/api/v1/integrations`` (task 5.2). The authorize route requires a
    # Session_Token and is RBAC-guarded; the callback is public (trusted via the
    # single-use Redis ``state``). Neither route returns or logs decrypted tokens
    # (Req 1.1, 3.1, 7.2).
    app.include_router(integrations_oauth_router)

    # Public webhook gateway under ``/api/v1/webhooks`` — providers POST events
    # here to trigger tenant-scoped agent runs. No Session_Token (verified by
    # per-integration secret + provider signature).
    app.include_router(webhooks_router)
    # Automation rules CRUD under ``/api/v1/rules`` (task 10.3); each route
    # requires a Session_Token and workspace-wide writes require Owner/Admin.
    app.include_router(rules_router)
    # Workspace CRUD + invites + role management under ``/api/v1/workspaces``
    # (task 7.5); each route requires a Session_Token and self-guards via RBAC
    # (Owner-only member management), writing an audit row on state changes
    # (Req 4.5, 15.1).
    app.include_router(workspaces_router)
    # Agents trigger router under ``/api/v1/agents`` (task 11.5); each route
    # requires a Session_Token and TRIGGER_WORKFLOW, and enqueues a Job_Queue
    # task carrying the originating workspace_id so the job re-applies tenant
    # scoping when it runs (Req 16.3).
    app.include_router(agents_router)
    # Super admin control plane under ``/api/v1/admin`` (task 14.1); every route
    # requires a Session_Token AND super-admin privileges (403 otherwise), and
    # mutating operations (ban / owner reassignment) write an audit row
    # (Req 12.1, 12.2, 12.3, 12.4, 12.5).
    app.include_router(admin_router)
    # Approvals router under ``/api/v1/approvals`` (task 13.7); each route
    # requires a Session_Token. Listing the queue needs only membership;
    # resolving (approve/reject) requires Owner/Admin (RESOLVE_APPROVAL, 403
    # otherwise, Req 10.5). A successful resolution broadcasts the terminal
    # event to authorized reviewers via the WebSocket_Gateway (Req 10.8).
    app.include_router(approvals_router)
    # Account profile router under ``/api/v1/profile`` (SMS reply-approval
    # notifications). Each route requires a Session_Token and acts only on the
    # caller's OWN user row (user_id from the resolved context). Lets a user
    # set their phone number + SMS preferences and view SMS usage/spend.
    app.include_router(profile_router)
    # WebSocket_Gateway for real-time approval delivery at
    # ``/api/v1/ws/approvals`` (task 13.5). The connection authenticates with a
    # valid Session_Token (``?token=`` query param or ``session`` cookie) BEFORE
    # any message is delivered; unauthenticated handshakes are closed with 4401
    # (Req 11.1, 11.2). Broadcasts reach only Owner/Admin reviewers of the owning
    # workspace (Req 10.2, 10.8, 11.3).
    register_ws_routes(app)
    # Voice_Gateway WebSocket for the Nova Sonic accessibility bridge at
    # ``/api/v1/voice/stream`` (Phase 1b). The connection authenticates with a
    # valid Session_Token (``session`` cookie or ``?token=`` query param) and
    # must hold a role in the ``?workspace_id=`` workspace BEFORE any Nova Sonic
    # session starts; otherwise the handshake is closed (4401/4403). Disabled
    # entirely when ``VOICE_ENABLED`` is false (4404). Tool actions still route
    # only through approval_service (Phase 1a, unchanged).
    register_voice_routes(app)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the FastAPI application.

    ``settings`` is injectable so tests can construct an app with a specific
    configuration (CORS allowlist, body-size and rate-limit values) without
    touching the process environment. When omitted, the module-level cached
    settings are used.
    """
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="Atomic AI Backend",
        lifespan=lifespan,
    )

    # Transport security / request guards (CORS, security headers, body-size
    # limit, auth rate limiting, forwarded-proto) run before any handler.
    install_middleware(app, settings)
    # Central, secret-free error envelope for every failure path.
    install_exception_handlers(app)

    @app.get("/health", tags=["system"])
    async def health(response: Response) -> dict[str, object]:
        """Public liveness/readiness probe (Req 14.3).

        Reports the reachability of PostgreSQL and Redis. Returns HTTP 200 with
        ``status: "ok"`` when both dependencies answer, and HTTP 503 with
        ``status: "degraded"`` when either is unreachable. This endpoint
        requires no authentication and never leaks secrets.
        """
        postgres_ok = await _check_postgres()
        redis_ok = await _check_redis()
        healthy = postgres_ok and redis_ok

        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return {
            "status": "ok" if healthy else "degraded",
            "postgres": postgres_ok,
            "redis": redis_ok,
        }

    register_routers(app)
    return app


_app_singleton: FastAPI | None = None


def get_app() -> FastAPI:
    """Return the process-wide application, constructing it on first use.

    Construction is deferred (rather than done at import time) so importing this
    module never requires configuration to be present in the environment. The
    ASGI ``app`` attribute below resolves through this, so ``uvicorn
    app.main:app`` still works and builds the app with the effective settings.
    """
    global _app_singleton
    if _app_singleton is None:
        _app_singleton = create_app()
    return _app_singleton


def __getattr__(name: str) -> object:
    """Lazily expose ``app`` at module level (PEP 562).

    ``uvicorn app.main:app`` and ``from app.main import app`` both trigger this,
    constructing the application only when actually requested.
    """
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_startup_settings()
