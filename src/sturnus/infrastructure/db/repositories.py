"""Repositories. Everything the bot reads and writes, in one place.

Tested against real PostgreSQL through Testcontainers. There are
deliberately no repository interfaces — an interface with one
implementation behind a real database test is ceremony.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.assembly import deserialize_transcript
from sturnus.application.publishing import DOCUMENTED_STATUS
from sturnus.application.transcription import TranscriptionResult
from sturnus.domain.consent import ConsentRecord
from sturnus.infrastructure.db.models import (
    AccountLink,
    Consent,
    Session,
    SessionParticipant,
    TranscriptionJob,
)

log = logging.getLogger(__name__)


class ConsentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_grant(
        self,
        discord_user_id: int,
        guild_id: int,
        policy_version: str,
        source: str,
        now: datetime,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                Consent(
                    discord_user_id=discord_user_id,
                    guild_id=guild_id,
                    granted_at=now,
                    revoked_at=None,
                    policy_version=policy_version,
                    source=source,
                )
            )
            await session.commit()

    async def record_revocation(self, discord_user_id: int, guild_id: int, now: datetime) -> None:
        """Sets `revoked_at` on the newest row rather than inserting a new one.

        The history keeps grants; a revocation modifies the grant it revokes.
        """
        async with self._session_factory() as session:
            newest_id = await session.scalar(
                select(Consent.id)
                .where(Consent.discord_user_id == discord_user_id, Consent.guild_id == guild_id)
                .order_by(Consent.granted_at.desc())
                .limit(1)
            )
            if newest_id is not None:
                await session.execute(
                    update(Consent).where(Consent.id == newest_id).values(revoked_at=now)
                )
            await session.commit()

    async def current(self, discord_user_id: int, guild_id: int) -> ConsentRecord | None:
        """Returns the newest grant for this user and guild.

        Consent history is kept permanently, so several rows exist; someone
        who revoked and later consented again must read as consenting. This
        selection rule lives here so no caller has to invent it.
        """
        async with self._session_factory() as session:
            row = await session.scalar(
                select(Consent)
                .where(Consent.discord_user_id == discord_user_id, Consent.guild_id == guild_id)
                .order_by(Consent.granted_at.desc())
                .limit(1)
            )
        if row is None:
            return None
        return ConsentRecord(
            granted_at=row.granted_at,
            revoked_at=row.revoked_at,
            policy_version=row.policy_version,
        )


class SessionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def open_session(
        self, guild_id: int, channel_id: int, channel_name: str | None, now: datetime
    ) -> int:
        async with self._session_factory() as session:
            record = Session(
                guild_id=guild_id,
                channel_id=channel_id,
                channel_name=channel_name,
                started_at=now,
                ended_at=None,
                end_reason=None,
                status="open",
                document_provider=None,
                document_id=None,
                document_url=None,
                announced_at=None,
            )
            session.add(record)
            await session.commit()
            return record.id

    async def add_participant(
        self,
        session_id: int,
        discord_user_id: int,
        discord_display_name: str,
        now: datetime,
    ) -> None:
        """Idempotent per (session_id, discord_user_id); keeps the first display name."""
        async with self._session_factory() as session:
            statement = insert(SessionParticipant).values(
                session_id=session_id,
                discord_user_id=discord_user_id,
                discord_display_name=discord_display_name,
                detected_language=None,
                first_seen_at=now,
                audio_started_at=None,
            )
            await session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=["session_id", "discord_user_id"],
                )
            )
            await session.commit()

    async def set_audio_epoch(self, session_id: int, discord_user_id: int, now: datetime) -> None:
        """Writes only while `audio_started_at` is still null.

        The epoch marks the first packet; a later packet must not move it.
        """
        async with self._session_factory() as session:
            await session.execute(
                update(SessionParticipant)
                .where(
                    SessionParticipant.session_id == session_id,
                    SessionParticipant.discord_user_id == discord_user_id,
                    SessionParticipant.audio_started_at.is_(None),
                )
                .values(audio_started_at=now)
            )
            await session.commit()

    async def audio_epoch(self, session_id: int, discord_user_id: int) -> datetime | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(SessionParticipant.audio_started_at).where(
                    SessionParticipant.session_id == session_id,
                    SessionParticipant.discord_user_id == discord_user_id,
                )
            )

    async def participant_names(self, session_id: int) -> dict[int, str]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    SessionParticipant.discord_user_id,
                    SessionParticipant.discord_display_name,
                ).where(SessionParticipant.session_id == session_id)
            )
            return {discord_user_id: name for discord_user_id, name in rows}

    async def close_session(self, session_id: int, now: datetime, end_reason: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(ended_at=now, end_reason=end_reason, status="closed")
            )
            await session.commit()

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None:
        """Persists the session's data key onto its row.

        The session row is the source of truth for which key encrypted this
        session's recordings -- crash recovery reads it back from here
        rather than ever generating a fresh key that could not decrypt
        files the original key already produced.
        """
        async with self._session_factory() as session:
            await session.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(encryption_key_id=encryption_key_id, wrapped_data_key=wrapped_data_key)
            )
            await session.commit()

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:
        """Returns `(encryption_key_id, wrapped_data_key)` for a session, or `None`.

        `None` covers a session that predates this column and one that
        crashed before `record_session_key` ever ran alike -- either way,
        there is no key here to recover with.
        """
        async with self._session_factory() as session:
            row = await session.execute(
                select(Session.encryption_key_id, Session.wrapped_data_key).where(
                    Session.id == session_id
                )
            )
            result = row.first()
        if result is None or result[0] is None or result[1] is None:
            return None
        return (result[0], result[1])

    async def session_status(self, session_id: int) -> str | None:
        """Returns the row's `status`, or `None` if no such session exists."""
        async with self._session_factory() as session:
            status: str | None = await session.scalar(
                select(Session.status).where(Session.id == session_id)
            )
            return status

    async def guild_id(self, session_id: int) -> int:
        """Returns the guild a session belongs to.

        Lets a caller resolve per-guild configuration (Spec 11's
        `document_target`, `document_provider`, `merge_gap_seconds`) once a
        session is in hand -- the worker serves every guild from one
        process, so none of those can be read until this is known.
        """
        async with self._session_factory() as session:
            value = await session.scalar(select(Session.guild_id).where(Session.id == session_id))
        if value is None:
            raise ValueError(f"session {session_id} does not exist")
        return value

    async def find_open_session(self, guild_id: int) -> int | None:
        """Returns the id of the guild's session whose status is not `closed`, or None."""
        async with self._session_factory() as session:
            session_id: int | None = await session.scalar(
                select(Session.id).where(Session.guild_id == guild_id, Session.status != "closed")
            )
            return session_id

    async def session_bounds(self, session_id: int) -> tuple[datetime, datetime]:
        """Returns `(started_at, ended_at)` for a session that has closed.

        Raises if the session is still open: an ongoing session has no end
        yet, and inventing one (e.g. "now") would misrepresent how long it
        actually ran in the finished protocol.
        """
        async with self._session_factory() as session:
            row = await session.execute(
                select(Session.started_at, Session.ended_at).where(Session.id == session_id)
            )
            result = row.first()
        if result is None:
            raise ValueError(f"session {session_id} does not exist")
        started_at, ended_at = result
        if ended_at is None:
            raise ValueError(f"session {session_id} is still open")
        return started_at, ended_at

    async def candidates_for_announcement(self) -> list[dict[str, object]]:
        """Every `documented` session, shaped for
        `sturnus.application.publishing.sessions_to_announce`.

        Filters only by `status`; `announced_at`/`document_url` are left
        for that pure function to check, so there is exactly one
        definition of the selection rule.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    Session.id,
                    Session.channel_id,
                    Session.status,
                    Session.document_url,
                    Session.announced_at,
                ).where(Session.status == DOCUMENTED_STATUS)
            )
            return [
                {
                    "id": row.id,
                    "channel_id": row.channel_id,
                    "status": row.status,
                    "document_url": row.document_url,
                    "announced_at": row.announced_at,
                }
                for row in rows
            ]

    async def mark_announced(self, session_id: int, now: datetime) -> None:
        """Stamps `announced_at`, but only on the session that was announced.

        A compare-and-set, not a plain write, and the condition is the
        whole point. `sturnus.application.publishing.
        announce_ready_sessions` selects a `documented` session whose
        `announced_at` is null, awaits `announcer.post` -- a Discord HTTP
        call that takes seconds under rate limiting -- and calls this only
        afterwards. `/queue requeue` can land inside that window: it puts
        the session back to `closed` and clears `announced_at` precisely
        so the redo's new link will be posted. An unconditional stamp
        arriving late would put a timestamp back on a session that has not
        been announced since, and `sessions_to_announce` selects only
        sessions whose `announced_at` is null -- so the corrected
        transcript would be documented and then never posted, with nothing
        logged and nothing raised. That is the exact failure clearing the
        column exists to prevent, reintroduced by the sweep that was
        racing it.

        Restricting the UPDATE to the state the selection was made on --
        still `documented`, still unannounced -- makes the stamp land only
        if the session is still the one the post was about. When it is
        not, no row matches, the re-queued session keeps its null
        `announced_at`, and the sweep after the redo announces the new
        link. The cost is one duplicate post of the superseded link, which
        is the side `announce_ready_sessions` already documents itself as
        erring towards: losing an announcement entirely is the worse half
        of that trade.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                update(Session)
                .where(
                    Session.id == session_id,
                    Session.status == DOCUMENTED_STATUS,
                    Session.announced_at.is_(None),
                )
                .values(announced_at=now)
            )
            await session.commit()
        # Same narrowing `AccountLinkRepository.delete` uses: `execute` on
        # a Core UPDATE always yields a `CursorResult` at runtime, and the
        # assertion makes `.rowcount` available without an unchecked cast.
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            # Not an error: the announcement went out, and the session it
            # went out for no longer exists in that form. Worth a line
            # anyway -- it is the only trace connecting a link posted in a
            # channel to a session row that does not say it was announced.
            log.info(
                "Session %d changed while its announcement was being posted; "
                "announced_at left unset so the next sweep can announce it again",
                session_id,
            )

    async def closed_undocumented_sessions(self) -> list[int]:
        """Closed sessions whose jobs are all terminal but which never got documented.

        Used by `sturnus.application.worker.retry_pending_documents` to
        retry document creation independently of any one job -- see that
        function's docstring for why a session can end up here at all. A
        session with no jobs at all (nobody ever spoke) is excluded: there
        is nothing to assemble.
        """
        async with self._session_factory() as session:
            has_jobs = select(TranscriptionJob.session_id).distinct()
            unfinished = select(TranscriptionJob.session_id).where(
                TranscriptionJob.status.not_in(("done", "dead"))
            )
            rows = await session.execute(
                select(Session.id).where(
                    Session.status == "closed",
                    Session.id.in_(has_jobs),
                    Session.id.not_in(unfinished),
                )
            )
            return [row[0] for row in rows]


class JobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        session_id: int,
        discord_user_id: int,
        s3_key: str,
        encryption_key_id: str,
        wrapped_data_key: bytes,
        retention_until: datetime,
    ) -> int:
        async with self._session_factory() as session:
            job = TranscriptionJob(
                session_id=session_id,
                discord_user_id=discord_user_id,
                s3_key=s3_key,
                encryption_key_id=encryption_key_id,
                wrapped_data_key=wrapped_data_key,
                retention_until=retention_until,
                audio_deleted_at=None,
                status="pending",
                attempts=0,
                error=None,
                transcript=None,
            )
            session.add(job)
            await session.commit()
            return job.id

    async def candidates_for_retention(self) -> list[dict[str, object]]:
        """Every job not yet marked `audio_deleted_at`, shaped for
        `sturnus.application.retention.expired_jobs`.

        Filters only by `audio_deleted_at`; the `retention_until` boundary
        is left for that pure function to check, so there is exactly one
        definition of it.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    TranscriptionJob.id,
                    TranscriptionJob.s3_key,
                    TranscriptionJob.retention_until,
                    TranscriptionJob.audio_deleted_at,
                ).where(TranscriptionJob.audio_deleted_at.is_(None))
            )
            return [
                {
                    "id": row.id,
                    "s3_key": row.s3_key,
                    "retention_until": row.retention_until,
                    "audio_deleted_at": row.audio_deleted_at,
                }
                for row in rows
            ]

    async def mark_audio_deleted(self, job_id: int, now: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(TranscriptionJob)
                .where(TranscriptionJob.id == job_id)
                .values(audio_deleted_at=now)
            )
            await session.commit()

    async def transcripts_for(self, session_id: int) -> dict[int, TranscriptionResult]:
        """Each speaker's stored transcript for a session, keyed by `discord_user_id`.

        Only `done` jobs are read: a `dead` job (one that exhausted its
        retries, see `JobQueue.fail`) or one still `pending`/`running` has
        no transcript to read, and skipping it must not stop the session's
        other speakers from appearing in the document.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(TranscriptionJob.discord_user_id, TranscriptionJob.transcript).where(
                    TranscriptionJob.session_id == session_id,
                    TranscriptionJob.status == "done",
                )
            )
            return {
                discord_user_id: deserialize_transcript(transcript)
                for discord_user_id, transcript in rows
                if transcript is not None
            }


