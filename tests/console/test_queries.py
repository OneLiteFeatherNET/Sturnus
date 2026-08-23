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

from sturnus.console.filters import session_filter
from sturnus.console.queries import ConsoleQueries
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    SessionTag,
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
    title: str | None = None,
    description: str | None = None,
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
            title=title,
            description=description,
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
    sample_rate: int | None = 16_000,
    channels: int | None = 1,
    stored_bytes: int | None = 4096,
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
                sample_rate=sample_rate,
                channels=channels,
                stored_bytes=stored_bytes,
            )
        )
        await db.commit()


async def a_tag(
    factory: async_sessionmaker[AsyncSession],
    session_id: int,
    discord_user_id: int,
    tag: str,
) -> None:
    """One person's label on one session, written straight to the table."""
    async with factory() as db:
        db.add(
            SessionTag(
                session_id=session_id,
                discord_user_id=discord_user_id,
                tag=tag,
                created_at=T0,
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


# ---------------------------------------------------------------------------
# Tags, which are only ever your own
# ---------------------------------------------------------------------------


async def test_a_session_carries_the_labels_you_put_on_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await a_session(factory)
    await a_tag(factory, mine, ANNA, "retro")
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.tags == ("retro",)


async def test_somebody_elses_label_on_your_own_meeting_is_not_yours_to_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole privacy decision, in one query.

    Ben was in the same meeting and called it something. That is a remark
    about a conversation Anna was also in, and she does not get to read
    it -- `session_tag` is keyed by its owner and the statement names
    her, so there is no version of this read that returns his word.
    """
    ours = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_tag(factory, ours, BEN, "zeitverschwendung")
    found = await ConsoleQueries(factory).session_for(ANNA, ours)
    assert found is not None
    assert found.tags == ()


async def test_two_people_may_label_the_same_meeting_differently(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Neither overwrites the other, because the owner is in the key."""
    ours = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_tag(factory, ours, ANNA, "retro")
    await a_tag(factory, ours, BEN, "planung")
    hers = await ConsoleQueries(factory).session_for(ANNA, ours)
    his = await ConsoleQueries(factory).session_for(BEN, ours)
    assert hers is not None and his is not None
    assert (hers.tags, his.tags) == (("retro",), ("planung",))


async def test_a_sessions_labels_come_back_alphabetical(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """So a recording's chips do not rearrange themselves between two
    page loads, which is what an unordered read would do the moment the
    planner chose a different join."""
    mine = await a_session(factory)
    await a_tag(factory, mine, ANNA, "retro")
    await a_tag(factory, mine, ANNA, "abschluss")
    found = await ConsoleQueries(factory).session_for(ANNA, mine)
    assert found is not None
    assert found.tags == ("abschluss", "retro")


async def test_your_own_labels_are_counted_across_your_meetings(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await a_session(factory, started_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    second = await a_session(factory, started_at=datetime(2026, 2, 1, 9, 0, tzinfo=UTC))
    await a_tag(factory, first, ANNA, "retro")
    await a_tag(factory, second, ANNA, "retro")
    await a_tag(factory, second, ANNA, "kunde")
    used = await ConsoleQueries(factory).tags_of(ANNA)
    assert [(use.tag, use.sessions) for use in used] == [("retro", 2), ("kunde", 1)]


async def test_the_labels_you_are_offered_are_not_anybody_elses(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ours = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_tag(factory, ours, BEN, "planung")
    assert await ConsoleQueries(factory).tags_of(ANNA) == ()


async def test_a_label_on_a_meeting_you_are_no_longer_in_is_not_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tag outliving the participation that justified it would put a
    chip in the filter bar counting a meeting the person can no longer
    open -- and the count is itself a statement about a session they are
    no longer entitled to see."""
    theirs = await a_session(factory, people={BEN: "ben"})
    await a_tag(factory, theirs, ANNA, "retro")
    assert await ConsoleQueries(factory).tags_of(ANNA) == ()


# ---------------------------------------------------------------------------
# One page of a history, and how long the history is
# ---------------------------------------------------------------------------


async def a_history(factory: async_sessionmaker[AsyncSession], length: int) -> list[int]:
    """`length` sessions Anna was in, an hour apart, oldest first."""
    return [
        await a_session(factory, started_at=T0 + timedelta(hours=index)) for index in range(length)
    ]


async def test_a_page_holds_only_what_was_asked_for(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_history(factory, 5)
    page = await ConsoleQueries(factory).sessions_page(ANNA, limit=2, offset=0)
    assert len(page.sessions) == 2


async def test_a_page_says_how_many_there_are_in_all(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The number the list needs to say "1-2 of 5". Without it a reader
    has to page to the end to find out how much they are not seeing."""
    await a_history(factory, 5)
    page = await ConsoleQueries(factory).sessions_page(ANNA, limit=2, offset=0)
    assert page.total == 5


async def test_the_pages_of_a_history_do_not_overlap_or_skip(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two adjacent windows must together be exactly the whole history.

    This is what the tie-break in the ordering buys: without ordering by
    id as well as by time, two sessions that opened in the same instant
    are free to swap, and the same row can land on both pages while
    another lands on neither.
    """
    await a_history(factory, 5)
    queries = ConsoleQueries(factory)
    first = await queries.sessions_page(ANNA, limit=3, offset=0)
    second = await queries.sessions_page(ANNA, limit=3, offset=3)
    seen = [session.id for session in (*first.sessions, *second.sessions)]
    assert len(seen) == len(set(seen)) == 5


async def test_a_page_is_still_newest_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sessions = await a_history(factory, 3)
    page = await ConsoleQueries(factory).sessions_page(ANNA, limit=3, offset=0)
    assert [session.id for session in page.sessions] == list(reversed(sessions))


async def test_a_window_past_the_end_is_empty_and_still_counts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """What a bookmark to page five looks like after a retention sweep.
    The count is what lets the console say so rather than claim this
    person has never been recorded."""
    await a_history(factory, 3)
    page = await ConsoleQueries(factory).sessions_page(ANNA, limit=10, offset=50)
    assert (page.sessions, page.total) == ((), 3)


async def test_the_count_is_of_your_own_meetings_and_nobody_elses(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A count is a smaller disclosure than a list and is not therefore a
    free one: "how many meetings are there" asked without a scope answers
    a question about everybody."""
    await a_session(factory, people={ANNA: "anna"})
    await a_session(factory, people={BEN: "ben"})
    assert (await ConsoleQueries(factory).sessions_page(ANNA, limit=10, offset=0)).total == 1


async def test_a_page_carries_the_participants_and_tracks_of_its_own_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The narrowing has to reach the other three statements too, or a
    page of twenty fetches a whole history's tracks to throw most away."""
    old = await a_session(factory, started_at=T0, people={ANNA: "anna", BEN: "ben"})
    await a_track(factory, old, ANNA)
    recent = await a_session(factory, started_at=T0 + timedelta(hours=1))
    await a_track(factory, recent, ANNA)

    page = await ConsoleQueries(factory).sessions_page(ANNA, limit=1, offset=0)

    assert [session.id for session in page.sessions] == [recent]
    assert len(page.sessions[0].tracks) == 1


# ---------------------------------------------------------------------------
# Narrowing a history, in the statement rather than afterwards
# ---------------------------------------------------------------------------


async def matching(
    factory: async_sessionmaker[AsyncSession],
    *,
    who: int = ANNA,
    text: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    protocol: str | None = None,
) -> list[int]:
    """The ids a filter selects, newest first."""
    page = await ConsoleQueries(factory).sessions_page(
        who,
        limit=100,
        offset=0,
        matching=session_filter(
            text=text, tags=tags or [], since=since, until=until, protocol=protocol
        ),
    )
    return [session.id for session in page.sessions]


async def test_a_search_finds_a_channel_by_part_of_its_name(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await a_session(factory, channel_name="weekly retro")
    await a_session(factory, channel_name="standup")
    assert await matching(factory, text="retro") == [wanted]


async def test_a_search_ignores_the_case_a_channel_was_named_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody types a channel name the way it was written."""
    wanted = await a_session(factory, channel_name="Weekly Retro")
    assert await matching(factory, text="retro") == [wanted]


async def test_a_search_finds_a_meeting_by_who_was_in_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A name is already in the response this same person gets for these
    sessions, so searching it narrows what they can see rather than
    widening it."""
    wanted = await a_session(factory, people={ANNA: "anna", BEN: "bernd"})
    await a_session(factory, people={ANNA: "anna", CARL: "carla"})
    assert await matching(factory, text="bern") == [wanted]


async def test_a_search_finds_a_meeting_by_your_own_label_for_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await a_session(factory)
    await a_session(factory)
    await a_tag(factory, wanted, ANNA, "kundengespräch")
    assert await matching(factory, text="kunden") == [wanted]


async def test_a_search_does_not_find_a_meeting_by_somebody_elses_label(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ben's word for a meeting is not Anna's to search by, for the same
    reason it is not hers to read."""
    ours = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_tag(factory, ours, BEN, "zeitverschwendung")
    assert await matching(factory, text="zeit") == []


async def test_a_search_never_reaches_a_session_you_were_not_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scope survives the filter. A search is a narrowing of what
    somebody may already see, never a way to reach past it."""
    await a_session(factory, channel_name="weekly retro", people={BEN: "ben"})
    assert await matching(factory, text="retro") == []


async def test_a_percent_sign_in_a_search_is_a_percent_sign(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Honoured as a wildcard it would match every recording there is."""
    await a_session(factory, channel_name="standup")
    wanted = await a_session(factory, channel_name="100% done")
    assert await matching(factory, text="100%") == [wanted]


async def test_an_underscore_in_a_search_is_an_underscore(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, channel_name="axb")
    wanted = await a_session(factory, channel_name="a_b")
    assert await matching(factory, text="a_b") == [wanted]


async def test_a_channel_that_was_never_named_does_not_match_a_search(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`channel_name` is null for sessions from before the column. A null
    matches no pattern, which is right: the recording has no name to have
    been searched for."""
    await a_session(factory, channel_name=None)
    assert await matching(factory, text="retro") == []


async def test_a_tag_filter_selects_only_the_meetings_carrying_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await a_session(factory)
    await a_session(factory)
    await a_tag(factory, wanted, ANNA, "retro")
    assert await matching(factory, tags=["retro"]) == [wanted]


async def test_two_tags_select_the_meetings_carrying_both(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second chip narrows. Selecting "retro" and "kunde" and getting
    more rows than "retro" alone is the opposite of what pressing it
    looks like."""
    both = await a_session(factory)
    only_one = await a_session(factory)
    await a_tag(factory, both, ANNA, "retro")
    await a_tag(factory, both, ANNA, "kunde")
    await a_tag(factory, only_one, ANNA, "retro")
    assert await matching(factory, tags=["retro", "kunde"]) == [both]


async def test_a_tag_filter_never_matches_somebody_elses_label(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ours = await a_session(factory, people={ANNA: "anna", BEN: "ben"})
    await a_tag(factory, ours, BEN, "retro")
    assert await matching(factory, tags=["retro"]) == []


async def test_a_range_keeps_the_whole_of_the_day_it_ends_on(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Somebody who picks "to 21 August" means the whole of the 21st, and
    a bound at midnight silently drops the day they named."""
    late = await a_session(factory, started_at=datetime(2026, 8, 21, 23, 30, tzinfo=UTC))
    assert await matching(factory, since="2026-08-21", until="2026-08-21") == [late]


async def test_a_range_excludes_what_started_outside_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, started_at=datetime(2026, 8, 20, 23, 59, tzinfo=UTC))
    inside = await a_session(factory, started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    await a_session(factory, started_at=datetime(2026, 8, 22, 0, 1, tzinfo=UTC))
    assert await matching(factory, since="2026-08-21", until="2026-08-21") == [inside]


async def test_asking_for_recordings_with_a_protocol(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    documented = await a_session(factory, document_url="https://outline.example/d/1")
    await a_session(factory)
    assert await matching(factory, protocol="with") == [documented]


async def test_asking_for_recordings_without_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """How you find the meeting whose document never got written."""
    await a_session(factory, document_url="https://outline.example/d/1")
    undocumented = await a_session(factory)
    assert await matching(factory, protocol="without") == [undocumented]


async def test_filters_combine_by_narrowing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await a_session(
        factory,
        channel_name="weekly retro",
        started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        document_url="https://outline.example/d/1",
    )
    await a_session(
        factory, channel_name="weekly retro", started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
    )
    await a_session(
        factory,
        channel_name="standup",
        started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        document_url="https://outline.example/d/2",
    )
    found = await matching(
        factory, text="retro", since="2026-08-21", until="2026-08-21", protocol="with"
    )
    assert found == [wanted]


async def test_the_total_counts_what_the_filter_matched_and_not_the_history(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A total counted under a different filter than the rows is a list
    saying "1-20 of 47" over twelve results."""
    await a_session(factory, channel_name="weekly retro")
    await a_session(factory, channel_name="standup")
    page = await ConsoleQueries(factory).sessions_page(
        ANNA,
        limit=100,
        offset=0,
        matching=session_filter(text="retro", tags=[], since=None, until=None, protocol=None),
    )
    assert page.total == 1


async def test_a_filtered_list_is_still_paged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(3):
        await a_session(
            factory, channel_name="weekly retro", started_at=T0 + timedelta(hours=index)
        )
    await a_session(factory, channel_name="standup")
    page = await ConsoleQueries(factory).sessions_page(
        ANNA,
        limit=2,
        offset=0,
        matching=session_filter(text="retro", tags=[], since=None, until=None, protocol=None),
    )
    assert (len(page.sessions), page.total) == (2, 3)


# ---------------------------------------------------------------------------
# What a meeting is called, and searching for it by that
# ---------------------------------------------------------------------------


async def test_a_session_carries_what_somebody_named_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await a_session(factory, title="Sprint 34 planning", description="agenda")
    found = await ConsoleQueries(factory).session_for(ANNA, session_id)
    assert found is not None
    assert found.title == "Sprint 34 planning"
    assert found.description == "agenda"


async def test_a_session_nobody_has_named_reads_as_null(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await a_session(factory)
    found = await ConsoleQueries(factory).session_for(ANNA, session_id)
    assert found is not None
    assert found.title is None
    assert found.description is None


async def test_a_search_finds_a_meeting_by_what_it_was_called(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason somebody types a title onto a recording at all.

    A title is already in the response this same person gets for these
    sessions, so searching it narrows what they can see rather than
    widening it.
    """
    wanted = await a_session(factory, channel_name="allgemein", title="Sprint 34 planning")
    await a_session(factory, channel_name="allgemein", title="Retro")
    assert await matching(factory, text="sprint") == [wanted]


async def test_a_search_finds_a_meeting_by_what_was_written_about_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await a_session(factory, description="we decided to drop the second queue")
    await a_session(factory)
    assert await matching(factory, text="second queue") == [wanted]


async def test_a_search_over_a_title_ignores_the_case_it_was_typed_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    wanted = await a_session(factory, title="Sprint 34 Planning")
    assert await matching(factory, text="PLANNING") == [wanted]


async def test_a_title_a_colleague_typed_is_searchable_by_everybody_in_the_room(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The asymmetry with a tag, in one test. Ben's *label* is not Anna's
    to search by, because a label is a private remark; the meeting's
    *name* is shared, because it is what the meeting was."""
    wanted = await a_session(
        factory, title="Kunde OneLiteFeather", people={ANNA: "anna", BEN: "ben"}
    )
    assert await matching(factory, text="onelitefeather") == [wanted]


async def test_a_search_over_titles_never_reaches_a_session_you_were_not_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scope survives the filter. A search is a narrowing of what
    somebody may already see, never a way to reach past it."""
    await a_session(factory, title="Sprint 34 planning", people={BEN: "ben"})
    assert await matching(factory, text="sprint") == []


async def test_a_meeting_nobody_named_does_not_match_a_search(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A null matches no pattern, which is right: the recording has no
    name to have been searched for."""
    await a_session(factory, title=None, channel_name=None)
    assert await matching(factory, text="sprint") == []


async def test_a_percent_sign_in_a_title_search_is_a_percent_sign(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, title="planning")
    wanted = await a_session(factory, title="100% agreed")
    assert await matching(factory, text="100%") == [wanted]


async def test_a_search_still_does_not_reach_a_transcript(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The one thing extending the search must not have done.

    A transcript index turns spoken words into a lookup key, which is a
    use of a colleague's voice nobody agreed to when they consented to
    being recorded. The console says so on screen and this is what keeps
    it true.
    """
    session_id = await a_session(factory, title="Retro", channel_name="allgemein")
    await a_track(factory, session_id, ANNA, transcript=words("zeitverschwendung"))
    assert await matching(factory, text="zeitverschwendung") == []


# ---------------------------------------------------------------------------
# What each track is, as a file
# ---------------------------------------------------------------------------


async def test_a_session_carries_what_each_track_is_as_a_file(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Read from the row the worker wrote, so a metadata tab costs the
    statement the page already issues rather than a ranged GET and a
    chunk decrypt per track."""
    session_id = await a_session(factory)
    await a_track(factory, session_id, ANNA, sample_rate=16_000, channels=1, stored_bytes=4096)
    found = await ConsoleQueries(factory).session_for(ANNA, session_id)
    assert found is not None
    assert (found.tracks[0].sample_rate, found.tracks[0].channels) == (16_000, 1)
    assert found.tracks[0].stored_bytes == 4096


async def test_a_track_from_before_those_columns_reads_as_null_not_zero(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nullable with no backfill, exactly as `audio_seconds` was: a row
    that predates the columns has audio that may already be deleted, so
    there is nothing left to read a header from."""
    session_id = await a_session(factory)
    await a_track(factory, session_id, ANNA, sample_rate=None, channels=None, stored_bytes=None)
    found = await ConsoleQueries(factory).session_for(ANNA, session_id)
    assert found is not None
    assert found.tracks[0].sample_rate is None
    assert found.tracks[0].channels is None
    assert found.tracks[0].stored_bytes is None
