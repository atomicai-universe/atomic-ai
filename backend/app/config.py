"""Application configuration loader.

Reads all runtime configuration from environment variables using Pydantic v2
``BaseSettings``. The loader fails fast at startup: if any required secret is
absent, :func:`load_settings` raises :class:`ConfigurationError` naming the
missing variable(s) and callers exit with a non-zero status.

Secret values are never emitted in logs or error output. Secrets are held as
``pydantic.SecretStr``/``SecretBytes`` so their string representations are
redacted, and :meth:`Settings.safe_dump` returns a log-safe view that lists
secret keys as ``"***"`` without exposing their values.

Requirements: 18.1 (read config from environment), 18.3 (fail-fast naming the
missing variable), 18.4 (exclude secret values from startup logs and errors).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Environment variable names that carry secret values. These must never appear
# in logs or error messages. Used by the scrubber and by safe_dump.
SECRET_ENV_VARS: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "REDIS_URL",
        "ENCRYPTION_KEY",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GITHUB_OAUTH_CLIENT_SECRET",
    }
)

# Every environment variable required for the backend to start. Absence of any
# of these is a fatal configuration error at startup (Req 18.3).
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "DATABASE_URL",
    "REDIS_URL",
    "ENCRYPTION_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_SECRET",
)

_REDACTED = "***"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid at startup.

    The message names each missing variable so operators can fix it, and it
    never includes any secret value (Req 18.3, 18.4).
    """

    def __init__(self, missing: list[str] | None = None, message: str | None = None):
        self.missing = list(missing or [])
        if message is not None:
            super().__init__(message)
        elif self.missing:
            names = ", ".join(sorted(self.missing))
            super().__init__(
                f"Missing required configuration variable(s): {names}. "
                f"Set them in the environment (see .env.example)."
            )
        else:
            super().__init__("Invalid configuration.")


