"""The guild-wide queue overview, against the real database.

Against PostgreSQL rather than a double because the whole question is
which sessions the statement selects, and a double would select whatever
it was written to select. The one property that carries the rest is the
definition of "unfinished": it is deliberately two conditions, and the
second exists because of a case the first silently loses -- a session that
reached `documented` with a `dead` job in it, which is the exact moment
somebody needs to notice a speaker whose transcription failed for good.

The totals are `load_status`, unchanged and unwrapped, so they are pinned
where that function is. What is pinned here is the guild scoping of the
whole answer and the per-session counts the page is made of.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.adapters import ConsoleQueueOverview
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    TranscriptionJob,
)
from sturnus.infrastructure.db.requeue import ACTIVE_SESSION_LIMIT, load_active_sessions

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
GUILD, OTHER_GUILD = 4711, 9999
ANNA, BEN, CARL = 100, 200, 300


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class Admins:
    """The mirrored administrator membership, per guild."""

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


def overview(
    factory: async_sessionmaker[AsyncSession],
    *,
    admins: Admins | None = None,
    now: datetime = T0,
    lease_seconds: float = 1800.0,
) -> ConsoleQueueOverview:
    return ConsoleQueueOverview(factory, admins or Admins(), lambda: now, lease_seconds)


async def a_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: int = GUILD,
    started_at: datetime = T0,
    ended_at: datetime | None = None,
    status: str = "closed",
    document_url: str | None = None,
    channel_name: str | None = "meeting",
    jobs: dict[int, str] | None = None,
    claimed_at: datetime | None = None,
) -> int:
    """One session with one job per speaker, written straight to the tables.

    Direct inserts rather than `RecordingService`: what is under test is
    which rows the query selects, and going through the writer would make
    it a test of two things at once -- and there is no writer that can
    produce a `dead` job on demand.
    """
    async with factory() as db:
        session = Session(
            guild_id=guild_id,
            channel_id=555,
            channel_name=channel_name,
            started_at=started_at,
            ended_at=ended_at if ended_at is not None else started_at + timedelta(hours=1),
            status=status,
            document_url=document_url,
        )
        db.add(session)
        await db.flush()
        for discord_user_id, job_status in (jobs or {}).items():
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    discord_display_name=f"user-{discord_user_id}",
                    first_seen_at=started_at,
                )
            )
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    s3_key=f"sessions/{session.id}/speakers/{discord_user_id}.enc",
                    encryption_key_id="k1",
                    wrapped_data_key=b"wrapped",
                    retention_until=started_at + timedelta(days=30),
                    status=job_status,
                    attempts=1,
                    claimed_at=claimed_at,
                )
            )
        await db.commit()
        return session.id


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_an_administrator_of_the_guild_sees_its_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.pending == 1


async def test_an_administrator_of_another_guild_is_nobody_here(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, jobs={BEN: "pending"})
    admins = Admins({OTHER_GUILD: {CARL}})

    assert await overview(factory, admins=admins).for_guild(GUILD, requested_by=CARL) is None


async def test_a_participant_who_administers_nothing_sees_no_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, jobs={BEN: "pending"})

    assert await overview(factory).for_guild(GUILD, requested_by=BEN) is None


# ---------------------------------------------------------------------------
# What counts as unfinished
# ---------------------------------------------------------------------------


async def test_a_session_still_waiting_for_a_worker_is_in_the_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await a_session(factory, jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert [s.id for s in queue.sessions] == [session_id]


async def test_a_recording_in_progress_is_in_the_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open session has no jobs at all and belongs here anyway.

    It is a recording happening right now, which is the one thing an
    administrator looking at a queue most wants confirmed.
    """
    session_id = await a_session(factory, status="open", jobs={})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert [s.id for s in queue.sessions] == [session_id]


async def test_a_finished_session_is_not_in_the_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(
        factory,
        status="documented",
        document_url="https://outline.example/doc/1",
        jobs={BEN: "done"},
    )

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions == ()


