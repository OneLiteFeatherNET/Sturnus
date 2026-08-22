"""The attendance ranking: its order, its shape, and who may read it.

Three files' worth of subject in one, because the feature is small and its
properties are not independent of each other. What is pinned:

- the ordering is total and stable, so a list of named colleagues does not
  appear to reshuffle itself between two page loads;
- speaking time is not the sort key, and never becomes one by accident;
- null is not zero, so somebody whose recordings predate the measurement
  columns does not read as silent;
- reading it is logged, which is the one thing that makes this feature
  reviewable after the fact.

The last of those is the test most worth having. This is the only endpoint
in the console that names other people and orders them, and the log line
is the entire answer to "who looked at the attendance ranking, and when".
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.adapters import ConsoleParticipationReports
from sturnus.console.app import SESSION_COOKIE
from sturnus.console.participation import Attendance, ParticipationJson, participation
from sturnus.console.ports import GuildParticipation
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    TranscriptionJob,
)
from sturnus.observability.events import Event
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeParticipation,
    build_test_api,
)

CARL = 300
OTHER_GUILD = 9999


def someone(**over: object) -> Attendance:
    base: dict[str, object] = {
        "discord_user_id": BEN,
        "display_name": "ben",
        "sessions": 3,
        "speech_seconds": 600.0,
        "unmeasured_tracks": 0,
        "first_seen_at": T0 - timedelta(days=30),
        "last_seen_at": T0,
    }
    base.update(over)
    return Attendance(**base)  # type: ignore[arg-type]


def ranked(*people: Attendance, sessions: int = 10) -> ParticipationJson:
    """The shaped payload, typed as the contract rather than as a dict.

    Keeping `ParticipationJson` means these tests type-check against the
    shape the API actually promises: a key renamed in the TypedDict and
    not here is a red type check rather than a green test asserting on a
    key that no longer exists.
    """
    return participation(people, guild_id=GUILD, sessions=sessions)


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


def test_the_most_meetings_come_first() -> None:
    order = ranked(
        someone(discord_user_id=1, display_name="anna", sessions=2),
        someone(discord_user_id=2, display_name="ben", sessions=9),
    )

    assert [person["display_name"] for person in order["people"]] == ["ben", "anna"]


def test_speaking_time_is_not_the_order_and_does_not_become_it() -> None:
    """Ordering people by how much they talked is a different claim.

    "Was present most often" and "talked most" are not the same statement
    about a colleague, and the second is the one that reads as a
    judgement. It is reported and never sorted on.
    """
    order = ranked(
        someone(discord_user_id=1, display_name="anna", sessions=9, speech_seconds=10.0),
        someone(discord_user_id=2, display_name="ben", sessions=2, speech_seconds=99_999.0),
    )

    assert [person["display_name"] for person in order["people"]] == ["anna", "ben"]


def test_a_tie_breaks_on_the_name_rather_than_on_the_row_order() -> None:
    # Left to the order rows came back in, a list of named colleagues would
    # appear to reshuffle itself between two page loads.
    order = ranked(
        someone(discord_user_id=3, display_name="carl", sessions=4),
        someone(discord_user_id=1, display_name="anna", sessions=4),
        someone(discord_user_id=2, display_name="ben", sessions=4),
    )

    assert [person["display_name"] for person in order["people"]] == ["anna", "ben", "carl"]


def test_somebody_the_system_has_no_name_for_sorts_after_everyone_named() -> None:
    # A row of eighteen digits at the head of a tie is the least useful
    # place for it in a list people read top-down.
    order = ranked(
        someone(discord_user_id=1, display_name=None, sessions=4),
        someone(discord_user_id=2, display_name="ben", sessions=4),
    )

    assert [person["display_name"] for person in order["people"]] == ["ben", None]


def test_two_people_with_the_same_name_still_have_a_stable_order() -> None:
    order = ranked(
        someone(discord_user_id=22, display_name="ben", sessions=4),
        someone(discord_user_id=11, display_name="ben", sessions=4),
    )

    assert [person["discord_user_id"] for person in order["people"]] == ["11", "22"]


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def test_every_discord_id_travels_as_a_string() -> None:
    big = 308_000_000_000_000_001
    order = ranked(someone(discord_user_id=big))

    assert order["guild_id"] == str(GUILD)
    assert order["people"][0]["discord_user_id"] == str(big)


def test_the_number_of_sessions_the_ranking_is_over_travels_with_it() -> None:
    """A rank means nothing without it.

    "In eleven meetings" is one claim out of twelve and quite another out
    of four hundred, and a bare rank invites the first reading regardless.
    """
    assert ranked(someone(sessions=11), sessions=400)["sessions"] == 400


def test_a_track_nobody_measured_is_not_reported_as_silence() -> None:
    # Null means nobody ever measured; zero means somebody did and it was
    # nothing. A person whose recordings predate the measurement columns
    # must not read as having said nothing.
    order = ranked(someone(speech_seconds=None, unmeasured_tracks=4))

    assert order["people"][0]["speech_seconds"] is None
    assert order["people"][0]["unmeasured_tracks"] == 4


def test_an_empty_guild_ranks_nobody() -> None:
    assert ranked(sessions=0)["people"] == []


# ---------------------------------------------------------------------------
# The endpoint, and its audit line
# ---------------------------------------------------------------------------


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/report/participation"


def _events(caplog: pytest.LogCaptureFixture, event: Event) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "sturnus_event", None) == str(event)]


async def test_an_administrator_sees_who_took_part_in_the_most_meetings(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    people = FakeParticipation(GuildParticipation((someone(),), sessions=10))
    client = await signed_in(aiohttp_client, build_test_api(participation=people))

    response = await client.get(url())

    assert response.status == 200
    body = await response.json()
    assert body["people"][0]["sessions"] == 3
    assert body["sessions"] == 10


async def test_the_ranking_is_asked_for_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    people = FakeParticipation(GuildParticipation((), sessions=0))
    client = await signed_in(aiohttp_client, build_test_api(participation=people), as_user=BEN)

    await client.get(url())

    assert people.asked == [(GUILD, BEN)]


async def test_a_guild_this_person_does_not_administer_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(participation=FakeParticipation()))

    response = await client.get(url())

    assert response.status == 404


async def test_reading_the_ranking_is_written_down(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one read in this console that leaves a trace, on purpose.

    An ordered list of colleagues by meeting attendance is subject to
    co-determination, and "who looked at it, and when" is the first
    question anybody reviewing the arrangement will ask. Without this line
    there is no answer to it at all.
    """
    people = FakeParticipation(GuildParticipation((someone(), someone(discord_user_id=CARL)), 10))
    client = await signed_in(aiohttp_client, build_test_api(participation=people), as_user=ANNA)

    with caplog.at_level(logging.INFO):
        await client.get(url())

    lines = _events(caplog, Event.CONSOLE_PARTICIPATION_READ)
    assert len(lines) == 1
    fields = lines[0].sturnus_fields  # type: ignore[attr-defined]
    assert fields["guild_id"] == GUILD
    assert fields["requested_by"] == ANNA
    assert fields["participants"] == 2


