"""session channel name

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-20 00:00:00.000000

The protocol names the channel the meeting was held in, and the worker --
which writes it -- has no Discord connection and knows only the id. The bot
does know the name at the moment it opens the session, so it records it
there.

Nullable, and deliberately not backfilled: sessions opened before this
column existed have no name to recover, and a channel renamed later should
not rewrite the protocols of meetings held under the old name.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("session", sa.Column("channel_name", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("session", "channel_name")
