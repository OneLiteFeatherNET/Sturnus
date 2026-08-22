from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.assembly import serialize_transcript
from sturnus.application.transcription import TranscribedSegment, TranscriptionResult
from sturnus.entrypoints.worker import _WorkerSessionStore
from sturnus.infrastructure.db.models import AccountLink, Base, Session, SessionParticipant
from sturnus.infrastructure.db.queue import JobQueue
from sturnus.infrastructure.db.repositories import (
    AccountLinkRepository,
    ConsentRepository,
    JobRepository,
    SessionRepository,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200
POLICY = "2026-08-01"


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_consent_grant_is_readable(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0)
    record = await repo.current(ANNA, GUILD)
    assert record is not None
    assert record.granted_at == T0
    assert record.revoked_at is None
    assert record.policy_version == POLICY


async def test_no_record_returns_none(factory: async_sessionmaker[AsyncSession]) -> None:
    assert await ConsentRepository(factory).current(BEN, GUILD) is None


async def test_revocation_is_visible_in_the_current_record(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0)
    await repo.record_revocation(ANNA, GUILD, T0 + timedelta(hours=1))
    record = await repo.current(ANNA, GUILD)
    assert record is not None
    assert record.revoked_at == T0 + timedelta(hours=1)


async def test_current_returns_the_newest_grant(factory: async_sessionmaker[AsyncSession]) -> None:
    """Consent history is kept permanently (Spec 12.4), so several rows exist.

    A user who revokes and later consents again must read as consenting; the
    repository, not the caller, owns that selection rule.
    """
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, "2026-01-01", "button", T0)
    await repo.record_revocation(ANNA, GUILD, T0 + timedelta(days=1))
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0 + timedelta(days=2))

    record = await repo.current(ANNA, GUILD)
    assert record is not None
    assert record.revoked_at is None
    assert record.policy_version == POLICY


async def test_guilds_do_not_share_consent(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0)
    assert await repo.current(ANNA, 999) is None


async def test_session_lifecycle(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    assert await repo.find_open_session(GUILD) == session_id

    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=3))
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")

    assert await repo.find_open_session(GUILD) is None


