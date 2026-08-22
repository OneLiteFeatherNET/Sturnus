"""The console's reads, against the real database.

One property carries all the others and is why these tests exist against
PostgreSQL rather than a double: **every statement is scoped by the
signed-in Discord id in the query itself.** A handler that filters
afterwards is a filter somebody can forget; a `WHERE` that names
`session_participant` cannot be forgotten, because without it the
statement returns nothing at all rather than everything.

So the tests below are mostly about what is *not* returned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.queries import ConsoleQueries
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    TranscriptionJob,
)

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
GUILD, CHANNEL = 1, 555
ANNA, BEN, CARL = 100, 200, 300


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def a_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    started_at: datetime = T0,
    ended_at: datetime | None = None,
    channel_id: int = CHANNEL,
    channel_name: str | None = "meeting",
    document_url: str | None = None,
    people: dict[int, str] | None = None,
) -> int:
    """A closed session with participants, written straight to the tables.

    Direct inserts rather than `SessionRepository`: what is being tested
    is which columns the query reads back, and going through the writer
    would make that a test of two things at once.
    """
    async with factory() as db:
        session = Session(
            guild_id=GUILD,
            channel_id=channel_id,
            channel_name=channel_name,
            started_at=started_at,
            ended_at=ended_at,
            status="closed" if ended_at else "recording",
            document_url=document_url,
        )
        db.add(session)
        await db.flush()
        for discord_user_id, name in (people or {ANNA: "anna"}).items():
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    discord_display_name=name,
                    first_seen_at=started_at,
                )
            )
        await db.commit()
        return session.id


async def a_track(
    factory: async_sessionmaker[AsyncSession],
    session_id: int,
    discord_user_id: int,
    *,
    audio_seconds: float | None = 60.0,
    speech_seconds: float | None = 30.0,
    segment_count: int | None = 4,
    transcript: str | None = None,
    status: str = "done",
) -> None:
    async with factory() as db:
        db.add(
            TranscriptionJob(
                session_id=session_id,
                discord_user_id=discord_user_id,
                s3_key=f"sessions/{session_id}/{discord_user_id}.enc",
                encryption_key_id="key-1",
                wrapped_data_key=b"wrapped",
                retention_until=T0 + timedelta(days=30),
                status=status,
                transcript=transcript,
                audio_seconds=audio_seconds,
                speech_seconds=speech_seconds,
                segment_count=segment_count,
            )
        )
        await db.commit()


def words(*texts: str) -> str:
    segments = ", ".join(
        f'{{"start": {index}.0, "end": {index + 1}.0, "text": "{text}"}}'
        for index, text in enumerate(texts)
    )
    return f'{{"language": "de", "segments": [{segments}]}}'


# ---------------------------------------------------------------------------
# The scope: a session you were not in does not exist
# ---------------------------------------------------------------------------


async def test_a_session_you_were_not_in_is_not_reachable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole authorisation model, in one query.

    Not "fetched and then refused" -- the statement names
    `session_participant`, so a session Anna was not in is not among the
    rows the database ever sends back.
    """
    theirs = await a_session(factory, people={BEN: "ben"})
    assert await ConsoleQueries(factory).session_for(ANNA, theirs) is None


async def test_a_session_you_were_not_in_is_not_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, people={BEN: "ben"})
    mine = await a_session(factory, people={ANNA: "anna"})
    listed = await ConsoleQueries(factory).sessions_for(ANNA)
    assert [session.id for session in listed] == [mine]


