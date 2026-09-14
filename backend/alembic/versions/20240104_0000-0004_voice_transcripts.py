"""voice transcripts table

Adds a ``voice_transcripts`` table storing the FULL transcript of every voice
session (from the welcome message onward), purely for debugging via the DB —
transcript text is NEVER written to the application logs (ERROR.md Task 1):

- ``session_id`` (uuid, indexed): a per-WebSocket-connection id grouping all
  turns of one voice conversation.
- ``role`` (varchar): "user" | "assistant" | "system".
- ``text`` (text): the spoken turn.

Foreign keys follow the design's ``ON DELETE`` conventions: CASCADE to
``workspaces`` (Req 20.3) and SET NULL to ``users`` so a transcript survives a
user deletion. Workspace is indexed like the other tenant-scoped tables.

Revision ID: 0004_voice_transcripts
Revises: 0003_integration_flex_creds
Create Date: 2024-01-04 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_voice_transcripts"
down_revision: str | None = "0003_integration_flex_creds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_transcripts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_voice_transcripts_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_voice_transcripts_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_voice_transcripts"),
    )
    op.create_index(
        "ix_voice_transcripts_workspace_id", "voice_transcripts", ["workspace_id"]
    )
    op.create_index(
        "ix_voice_transcripts_session_id", "voice_transcripts", ["session_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_voice_transcripts_session_id", table_name="voice_transcripts")
    op.drop_index(
        "ix_voice_transcripts_workspace_id", table_name="voice_transcripts"
    )
    op.drop_table("voice_transcripts")
