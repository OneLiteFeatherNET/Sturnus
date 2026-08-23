"""what a consent covers, before anything can do the wider thing

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23 00:00:00.000000

One column on `consent`: `scope`, text, NOT NULL, default `audio`.

**Why a column for something nothing does yet.** Sturnus does not record
video. It detects it -- `infrastructure/discord/video_probe.py` measures
whether Discord will send a bot the streams it announces, and
`sink.py` counts the packets and drops them without decoding a byte. This
migration does not change that and the change it belongs to does not
propose it.

It goes in first on purpose. A system must be able to record that
somebody said no before it acquires the ability to do the thing they said
no to; built the other way round there is a window in which the only
answer the schema can hold is yes, and every grant taken during that
window has to be taken again. The column costs one `ALTER TABLE` now and
removes that window entirely.

**Why `audio` rather than nullable.** Every row already in this table was
written by `/consent grant` under a policy document describing audio
recording, and `audio` is exactly what those people consented to. That is
not a guess being backfilled -- it is the fact restated in a column that
can now hold something else. A nullable column would mean "we do not know
what they agreed to", which is untrue and would push a `None` check into
every reader for a state that does not exist.

The default stays on the column rather than being dropped after the
backfill. `consent` is an append-only history and the writers that insert
into it are `/consent grant` and the console; a writer that forgets the
scope should produce the narrow one, not fail. Reading a missing or
unrecognised value as `audio` is the same rule stated in code
(`sturnus.domain.consent.scope_of`), and the two agree deliberately:
whatever goes wrong, it goes wrong towards recording less.

No index. `scope` is never selected on -- it is read alongside the row it
belongs to, which `ix_consent_user_guild` already finds.

**What `downgrade` costs, stated rather than implied.** Dropping the
column drops the distinction: a guild that had turned
`video_consent_offered` on and had people consenting to video would come
back up with every one of those grants reading as audio-only. That is the
safe direction -- nothing is recorded that was not consented to -- but it
is data loss, and re-upgrading does not restore it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "consent",
        sa.Column("scope", sa.Text(), nullable=False, server_default="audio"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("consent", "scope")
