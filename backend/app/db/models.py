"""SQLAlchemy 2.0 ORM models for the Atomic AI platform.

Contains all nine ``PROJECT.md`` models plus a ``sessions`` table used for true
session invalidation. The mapping uses the typed ``Mapped``/``mapped_column``
style on the shared :class:`app.db.session.Base`.

Design decisions (see design.md "Model Notes"):

- Domain enums are declared once as Python :class:`enum.StrEnum` and mirrored as
  Postgres native ``ENUM`` types so "exactly one of" constraints are enforced at
  the database level (Req 1.6, 4.1, 5.7, 10.6).
- Foreign keys to ``workspaces`` use ``ON DELETE CASCADE`` for members, invites,
  integrations, rules, agent_sessions, and approval_requests (Req 20.3, 20.4).
- ``system_audit_logs`` is retained on workspace deletion: its ``workspace_id``
  is nullable and uses ``ON DELETE SET NULL`` so the audit record survives.
- ``slug`` and invite ``token`` are unique (Req 3.2, 5.1). Encrypted token
  columns and session tokens are stored as ``bytea`` ciphertext (Req 6.2).

Requirements: 1.6, 4.1, 5.7, 6.2, 7.2, 10.6, 20.1, 20.3.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ---------------------------------------------------------------------------
# Enum types (Python StrEnum mirrored as Postgres native ENUM)
# ---------------------------------------------------------------------------


class AuthProvider(enum.StrEnum):
    """OAuth identity providers a user may authenticate with (Req 1.6)."""

    GOOGLE = "google"
    GITHUB = "github"


class MemberRole(enum.StrEnum):
    """Workspace membership roles, ordered most→least privileged (Req 4.1)."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class InviteRole(enum.StrEnum):
    """Roles assignable via an invite. ``owner`` is not invitable (Req 5.7)."""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class InviteStatus(enum.StrEnum):
    """Lifecycle states of a workspace invite."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"


class IntegrationCategory(enum.StrEnum):
    """The 12 platform categories from PROJECT.md (Req 7.2, 10.6)."""

    EMAIL = "email"
    EMAIL_MARKETING = "email_marketing"
    SOCIAL = "social"
    OFFICE = "office"
    DEVELOPER = "developer"
    CRM = "crm"
    SUPPORT = "support"
    ERP = "erp"
    COLLABORATION = "collaboration"
    CLOUD = "cloud"
    HR = "hr"
    CALENDAR = "calendar"


class IntegrationStatus(enum.StrEnum):
    """Connection health of an integration."""

    ACTIVE = "active"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class AgentSessionStatus(enum.StrEnum):
    """Lifecycle of an agent execution session."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"