async def test_a_documented_session_with_a_dead_job_stays_in_the_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case the obvious condition loses.

    The document is written once every job is terminal, and `dead` is
    terminal -- so a speaker whose transcription failed permanently would
    disappear from the queue view at exactly the moment somebody needs to
    notice them.
    """
    session_id = await a_session(
        factory,
        status="documented",
        document_url="https://outline.example/doc/1",
        jobs={BEN: "done", CARL: "dead"},
    )

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert [s.id for s in queue.sessions] == [session_id]
    assert queue.sessions[0].dead == 1


async def test_another_guild_s_unfinished_work_is_not_this_guild_s_business(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, guild_id=OTHER_GUILD, jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions == ()
    assert queue.pending == 0


# ---------------------------------------------------------------------------
# What each row says
# ---------------------------------------------------------------------------


async def test_a_session_carries_the_counts_it_is_made_of(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, jobs={ANNA: "done", BEN: "pending", CARL: "dead"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    session = queue.sessions[0]
    assert (session.pending, session.running, session.done, session.dead) == (1, 0, 1, 1)


async def test_a_session_with_no_jobs_reports_zeroes_rather_than_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A recording in progress has no jobs yet, and a row with absent
    # counts would render as a gap where the numbers go.
    await a_session(factory, status="open", jobs={})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    session = queue.sessions[0]
    assert (session.pending, session.running, session.done, session.dead) == (0, 0, 0, 0)


async def test_a_session_carries_the_channel_name_it_opened_under(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A web page has no `<#id>` to resolve, so it needs the stored name --
    # the same one every other console view shows.
    await a_session(factory, channel_name="planning", jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions[0].channel_name == "planning"


async def test_the_newest_session_comes_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    old = await a_session(factory, started_at=T0 - timedelta(days=2), jobs={BEN: "pending"})
    new = await a_session(factory, started_at=T0 - timedelta(hours=1), jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert [s.id for s in queue.sessions] == [new, old]


# ---------------------------------------------------------------------------
# The totals, and the caveats that travel with them
# ---------------------------------------------------------------------------


async def test_an_expired_lease_is_counted_against_the_lease_that_was_assumed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `running` job whose lease expired is one whose worker died holding it.

    No amount of waiting fixes it, which is why it is the number worth
    reading first -- and why the lease it was measured against is sent
    with it rather than left implicit.
    """
    await a_session(
        factory,
        jobs={BEN: "running"},
        claimed_at=T0 - timedelta(hours=2),
    )

    queue = await overview(factory, lease_seconds=600.0).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.running_past_lease == 1
    assert queue.lease_seconds == 600.0


async def test_a_job_claimed_a_moment_ago_is_not_past_its_lease(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, jobs={BEN: "running"}, claimed_at=T0 - timedelta(seconds=30))

    queue = await overview(factory, lease_seconds=600.0).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.running_past_lease == 0


