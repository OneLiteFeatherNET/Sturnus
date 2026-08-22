"""job measurements and mirrored administrators

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-21 00:00:00.000000

Two changes the console needs, both of which persist something the system
already knew and threw away.

**Three columns on `transcription_job`.** The worker has always computed
how long a track was, how much of it the gate judged to be speech, and how
many segments came back -- and put all three into a log line and a metric.
Both of those are retained for weeks while the job's own row lives as long
as the guild does, so "how much has this person actually said, across every
meeting they were in" was a question the database could not answer at all.

Nullable, and deliberately not backfilled. The audio a historical row was
measured from may already have been deleted by the retention sweep, so
there is nothing to derive the numbers from -- and a zero would be a claim
("said nothing") where the truth is an absence ("never measured"). The
console renders null as "not recorded" rather than as a figure.

**`admin_member`.** `admin_role_id` is a Discord role, and the API process
that serves the console has no gateway to ask about role membership. Giving
it one would undo the credential separation the system rests on: it already
holds S3 and the master key, so it can decrypt every recording ever made --
not a process to also hand the ability to act as the bot (Spec 13.2). The
bot mirrors the membership into this table on the sweep it already runs.

The composite primary key is the whole integrity story: one row per person
per guild, so a sync cannot duplicate somebody, and an admin in one guild
is an ordinary participant in another.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("transcription_job", sa.Column("audio_seconds", sa.Float(), nullable=True))
    op.add_column("transcription_job", sa.Column("speech_seconds", sa.Float(), nullable=True))
    op.add_column("transcription_job", sa.Column("segment_count", sa.Integer(), nullable=True))
    op.create_table(
        "admin_member",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "discord_user_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("admin_member")
    op.drop_column("transcription_job", "segment_count")
    op.drop_column("transcription_job", "speech_seconds")
    op.drop_column("transcription_job", "audio_seconds")
