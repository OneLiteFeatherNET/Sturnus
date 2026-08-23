"""Dragging one session, through `ConsoleQueueControl`, against the database.

The arithmetic is `tests/application/test_priorities.py` and the write is
`tests/infrastructure/test_priority.py`. What is left, and what is tested
here, is the join between them: that the authorisation check is the one
`requeue` already uses, that it is made against the session's *own* guild,
and that a person who fails it changes nothing rather than merely being
told no.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.priorities import Placement
from sturnus.console.adapters import STALE_DRAG, ConsoleQueueControl
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    TranscriptionJob,
)

T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
GUILD, OTHER_GUILD = 4711, 9999
ANNA, BEN = 100, 200


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class Admins:
    def __init__(self, by_guild: dict[int, set[int]] | None = None) -> None:
        self.by_guild = by_guild if by_guild is not None else {GUILD: {ANNA}}

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        return any(discord_user_id in members for members in self.by_guild.values())

    async def administered_guilds(self, discord_user_id: int) -> tuple[int, ...]:
        return tuple(
            sorted(g for g, members in self.by_guild.items() if discord_user_id in members)
        )

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool:
        return discord_user_id in self.by_guild.get(guild_id, set())


def control(
    factory: async_sessionmaker[AsyncSession], admins: Admins | None = None
) -> ConsoleQueueControl:
    return ConsoleQueueControl(factory, admins or Admins())


async def a_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: int = GUILD,
    speakers: tuple[int, ...] = (BEN,),
    status: str = "pending",
) -> int:
    async with factory() as db:
        session = Session(
            guild_id=guild_id,
            channel_id=555,
            channel_name="meeting",
            started_at=T0,
            ended_at=T0 + timedelta(hours=1),
            status="closed",
        )
        db.add(session)
        await db.flush()
        for discord_user_id in speakers:
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    discord_display_name=f"user-{discord_user_id}",
                    first_seen_at=T0,
                )
            )
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    s3_key=f"sessions/{session.id}/speakers/{discord_user_id}.enc",
                    encryption_key_id="k1",
                    wrapped_data_key=b"wrapped",
                    retention_until=T0 + timedelta(days=30),
                    status=status,
                )
            )
        await db.commit()
        return session.id


async def priorities(factory: async_sessionmaker[AsyncSession]) -> dict[int, int]:
    async with factory() as db:
        rows = await db.execute(
            select(TranscriptionJob.session_id, TranscriptionJob.priority).order_by(
                TranscriptionJob.id
            )
        )
        return {session_id: priority for session_id, priority in rows}


async def test_an_administrator_moves_a_session_to_the_front(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await a_session(factory)
    second = await a_session(factory)

    order = await control(factory).place(second, requested_by=ANNA, placement=Placement("first"))

    assert order is not None
    assert order.accepted is True
    assert [position.session_id for position in order.sessions] == [second, first]
    assert order.changed == (first,)


async def test_somebody_who_does_not_administer_the_guild_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`None`, which the route renders as the same 404 a missing session gets.

    And, more importantly than the answer: the queue is untouched. An
    authorisation check that refused the *response* while the write had
    already happened would be no check at all.
    """
    first = await a_session(factory)
    second = await a_session(factory)

    order = await control(factory).place(second, requested_by=BEN, placement=Placement("first"))

    assert order is None
    assert await priorities(factory) == {first: 0, second: 0}


async def test_administering_another_guild_is_not_administering_this_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = Admins({GUILD: {ANNA}, OTHER_GUILD: {BEN}})
    first = await a_session(factory)
    second = await a_session(factory)

    order = await control(factory, admins).place(
        second, requested_by=BEN, placement=Placement("first")
    )

    assert order is None
    assert await priorities(factory) == {first: 0, second: 0}


async def test_a_session_that_does_not_exist_is_refused_the_same_way(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    order = await control(factory).place(9999, requested_by=ANNA, placement=Placement("first"))

    assert order is None


async def test_a_drag_the_queue_has_moved_past_is_refused_with_the_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session finished while the page was open.

    Not `None`: the person may make this request, so the answer is a
    refusal rather than a 404 -- and it carries the queue as it now is, so
    the page redraws instead of replaying a drag that cannot land.
    """
    waiting = await a_session(factory)
    finished = await a_session(factory, status="done")

    order = await control(factory).place(finished, requested_by=ANNA, placement=Placement("first"))

    assert order is not None
    assert order.accepted is False
    assert order.refusal == STALE_DRAG
    assert [position.session_id for position in order.sessions] == [waiting]