async def test_a_closed_session_with_no_document_and_nothing_queued_is_counted_as_stuck(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Nothing is queued for it and nothing will happen on its own, which is
    # the one state that needs a person rather than patience.
    await a_session(factory, status="closed", jobs={BEN: "done"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.closed_undocumented == 1


async def test_the_oldest_pending_work_is_dated_by_its_session_s_end(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`transcription_job` has no enqueue timestamp at all.

    A session's end is within seconds of when its jobs were created, which
    is close enough to answer "has something been sitting here for hours?"
    -- the only question the figure exists for.
    """
    ended = T0 - timedelta(hours=5)
    await a_session(
        factory, started_at=ended - timedelta(hours=1), ended_at=ended, jobs={BEN: "pending"}
    )

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.oldest_pending_session_ended_at == ended


async def test_a_guild_with_nothing_pending_has_no_oldest_anything(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, status="documented", document_url="u", jobs={BEN: "done"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.oldest_pending_session_ended_at is None


# ---------------------------------------------------------------------------
# Cutting the list
# ---------------------------------------------------------------------------


async def test_a_long_list_is_cut_and_says_so(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A guild broken for a month has hundreds, and hundreds is unreadable.

    What matters is that "twenty" never reads as "twenty exist" -- so the
    cut is reported rather than merely applied.
    """
    for day in range(ACTIVE_SESSION_LIMIT + 3):
        await a_session(factory, started_at=T0 - timedelta(days=day), jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert len(queue.sessions) == ACTIVE_SESSION_LIMIT
    assert queue.truncated is True
    # The totals are not cut with the list: the counts are the guild's,
    # however many sessions fit on the page.
    assert queue.pending == ACTIVE_SESSION_LIMIT + 3


async def test_a_list_that_fits_is_not_reported_as_cut(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    for day in range(3):
        await a_session(factory, started_at=T0 - timedelta(days=day), jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.truncated is False


async def test_exactly_the_limit_is_not_reported_as_cut(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The off-by-one worth pinning: the query asks for one more than it
    # will show precisely so this case answers `False`.
    for day in range(ACTIVE_SESSION_LIMIT):
        await a_session(factory, started_at=T0 - timedelta(days=day), jobs={BEN: "pending"})

    sessions, truncated = await load_active_sessions(factory, GUILD)

    assert len(sessions) == ACTIVE_SESSION_LIMIT
    assert truncated is False


async def test_a_guild_with_nothing_outstanding_returns_an_empty_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions, truncated = await load_active_sessions(factory, GUILD)

    assert sessions == []
    assert truncated is False


# ---------------------------------------------------------------------------
# What runs first
# ---------------------------------------------------------------------------


async def test_a_session_reports_where_it_sits_in_the_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await a_session(factory, jobs={BEN: "pending"})
    await hold_back(factory, session_id, 3)

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions[0].priority == 3


async def test_a_session_nobody_has_reordered_sits_at_the_ordinary_priority(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, jobs={BEN: "pending"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions[0].priority == 0


async def test_a_recording_in_progress_has_no_place_in_the_queue_at_all(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Null, not zero -- there is nothing queued for it to have a place with.

    Zero is the ordinary priority and a real position. Reporting it here
    would put a drag handle on a row that nothing can be reordered about.
    """
    await a_session(factory, status="open", jobs={})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions[0].priority is None


async def test_a_session_listed_only_for_a_dead_job_has_no_place_either(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every job terminal means nothing will ever be claimed for it again."""
    await a_session(factory, status="documented", document_url="u", jobs={BEN: "dead"})

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions[0].priority is None


async def test_a_place_is_read_from_the_outstanding_jobs_and_not_the_finished_ones(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A finished job's number describes a queue it has already left."""
    session_id = await a_session(factory, jobs={ANNA: "done", BEN: "pending"})
    await hold_back(factory, session_id, 7, status="done")

    queue = await overview(factory).for_guild(GUILD, requested_by=ANNA)

    assert queue is not None
    assert queue.sessions[0].priority == 0


# ---------------------------------------------------------------------------
# Reordering a guild's queue by a rule
# ---------------------------------------------------------------------------


async def test_an_administrator_can_put_the_biggest_meetings_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    small = await a_session(factory, jobs={BEN: "pending"})
    large = await a_session(factory, jobs={ANNA: "pending", BEN: "pending", CARL: "pending"})

    order = await overview(factory).reprioritise(
        GUILD, requested_by=ANNA, rule="many-participants-first"
    )

    assert order is not None
    assert order.accepted is True
    assert [position.session_id for position in order.sessions] == [large, small]
    assert order.changed == (small,)


async def test_somebody_who_does_not_administer_the_guild_cannot_reorder_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`None` for "no such guild" and "not yours" alike, as everywhere else."""
    session_id = await a_session(factory, jobs={BEN: "pending"})

    order = await overview(factory).reprioritise(
        GUILD, requested_by=BEN, rule="many-participants-first"
    )

    assert order is None
    async with factory() as db:
        rows = await db.execute(
            select(TranscriptionJob.priority).where(TranscriptionJob.session_id == session_id)
        )
        assert [row.priority for row in rows] == [0]


async def hold_back(
    factory: async_sessionmaker[AsyncSession],
    session_id: int,
    priority: int,
    status: str | None = None,
) -> None:
    """Puts a session's jobs at a priority, as a reorder does."""
    async with factory() as db:
        statement = update(TranscriptionJob).where(TranscriptionJob.session_id == session_id)
        if status is not None:
            statement = statement.where(TranscriptionJob.status == status)
        await db.execute(statement.values(priority=priority))
        await db.commit()
