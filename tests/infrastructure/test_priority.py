"""Writing a queue order, against a real PostgreSQL.

The pure arithmetic is `tests/application/test_priorities.py`. What is
tested here is everything the database has an opinion about: that a
session's jobs move together, that the read sees what the claim would see,
that a guild's write cannot reach another guild's rows, and that two
administrators reordering at the same instant produce one coherent order
rather than a blend of two.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.priorities import (
    Placement,
    QueuedSession,
    order_by_rule,
    order_with,
    resolve_rule,
)
from sturnus.infrastructure.db.models import Base, Session, TranscriptionJob
from sturnus.infrastructure.db.priority import Decision, load_queued_sessions, reorder
from sturnus.infrastructure.db.repositories import JobRepository, SessionRepository

T0 = datetime(2026, 8, 23, 9, 0, 0, tzinfo=UTC)
GUILD, CHANNEL = 1, 2
OTHER_GUILD, OTHER_CHANNEL = 11, 12
ANNA, BEN, CARLA, DORA = 100, 200, 300, 400


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def seed(
    factory: async_sessionmaker[AsyncSession],
    speakers: list[int],
    guild: int = GUILD,
    channel: int = CHANNEL,
    started_at: datetime = T0,
) -> int:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(guild, channel, "meeting-raum", started_at)
    for user_id in speakers:
        await sessions.add_participant(session_id, user_id, f"user{user_id}", started_at)
        await jobs.enqueue(
            session_id=session_id,
            discord_user_id=user_id,
            s3_key=f"sessions/{session_id}/speakers/{user_id}.enc",
            encryption_key_id="k1",
            wrapped_data_key=b"wrapped",
            retention_until=started_at + timedelta(days=30),
        )
    await sessions.close_session(session_id, started_at + timedelta(hours=1), "empty")
    return session_id


async def priorities_of(factory: async_sessionmaker[AsyncSession], session_id: int) -> list[int]:
    async with factory() as db:
        rows = await db.execute(
            select(TranscriptionJob.priority)
            .where(TranscriptionJob.session_id == session_id)
            .order_by(TranscriptionJob.id)
        )
        return [row.priority for row in rows]


async def finish(
    factory: async_sessionmaker[AsyncSession],
    session_id: int,
    *,
    status: str = "done",
    audio_seconds: float | None = None,
) -> None:
    """Takes a session's jobs out of the queue, optionally measuring them."""
    values: dict[str, object] = {"status": status}
    if audio_seconds is not None:
        values["audio_seconds"] = audio_seconds
    async with factory() as db:
        await db.execute(
            update(TranscriptionJob)
            .where(TranscriptionJob.session_id == session_id)
            .values(**values)
        )
        await db.commit()


async def measure(
    factory: async_sessionmaker[AsyncSession], session_id: int, audio_seconds: float
) -> None:
    async with factory() as db:
        await db.execute(
            update(TranscriptionJob)
            .where(TranscriptionJob.session_id == session_id)
            .values(audio_seconds=audio_seconds)
        )
        await db.commit()


def drag(session_id: int, placement: Placement) -> Decision:
    """The decision a drag makes, as `reorder` takes it."""

    def decide(sessions: Sequence[QueuedSession]) -> tuple[int, ...] | None:
        return order_with(sessions, session_id, placement)

    return decide


def quick_action(name: str) -> Decision:
    rule = resolve_rule(name)

    def decide(sessions: Sequence[QueuedSession]) -> tuple[int, ...] | None:
        return order_by_rule(sessions, rule)

    return decide


# ---------------------------------------------------------------------------
# What the reader sees
# ---------------------------------------------------------------------------


async def test_a_queue_nobody_has_touched_reads_as_ordinary_and_oldest_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await seed(factory, [ANNA])
    second = await seed(factory, [BEN])

    queued = await load_queued_sessions(factory, GUILD)

    assert [row.id for row in queued] == [first, second]
    assert [row.priority for row in queued] == [0, 0]


