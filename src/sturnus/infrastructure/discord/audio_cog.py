"""Erasure commands (Spec 12.3): Art. 17 GDPR requests, served immediately.

`/audio delete` erases the caller's own recordings across every session;
`/audio purge <user>` does the same for a named user and is admin-only, so
an erasure request raised through any channel -- not only by the data
subject typing the command themselves -- can still be served. Both bypass
`retention_until` entirely: retention exists to keep a poor transcription
redoable for a while (Spec 12.2), not to delay a subject's own erasure
request. Both reply ephemerally with a count, and both say plainly that
existing transcripts are untouched -- a transcript is a separate processing
result that lives in the document system, not in the audio store this cog
erases from.

Deletion is recorded the same way the periodic retention sweep
(`sturnus.application.retention`) records it: the S3 object is removed and
`audio_deleted_at` is stamped on its job row, because that stamp is the
evidence the deletion happened, not the S3 call alone. There is no
dedicated repository method for this selection -- it is specific to
erasure, used nowhere else -- so this module reads and writes
`transcription_job` directly through the ORM, the project's one data access
path.
"""

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.ports import AudioStore, Clock
from sturnus.infrastructure.db.models import TranscriptionJob
from sturnus.infrastructure.discord.permissions import require_admin

log = logging.getLogger(__name__)

#: Shown after both commands -- neither one touches the document system,
#: and a caller must not be left assuming a transcript vanished along with
#: the audio it was made from.
_TRANSCRIPTS_UNTOUCHED_NOTICE = (
    "Existing transcripts are unaffected: they are a separate processing "
    "result stored in the document system, not in the audio deleted here."
)


async def _erase_audio(
    session_factory: async_sessionmaker[AsyncSession],
    audio_store: AudioStore,
    discord_user_id: int,
    now: datetime,
) -> int:
    """Deletes every not-yet-deleted recording belonging to one user, immediately.

    Ignores `retention_until` entirely -- this is an erasure request, not a
    routine sweep. Each S3 object is removed before its row is stamped, so
    a crash partway through leaves the affected job looking untouched
    rather than claiming an erasure that never actually reached S3; a rerun
    of this same erasure would simply pick it up again.

    Returns the number of recordings actually removed.
    """
    async with session_factory() as session:
        rows = await session.execute(
            select(TranscriptionJob.id, TranscriptionJob.s3_key).where(
                TranscriptionJob.discord_user_id == discord_user_id,
                TranscriptionJob.audio_deleted_at.is_(None),
            )
        )
        candidates = rows.all()
        deleted = 0
        for job_id, s3_key in candidates:
            await audio_store.delete(s3_key)
            await session.execute(
                update(TranscriptionJob)
                .where(TranscriptionJob.id == job_id)
                .values(audio_deleted_at=now)
            )
            deleted += 1
        await session.commit()
    return deleted


@app_commands.guild_only()
class AudioCog(commands.GroupCog, name="audio", description="Erase recorded audio (Spec 12.3)."):
    """`/audio` command group: immediate, retention-ignoring erasure."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audio_store: AudioStore,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._audio_store = audio_store
        self._clock = clock
        super().__init__()

    @app_commands.command(
        name="delete", description="Delete all of your recorded audio, immediately."
    )
    async def delete(self, interaction: discord.Interaction) -> None:
        count = await _erase_audio(
            self._session_factory, self._audio_store, interaction.user.id, self._clock.now()
        )
        log.info("Erased %d recording(s) for user %d via /audio delete", count, interaction.user.id)
        await interaction.response.send_message(
            f"Deleted {count} recording(s) of your audio. {_TRANSCRIPTS_UNTOUCHED_NOTICE}",
            ephemeral=True,
        )

    @app_commands.command(
        name="purge", description="Delete all recorded audio for a named user, immediately."
    )
    @app_commands.describe(user="The user whose recordings should be erased")
    @require_admin()
    async def purge(self, interaction: discord.Interaction, user: discord.User) -> None:
        count = await _erase_audio(
            self._session_factory, self._audio_store, user.id, self._clock.now()
        )
        log.info(
            "Erased %d recording(s) for user %d via /audio purge (requested by %d)",
            count,
            user.id,
            interaction.user.id,
        )
        await interaction.response.send_message(
            f"Deleted {count} recording(s) for {user.mention}. {_TRANSCRIPTS_UNTOUCHED_NOTICE}",
            ephemeral=True,
        )