async def test_the_audit_line_never_names_the_people_in_the_list(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The list is the thing under discussion; copying it into a retained,
    # searchable log store would be making a second copy of it.
    people = FakeParticipation(GuildParticipation((someone(),), 10))
    client = await signed_in(aiohttp_client, build_test_api(participation=people))

    with caplog.at_level(logging.INFO):
        await client.get(url())

    fields = _events(caplog, Event.CONSOLE_PARTICIPATION_READ)[0].sturnus_fields  # type: ignore[attr-defined]
    assert "discord_user_id" not in fields
    assert "display_name" not in fields


async def test_a_refusal_is_not_an_audit_line(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing was disclosed and nobody was authorised. A line here would
    # let anybody with a session fill the audit trail with guild ids of
    # their choosing.
    client = await signed_in(aiohttp_client, build_test_api(participation=FakeParticipation()))

    with caplog.at_level(logging.INFO):
        await client.get(url())

    assert not _events(caplog, Event.CONSOLE_PARTICIPATION_READ)


async def test_the_ranking_needs_a_session_like_every_other_endpoint(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    people = FakeParticipation(GuildParticipation((someone(),), 10))
    client = await aiohttp_client(build_test_api(participation=people))

    assert (await client.get(url())).status == 401


async def test_nothing_in_between_may_cache_the_ranking(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    people = FakeParticipation(GuildParticipation((someone(),), 10))
    client = await signed_in(aiohttp_client, build_test_api(participation=people))

    response = await client.get(url())

    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# The reads, against the real database
# ---------------------------------------------------------------------------


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
) -> ConsoleParticipationReports:
    return ConsoleParticipationReports(factory, admins or Admins())


async def a_meeting(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: int = GUILD,
    started_at: datetime = T0,
    people: dict[int, str] | None = None,
    speech: dict[int, float | None] | None = None,
) -> int:
    async with factory() as db:
        session = Session(
            guild_id=guild_id,
            channel_id=555,
            channel_name="meeting",
            started_at=started_at,
            ended_at=started_at + timedelta(hours=1),
            status="documented",
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
                    speech_seconds=speech_seconds,
                )
            )
        await db.commit()
        return session.id


async def test_somebody_who_administers_nothing_gets_no_ranking(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_meeting(factory, people={BEN: "ben"})

    assert await reports(factory).attendance_in(GUILD, requested_by=BEN) is None


async def test_an_administrator_of_another_guild_is_nobody_here(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_meeting(factory, people={BEN: "ben"})
    admins = Admins({OTHER_GUILD: {CARL}})

    assert await reports(factory, admins).attendance_in(GUILD, requested_by=CARL) is None


async def test_attendance_is_counted_across_this_guild_s_meetings(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    for day in range(3):
        await a_meeting(factory, started_at=T0 - timedelta(days=day), people={BEN: "ben"})
    await a_meeting(factory, started_at=T0 - timedelta(days=9), people={CARL: "carl"})

    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert {person.discord_user_id: person.sessions for person in found.people} == {
        BEN: 3,
        CARL: 1,
    }
    assert found.sessions == 4


async def test_one_person_cannot_be_counted_twice_for_one_meeting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`uq_participant_per_session` is what makes this true, not the query.

    Worth a test all the same: the number being defended is a ranking of
    colleagues, and if that constraint were ever relaxed a plain count
    would quietly turn it into a ranking by how often somebody's
    connection dropped. The statement counts distinct sessions so the
    query does not depend on the schema for a property this important.
    """
    async with factory() as db:
        db.add(
            Session(
                guild_id=GUILD,
                channel_id=555,
                started_at=T0,
                ended_at=T0 + timedelta(hours=1),
                status="documented",
            )
        )
        await db.commit()

    with pytest.raises(IntegrityError):
        async with factory() as db:
            for _ in range(2):
                db.add(
                    SessionParticipant(
                        session_id=1,
                        discord_user_id=BEN,
                        discord_display_name="ben",
                        first_seen_at=T0,
                    )
                )
            await db.commit()


async def test_meetings_in_another_guild_do_not_count_here(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_meeting(factory, people={BEN: "ben"})
    await a_meeting(factory, guild_id=OTHER_GUILD, people={BEN: "ben"})

    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert found.people[0].sessions == 1
    assert found.sessions == 1


async def test_speaking_time_sums_only_this_guild_s_recordings(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_meeting(factory, people={BEN: "ben"}, speech={BEN: 120.0})
    await a_meeting(factory, guild_id=OTHER_GUILD, people={BEN: "ben"}, speech={BEN: 9_000.0})

    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert found.people[0].speech_seconds == 120.0


async def test_a_person_whose_recordings_were_never_measured_has_no_speaking_total(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_meeting(factory, people={BEN: "ben"}, speech={BEN: None})

    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert found.people[0].speech_seconds is None
    assert found.people[0].unmeasured_tracks == 1


async def test_a_person_gets_the_name_they_last_appeared_under(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_meeting(factory, started_at=T0 - timedelta(days=9), people={BEN: "old name"})
    await a_meeting(factory, started_at=T0 - timedelta(days=1), people={BEN: "ben"})

    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert found.people[0].display_name == "ben"


async def test_when_somebody_was_first_and_last_in_a_meeting_is_reported(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = T0 - timedelta(days=90)
    await a_meeting(factory, started_at=first, people={BEN: "ben"})
    await a_meeting(factory, started_at=T0, people={BEN: "ben"})

    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert (found.people[0].first_seen_at, found.people[0].last_seen_at) == (first, T0)


async def test_a_guild_that_has_never_met_ranks_nobody(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    found = await reports(factory).attendance_in(GUILD, requested_by=ANNA)

    assert found is not None
    assert found.people == ()
    assert found.sessions == 0