class ApprovalStatus(enum.StrEnum):
    """Review states of a collaborative approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Native Postgres ENUM type definitions. ``values_callable`` makes the stored
# labels the StrEnum *values* (e.g. "google") rather than the member *names*
# (e.g. "GOOGLE"). ``create_type=True`` lets Alembic/create_all emit CREATE TYPE.
def _pg_enum(python_enum: type[enum.StrEnum], name: str) -> SAEnum:
    return SAEnum(
        python_enum,
        name=name,
        native_enum=True,
        create_type=True,
        values_callable=lambda e: [member.value for member in e],
    )


AUTH_PROVIDER_ENUM = _pg_enum(AuthProvider, "auth_provider")
MEMBER_ROLE_ENUM = _pg_enum(MemberRole, "member_role")
INVITE_ROLE_ENUM = _pg_enum(InviteRole, "invite_role")
INVITE_STATUS_ENUM = _pg_enum(InviteStatus, "invite_status")
INTEGRATION_CATEGORY_ENUM = _pg_enum(IntegrationCategory, "integration_category")
INTEGRATION_STATUS_ENUM = _pg_enum(IntegrationStatus, "integration_status")
AGENT_SESSION_STATUS_ENUM = _pg_enum(AgentSessionStatus, "agent_session_status")
APPROVAL_STATUS_ENUM = _pg_enum(ApprovalStatus, "approval_status")


# ---------------------------------------------------------------------------
# Column type helpers
# ---------------------------------------------------------------------------

# Postgres ``uuid`` column type with a client-side default so ids are populated
# without a DB round-trip.
_UUID = PGUUID(as_uuid=True)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(_UUID, primary_key=True, default=uuid.uuid4)


def _tz_now() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_provider: Mapped[AuthProvider] = mapped_column(
        AUTH_PROVIDER_ENUM, nullable=False
    )
    is_superadmin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Persistent ban flag (Req 12.4). A banned user's active sessions are
    # revoked at ban time and future authentication is rejected at the
    # find-or-create seam (see ``app.services.auth_service``). Kept as a column
    # (rather than a session-only revocation) because blocking *future* auth
    # requires durable state that outlives any single session.
    is_banned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # --- SMS reply-approval notifications (Amazon SNS) -----------------------
    # The user's mobile number in E.164 form (e.g. "+14155550123"); NULL until
    # the user opts in on their account profile. ``phone_country`` is the ISO
    # 3166-1 alpha-2 code the number belongs to (e.g. "US"), used to price SMS
    # by Amazon SNS per-destination rates. ``sms_notifications_enabled`` lets a
    # user keep a number on file but pause notifications. A phone number is
    # PII-sensitive: it is never written to application logs.
    phone_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    sms_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = _tz_now()

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    """Server-side session records backing ``core/security.py`` invalidation.

    ``token`` is stored as ``bytea`` ciphertext/opaque bytes so a raw token is
    never persisted in plaintext (Req 6.2 aligned).
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = _tz_now()

    user: Mapped["User"] = relationship(back_populates="sessions")


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _tz_now()

    members: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    invites: Mapped[list["WorkspaceInvite"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[MemberRole] = mapped_column(MEMBER_ROLE_ENUM, nullable=False)
    joined_at: Mapped[datetime] = _tz_now()

    workspace: Mapped["Workspace"] = relationship(back_populates="members")


class WorkspaceInvite(Base):
    __tablename__ = "workspace_invites"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[InviteRole] = mapped_column(INVITE_ROLE_ENUM, nullable=False)
    token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    status: Mapped[InviteStatus] = mapped_column(
        INVITE_STATUS_ENUM,
        nullable=False,
        default=InviteStatus.PENDING,
        server_default=InviteStatus.PENDING.value,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _tz_now()

    workspace: Mapped["Workspace"] = relationship(back_populates="invites")


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[IntegrationCategory] = mapped_column(
        INTEGRATION_CATEGORY_ENUM, nullable=False
    )
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_shared_with_workspace: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Legacy single-token columns (kept for backward compatibility with rows
    # created before the flexible credential store). Now nullable: new rows use
    # `encrypted_credentials` instead. (BUILD.md flexible per-provider creds.)
    encrypted_access_token: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    # Flexible encrypted credential store: a single Fernet-encrypted JSON blob
    # holding every SECRET field a provider needs ({field_name: value}), so any
    # credential shape (OAuth, API key, bot token, key+secret pairs, basic auth)
    # is stored encrypted at rest (Req 6.1/6.2). Nullable so legacy rows and the
    # migration remain valid.
    encrypted_credentials: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    # Non-secret operational config for the provider (region, host, base_url,
    # tenant/account/instance id, etc.). Never contains secrets — those live in
    # `encrypted_credentials`. Stored as JSONB for queryability.
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[IntegrationStatus] = mapped_column(
        INTEGRATION_STATUS_ENUM,
        nullable=False,
        default=IntegrationStatus.ACTIVE,
        server_default=IntegrationStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = _tz_now()


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rule_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    is_workspace_wide: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = _tz_now()


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    triggered_by_user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    total_tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    execution_time_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    execution_logs: Mapped[list | dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[AgentSessionStatus] = mapped_column(
        AGENT_SESSION_STATUS_ENUM,
        nullable=False,
        default=AgentSessionStatus.RUNNING,
        server_default=AgentSessionStatus.RUNNING.value,
    )
    created_at: Mapped[datetime] = _tz_now()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID,
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    triggered_by_user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        APPROVAL_STATUS_ENUM,
        nullable=False,
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = _tz_now()


class VoiceTranscript(Base):
    """Full transcript of a voice session, for debugging (never logged to app logs).

    One row per spoken turn — the greeting/welcome, every user transcript, every
    assistant transcript, and every deterministic confirmation the gateway
    speaks. ``session_id`` is a per-WebSocket-connection id (uuid4 minted at
    session construction) so all turns of one voice conversation share it.
    Tenancy is server-derived (workspace/user come from the request context,
    never the model). Retained via CASCADE on workspace deletion.
    """

    __tablename__ = "voice_transcripts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Per-WebSocket-connection id grouping every turn of one voice conversation.
    session_id: Mapped[uuid.UUID] = mapped_column(_UUID, nullable=False, index=True)
    # "user" | "assistant" | "system".
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _tz_now()


class ProcessedMessage(Base):
    """Durable "already-seen" set of provider messages (Bedrock cost-control).

    WHY this table exists: the scheduled poller used to enqueue a FULL, multi-
    step Bedrock agent run every interval for every active integration —
    unconditionally, even for an empty inbox — and the agent re-read the SAME
    unread emails on every poll. Each run cost ~12 Bedrock round-trips. This
    table lets the cheap (no-Bedrock) unread pre-check in
    :func:`app.agents.tasks.poll_integrations` remember which provider messages
    have ALREADY been handed to an agent run, so a message is handed to Bedrock
    AT MOST ONCE and an empty/unchanged inbox enqueues zero runs.

    Semantics of "seen" = PROCESSED, independent of read/unread state. A row is
    written the moment a message id is included in a scheduled run's scope (see
    :func:`app.services.gmail_poll.record_seen`) AND when a reply to it is
    sent/drafted (see the approval execution path). Once seen it is never re-run,
    even if it is still unread on a later poll.

    Tenancy is server-derived: ``workspace_id`` / ``integration_id`` come from
    the DB integration row, never from model input. The UNIQUE constraint on
    ``(integration_id, provider_message_id)`` makes re-inserts idempotent (a
    second poll that sees the same id is a no-op). CASCADE on workspace and
    integration deletion keeps the set from outliving its owner.
    """

    __tablename__ = "processed_messages"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "provider_message_id",
            name="uq_processed_message_integration_msg",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        _UUID,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Provider slug (e.g. "gmail"); kept alongside the id so the same table can
    # host other poll providers later without ambiguity.
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    # The provider's own message id (e.g. the Gmail message id). Paired with
    # integration_id in the UNIQUE constraint above for idempotent inserts.
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[datetime] = _tz_now()


class SystemAuditLog(Base):
    """Append-only audit log. Retained on workspace/user deletion (Req 20.4)."""

    __tablename__ = "system_audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``metadata`` is reserved on the Declarative base, so map the attribute
    # ``log_metadata`` to the physical ``metadata`` column.
    log_metadata: Mapped[dict | list | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    timestamp: Mapped[datetime] = _tz_now()


class SmsNotification(Base):
    """One record per SMS sent via Amazon SNS, for usage + spend tracking.

    Written every time the platform publishes a reply-approval SMS. It captures
    the SNS ``message_id``, the number of billed message segments, and the
    estimated cost (unit price × segments) computed from the destination
    country's Amazon SNS per-message price — so the app can report SMS COUNT and
    SMS SPEND per phone-number country without querying AWS billing.

    ``status`` is ``"sent"`` on a successful publish or ``"failed"`` when the
    publish raised (``error`` then holds a short, secret-free reason). The full
    phone number is stored (it is the user's own number, needed for support and
    per-number accounting) but is NEVER written to application logs. Retained via
    CASCADE on user/workspace deletion is intentionally NOT applied to the user
    FK (``ON DELETE SET NULL``) so historical spend survives a user removal, in
    the same spirit as the audit log.
    """

    __tablename__ = "sms_notifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID,
        ForeignKey("approval_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Destination number (E.164) and its ISO 3166-1 alpha-2 country. Country is
    # the pricing key. Never logged.
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # SNS Publish MessageId (opaque), null when the publish failed.
    sns_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Billed segments (a long message is split; each segment is priced).
    segments: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Per-segment price (USD) for the destination country, and the total
    # (unit_price × segments). Stored as strings to preserve exact decimals
    # without float drift; parsed as Decimal by the service/schema layer.
    unit_price_usd: Mapped[str] = mapped_column(
        String(16), nullable=False, default="0", server_default="0"
    )
    total_cost_usd: Mapped[str] = mapped_column(
        String(16), nullable=False, default="0", server_default="0"
    )
    # "sent" | "failed".
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = _tz_now()


__all__ = [
    "AuthProvider",
    "MemberRole",
    "InviteRole",
    "InviteStatus",
    "IntegrationCategory",
    "IntegrationStatus",
    "AgentSessionStatus",
    "ApprovalStatus",
    "User",
    "Session",
    "Workspace",
    "WorkspaceMember",
    "WorkspaceInvite",
    "Integration",
    "Rule",
    "AgentSession",
    "ApprovalRequest",
    "VoiceTranscript",
    "ProcessedMessage",
    "SystemAuditLog",
    "SmsNotification",
]
