"""integration flexible per-provider credentials

Adds a flexible, encrypted credential store to ``integrations`` so every
provider's credential shape (OAuth, API key, bot token, key+secret pairs, basic
auth) can be stored encrypted at rest (BUILD.md; Req 6.1/6.2):

- ``encrypted_credentials`` (bytea, nullable): a single Fernet-encrypted JSON
  blob holding all SECRET fields a provider needs ({field_name: value}).
- ``config`` (jsonb, nullable): non-secret operational values (region, host,
  base_url, tenant/account/instance id, etc.) — never secrets.

Also relaxes the legacy ``encrypted_access_token`` to nullable, since new rows
store their credentials in ``encrypted_credentials`` instead. Existing rows keep
their ``encrypted_access_token`` / ``encrypted_refresh_token`` and continue to
work (the vault reads either shape).

Requirements: 6.1, 6.2, 7.1, 7.2.

Revision ID: 0003_integration_flex_creds
Revises: 0002_user_is_banned
Create Date: 2024-01-03 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0003_integration_flex_creds"
down_revision: str | None = "0002_user_is_banned"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "integrations",
        sa.Column("config", JSONB(), nullable=True),
    )
    # New rows keep credentials in encrypted_credentials, so the legacy
    # single-token column is no longer required.
    op.alter_column(
        "integrations",
        "encrypted_access_token",
        existing_type=sa.LargeBinary(),
        nullable=True,
    )


def downgrade() -> None:
    # Re-tighten the legacy column only if no NULLs exist; guard to avoid failing
    # a downgrade on rows that used the new store. Best-effort: leave nullable if
    # any row lacks a legacy token.
    conn = op.get_bind()
    null_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM integrations WHERE encrypted_access_token IS NULL"
        )
    ).scalar_one()
    if null_count == 0:
        op.alter_column(
            "integrations",
            "encrypted_access_token",
            existing_type=sa.LargeBinary(),
            nullable=False,
        )
    op.drop_column("integrations", "config")
    op.drop_column("integrations", "encrypted_credentials")