async def test_a_session_counts_the_people_who_were_in_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [ANNA, BEN, CARLA])

    queued = await load_queued_sessions(factory, GUILD)

    assert [row.participants for row in queued] == [3]
    assert session_id == queued[0].id


async def test_a_session_nothing_has_measured_has_no_length_rather_than_none_of_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Null, never zero. The quick action's whole correctness rests on it."""
    await seed(factory, [ANNA])

    queued = await load_queued_sessions(factory, GUILD)

    assert queued[0].audio_seconds is None


async def test_a_sessions_length_is_the_audio_its_tracks_were_measured_at(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Summed across speakers, because that is the work the queue still owes."""
    session_id = await seed(factory, [ANNA, BEN])
    await measure(factory, session_id, 120.0)

    queued = await load_queued_sessions(factory, GUILD)

    assert queued[0].audio_seconds == 240.0


async def test_a_session_with_nothing_outstanding_is_not_in_the_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A documented meeting has no place in the queue and cannot be dragged."""
    finished = await seed(factory, [ANNA])
    waiting = await seed(factory, [BEN])
    await finish(factory, finished)

    queued = await load_queued_sessions(factory, GUILD)

    assert [row.id for row in queued] == [waiting]


async def test_the_queue_of_one_guild_never_shows_another_guilds_sessions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await seed(factory, [ANNA], guild=GUILD, channel=CHANNEL)
    await seed(factory, [BEN], guild=OTHER_GUILD, channel=OTHER_CHANNEL)

    queued = await load_queued_sessions(factory, GUILD)

    assert [row.id for row in queued] == [mine]


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


async def test_dragging_a_session_to_the_front_puts_it_there(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await seed(factory, [ANNA])
    second = await seed(factory, [BEN])
    third = await seed(factory, [CARLA])

    result = await reorder(factory, GUILD, drag(third, Placement("first")))

    assert result is not None
    assert result.order == (third, first, second)
    assert [row.id for row in await load_queued_sessions(factory, GUILD)] == [
        third,
        first,
        second,
    ]


async def test_every_speaker_of_a_meeting_moves_together(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The unit is the session, and this is the reason it has to be.

    The rows are one per speaker. A write that moved some of a meeting's
    speakers and not the others would leave a queue that is half
    reordered, which no page can render and nobody would ever see.
    """
    crowded = await seed(factory, [ANNA, BEN, CARLA, DORA])
    await seed(factory, [ANNA])

    await reorder(factory, GUILD, drag(crowded, Placement("last")))

    assert await priorities_of(factory, crowded) == [1, 1, 1, 1]


async def test_an_order_that_already_holds_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await seed(factory, [ANNA])
    second = await seed(factory, [BEN])

    result = await reorder(factory, GUILD, drag(second, Placement("after", anchor=first)))

    assert result is not None
    assert result.changed == ()
    assert await priorities_of(factory, first) == [0]
    assert await priorities_of(factory, second) == [0]


async def test_a_reorder_never_touches_another_guilds_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `WHERE` that names the guild is the whole of this rule.

    Priority is one number in one column shared by every guild in the
    deployment, so a write that forgot its guild would silently reorder
    somebody else's meetings.
    """
    mine = await seed(factory, [ANNA], guild=GUILD, channel=CHANNEL)
    also_mine = await seed(factory, [BEN], guild=GUILD, channel=CHANNEL)
    theirs = await seed(factory, [CARLA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)

    await reorder(factory, GUILD, drag(also_mine, Placement("first")))

    assert await priorities_of(factory, theirs) == [0]
    assert await priorities_of(factory, mine) == [1]


async def test_a_drag_of_a_session_that_has_left_the_queue_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """It finished while the page was open. Refused, and nothing is written."""
    gone = await seed(factory, [ANNA])
    still_here = await seed(factory, [BEN])
    await finish(factory, gone)

    result = await reorder(factory, GUILD, drag(gone, Placement("first")))

    assert result is None
    assert await priorities_of(factory, still_here) == [0]


async def test_a_drag_against_another_guilds_session_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An anchor is looked up in this guild's queue and nowhere else."""
    mine = await seed(factory, [ANNA], guild=GUILD, channel=CHANNEL)
    theirs = await seed(factory, [BEN], guild=OTHER_GUILD, channel=OTHER_CHANNEL)

    result = await reorder(factory, GUILD, drag(mine, Placement("after", anchor=theirs)))

    assert result is None


async def test_only_the_outstanding_jobs_of_a_session_are_renumbered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A finished job's priority means nothing and is left alone.

    Its transcript is written; nothing will ever claim it again. Writing a
    queue position onto it would be recording an intention about work that
    is over.
    """
    partly_done = await seed(factory, [ANNA, BEN])
    await seed(factory, [CARLA])
    async with factory() as db:
        first_job = await db.scalar(
            select(TranscriptionJob.id)
            .where(TranscriptionJob.session_id == partly_done)
            .order_by(TranscriptionJob.id)
        )
        await db.execute(
            update(TranscriptionJob).where(TranscriptionJob.id == first_job).values(status="done")
        )
        await db.commit()

    await reorder(factory, GUILD, drag(partly_done, Placement("last")))

    assert await priorities_of(factory, partly_done) == [0, 1]


# ---------------------------------------------------------------------------
# The quick actions, end to end
# ---------------------------------------------------------------------------


async def test_the_biggest_meeting_is_moved_to_the_front(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    small = await seed(factory, [ANNA])
    large = await seed(factory, [ANNA, BEN, CARLA])

    result = await reorder(factory, GUILD, quick_action("many-participants-first"))

    assert result is not None
    assert result.order == (large, small)


async def test_the_shortest_measured_recording_is_moved_to_the_front(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    long_one = await seed(factory, [ANNA])
    short_one = await seed(factory, [BEN])
    await measure(factory, long_one, 900.0)
    await measure(factory, short_one, 90.0)

    result = await reorder(factory, GUILD, quick_action("short-recordings-first"))

    assert result is not None
    assert result.order == (short_one, long_one)


async def test_an_unmeasured_recording_is_not_promoted_by_what_nobody_knows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    unmeasured = await seed(factory, [ANNA])
    measured = await seed(factory, [BEN])
    await measure(factory, measured, 300.0)

    result = await reorder(factory, GUILD, quick_action("short-recordings-first"))

    assert result is not None
    assert result.order == (measured, unmeasured)


# ---------------------------------------------------------------------------
# Two administrators at once
# ---------------------------------------------------------------------------


async def test_two_reorders_at_the_same_instant_produce_one_coherent_order(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The property this endpoint has to have, and the only way to show it.

    Two administrators drag two different sessions to the front at the
    same instant, on two connections, against a real PostgreSQL. Each
    drag is decided *inside* the lock, from the queue as it stands at that
    moment, so the second is applied to what the first left rather than to
    the list its browser was showing.

    The assertion is exact on purpose. There are two ways these two calls
    can serialise and each leaves a different set of numbers behind, so
    the result matching one of them is what rules out the failure that
    matters: two decisions taken from the same snapshot and both written,
    which would leave an order neither administrator asked for and which
    obeys neither instruction.

    Repeated, because a race that fires once in twenty runs fires in
    production.
    """
    for _ in range(20):
        await wipe(factory)
        first = await seed(factory, [ANNA])
        second = await seed(factory, [BEN])
        third = await seed(factory, [CARLA])

        await asyncio.gather(
            reorder(factory, GUILD, drag(third, Placement("first"))),
            reorder(factory, GUILD, drag(second, Placement("first"))),
        )

        queued = await load_queued_sessions(factory, GUILD)
        written = {row.id: row.priority for row in queued}
        assert written in (
            # The third session went first, then the second overtook it.
            {second: 1, third: 1, first: 2},
            # The other way round.
            {third: 1, second: 2, first: 3},
        ), written


async def wipe(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        await db.execute(delete(Session))
        await db.commit()
