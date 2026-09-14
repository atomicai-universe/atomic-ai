"""add users.is_banned

Adds the persistent ``is_banned`` boolean flag to ``users`` (Req 12.4). A super
admin ban sets this flag to ``true``; the auth find-or-create seam rejects a
banned existing user so a banned account cannot obtain a new session, while the
ban operation additionally revokes the user's existing ``sessions`` rows to
invalidate any active session immediately.

The column is non-nullable with a ``false`` server default so it back-fills
cleanly on an existing populated table (every pre-existing user is unbanned).

Requirements: 12.4.

Revision ID: 0002_user_is_banned
Revises: 0001_initial_schema
Create Date: 2024-01-02 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_user_is_banned"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_banned",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_banned")
