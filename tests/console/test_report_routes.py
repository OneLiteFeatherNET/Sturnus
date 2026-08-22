"""Who may read a guild's report, and what it is allowed to contain.

The arithmetic is `sturnus.console.reporting` and is pinned there without
a database. What is pinned here is the endpoint: that the id reaching the
reports comes out of the signed cookie rather than out of the URL, that a
guild somebody does not administer is indistinguishable from one that does
not exist, and -- the test worth having most -- that the payload names
nobody.

That last one is a boundary rather than an implementation detail. A
per-person readout of meeting attendance and speaking time is a means of
monitoring performance and conduct, which is a works-council matter rather
than a field that appears in a payload because the columns were there. A
test that would fail the moment a name appeared is how it stays a decision
somebody has to take on purpose.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import GuildRecording
from sturnus.console.reporting import RecordedSession
from sturnus.console.session import SessionCookie, SignedSession
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeReports,
    build_test_api,
)

BERLIN = ZoneInfo("Europe/Berlin")


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def report_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/report"


def a_session(**over: object) -> RecordedSession:
    base: dict[str, object] = {
        "id": 1,
        "started_at": T0,
        "ended_at": T0 + timedelta(hours=1),
        "documented": True,
        "participants": 4,
        "tracks": 4,
        "audio_seconds": 900.0,
        "speech_seconds": 300.0,
        "unmeasured_tracks": 0,
    }
    base.update(over)
    return RecordedSession(**base)  # type: ignore[arg-type]


def recording(*sessions: RecordedSession, distinct: int = 6) -> GuildRecording:
    return GuildRecording(
        sessions=sessions or (a_session(),),
        distinct_participants=distinct,
        zone=BERLIN,
        zone_name="Europe/Berlin",
    )


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_an_administrator_sees_what_their_guild_has_recorded(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(reports=FakeReports(recording())))

    response = await client.get(report_url())

    assert response.status == 200
    body = await response.json()
    assert body["sessions"] == 1
    assert body["recorded_seconds"] == 3600


async def test_the_report_is_asked_for_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    reports = FakeReports(recording())
    client = await signed_in(aiohttp_client, build_test_api(reports=reports), as_user=BEN)

    await client.get(report_url())

    assert reports.asked == [(GUILD, BEN)]


async def test_a_guild_this_person_does_not_administer_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # The report says when a guild meets and how often, which is a
    # description of a team's working week. A 403 would confirm that such
    # a description exists here.
    client = await signed_in(aiohttp_client, build_test_api(reports=FakeReports()))

    response = await client.get(report_url())

    assert response.status == 404
    assert (await response.json())["error"] == "no such guild"


async def test_a_guild_id_that_is_not_a_number_never_reaches_the_reports(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    reports = FakeReports(recording())
    client = await signed_in(aiohttp_client, build_test_api(reports=reports))

    assert (await client.get(report_url("nope"))).status == 404
    assert reports.asked == []


async def test_the_report_needs_a_session_like_every_other_endpoint(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(reports=FakeReports(recording())))
    assert (await client.get(report_url())).status == 401


# ---------------------------------------------------------------------------
# What it may contain, and what it may not
# ---------------------------------------------------------------------------


async def test_the_report_names_nobody(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The test this endpoint exists to keep passing.

    A guild report is about a guild. The moment a Discord id or a display
    name appears in it, it has become a per-person record of who attends
    which meetings -- and that is a decision for a works council, not a
    field somebody added because the column was already selected.
    """
    reports = FakeReports(recording(a_session(participants=9), distinct=12))
    client = await signed_in(aiohttp_client, build_test_api(reports=reports))

    raw = await (await client.get(report_url())).text()
    body = json.loads(raw)

    assert body["largest_meeting"] == 9
    assert body["distinct_participants"] == 12
    # Not a check of the keys alone: a name would arrive as a value.
    assert "discord_user_id" not in raw
    assert "display_name" not in raw


async def test_the_guild_id_travels_as_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(reports=FakeReports(recording())))

    assert (await (await client.get(report_url())).json())["guild_id"] == str(GUILD)


async def test_the_months_say_which_calendar_they_were_cut_in(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # A meeting that opened at half past midnight in Berlin belongs to the
    # month the people in it think it does, and a reader who is not told
    # which calendar was used will assume theirs.
    late = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
    reports = FakeReports(recording(a_session(started_at=late, ended_at=None)))
    client = await signed_in(aiohttp_client, build_test_api(reports=reports))

    body = await (await client.get(report_url())).json()

    assert body["timezone"] == "Europe/Berlin"
    assert [month["month"] for month in body["months"]] == ["2026-09"]


async def test_a_guild_that_has_never_recorded_gets_a_report_saying_so(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # Not a 404: "you administer this guild and it has recorded nothing"
    # and "this is not your guild" are different answers.
    empty = GuildRecording(sessions=(), distinct_participants=0, zone=UTC, zone_name="UTC")
    client = await signed_in(aiohttp_client, build_test_api(reports=FakeReports(empty)))

    response = await client.get(report_url())

    assert response.status == 200
    body = await response.json()
    assert body["sessions"] == 0
    assert body["average_duration_seconds"] is None


async def test_nothing_in_between_may_cache_a_report(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(reports=FakeReports(recording())))

    response = await client.get(report_url())

    assert response.headers["Cache-Control"] == "private, no-store"