class Settings(BaseSettings):
    """Strongly-typed application settings sourced from the environment.

    Required secrets have no defaults, so their absence surfaces as a
    validation error that :func:`load_settings` converts into a
    :class:`ConfigurationError` naming the missing variable(s).
    """

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="ignore",
    )

    # --- Required secrets (no defaults; absence is fatal at startup) ---------
    DATABASE_URL: SecretStr = Field(..., description="PostgreSQL async DSN")
    REDIS_URL: SecretStr = Field(..., description="Redis connection URL")
    ENCRYPTION_KEY: SecretStr = Field(
        ..., description="Symmetric key for the Encryption_Service; never stored in the DB"
    )

    # --- OAuth provider credentials ------------------------------------------
    GOOGLE_OAUTH_CLIENT_ID: str = Field(..., description="Google OAuth client id")
    GOOGLE_OAUTH_CLIENT_SECRET: SecretStr = Field(..., description="Google OAuth client secret")
    GITHUB_OAUTH_CLIENT_ID: str = Field(..., description="GitHub OAuth client id")
    GITHUB_OAUTH_CLIENT_SECRET: SecretStr = Field(..., description="GitHub OAuth client secret")

    # --- CORS allowlist (Req 21.1) -------------------------------------------
    # ``NoDecode`` disables pydantic-settings' automatic JSON decoding of this
    # complex field from the environment source, so the ``mode="before"``
    # validator below can accept the common comma-separated ``a,b,c`` form used
    # in ``.env`` files as well as a JSON list.
    CORS_ALLOW_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Allowlisted CORS origins; comma-separated or JSON in the env var",
    )

    # --- Request limits and rate limiting (Req 17.5, 21.2) -------------------
    MAX_BODY_SIZE_BYTES: int = Field(
        default=1_048_576,
        ge=1,
        description="Maximum accepted request body size in bytes (413 above)",
    )
    AUTH_RATE_LIMIT_PER_MINUTE: int = Field(
        default=10,
        ge=1,
        description="Allowed authentication requests per client per minute (429 above)",
    )

    # --- Non-secret operational settings -------------------------------------
    ENVIRONMENT: str = Field(default="development", description="Deployment environment name")

    # --- OAuth callback / post-login routing (Req 1.1, 2.1) ------------------
    # Base URL the provider redirects back to; the per-provider callback path
    # (``/auth/callback/{provider}``) is appended to it to form the ``redirect_uri``
    # sent to the provider and expected on the callback. Kept configurable so the
    # same code works behind different hostnames/schemes per deployment.
    OAUTH_REDIRECT_BASE_URL: str = Field(
        default="http://localhost:8000",
        description="Public base URL of the backend used to build the OAuth redirect_uri",
    )
    # Public base URL used ONLY to build inbound WEBHOOK URLs (provider push /
    # Gmail Pub/Sub callbacks). Split from OAUTH_REDIRECT_BASE_URL so a local dev
    # box can expose webhooks via a public tunnel (e.g. ngrok) WITHOUT changing
    # the OAuth redirect_uri (which must stay the host registered with the OAuth
    # client, usually http://localhost:8000). Empty = fall back to
    # OAUTH_REDIRECT_BASE_URL (so existing single-host deployments are unchanged).
    WEBHOOK_PUBLIC_BASE_URL: str = Field(
        default="",
        description="Public HTTPS base URL for inbound webhooks (e.g. an ngrok tunnel); empty falls back to OAUTH_REDIRECT_BASE_URL",
    )
    # Where the browser is sent after a successful login (typically the frontend
    # app). A relative path (e.g. ``/``) or an absolute URL are both accepted.
    POST_LOGIN_REDIRECT_URL: str = Field(
        default="/",
        description="Destination the browser is redirected to after a successful login",
    )

    # --- Database seed values (Req 19.4) -------------------------------------
    # Optional with sensible defaults so the idempotent seed provisions a
    # working default Super_Admin and sample Workspace out of the box, while
    # remaining configurable per-deployment.
    SEED_SUPERADMIN_EMAIL: str = Field(
        default="admin@atomic.local",
        description="Email of the default Super_Admin provisioned by the seed",
    )
    SEED_SUPERADMIN_NAME: str = Field(
        default="Atomic Super Admin",
        description="Display name of the default Super_Admin provisioned by the seed",
    )
    SEED_SUPERADMIN_AUTH_PROVIDER: str = Field(
        default="google",
        description="OAuth provider (google|github) recorded for the seeded Super_Admin",
    )
    SEED_WORKSPACE_NAME: str = Field(
        default="Sample Workspace",
        description="Name of the sample Workspace provisioned by the seed",
    )

    # --- Strands agent model provider (Amazon Bedrock) -----------------------
    # The Strands Agents SDK defaults to Amazon Bedrock. These settings let the
    # Strands_Engine build a BedrockModel without hardcoding anything. AWS
    # credentials themselves are NOT stored here — they are read from the
    # standard AWS credential chain (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
    # AWS_SESSION_TOKEN, a shared profile, or an instance/role) which boto3
    # resolves automatically. Region defaults to Bedrock's us-west-2.
    BEDROCK_MODEL_ID: str = Field(
        default="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        description="Amazon Bedrock model id used by the Strands agent loop",
    )
    AWS_REGION: str = Field(
        default="us-west-2",
        description="AWS region for Amazon Bedrock (Strands default is us-west-2)",
    )
    BEDROCK_STREAMING: bool = Field(
        default=False,
        description=(
            "Use streaming Converse; disabled by default because some Bedrock "
            "models (e.g. Amazon Nova) emit invalid tool-use sequences when "
            "streaming."
        ),
    )
    # --- Agent run cost-control (HARD caps on a SINGLE agent run) ------------
    # Root cause of the 7M-token blowup: a single "process the inbox" run had no
    # turn/token cap and no conversation window, so message history (every email
    # body + every draft) accumulated and was re-sent each turn — quadratic token
    # growth. These caps bound EACH run. Tune per deployment; do not set to 0.
    #
    # Hard per-run budgets passed to the Strands agent as ``Limits`` — the run
    # stops gracefully when any is hit (stop_reason limit_turns/limit_total_tokens).
    AGENT_MAX_TURNS: int = Field(
        default=12,
        ge=1,
        le=200,
        description="Max event-loop turns per single agent run (bounds runaway tool loops)",
    )
    AGENT_MAX_TOTAL_TOKENS: int = Field(
        default=200_000,
        ge=1000,
        description="Hard cap on total tokens (input+output) for a single agent run",
    )
    # Max tokens the model may emit per response (Bedrock inferenceConfig).
    AGENT_MAX_OUTPUT_TOKENS: int = Field(
        default=2048,
        ge=128,
        le=8192,
        description="Max tokens the model may generate per response turn",
    )
    # Sliding-window context cap: only the most recent N messages are kept in
    # context, so history cannot grow unbounded within the allowed turns.
    AGENT_CONVERSATION_WINDOW: int = Field(
        default=24,
        ge=4,
        le=200,
        description="Sliding-window size (messages kept in agent context)",
    )
    # Per-integration poll lock TTL (seconds). A Redis lock held while a poll
    # sweep processes one integration so OVERLAPPING sweeps (or a webhook run
    # coinciding with a poll) cannot both read the same unread inbox and enqueue
    # duplicate runs — the structural cause of the four same-second runs that
    # drove the token blowup. Set comfortably longer than a single run so a
    # crashed worker's lock self-expires. Range 30-1800s.
    POLL_LOCK_TTL_SECONDS: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="TTL for the per-integration poll lock that prevents overlapping runs",
    )
    # Max Gmail messages a SINGLE agent run may process (batch size). The cheap
    # pre-check hands at most this many genuinely-new ids to one run; overflow is
    # left for the next cycle. Bounds tokens/cost per run on top of the Limits
    # caps. Lowered to 5 after the webhook-path blowup (see TOKEN-BLOWUP.md).
    GMAIL_MAX_IDS_PER_RUN: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Max Gmail messages processed per single agent run (batch size)",
    )
    VOICE_ENABLED: bool = Field(
        default=True,
        description="Enable the Nova Sonic voice accessibility feature.",
    )
    NOVA_SONIC_MODEL_ID: str = Field(
        default="amazon.nova-sonic-v1:0",
        description="Amazon Bedrock Nova Sonic speech-to-speech model id.",
    )

    # --- Amazon SNS SMS reply-approval notifications -------------------------
    # SNS reuses the SAME AWS credential chain as Bedrock (AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN or an instance/role) and the same
    # AWS_REGION — no separate secret is introduced. When a reply awaits
    # approval, the platform SMSes the user's stored phone number so they can
    # act without watching the dashboard. Sending is best-effort and NEVER blocks
    # or fails approval creation.
    SNS_ENABLED: bool = Field(
        default=True,
        description="Enable Amazon SNS SMS reply-approval notifications.",
    )
    # SNS transactional SMS is delivered with highest reliability; promotional is
    # cheaper but may be filtered. Reply-approval alerts are transactional.
    SNS_SMS_TYPE: str = Field(
        default="Transactional",
        description="Amazon SNS SMSType attribute: Transactional or Promotional.",
    )
    # Optional alphanumeric Sender ID (where supported by the destination
    # country/carrier). Empty leaves it unset (SNS uses a default long/short code).
    SNS_SMS_SENDER_ID: str = Field(
        default="",
        description="Optional alphanumeric SNS Sender ID; empty leaves it unset.",
    )
    # Safety cap: if a user's estimated SMS spend (USD) reaches this, further
    # notifications to that user are skipped until the next calendar month. 0
    # disables the cap. Protects against runaway cost from a misbehaving loop.
    SNS_MONTHLY_USER_SPEND_CAP_USD: float = Field(
        default=5.0,
        ge=0,
        description="Per-user monthly SMS spend cap in USD (0 disables the cap).",
    )

    # --- Gmail push notifications (watch + Pub/Sub) --------------------------
    # Full Pub/Sub topic name Gmail publishes change notifications to:
    # ``projects/{project}/topics/{topic}``. When set (and a Gmail access token
    # is available), connecting Gmail registers a users.watch for instant
    # triggering; otherwise Gmail falls back to polling.
    GMAIL_PUBSUB_TOPIC: str = Field(
        default="",
        description="Pub/Sub topic (projects/<p>/topics/<t>) for Gmail push; empty disables push",
    )
    # How often the worker polls poll-based integrations (incl. Gmail when push
    # is not configured). Lower = fresher approvals, more API/model usage. The
    # cron fires on minutes that are multiples of this value (1 = every minute).
    # Default raised 1 -> 10 for Bedrock cost-control: a full agent run is only
    # enqueued when the cheap unread pre-check finds genuinely NEW mail, so a
    # 10-minute cadence keeps approvals reasonably fresh while cutting the number
    # of poll sweeps (and any per-sweep API calls) 10x versus every-minute.
    GMAIL_POLL_MINUTES: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Poll interval in minutes for scheduled integration checks (1-60).",
    )

    @field_validator("CORS_ALLOW_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        """Accept a comma-separated string for the CORS allowlist.

        Pydantic natively parses JSON lists for complex fields; this validator
        additionally accepts the common ``a,b,c`` form used in ``.env`` files.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                # A JSON list form (e.g. '["https://a", "https://b"]').
                return json.loads(stripped)
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return value

    def safe_dump(self) -> dict[str, Any]:
        """Return a log-safe view of settings with all secret values redacted.

        Secret-typed fields and any field whose name is in
        :data:`SECRET_ENV_VARS` are replaced with ``"***"`` so this mapping can
        be logged at startup without leaking credentials (Req 18.4).
        """
        safe: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name in SECRET_ENV_VARS or isinstance(value, SecretStr):
                safe[name] = _REDACTED
            else:
                safe[name] = value
        return safe


def _missing_from_validation_error(exc: ValidationError) -> list[str]:
    """Extract the names of missing required fields from a ValidationError."""
    missing: list[str] = []
    for err in exc.errors():
        if err.get("type") == "missing":
            loc = err.get("loc") or ()
            if loc:
                missing.append(str(loc[0]))
    return missing


def load_settings(**overrides: Any) -> Settings:
    """Load and validate settings, failing fast on missing required variables.

    Raises:
        ConfigurationError: naming every missing required variable when one or
            more required secrets are absent, or describing other invalid
            configuration. The error message contains no secret values.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        missing = _missing_from_validation_error(exc)
        if missing:
            raise ConfigurationError(missing=missing) from None
        # Non-"missing" validation problems: surface a generic, secret-free message.
        raise ConfigurationError(
            message="Invalid configuration; check environment variable values."
        ) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton, loading it on first use."""
    return load_settings()
