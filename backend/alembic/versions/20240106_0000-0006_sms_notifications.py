"""add SMS reply-approval notifications (phone on users + sms_notifications table)

Adds Amazon SNS SMS notification support:

- ``users.phone_number`` (varchar(20), nullable): the user's E.164 mobile number,
  NULL until they opt in on their account profile. PII — never logged.
- ``users.phone_country`` (varchar(2), nullable): ISO 3166-1 alpha-2 country of
  the number, used to price SMS by the destination's Amazon SNS rate.
- ``users.sms_notifications_enabled`` (bool, default true): pause switch that
  keeps a number on file without sending. Back-fills true on existing rows.
- ``sms_notifications`` table: one row per SMS published, capturing the SNS
  ``message_id``, billed ``segments``, and the estimated ``unit_price_usd`` /
  ``total_cost_usd`` (from the destination country's per-message SNS price) so
  the app can report SMS COUNT and SMS SPEND per country without AWS billing
  access. ``user_id`` / ``workspace_id`` / ``approval_request_id`` are
  ``ON DELETE SET NULL`` so historical spend survives deletions (like the audit
  log). ``user_id`` and ``workspace_id`` are indexed for per-user/per-workspace
  aggregation.

Revision ID: 0006_sms_notifications
Revises: 0005_processed_messages
Create Date: 2024-01-06 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_sms_notifications"
down_revision: str | None = "0005_processed_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("phone_number", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("phone_country", sa.String(length=2), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "sms_notifications_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )

    op.create_table(
        "sms_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "approval_request_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("phone_number", sa.String(length=20), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("sns_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "segments", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "unit_price_usd",
            sa.String(length=16),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column(
            "total_cost_usd",
            sa.String(length=16),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_sms_notifications_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_sms_notifications_workspace_id_workspaces",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id"],
            ["approval_requests.id"],
            name="fk_sms_notifications_approval_request_id_approval_requests",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sms_notifications"),
    )
    op.create_index(
        "ix_sms_notifications_user_id", "sms_notifications", ["user_id"]
    )
    op.create_index(
        "ix_sms_notifications_workspace_id", "sms_notifications", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_sms_notifications_workspace_id", table_name="sms_notifications")
    op.drop_index("ix_sms_notifications_user_id", table_name="sms_notifications")
    op.drop_table("sms_notifications")
    op.drop_column("users", "sms_notifications_enabled")
    op.drop_column("users", "phone_country")
    op.drop_column("users", "phone_number")