class AccountLinkRepository:
    """Reads and writes `account_link`: the mapping between a Discord user and an
    external (e.g. Outline) account.

    Two call shapes coexist here because the read side and the write side
    are used by different callers with different needs:

    - **Read** (`external_identity`): the bot fixes `provider` at
      construction (`AccountLinkRepository(session_factory, provider=
      "outline")`) because it only ever reads back the one Outline mapping
      it links against (`/link status`); a later Confluence adapter used
      the same way would construct its own `AccountLinkRepository(
      session_factory, provider="confluence")`. The worker instead reads
      *every* guild's protocol from one process, and which provider's
      mapping applies is itself per-guild configuration (Spec 11's
      `document_provider`) unknowable at construction time -- so
      `external_identity` also accepts `provider` per call, overriding
      whatever (if anything) was fixed at construction. Exactly one of the
      two must be supplied; a caller that always passes it per call is free
      to construct without one, the same way write-only callers already do.
    - **Write** (`save`/`delete`): the link service's OAuth callback (Spec
      8.4) and the `/link remove` command take `provider` per call instead,
      because the caller only learns which provider a link is for from the
      consumed `PendingLink`/command argument, not from how this repository
      was wired up. `provider` is therefore optional at construction --
      callers that only ever write never need to pass it.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], provider: str | None = None
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    async def external_identity(
        self, discord_user_id: int, provider: str | None = None
    ) -> tuple[str, str] | None:
        """Returns `(external_user_id, display_name)` for `provider`, or `None`.

        `provider` given here wins over whatever was fixed at construction
        (see the class docstring's "Read" case); at least one of the two
        must be available, or this is a caller bug.
        """
        resolved_provider = provider if provider is not None else self._provider
        assert resolved_provider is not None, (
            "external_identity() requires a provider, either passed per call or fixed "
            "at AccountLinkRepository construction"
        )
        async with self._session_factory() as session:
            row = await session.execute(
                select(AccountLink.external_user_id, AccountLink.display_name).where(
                    AccountLink.discord_user_id == discord_user_id,
                    AccountLink.provider == resolved_provider,
                )
            )
            result = row.first()
        if result is None:
            return None
        return (result[0], result[1])

    async def save(
        self, discord_user_id: int, provider: str, external_user_id: str, display_name: str
    ) -> None:
        """Upserts the mapping for `(discord_user_id, provider)`.

        Someone re-linking after changing their external account would
        otherwise hit a primary-key violation on the second attempt --
        "link my account again" reads as replace, not fail, so this writes
        `INSERT ... ON CONFLICT DO UPDATE` rather than a plain insert.
        """
        async with self._session_factory() as session:
            statement = insert(AccountLink).values(
                discord_user_id=discord_user_id,
                provider=provider,
                external_user_id=external_user_id,
                display_name=display_name,
                linked_at=datetime.now(UTC),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["discord_user_id", "provider"],
                    set_={
                        "external_user_id": statement.excluded.external_user_id,
                        "display_name": statement.excluded.display_name,
                        "linked_at": statement.excluded.linked_at,
                    },
                )
            )
            await session.commit()

    async def delete(self, discord_user_id: int, provider: str) -> bool:
        """Deletes the mapping for `(discord_user_id, provider)`.

        Returns whether a row actually existed to remove, which `/link
        remove` reports back to the user rather than claiming success
        unconditionally.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                delete(AccountLink).where(
                    AccountLink.discord_user_id == discord_user_id,
                    AccountLink.provider == provider,
                )
            )
            await session.commit()
        # `execute` on a Core DELETE always yields a `CursorResult` at
        # runtime; the assertion narrows the statically-typed `Result[Any]`
        # so `.rowcount` is available without an unchecked cast.
        assert isinstance(result, CursorResult)
        return result.rowcount > 0
