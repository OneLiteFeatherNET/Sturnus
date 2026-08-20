from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.assembly import serialize_transcript
from sturnus.application.transcription import TranscribedSegment, TranscriptionResult
from sturnus.infrastructure.db.models import AccountLink, Base
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
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    assert await repo.find_open_session(GUILD) == session_id

    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=3))
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")

    assert await repo.find_open_session(GUILD) is None


async def test_session_row_carries_the_key_after_opening(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session row is the source of truth crash recovery reads from.

    Without this, a process that dies between encrypting a recording and
    enqueueing the job for it leaves a key that only ever lived in memory
    -- unrecoverable.
    """
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    assert await repo.session_key(session_id) is None

    await repo.record_session_key(session_id, "k1", b"wrapped-bytes")

    assert await repo.session_key(session_id) == ("k1", b"wrapped-bytes")


async def test_session_key_is_none_for_a_session_without_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Covers both a session that predates the column and one that crashed early."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    assert await repo.session_key(session_id) is None


async def test_adding_a_participant_twice_is_harmless(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Someone may leave and rejoin within one session."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.add_participant(session_id, ANNA, "anna-renamed", T0 + timedelta(minutes=1))
    # The first display name wins: it is the one in force when recording began.
    names = await repo.participant_names(session_id)
    assert names == {ANNA: "anna"}


async def test_audio_epoch_is_written_once(factory: async_sessionmaker[AsyncSession]) -> None:
    """The epoch marks the first packet; a later packet must not move it."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=3))
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=9))
    assert await repo.audio_epoch(session_id, ANNA) == T0 + timedelta(seconds=3)


async def test_job_enqueue(factory: async_sessionmaker[AsyncSession]) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, T0)
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
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")
    assert await repo.session_bounds(session_id) == (T0, T0 + timedelta(hours=1))


async def test_session_bounds_raises_while_the_session_is_still_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open session has no end yet; inventing one would misrepresent how long it ran."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
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
    session_id = await sessions.open_session(GUILD, CHANNEL, T0)
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
    session_id = await sessions.open_session(GUILD, CHANNEL, T0)
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
