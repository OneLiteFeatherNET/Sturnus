"""several destinations, several tenants, and the names nobody stored

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-23 00:00:00.000000

One migration for a whole round of work, and that is the decision this
file exists to record. The previous round produced four branches that
each invented `0011`, discovered it at merge time, and renumbered by
hand -- a class of conflict that costs an afternoon and can silently
produce a history in which one deployment ran a revision another never
did. So every table and every column this round needs is here, including
those for features that land in later pull requests and read nothing
today. Six branches can then build in parallel on a schema that already
exists rather than racing to define it.

Nothing reads most of what follows yet. That is the intended state, not
an oversight.

**Names, which is the oldest complaint here.** `0011` mirrored a guild's
channels, roles and people so that `api` -- which has no Discord token
and must never be given one (Spec 13.2) -- could show words instead of
snowflakes. It did not mirror the guild itself. So `GET /api/guilds`
answers with ids alone and every guild switcher in the console renders
"Server 1289374650912837465", on every admin page, which makes the one
name that appears everywhere the one name nobody could resolve. `guild`
closes that, written by the same sweep on the same tick as the other
three.

**Where a guild publishes.** `guild_export_target` is a table and not a
set of `guild_config` keys, and the reason is not that a flat registry
would be untidy. The settings API renders every value in `guild_config`
straight back to whichever administrator asks for it -- that is what the
settings page *is* -- so a Confluence token stored there would be a token
the API hands out on request. A destination also has structure: a base
URL, a space key, a credential. The credential is wrapped by
`KeyWrapper`, alongside the `encryption_key_id` that names the master key
which wrapped it, so rotation works exactly as it already does for audio
data keys.

That wrap is bound to the guild and to the purpose
(`sturnus.infrastructure.crypto.secret_context`). Unbound, a wrapped blob
is portable: it is sealed under the master key and says nothing about
which row it came out of, so anybody able to write a row but not decrypt
one -- a restored backup, a support script, a bulk import with a bug --
could move one guild's token into another guild's target and have it
publish under a credential that guild was never given. Binding turns that
from a silent success into an authentication failure. It defends against
relocation, not against somebody holding the master key; nothing in a
process that holds the master key could.

**Documents, plural.** A guild may enable several destinations, and
publishing writes to each -- one failing destination must not lose the
others. `session.document_url` cannot hold that, so `session_document`
records one row per destination. `session.document_url` **stays** as the
primary: the announcement posts it and everything already reading a
session reads it, and this migration removes nothing. `session_document`
keeps what a removed destination published (`ON DELETE SET NULL`, not
`CASCADE`) because deleting a target means "stop publishing here", not
"forget what was published" -- the document still exists in the other
system and the URL is what somebody follows a quarter later.

**Tenants.** `guild_oauth_client` gives a guild its own console sign-in
client, addressed by a slug in the URL because `/api/auth/login` takes no
parameters and reads no cookie: there is no session yet, that is what
login is for, so the guild cannot be inferred and has to be carried.
`console_state.guild_id` is what carries it across the round trip, and
the state is then what selects the client for the code exchange. Both are
nullable, and that is the compatibility promise: a sign-in with no guild
stays supported, and a deployment that never configures a per-guild
client behaves identically to v0.15.0.

**Onboarding, in the one direction that is safe.** Every step of setting
up a guild needs a Discord token and `api` must never hold one. The bot
already mirrors Discord state in for `api` to read; `guild_setup_intent`
is that arrangement run backwards -- `api` writes down what should be
true, and the bot's existing ten-second reconcile tick makes it true and
writes back what happened. `applied_at` with `outcome` and `error` is
what makes an intent settle exactly once: a tick running six times a
minute forever would otherwise re-create a consent role for the life of
the guild, or retry a permission error against Discord's rate limiter
just as often.

**The job columns, and what is nullable and why.** `sample_rate`,
`channels` and `stored_bytes` are re-read out of the object store on
every request today -- `sturnus.console.spectrogram.parse_track_format`
walks the RIFF header live, which costs a ranged GET and a chunk decrypt
to answer "how many channels" -- and the worker has the plaintext WAV on
disk at the moment it could simply write them down. All three are
nullable with no backfill, exactly as `audio_seconds` and its neighbours
were in `0007`: a row predating the column has audio that may already be
deleted, so there is nothing to read it from, and stamping a default
would turn an absence into a claim.

`spectrogram_key` points at a stored spectrogram, and it carries a rule
this migration does not implement. **A stored spectrogram is deleted when
its audio is deleted.** The retention sweep deletes the S3 object and
nothing else, so without that rule enabling spectrograms would create a
retained rendering of a person's voice activity that outlives the
retention window their audio was subject to -- a spectrogram is less than
the audio and it is not nothing, and it must not become the thing that
survives. The sweep is somebody else's pull request; the rule is written
here because the next person to touch retention needs to know it before
they read the sweep rather than after.

`priority` is **lower first**. Zero is normal and is what every existing
row gets from the server default, so raising a job above the ordinary run
is a negative number and holding one back is a positive one -- the sense
`nice(1)` uses, and the sense Delayed::Job and Que use for exactly this
table. The choice is not only taste: the claim will read
`ORDER BY priority, id`, and all-ascending is one forward scan of
`ix_job_claim_order (status, priority, id)`, which also keeps
first-in-first-out within a priority for free. `priority DESC, id ASC`
would have needed an index with a descending column to avoid a sort.
`JobQueue.claim` is unchanged here: the column and the index land first
so that the branch adding the ordering adds a query and not a schema.

**`session.title` and `session.description` are searchable and are not
indexed, deliberately.** The search the console will run over them is an
`ILIKE '%…%'` over free text, which no btree can answer -- a btree on
`title` would sit there costing writes and serving nothing. What answers
it is a GIN trigram index, and that needs `CREATE EXTENSION pg_trgm`: a
privileged statement, in a migration the worker runs in-process at
startup, on a deployment whose database role may not be permitted to
create extensions. Putting it here would mean a deployment that cannot
start because of an index nothing yet queries. It belongs to the branch
that writes the search, where failing to create it is a feature that will
not switch on rather than a system that will not come up. Until then the
search is narrowed by guild and by participant before it looks at any
text at all.

`downgrade` is real and reverses all of it. Columns come off in the
opposite order to the one they went on, and the new tables drop before
`guild_export_target`, which `session_document` references.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "guild",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("icon_url", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("guild_id"),
    )
    op.create_table(
        "guild_export_target",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("wrapped_secret", sa.LargeBinary(), nullable=True),
        sa.Column("encryption_key_id", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "name", name="uq_export_target_name"),
    )
    op.create_table(
        "session_document",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["session.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_id"], ["guild_export_target.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "target_id", name="uq_document_per_target"),
    )
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
    op.create_table(
        "guild_setup_intent",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel_ids", sa.Text(), nullable=True),
        sa.Column("consent_role_name", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_setup_intent_guild", "guild_setup_intent", ["guild_id", "requested_at"]
    )

    op.add_column("console_state", sa.Column("guild_id", sa.BigInteger(), nullable=True))

    op.add_column("session", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("session", sa.Column("description", sa.Text(), nullable=True))

    # `server_default` rather than a backfill: the table has rows, and
    # `NOT NULL` on an added column needs a value for every one of them.
    # Zero is the ordinary priority rather than a stand-in for one, so
    # unlike `audio_seconds` this default is a fact and not a claim.
    op.add_column(
        "transcription_job",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("transcription_job", sa.Column("sample_rate", sa.Integer(), nullable=True))
    op.add_column("transcription_job", sa.Column("channels", sa.Integer(), nullable=True))
    op.add_column("transcription_job", sa.Column("stored_bytes", sa.BigInteger(), nullable=True))
    op.add_column("transcription_job", sa.Column("spectrogram_key", sa.Text(), nullable=True))
    op.create_index(
        "ix_job_claim_order", "transcription_job", ["status", "priority", "id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_job_claim_order", table_name="transcription_job")
    op.drop_column("transcription_job", "spectrogram_key")
    op.drop_column("transcription_job", "stored_bytes")
    op.drop_column("transcription_job", "channels")
    op.drop_column("transcription_job", "sample_rate")
    op.drop_column("transcription_job", "priority")

    op.drop_column("session", "description")
    op.drop_column("session", "title")

    op.drop_column("console_state", "guild_id")

    op.drop_index("ix_setup_intent_guild", table_name="guild_setup_intent")
    op.drop_table("guild_setup_intent")
    op.drop_table("guild_oauth_client")
    # Before `guild_export_target`, which it references.
    op.drop_table("session_document")
    op.drop_table("guild_export_target")
    op.drop_table("guild")
