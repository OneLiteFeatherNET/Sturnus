"""console sign-in states

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-21 00:00:00.000000

A single-use OAuth state for a console sign-in, in a table of its own.

`/link` knows who is linking before the browser ever leaves -- a slash
command was run by somebody -- so `oauth_state` carries a
`discord_user_id`. A console sign-in does not: who this is only becomes
known when the provider answers, which is after the round trip.

An earlier draft of this work squeezed console states into `oauth_state`
behind a placeholder id, and a test caught what that costs.
`LinkStateStore.consume` does not filter by provider, so the account-link
callback consumed a console state and produced a pending link for a user
id that does not exist. A table of its own makes that unrepresentable.

No foreign key and no owner column, deliberately: this row exists for the
ten minutes between a browser leaving and coming back, and it names
nobody for that whole time.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "console_state",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("console_state")
