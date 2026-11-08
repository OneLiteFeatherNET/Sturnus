"""remove the per-guild sign-in clients

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-08 00:00:00.000000

**This cannot be undone.** `upgrade` drops `guild_oauth_client`, which holds
each guild's OAuth client id and its client secret wrapped by the master key,
and drops `console_state.guild_id`. The rows and the wrapped secrets are gone
once this runs. `downgrade` recreates the two shapes empty so that Alembic can
walk the revision graph backwards; it does not restore data. Take a database
backup before upgrading.

Why these two: the table is read only by the per-guild sign-in path that this
release removes, and `console_state.guild_id` exists only to carry a guild
across the sign-in round trip for that path. Sign-in through the installation's
own Outline client reads neither. Migration 0013 created both as part of a
larger round of work; this revision removes them with the code that used them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("guild_oauth_client")
    op.drop_column("console_state", "guild_id")


def downgrade() -> None:
    """Downgrade schema.

    Recreates the empty shapes exactly as 0013 defined them. No rows return.
    """
    op.add_column("console_state", sa.Column("guild_id", sa.BigInteger(), nullable=True))
    op.create_table(
        "guild_oauth_client",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("wrapped_client_secret", sa.LargeBinary(), nullable=True),
        sa.Column("encryption_key_id", sa.Text(), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id"),
        sa.UniqueConstraint("slug"),
    )
