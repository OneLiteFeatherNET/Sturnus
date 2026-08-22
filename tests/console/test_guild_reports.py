"""The report's reads, against the real database.

Against PostgreSQL rather than a double because every property worth
pinning is a property of the statements: that the whole answer is scoped
to one guild, that `COUNT(DISTINCT ...)` counts people rather than
participations, and that `SUM` skipping nulls is accounted for rather than
silently absorbed.

The arithmetic on top of these rows is `sturnus.console.reporting` and is
tested there without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.adapters import ConsoleGuildReports
from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    TranscriptionJob,
)

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


def reports(
    factory: async_sessionmaker[AsyncSession], admins: Admins | None = None
) -> ConsoleGuildReports:
    return ConsoleGuildReports(factory, admins or Admins(), ConfigStore(factory))


async def a_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: int = GUILD,
    started_at: datetime = T0,
    ended_at: datetime | None = None,
    status: str = "documented",
    people: dict[int, str] | None = None,
    speech: dict[int, float | None] | None = None,
) -> int:
    """One session with participants and, optionally, measured jobs.

    `speech` is separate from `people` because the two really are: a
    participant row and a `transcription_job` row are written by different
    parts of the pipeline, and a person can appear in a meeting whose
    recording was never measured.
    """
    async with factory() as db:
        session = Session(
            guild_id=guild_id,
            channel_id=555,
            channel_name="meeting",
            started_at=started_at,
            ended_at=ended_at if ended_at is not None else started_at + timedelta(hours=1),
            status=status,
        )
        db.add(session)
        await db.flush()
        for discord_user_id, name in (people or {}).items():
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    discord_display_name=name,
                    first_seen_at=started_at,
                )
            )
        for discord_user_id, speech_seconds in (speech or {}).items():
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    s3_key=f"sessions/{session.id}/speakers/{discord_user_id}.enc",
                    encryption_key_id="k1",
                    wrapped_data_key=b"wrapped",
                    retention_until=started_at + timedelta(days=30),
                    status="done",
                    attempts=1,
                    audio_seconds=None if speech_seconds is None else speech_seconds * 3,
                    speech_seconds=speech_seconds,
                )
            )
        await db.commit()
        return session.id


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_an_administrator_of_the_guild_gets_its_recording(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, people={BEN: "ben"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert len(recording.sessions) == 1


async def test_an_administrator_of_another_guild_is_nobody_here(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, people={BEN: "ben"})
    admins = Admins({OTHER_GUILD: {CARL}})

    assert await reports(factory, admins).recording_of(GUILD, requested_by=CARL) is None


async def test_a_participant_who_administers_nothing_gets_no_report(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, people={BEN: "ben"})

    assert await reports(factory).recording_of(GUILD, requested_by=BEN) is None


# ---------------------------------------------------------------------------
# Scoped to one guild, all the way down
# ---------------------------------------------------------------------------


async def test_another_guild_s_sessions_are_not_in_this_guild_s_report(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, guild_id=OTHER_GUILD, people={BEN: "ben"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.sessions == ()
    assert recording.distinct_participants == 0


async def test_somebody_in_two_guilds_is_counted_once_in_each(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The distinct count is over this guild's sessions, so a person who
    # meets in two guilds is one person in each rather than two in either.
    await a_session(factory, people={BEN: "ben"})
    await a_session(factory, guild_id=OTHER_GUILD, people={BEN: "ben", CARL: "carl"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.distinct_participants == 1


# ---------------------------------------------------------------------------
# Counting people, not participations
# ---------------------------------------------------------------------------


async def test_a_person_in_four_meetings_is_one_distinct_participant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`COUNT(DISTINCT ...)` and not `COUNT(*)`.

    "How many different people has this guild recorded" is a fact about
    the guild; counting participations instead would answer a question
    nobody asked and answer it with a bigger number.
    """
    for day in range(4):
        await a_session(factory, started_at=T0 - timedelta(days=day), people={BEN: "ben"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.distinct_participants == 1
    assert len(recording.sessions) == 4


async def test_each_session_carries_how_many_people_were_in_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, people={ANNA: "anna", BEN: "ben", CARL: "carl"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.sessions[0].participants == 3


async def test_the_sessions_carry_counts_and_never_identities(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The boundary, checked on the value rather than on the payload.

    A report module handed a list of who attended is one edit away from
    ranking them, so the identities are not carried out of the statement
    at all.
    """
    await a_session(factory, people={BEN: "ben"}, speech={BEN: 120.0})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    fields = vars(recording.sessions[0])
    assert not [name for name in fields if "user" in name or "name" in name]


# ---------------------------------------------------------------------------
# Null is not zero
# ---------------------------------------------------------------------------


async def test_a_track_nobody_measured_is_counted_as_unmeasured(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`SUM` skips nulls silently, so the skipped rows are counted beside it.

    Otherwise "we never measured this" and "they said nothing" arrive as
    the same total, and every job that predates the measurement columns
    quietly makes a guild look quieter than it was.
    """
    await a_session(factory, people={ANNA: "anna", BEN: "ben"}, speech={ANNA: 60.0, BEN: None})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    session = recording.sessions[0]
    assert session.tracks == 2
    assert session.speech_seconds == 60.0
    assert session.unmeasured_tracks == 1


async def test_a_session_whose_tracks_were_all_unmeasured_has_no_total_at_all(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # `None` rather than `0.0`: nobody measured, which is a different fact
    # from measuring and finding nothing.
    await a_session(factory, people={BEN: "ben"}, speech={BEN: None})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.sessions[0].speech_seconds is None
    assert recording.sessions[0].unmeasured_tracks == 1


async def test_a_session_with_no_recordings_reports_no_tracks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A meeting where nobody consented produces participants and no jobs,
    # and the report must not read that as a measurement of zero.
    await a_session(factory, people={BEN: "ben"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.sessions[0].tracks == 0
    assert recording.sessions[0].speech_seconds is None


# ---------------------------------------------------------------------------
# What the session rows say
# ---------------------------------------------------------------------------


async def test_a_session_that_produced_a_protocol_is_marked_as_documented(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_session(factory, status="documented", people={BEN: "ben"})
    await a_session(factory, started_at=T0 + timedelta(days=1), status="closed", people={BEN: "b"})

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert [session.documented for session in recording.sessions] == [True, False]


async def test_a_session_still_running_comes_back_without_an_end(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        db.add(
            Session(
                guild_id=GUILD,
                channel_id=555,
                started_at=T0,
                ended_at=None,
                status="open",
            )
        )
        await db.commit()

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.sessions[0].ended_at is None
    assert recording.sessions[0].duration_seconds is None


# ---------------------------------------------------------------------------
# The guild's own calendar
# ---------------------------------------------------------------------------


async def test_the_guild_s_configured_timezone_is_what_the_months_are_cut_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await ConfigStore(factory).set(GUILD, settings.TIMEZONE, "Europe/Berlin", T0)

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.zone == ZoneInfo("Europe/Berlin")
    assert recording.zone_name == "Europe/Berlin"


async def test_a_guild_that_never_set_a_timezone_gets_the_configured_default(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`ConfigStore.snapshot` resolves `DEFAULTS`, so unset is not absent.

    `timezone` defaults to Europe/Berlin, which is what this deployment's
    guilds actually meet in -- so the months of a guild nobody configured
    are cut in the same calendar its protocols are written in, rather than
    in UTC. Reaching UTC here would mean somebody set something odd, which
    is the case below.
    """
    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.zone_name == settings.DEFAULTS[settings.TIMEZONE]


async def test_an_unusable_timezone_falls_back_rather_than_failing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same fallback the worker applies when writing a protocol.

    A report with the wrong month boundary is a smaller loss than no
    report, and the value that caused it is one `/config set` from being
    fixed. Naming the zone in the answer is what tells the reader it
    happened.
    """
    await ConfigStore(factory).set(GUILD, settings.TIMEZONE, "Mars/Olympus_Mons", T0)

    recording = await reports(factory).recording_of(GUILD, requested_by=ANNA)

    assert recording is not None
    assert recording.zone_name == "UTC"
