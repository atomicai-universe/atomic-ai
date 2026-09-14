"""initial schema

Creates the full Atomic AI schema: all ten tables (``users``, ``sessions``,
``workspaces``, ``workspace_members``, ``workspace_invites``, ``integrations``,
``rules``, ``agent_sessions``, ``approval_requests``, ``system_audit_logs``),
the eight native Postgres ``ENUM`` types, every foreign key with its designed
``ON DELETE`` behavior, the unique constraints, and the append-only enforcement
trigger on ``system_audit_logs``.

Foreign-key ``ON DELETE`` behavior (design.md "Model Notes"):

- ``CASCADE`` to ``workspaces`` for members, invites, integrations, rules,
  agent_sessions, approval_requests (Req 20.3, 20.4).
- ``SET NULL`` for ``system_audit_logs.workspace_id`` / ``user_id`` so audit
  records survive workspace/user deletion (Req 20.4).
- ``RESTRICT`` for ``workspaces.created_by_user_id`` so a user who owns a
  workspace cannot be hard-deleted out from under it.

Append-only audit trail (Req 15.5): a ``plpgsql`` trigger function raises an
exception, wired to a ``BEFORE UPDATE OR DELETE`` trigger on
``system_audit_logs`` so persisted audit entries can never be mutated or
removed through any path, including direct SQL.

The one exception the trigger must tolerate is the ``ON DELETE SET NULL``
foreign-key cascade required by Req 20.4: when a ``workspaces`` (or ``users``)
row is deleted, Postgres issues an internal
``UPDATE system_audit_logs SET workspace_id = NULL`` to preserve the audit
record. A naive ``BEFORE UPDATE`` trigger would block that cascade and make
workspace deletion impossible whenever an audit row references it, so the
guard permits an UPDATE whose *only* effect is nulling ``workspace_id`` /
``user_id`` (the FK-cascade shape) and blocks every other UPDATE plus every
DELETE.

Requirements: 15.5, 20.2, 20.4.

Revision ID: 0001_initial_schema
Revises: (base)
Create Date: 2024-01-01 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Native Postgres ENUM type definitions.
#
# ``create_type=False`` so the types are created explicitly (once) below via
# ``.create()`` and NOT implicitly re-emitted by each ``create_table`` column.
# This mirrors the label *values* declared on the StrEnums in app/db/models.py.
# ---------------------------------------------------------------------------

auth_provider = postgresql.ENUM(
    "google", "github", name="auth_provider", create_type=False
)
member_role = postgresql.ENUM(
    "owner", "admin", "member", "viewer", name="member_role", create_type=False
)
invite_role = postgresql.ENUM(
    "admin", "member", "viewer", name="invite_role", create_type=False
)
invite_status = postgresql.ENUM(
    "pending", "accepted", "expired", name="invite_status", create_type=False
)
integration_category = postgresql.ENUM(
    "email",
    "email_marketing",
    "social",
    "office",
    "developer",
    "crm",
    "support",
    "erp",
    "collaboration",
    "cloud",
    "hr",
    "calendar",
    name="integration_category",
    create_type=False,
)
integration_status = postgresql.ENUM(
    "active", "error", "disconnected", name="integration_status", create_type=False
)
agent_session_status = postgresql.ENUM(
    "running",
    "completed",
    "failed",
    "terminated",
    name="agent_session_status",
    create_type=False,
)
approval_status = postgresql.ENUM(
    "pending", "approved", "rejected", name="approval_status", create_type=False
)

_ALL_ENUMS = (
    auth_provider,
    member_role,
    invite_role,
    invite_status,
    integration_category,
    integration_status,
    agent_session_status,
    approval_status,
)

# Trigger + function DDL enforcing the append-only invariant (Req 15.5).
#
# DELETE is always rejected. UPDATE is rejected too, EXCEPT the narrow
# FK-cascade case required by Req 20.4: an update whose only effect is setting
# ``workspace_id`` and/or ``user_id`` to NULL (with every other column and the
# ``id`` unchanged) is the ``ON DELETE SET NULL`` cascade preserving the audit
# record, and is allowed through unmodified.
_AUDIT_GUARD_FN = """
CREATE OR REPLACE FUNCTION system_audit_logs_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Allow ONLY the FK SET-NULL cascade: workspace_id/user_id may move to
        -- NULL, and no other column (including id) may change.
        IF NEW.id IS NOT DISTINCT FROM OLD.id
           AND NEW.action IS NOT DISTINCT FROM OLD.action
           AND NEW.metadata IS NOT DISTINCT FROM OLD.metadata
           AND NEW."timestamp" IS NOT DISTINCT FROM OLD."timestamp"
           AND (NEW.workspace_id IS NOT DISTINCT FROM OLD.workspace_id
                OR NEW.workspace_id IS NULL)
           AND (NEW.user_id IS NOT DISTINCT FROM OLD.user_id
                OR NEW.user_id IS NULL)
        THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'system_audit_logs is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$ LANGUAGE plpgsql;
