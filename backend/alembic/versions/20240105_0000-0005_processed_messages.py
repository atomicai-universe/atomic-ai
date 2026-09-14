"""processed messages table (Bedrock cost-control)

Adds a ``processed_messages`` table: the durable "already-seen" set the cheap
(no-Bedrock) unread pre-check in ``app.agents.tasks.poll_integrations`` uses so
a provider message (e.g. a Gmail email) is handed to a full multi-step agent run
AT MOST ONCE. Before this, the poller enqueued a full Bedrock run every interval
for every active integration unconditionally and re-read the same unread emails
each poll, wasting spend. "Seen" here means PROCESSED (independent of read state):

- ``workspace_id`` (uuid, indexed): owning workspace (CASCADE on delete).
- ``integration_id`` (uuid, indexed): owning integration (CASCADE on delete).
- ``provider`` (varchar): provider slug, e.g. "gmail".
- ``provider_message_id`` (varchar): the provider's own message id.
- ``first_seen_at`` (tz-aware, default now()).

A UNIQUE constraint on ``(integration_id, provider_message_id)`` makes re-inserts
idempotent — a later poll that sees the same id is a no-op (ON CONFLICT DO
NOTHING at the service layer). Foreign keys follow the design's ``ON DELETE``
conventions (CASCADE to workspaces/integrations, Req 20.3); workspace and
integration are indexed like the other tenant-scoped tables.

Revision ID: 0005_processed_messages
Revises: 0004_voice_transcripts
Create Date: 2024-01-05 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_processed_messages"
down_revision: str | None = "0004_voice_transcripts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_processed_messages_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name="fk_processed_messages_integration_id_integrations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processed_messages"),
        # Idempotent re-inserts: a second poll seeing the same message is a no-op.
        sa.UniqueConstraint(
            "integration_id",
            "provider_message_id",
            name="uq_processed_message_integration_msg",
        ),
    )
    op.create_index(
        "ix_processed_messages_workspace_id", "processed_messages", ["workspace_id"]
    )
    op.create_index(
        "ix_processed_messages_integration_id",
        "processed_messages",
        ["integration_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processed_messages_integration_id", table_name="processed_messages"
    )
    op.drop_index(
        "ix_processed_messages_workspace_id", table_name="processed_messages"
    )
    op.drop_table("processed_messages")
