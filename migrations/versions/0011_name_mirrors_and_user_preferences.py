"""the names behind the ids, and what a person sets about themselves

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-23 00:00:00.000000

Five tables. Four of them are mirrors -- copies of names that live
somewhere else, written by the process that holds the credential for that
somewhere -- and the fifth is a person's own settings.

**Why mirrors at all.** The console makes an administrator paste a raw
Discord snowflake into `voice_channel_id`, `consent_role_id` and
`admin_role_id`, and an Outline collection UUID into `document_target`,
and then shows those ids back at them as ids. It cannot do better,
because `api` has no Discord token and no Outline token: the console
design's Section 2.1 gives `api` S3 and the master key, which already
means it can decrypt every recording ever made, and a process in that
position is not one to also hand the ability to act as the bot (Spec
13.2). So `api` cannot ask Discord what channel `1234...` is.

The way out is the one this repository already took for `admin_member`
(the console design's Section 3.4): **the process that already holds the
credential writes the names down, and `api` reads them.** `bot` has the
gateway, so `bot` writes `guild_channel`, `guild_role` and
`guild_member`. `worker` has the Outline token, so `worker` writes
`outline_collection`. Nothing here gives any process a credential it did
not already have.

Every one of the four carries `synced_at`, which is the honest part of
the arrangement: a mirror is stale by construction, bounded by the sweep
that fills it, and a row that cannot say when it was written cannot be
told apart from one that is current.

**Why `guild_member` is deliberately not every member of the guild.**
It holds exactly the people who hold the consent role or the admin role,
and nobody else. Mirroring an entire member list would be copying a
Discord user directory into a database that exists to hold recordings of
meetings -- and it would be doing that for people who never joined a
recorded channel, never consented to anything, and are in the guild for
reasons that have nothing to do with Sturnus. Nobody asked for that
directory, no page in the console displays it, and the smallest set that
answers every question the console actually asks is the two role
memberships: a consent roster, the speakers in a queue, and an
administrator list are all drawn from those. A name this table does not
hold is a name the console does not need, and the correct behaviour there
is to show the id.

**`kind` is a plain string, not an enum.** Discord adds channel types --
stage channels, forums, whatever comes next -- and a database enum turns
"a type this code has never seen" into a failed write in the middle of a
sweep, taking the channels this code does understand down with it. A
string cannot fail that way. The values written today are `voice` and
`text`; a reader that does not recognise one must ignore it rather than
refuse the row.

**`user_preference` is shaped exactly like `guild_config`, on purpose.**
Same key/value pair, same nullable value, same `updated_at`, keyed by
person instead of guild. The reasons that shape was right there are the
reasons it is right here: a preference nobody has expressed is an absent
row rather than a column full of nulls, adding a second preference is a
write rather than a migration, and the registry of what may be stored
lives in `sturnus.domain.preferences` where both the writer and the
reader can see it. There is no foreign key to anything: a preference
belongs to a Discord identity, and this system has no table of Discord
identities -- `account_link` is about Outline accounts and
`session_participant` is about meetings somebody attended.

No indexes beyond the primary keys. Every read of every one of these
tables names its whole key or its whole guild, which the primary key
already answers, and the row counts are small: a guild's channels, a
guild's roles, two role memberships, one Outline instance's collections.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_preference",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("discord_user_id", "key"),
    )
    op.create_table(
        "guild_channel",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "channel_id"),
    )
    op.create_table(
        "guild_role",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "role_id"),
    )
    op.create_table(
        "guild_member",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "discord_user_id"),
    )
    op.create_table(
        "outline_collection",
        sa.Column("collection_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("collection_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("outline_collection")
    op.drop_table("guild_member")
    op.drop_table("guild_role")
    op.drop_table("guild_channel")
    op.drop_table("user_preference")
