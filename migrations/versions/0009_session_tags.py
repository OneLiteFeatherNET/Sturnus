"""private tags on recordings

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-22 00:00:00.000000

One table, carrying labels a participant puts on the meetings they were
in, so that a console with a hundred recordings in it can be narrowed to
the four somebody actually needs.

**The owner is in the primary key, and that is the whole privacy story.**
A tag is not a property of the meeting; it is one person's remark about a
conversation other people were also in. `(session_id, discord_user_id,
tag)` means two participants can label the same session differently
without overwriting each other, and every read is scoped by
`discord_user_id` so nobody ever sees anybody else's labels.

The alternative -- tags shared across a session's participants -- was
considered and not built. It is a new channel through which participants
publish opinions about each other's meetings, it needs deletion and
moderation rules that private tags do not, and it cannot be taken back:
private tags can be made shared later by a decision, but tags people have
already read cannot be made private again.

`ON DELETE CASCADE` on the session, like `session_participant` has: when a
session is deleted, one person's label on it names nothing.

The second index is what makes "which tags do I use" a lookup rather than
a scan of every tag anybody ever wrote -- and it puts the owner first, so
that even the index cannot be walked usefully without naming whose tags
are being asked for.

No timestamp beyond `created_at`. A tag has no content to edit: the write
path replaces a session's whole set for one owner, so an "updated_at"
would only ever record when somebody added a different label.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "session_tag",
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "discord_user_id", "tag"),
    )
    op.create_index("ix_session_tag_owner", "session_tag", ["discord_user_id", "tag"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_session_tag_owner", table_name="session_tag")
    op.drop_table("session_tag")
