from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.repositories import (
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