async def test_guild_id_returns_the_owning_guild(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lets the worker resolve per-guild configuration (Spec 11) for a
    session it only has the id of -- without this, `document_target`,
    `document_provider`, and `merge_gap_seconds` could never be looked up
    at document-creation time.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    assert await repo.guild_id(session_id) == GUILD


async def test_guild_id_raises_for_an_unknown_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SessionRepository(factory)
    with pytest.raises(ValueError, match="does not exist"):
        await repo.guild_id(999)


async def test_session_row_carries_the_key_after_opening(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session row is the source of truth crash recovery reads from.

    Without this, a process that dies between encrypting a recording and
    enqueueing the job for it leaves a key that only ever lived in memory
    -- unrecoverable.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    assert await repo.session_key(session_id) is None

    await repo.record_session_key(session_id, "k1", b"wrapped-bytes")

    assert await repo.session_key(session_id) == ("k1", b"wrapped-bytes")


async def test_session_key_is_none_for_a_session_without_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Covers both a session that predates the column and one that crashed early."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    assert await repo.session_key(session_id) is None


async def test_adding_a_participant_twice_is_harmless(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Someone may leave and rejoin within one session."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.add_participant(session_id, ANNA, "anna-renamed", T0 + timedelta(minutes=1))
    # The first display name wins: it is the one in force when recording began.
    names = await repo.participant_names(session_id)
    assert names == {ANNA: "anna"}


async def test_audio_epoch_is_written_once(factory: async_sessionmaker[AsyncSession]) -> None:
    """The epoch marks the first packet; a later packet must not move it."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=3))
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=9))
    assert await repo.audio_epoch(session_id, ANNA) == T0 + timedelta(seconds=3)


async def _silent_audio_at(
    factory: async_sessionmaker[AsyncSession], session_id: int, user_id: int
) -> datetime | None:
    """Reads the column back directly: nothing in the running system reads it.

    The bot writes it so that an operator investigating an empty transcript
    weeks later can tell "we could not hear them" from "they said nothing",
    and that reader is a person with a SQL prompt. Adding a repository
    method purely so this test could call one would be production code with
    no production caller.
    """
    async with factory() as session:
        return await session.scalar(
            select(SessionParticipant.silent_audio_detected_at).where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.discord_user_id == user_id,
            )
        )


async def test_silent_audio_is_recorded_on_the_participant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The durable half of the silent-audio warning (`sturnus.domain.silence`).

    The message posted into the channel is gone by the next meeting; this
    row is what is still there when somebody asks why a transcript was
    empty.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)

    await repo.record_silent_audio(session_id, ANNA, T0 + timedelta(seconds=30))

    assert await _silent_audio_at(factory, session_id, ANNA) == T0 + timedelta(seconds=30)


async def test_silent_audio_keeps_the_first_detection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """First detection wins, the same way `set_audio_epoch` does.

    The column answers "from when was this speaker's audio empty", and a
    later write would move that answer forward every time somebody looked
    -- turning the one fact worth keeping into the time of the most recent
    observation.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)

    await repo.record_silent_audio(session_id, ANNA, T0 + timedelta(seconds=30))
    await repo.record_silent_audio(session_id, ANNA, T0 + timedelta(minutes=10))

    assert await _silent_audio_at(factory, session_id, ANNA) == T0 + timedelta(seconds=30)


async def test_a_participant_with_audible_audio_has_no_silence_stamp(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Null is the normal case and must stay the default.

    Everyone in every meeting who was simply quiet shares this row shape;
    only a speaker whose audio actually arrived empty gets a timestamp, so
    a non-null value means something on its own.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)

    assert await _silent_audio_at(factory, session_id, ANNA) is None


async def test_job_enqueue(factory: async_sessionmaker[AsyncSession]) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await sessions.add_participant(session_id, ANNA, "anna", T0)

    job_id = await jobs.enqueue(
        session_id=session_id,
        discord_user_id=ANNA,
        s3_key="sessions/1/speakers/100.enc",
        encryption_key_id="k1",
        wrapped_data_key=b"wrapped",
        retention_until=T0 + timedelta(days=30),
    )
    assert job_id > 0


async def test_session_bounds_returns_start_and_end_of_a_closed_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    assert await repo.session_bounds(session_id) == (T0, T0 + timedelta(hours=1))


async def test_session_bounds_raises_while_the_session_is_still_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open session has no end yet; inventing one would misrepresent how long it ran."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    with pytest.raises(ValueError, match="open"):
        await repo.session_bounds(session_id)


async def _enqueue_job(
    sessions: SessionRepository,
    jobs: JobRepository,
    session_id: int,
    user_id: int,
) -> int:
    await sessions.add_participant(session_id, user_id, f"user{user_id}", T0)
    return await jobs.enqueue(
        session_id=session_id,
        discord_user_id=user_id,
        s3_key=f"sessions/{session_id}/speakers/{user_id}.enc",
        encryption_key_id="k1",
        wrapped_data_key=b"wrapped",
        retention_until=T0 + timedelta(days=30),
    )


async def test_transcripts_for_returns_each_speakers_stored_transcript(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    queue = JobQueue(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    anna_job = await _enqueue_job(sessions, jobs, session_id, ANNA)
    ben_job = await _enqueue_job(sessions, jobs, session_id, BEN)

    anna_result = TranscriptionResult(
        segments=(TranscribedSegment(0.0, 1.0, "hello"),), language="de"
    )
    ben_result = TranscriptionResult(segments=(TranscribedSegment(0.0, 1.0, "hi"),), language="en")
    await queue.complete(anna_job, serialize_transcript(anna_result))
    await queue.complete(ben_job, serialize_transcript(ben_result))

    transcripts = await jobs.transcripts_for(session_id)
    assert transcripts == {ANNA: anna_result, BEN: ben_result}


async def test_transcripts_for_skips_dead_and_unfinished_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A dead job must not stop the remaining speakers from appearing in the document."""
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    queue = JobQueue(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    anna_job = await _enqueue_job(sessions, jobs, session_id, ANNA)
    await _enqueue_job(sessions, jobs, session_id, BEN)  # left pending

    anna_result = TranscriptionResult(
        segments=(TranscribedSegment(0.0, 1.0, "hello"),), language="de"
    )
    await queue.complete(anna_job, serialize_transcript(anna_result))

    # A third speaker whose job died outright.
    dead_job = await _enqueue_job(sessions, jobs, session_id, 300)
    await queue.fail(dead_job, "boom", max_attempts=1)

    transcripts = await jobs.transcripts_for(session_id)
    assert transcripts == {ANNA: anna_result}


async def test_external_identity_returns_the_linked_account_for_the_configured_provider(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add(
            AccountLink(
                discord_user_id=ANNA,
                provider="outline",
                external_user_id="out-1",
                display_name="Anna Example",
                linked_at=T0,
            )
        )
        await session.commit()

    repo = AccountLinkRepository(factory, provider="outline")
    assert await repo.external_identity(ANNA) == ("out-1", "Anna Example")


async def test_external_identity_returns_none_for_an_unlinked_user(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = AccountLinkRepository(factory, provider="outline")
    assert await repo.external_identity(BEN) is None


async def test_external_identity_only_reads_its_own_provider(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A later Confluence adapter must read its own mapping, not Outline's."""
    async with factory() as session:
        session.add(
            AccountLink(
                discord_user_id=ANNA,
                provider="outline",
                external_user_id="out-1",
                display_name="Anna Example",
                linked_at=T0,
            )
        )
        await session.commit()

    repo = AccountLinkRepository(factory, provider="confluence")
    assert await repo.external_identity(ANNA) is None


async def test_external_identity_per_call_provider_overrides_construction(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The worker resolves `document_provider` per guild, at document-creation
    time, so `external_identity` must accept it per call rather than only
    the provider fixed at construction (Spec 11's `document_provider`) --
    one process serves every guild, and guilds may configure different
    providers.
    """
    async with factory() as session:
        session.add_all(
            [
                AccountLink(
                    discord_user_id=ANNA,
                    provider="outline",
                    external_user_id="out-1",
                    display_name="Anna Outline",
                    linked_at=T0,
                ),
                AccountLink(
                    discord_user_id=ANNA,
                    provider="confluence",
                    external_user_id="conf-1",
                    display_name="Anna Confluence",
                    linked_at=T0,
                ),
            ]
        )
        await session.commit()

    # Constructed with no fixed provider at all -- exactly how the worker
    # constructs it, since it cannot know any guild's provider up front.
    repo = AccountLinkRepository(factory)
    assert await repo.external_identity(ANNA, provider="outline") == ("out-1", "Anna Outline")
    assert await repo.external_identity(ANNA, provider="confluence") == (
        "conf-1",
        "Anna Confluence",
    )


async def test_external_identity_per_call_provider_wins_over_construction(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A per-call provider must override, not merely supplement, whatever was
    fixed at construction -- the worker's guild-resolved provider is always
    the one that governs which mapping is read.
    """
    async with factory() as session:
        session.add(
            AccountLink(
                discord_user_id=ANNA,
                provider="confluence",
                external_user_id="conf-1",
                display_name="Anna Confluence",
                linked_at=T0,
            )
        )
        await session.commit()

    repo = AccountLinkRepository(factory, provider="outline")
    assert await repo.external_identity(ANNA, provider="confluence") == (
        "conf-1",
        "Anna Confluence",
    )


# ---------------------------------------------------------------------------
# `AccountLinkRepository.save` / `.delete` -- the write side. These used to
# live in `sturnus.infrastructure.db.link_state` behind a second class of
# the same name; now that both sides live in one class in this module, the
# tests belong beside `external_identity`'s above rather than split into a
# different file.
# ---------------------------------------------------------------------------


@pytest.fixture
async def write_repo(clean_database: str) -> AccountLinkRepository:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return AccountLinkRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_save_persists_a_new_mapping(write_repo: AccountLinkRepository) -> None:
    await write_repo.save(ANNA, "outline", "ext-1", "Anna")
    assert await write_repo.delete(ANNA, "outline") is True


async def test_save_upserts_on_the_composite_key(write_repo: AccountLinkRepository) -> None:
    """Re-linking after changing accounts replaces, it does not conflict."""
    await write_repo.save(ANNA, "outline", "ext-1", "Anna Old")
    await write_repo.save(ANNA, "outline", "ext-2", "Anna New")
    # No primary-key violation was raised; the second save replaced the first.
    assert await write_repo.delete(ANNA, "outline") is True
    assert await write_repo.delete(ANNA, "outline") is False


async def test_different_providers_do_not_collide(write_repo: AccountLinkRepository) -> None:
    await write_repo.save(ANNA, "outline", "ext-1", "Anna")
    await write_repo.save(ANNA, "confluence", "ext-9", "Anna")
    assert await write_repo.delete(ANNA, "outline") is True
    assert await write_repo.delete(ANNA, "confluence") is True


async def test_delete_reports_whether_anything_was_removed(
    write_repo: AccountLinkRepository,
) -> None:
    assert await write_repo.delete(BEN, "outline") is False
    await write_repo.save(BEN, "outline", "ext-3", "Ben")
    assert await write_repo.delete(BEN, "outline") is True
    assert await write_repo.delete(BEN, "outline") is False


# ---------------------------------------------------------------------------
# `SessionRepository.candidates_for_announcement` / `.mark_announced` /
# `.closed_undocumented_sessions`, and `JobRepository.
# candidates_for_retention` / `.mark_audio_deleted` -- the periodic sweeps'
# infrastructure adapters (Defect 3, Defect 4).
# ---------------------------------------------------------------------------


async def _mark_documented(
    factory: async_sessionmaker[AsyncSession], session_id: int, provider: str = "outline"
) -> None:
    """Writes a `documented` row through the real write path.

    That path is `_WorkerSessionStore.mark_documented` in
    `sturnus.entrypoints.worker` rather than one of this module's
    repositories, so going through it here both sets up the sweeps' rows
    and keeps the adapter itself covered.
    """
    await _WorkerSessionStore(factory).mark_documented(
        session_id, "doc-1", "https://outline.example/doc/1", provider
    )


async def test_mark_documented_records_the_provider_it_was_given(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`session.document_provider` is what a re-publish or a migration reads
    back to learn which sink owns `document_id`, so it must record the
    provider the caller resolved from configuration (Spec 11) -- not the
    Outline default that held while Outline was the only sink.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, session_id, provider="confluence")

    async with factory() as session:
        stored = await session.get(Session, session_id)
        assert stored is not None
        assert stored.document_provider == "confluence"


async def test_candidates_for_announcement_returns_documented_sessions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, session_id)

    candidates = await repo.candidates_for_announcement()
    assert [c["id"] for c in candidates] == [session_id]
    assert candidates[0]["document_url"] == "https://outline.example/doc/1"
    assert candidates[0]["announced_at"] is None


async def test_candidates_for_announcement_carries_the_participants_to_mention(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The announcement mentions everyone the session recorded, so the ids
    have to travel with the candidate row -- in the order they first spoke,
    which is what makes a re-posted announcement read identically.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(session_id, ANNA, "Anna", T0)
    await repo.add_participant(session_id, BEN, "Ben", T0 + timedelta(minutes=1))
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, session_id)

    candidates = await repo.candidates_for_announcement()
    assert candidates[0]["participant_ids"] == (ANNA, BEN)


async def test_candidates_for_announcement_gives_a_speakerless_session_no_participants(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session nobody spoke in still gets its link posted; the key is
    present and empty rather than missing, so the caller never has to ask
    whether the reader supplied it.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, session_id)

    assert (await repo.candidates_for_announcement())[0]["participant_ids"] == ()


async def test_one_sessions_participants_do_not_leak_into_another(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions are read in one sweep and regrouped in Python; a wrong
    grouping would mention the wrong people in the wrong channel, which is
    a privacy failure, not a cosmetic one.
    """
    repo = SessionRepository(factory)
    first = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.add_participant(first, ANNA, "Anna", T0)
    await repo.close_session(first, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, first)

    second = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0 + timedelta(hours=2))
    await repo.add_participant(second, BEN, "Ben", T0 + timedelta(hours=2))
    await repo.close_session(second, T0 + timedelta(hours=3), "empty")
    await _mark_documented(factory, second)

    by_id = {c["id"]: c["participant_ids"] for c in await repo.candidates_for_announcement()}
    assert by_id == {first: (ANNA,), second: (BEN,)}


async def test_candidates_for_announcement_excludes_a_session_still_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SessionRepository(factory)
    await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    assert await repo.candidates_for_announcement() == []


async def test_mark_announced_stamps_the_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, session_id)

    await repo.mark_announced(session_id, T0 + timedelta(minutes=5))

    candidates = await repo.candidates_for_announcement()
    assert candidates[0]["announced_at"] == T0 + timedelta(minutes=5)


async def test_mark_announced_does_not_stamp_a_session_that_was_requeued_meanwhile(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stamp has to land on the session the post was actually about.

    `announce_ready_sessions` selects a `documented`, unannounced session,
    awaits `announcer.post` -- a Discord HTTP call that takes seconds
    under rate limiting -- and only then calls `mark_announced`. A
    `/queue requeue` can land inside that window: it puts the session back
    to `closed` and clears `announced_at` precisely so the redo's fresh
    link gets posted. An unconditional stamp arriving afterwards would put
    a timestamp back on a session that has *not* been announced since, and
    `sessions_to_announce` would then never select it again: the corrected
    transcript would be documented and silently never posted, which is the
    exact failure clearing the column exists to prevent.

    A duplicate post of the superseded link is the accepted cost here, and
    the one `announce_ready_sessions` already documents itself as erring
    towards -- losing an announcement is the worse half of that trade.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await _mark_documented(factory, session_id)
    # The re-queue, exactly as `queue_cog.apply_requeue` writes it, while
    # the sweep is somewhere inside `announcer.post`.
    await _requeue(factory, session_id)

    await repo.mark_announced(session_id, T0 + timedelta(minutes=5))

    async with factory() as session:
        row = await session.get(Session, session_id)
        assert row is not None
        assert row.announced_at is None, "the late stamp belongs to a run that is superseded"
    # And the consequence that matters: once the redo is documented, the
    # session is a candidate again and the new link does get posted.
    await _mark_documented(factory, session_id)
    assert [c["id"] for c in await repo.candidates_for_announcement()] == [session_id]


async def _requeue(factory: async_sessionmaker[AsyncSession], session_id: int) -> None:
    """The session-row half of a `/queue requeue`, without the cog."""
    async with factory() as session:
        await session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(status="closed", announced_at=None)
        )
        await session.commit()


async def test_closed_undocumented_sessions_finds_a_session_whose_jobs_are_all_terminal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    queue = JobQueue(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    job_id = await _enqueue_job(sessions, jobs, session_id, ANNA)
    await sessions.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await queue.complete(job_id, "hello")

    assert await sessions.closed_undocumented_sessions() == [session_id]


async def test_closed_undocumented_sessions_excludes_a_session_with_a_pending_job(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await _enqueue_job(sessions, jobs, session_id, ANNA)
    await sessions.close_session(session_id, T0 + timedelta(hours=1), "empty")

    assert await sessions.closed_undocumented_sessions() == []


async def test_closed_undocumented_sessions_excludes_a_session_with_no_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await sessions.close_session(session_id, T0 + timedelta(hours=1), "empty")

    assert await sessions.closed_undocumented_sessions() == []


async def test_closed_undocumented_sessions_excludes_an_already_documented_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    queue = JobQueue(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    job_id = await _enqueue_job(sessions, jobs, session_id, ANNA)
    await sessions.close_session(session_id, T0 + timedelta(hours=1), "empty")
    await queue.complete(job_id, "hello")
    await _mark_documented(factory, session_id)

    assert await sessions.closed_undocumented_sessions() == []


async def test_candidates_for_retention_returns_undeleted_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    job_id = await _enqueue_job(sessions, jobs, session_id, ANNA)

    candidates = await jobs.candidates_for_retention()
    assert [c["id"] for c in candidates] == [job_id]
    assert candidates[0]["audio_deleted_at"] is None


async def test_mark_audio_deleted_excludes_the_job_from_later_candidates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    job_id = await _enqueue_job(sessions, jobs, session_id, ANNA)

    await jobs.mark_audio_deleted(job_id, T0 + timedelta(days=31))

    assert await jobs.candidates_for_retention() == []
