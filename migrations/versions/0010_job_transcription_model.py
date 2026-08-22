"""which model a job asked for, and which one produced it

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-22 00:00:00.000000

Two columns on `transcription_job`, both nullable, so that the same
recording can be transcribed twice and the results compared.

**Why this is worth a migration at all.** Every model comparison this
project has made was made against recordings that turned out to be noise:
Discord encrypts voice end-to-end and the receiving half of that was never
implemented, so what reached the decoder was ciphertext (see
`sturnus.infrastructure.discord.dave`). "Neither `tiny` nor `large-v3`
could transcribe a word of it" said nothing about either model. With
speech finally arriving, the question "is a smaller model good enough for
this deployment's audio, in its language" can be asked for the first time
-- and it can only be answered by running one recording through two
engines and putting the results side by side.

**Why two columns rather than one.** `requested_model` is an instruction
and `model` is an observation, and they part company exactly when it
matters. A job re-queued with a model that fails to load never produces
the second, and a job nobody asked a question about has the first as null
while the second names the worker's default. One column would be
ambiguous in precisely the case a comparison has to be certain about:
which engine produced which side.

**Both nullable, and no backfill.** Rows written before this cannot say
what produced them. Stamping them with today's default would turn an
absence into a claim -- and a comparison drawn against a claim is worse
than one drawn against a gap, because the gap is visible.

No index: neither column is queried, both are read alongside the row they
belong to.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transcription_job", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("transcription_job", sa.Column("requested_model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("transcription_job", "requested_model")
    op.drop_column("transcription_job", "model")