async def test_a_session_that_never_happened_is_not_found(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await ConsoleQueries(factory).session_for(ANNA, 4711) is None


async def test_a_session_you_were_in_is_reachable_by_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.id == mine


# ---------------------------------------------------------------------------
# What a session carries
# ---------------------------------------------------------------------------


async def test_a_session_names_everyone_who_was_in_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert {(p.discord_user_id, p.display_name) for p in found.participants} == {
        (ANNA, "anna"),
        (BEN, "ben"),
    }


async def test_a_session_carries_its_channel_and_its_times(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await a_session(
        factory,
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        channel_id=386950399101370374,
        channel_name="standup",
    )
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.channel_id == 386950399101370374
    assert found.channel_name == "standup"
    assert found.started_at == T0
    assert found.ended_at == T0 + timedelta(hours=1)


async def test_a_session_that_produced_a_protocol_carries_its_link(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await a_session(factory, document_url="https://outline.example/doc/x")
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.document_url == "https://outline.example/doc/x"


async def test_a_session_carries_what_each_track_measured(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_track(factory, mine, ANNA, audio_seconds=600.0, speech_seconds=120.5, segment_count=17)
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert len(found.tracks) == 1
    assert found.tracks[0].discord_user_id == ANNA
    assert found.tracks[0].display_name == "anna"
    assert found.tracks[0].audio_seconds == 600.0
    assert found.tracks[0].speech_seconds == 120.5
    assert found.tracks[0].segment_count == 17


async def test_a_track_from_before_the_measurements_reads_as_null_not_zero(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """These three columns are newer than the rows they describe, and the
    audio those rows point at may already have been erased. There is
    nothing to backfill from, so null is the only honest value -- and a
    zero here would be indistinguishable from a real measured silence.
    """
    mine = await a_session(factory)
    await a_track(factory, mine, ANNA, audio_seconds=None, speech_seconds=None, segment_count=None)
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.tracks[0].audio_seconds is None
    assert found.tracks[0].speech_seconds is None
    assert found.tracks[0].segment_count is None


async def test_a_session_with_no_jobs_yet_has_no_tracks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session being recorded right now has participants and no jobs."""
    mine = await a_session(factory)
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.tracks == ()


async def test_tracks_of_a_session_you_were_not_in_do_not_leak_through(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second and third statements are scoped as well as the first.

    They are given ids that were already scoped, which is why this could
    be argued away -- and that argument is exactly how a later edit to the
    first statement quietly widens the other two.
    """
    theirs = await a_session(factory, people={BEN: "ben"})
    await a_track(factory, theirs, BEN)
    mine = await a_session(factory, people={ANNA: "anna"})
    listed = await ConsoleQueries(factory).sessions_for(ANNA)
    assert [session.id for session in listed] == [mine]
    assert listed[0].tracks == ()


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


async def test_sessions_come_back_newest_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    older = await a_session(factory, started_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    newer = await a_session(factory, started_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC))
    listed = await ConsoleQueries(factory).sessions_for(ANNA)
    assert [session.id for session in listed] == [newer, older]


# ---------------------------------------------------------------------------
# The calendar's windows
# ---------------------------------------------------------------------------


async def test_a_year_holds_only_what_started_inside_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both edges, because an off-by-one at either end is a session that
    disappears from one year without appearing in the next.
    """
    just_before = await a_session(
        factory, started_at=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    )
    first_instant = await a_session(factory, started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    last_instant = await a_session(
        factory, started_at=datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    )
    just_after = await a_session(factory, started_at=datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC))

    in_2026 = {s.id for s in await ConsoleQueries(factory).sessions_in_year(ANNA, 2026)}
    assert in_2026 == {first_instant, last_instant}
    assert just_before not in in_2026
    assert just_after not in in_2026


async def test_a_year_of_somebody_elses_meetings_is_empty(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, started_at=T0, people={BEN: "ben"})
    assert await ConsoleQueries(factory).sessions_in_year(ANNA, 2026) == ()


async def test_a_day_holds_only_what_started_on_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    midnight = await a_session(factory, started_at=datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC))
    last_second = await a_session(factory, started_at=datetime(2026, 8, 21, 23, 59, 59, tzinfo=UTC))
    next_day = await a_session(factory, started_at=datetime(2026, 8, 22, 0, 0, 0, tzinfo=UTC))

    on_the_day = {
        s.id
        for s in await ConsoleQueries(factory).sessions_on_day(
            ANNA, datetime(2026, 8, 21, tzinfo=UTC).date()
        )
    }
    assert on_the_day == {midnight, last_second}
    assert next_day not in on_the_day


async def test_a_day_of_somebody_elses_meetings_is_empty(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, started_at=T0, people={BEN: "ben"})
    assert (
        await ConsoleQueries(factory).sessions_on_day(
            ANNA, datetime(2026, 8, 21, tzinfo=UTC).date()
        )
        == ()
    )


# ---------------------------------------------------------------------------
# Transcripts, which are only ever your own
# ---------------------------------------------------------------------------


async def test_only_your_own_transcripts_come_back(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The dashboard's word count is "how much did *I* say".

    Reading the whole session's transcripts would answer a different
    question with somebody else's words -- and the transcript is the
    protected content, so a wider read here is a wider disclosure.
    """
    mine = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_track(factory, mine, ANNA, transcript=words("guten morgen"))
    await a_track(factory, mine, BEN, transcript=words("und dir auch"))
    assert await ConsoleQueries(factory).transcripts_of(ANNA) == (words("guten morgen"),)


async def test_a_job_that_has_not_finished_has_no_transcript_to_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`pending`, `running` and `dead` jobs all have an empty column, and
    a dead one may keep an old partial value -- neither is a transcript.
    """
    mine = await a_session(factory)
    await a_track(factory, mine, ANNA, transcript=None, status="pending")
    assert await ConsoleQueries(factory).transcripts_of(ANNA) == ()


async def test_transcripts_span_every_session_you_were_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await a_session(factory, started_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    second = await a_session(factory, started_at=datetime(2026, 2, 1, 9, 0, tzinfo=UTC))
    await a_track(factory, first, ANNA, transcript=words("eins"))
    await a_track(factory, second, ANNA, transcript=words("zwei"))
    assert set(await ConsoleQueries(factory).transcripts_of(ANNA)) == {
        words("eins"),
        words("zwei"),
    }
