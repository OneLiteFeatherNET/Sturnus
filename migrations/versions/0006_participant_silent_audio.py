"""participant silent audio

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-20 00:00:00.000000

Records when a speaker's audio was first seen arriving with no audible
level in it -- packets received and decoded, every sample at the noise
floor. The bot writes it during the session, at the same moment it says so
in the channel, because that message is gone by the next meeting and this
column is what an operator can still read afterwards.

Two live sessions produced empty transcripts from full-length recordings,
and nothing in the database distinguished "we could not hear them" from
"they said nothing" -- both leave exactly the same participant row. This is
the column that answers it.

Nullable, and deliberately not backfilled: being quiet is normal and null
is what nearly every participant will always carry, so there is nothing to
fill in for the sessions that predate this column -- their audio is gone,
and no value could be honestly inferred from what is left.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "session_participant",
        sa.Column("silent_audio_detected_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("session_participant", "silent_audio_detected_at")