"""

_AUDIT_GUARD_TRIGGER = """
CREATE TRIGGER trg_system_audit_logs_append_only
BEFORE UPDATE OR DELETE ON system_audit_logs
FOR EACH ROW EXECUTE FUNCTION system_audit_logs_append_only();
"""


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create the native ENUM types once, up front.
    for enum_type in _ALL_ENUMS:
        enum_type.create(bind, checkfirst=False)

    # 2. users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("auth_provider", auth_provider, nullable=False),
        sa.Column(
            "is_superadmin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # 3. sessions
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.Column(
            "revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token", name="uq_sessions_token"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # 4. workspaces
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_workspaces_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    # 5. workspace_members
    op.create_table(
        "workspace_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", member_role, nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_members_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_workspace_members_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_members"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )
    op.create_index(
        "ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"]
    )
    op.create_index(
        "ix_workspace_members_user_id", "workspace_members", ["user_id"]
    )

    # 6. workspace_invites
    op.create_table(
        "workspace_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", invite_role, nullable=False),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            invite_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_invites_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_invites"),
        sa.UniqueConstraint("token", name="uq_workspace_invites_token"),
    )
    op.create_index(
        "ix_workspace_invites_workspace_id", "workspace_invites", ["workspace_id"]
    )

    # 7. integrations
    op.create_table(
        "integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("category", integration_category, nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column(
            "is_shared_with_workspace",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column(
            "status",
            integration_status,
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_integrations_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_integrations_created_by_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integrations"),
    )
    op.create_index(
        "ix_integrations_workspace_id", "integrations", ["workspace_id"]
    )

    # 8. rules
    op.create_table(
        "rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=True),
        sa.Column("rule_prompt", sa.Text(), nullable=False),
        sa.Column(
            "is_workspace_wide",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_rules_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_rules_created_by_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rules"),
    )
    op.create_index("ix_rules_workspace_id", "rules", ["workspace_id"])

    # 9. agent_sessions
    op.create_table(
        "agent_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "triggered_by_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column(
            "total_tokens_used",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "execution_time_ms",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "execution_logs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "status",
            agent_session_status,
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_agent_sessions_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by_user_id"],
            ["users.id"],
            name="fk_agent_sessions_triggered_by_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sessions"),
    )
    op.create_index(
        "ix_agent_sessions_workspace_id", "agent_sessions", ["workspace_id"]
    )

    # 10. approval_requests
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "triggered_by_user_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column(
            "arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "status",
            approval_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_approval_requests_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_session_id"],
            ["agent_sessions.id"],
            name="fk_approval_requests_agent_session_id_agent_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by_user_id"],
            ["users.id"],
            name="fk_approval_requests_triggered_by_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name="fk_approval_requests_reviewed_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_requests"),
    )
    op.create_index(
        "ix_approval_requests_workspace_id", "approval_requests", ["workspace_id"]
    )
    op.create_index(
        "ix_approval_requests_agent_session_id",
        "approval_requests",
        ["agent_session_id"],
    )

    # 11. system_audit_logs (retained on workspace/user deletion -> SET NULL)
    op.create_table(
        "system_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_system_audit_logs_workspace_id_workspaces",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_system_audit_logs_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_system_audit_logs"),
    )
    op.create_index(
        "ix_system_audit_logs_workspace_id", "system_audit_logs", ["workspace_id"]
    )

    # 12. Append-only enforcement trigger (Req 15.5).
    op.execute(_AUDIT_GUARD_FN)
    op.execute(_AUDIT_GUARD_TRIGGER)


def downgrade() -> None:
    bind = op.get_bind()

    # Drop the append-only trigger + function first so the table can be dropped.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_system_audit_logs_append_only "
        "ON system_audit_logs"
    )
    op.execute("DROP FUNCTION IF EXISTS system_audit_logs_append_only()")

    # Drop tables in reverse dependency order.
    op.drop_index("ix_system_audit_logs_workspace_id", table_name="system_audit_logs")
    op.drop_table("system_audit_logs")

    op.drop_index(
        "ix_approval_requests_agent_session_id", table_name="approval_requests"
    )
    op.drop_index(
        "ix_approval_requests_workspace_id", table_name="approval_requests"
    )
    op.drop_table("approval_requests")

    op.drop_index("ix_agent_sessions_workspace_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")

    op.drop_index("ix_rules_workspace_id", table_name="rules")
    op.drop_table("rules")

    op.drop_index("ix_integrations_workspace_id", table_name="integrations")
    op.drop_table("integrations")

    op.drop_index(
        "ix_workspace_invites_workspace_id", table_name="workspace_invites"
    )
    op.drop_table("workspace_invites")

    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_index(
        "ix_workspace_members_workspace_id", table_name="workspace_members"
    )
    op.drop_table("workspace_members")

    op.drop_table("workspaces")

    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_table("users")

    # Finally drop the native ENUM types (reverse of creation order).
    for enum_type in reversed(_ALL_ENUMS):
        enum_type.drop(bind, checkfirst=False)
