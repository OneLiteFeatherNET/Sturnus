"""The console's read endpoints, over real HTTP.

What is tested here is the half that only exists at the HTTP boundary:
the status codes, the query-string parsing, and that every handler asks
its collaborator about the *signed-in* user rather than about anything
the request could name. The scoping itself is a property of the SQL and
lives in `tests/console/test_queries.py`; the shaping is pure and lives
in `tests/console/test_statistics.py`.

The one thing that would be invisible in either of those: a session id in
the path is a number a caller chose, and no handler may turn it into a
lookup that is not scoped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.session import SessionCookie, SignedSession
from sturnus.console.statistics import AttendedSession, Participant, Track
from tests.console.conftest import (
    ANNA,
    BEN,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeAdmins,
    FakeLinks,
    FakeOAuth,
    FakeReads,
    FakeStates,
    build_test_api,
    now_at,
)

SESSION_COOKIE = "sturnus_session"
CHANNEL = 555

READ_PATHS = [
    "/api/dashboard",
    "/api/sessions",
    "/api/sessions/1",
    "/api/calendar?year=2026",
    "/api/calendar/2026-08-21",
]


def app(reads: FakeReads | None = None) -> web.Application:
    return build_test_api(
        oauth=FakeOAuth(),
        states=FakeStates(),
        links=FakeLinks(),
        admins=FakeAdmins(),
        reads=reads or FakeReads(),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
        schema_ready=True,
    )


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory,
    reads: FakeReads | None = None,
    as_user: int = ANNA,
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app(reads))
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def attended(
    session_id: int = 1,
    *,
    started_at: datetime = T0,
    ended_at: datetime | None = None,
    channel_id: int = CHANNEL,
    channel_name: str | None = "meeting",
    document_url: str | None = None,
    participants: tuple[Participant, ...] = (Participant(ANNA, "anna"), Participant(BEN, "ben")),
    tracks: tuple[Track, ...] = (),
) -> AttendedSession:
    return AttendedSession(
        id=session_id,
        channel_id=channel_id,
        channel_name=channel_name,
        started_at=started_at,
        ended_at=ended_at,
        document_url=document_url,
        participants=participants,
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# Nothing is readable without a session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", READ_PATHS)
async def test_a_read_without_a_session_is_unauthorised(
    aiohttp_client: AiohttpClientFactory, path: str
) -> None:
    """Every one of them, by name. A list rather than a middleware with a
    path allowlist, for the same reason `require_session` is a decorator:
    the failure mode of forgetting an entry in such a list is an endpoint
    that is silently public.
    """
    client = await aiohttp_client(app())
    assert (await client.get(path)).status == 401


@pytest.mark.parametrize("path", READ_PATHS)
async def test_a_read_with_a_forged_cookie_is_unauthorised(
    aiohttp_client: AiohttpClientFactory, path: str
) -> None:
    client = await aiohttp_client(app())
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: "forged.token"})
    assert (await client.get(path)).status == 401


@pytest.mark.parametrize("path", READ_PATHS)
async def test_a_read_asks_only_about_the_signed_in_user(
    aiohttp_client: AiohttpClientFactory, path: str
) -> None:
    """The scope is the cookie, and nothing in the request can widen it."""
    reads = FakeReads(sessions=(attended(1),))
    client = await signed_in(aiohttp_client, reads, as_user=BEN)
    await client.get(path)
    assert reads.asked_for
    assert set(reads.asked_for) == {BEN}


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


async def test_the_dashboard_reports_what_this_person_accumulated(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    sessions = (
        attended(
            1,
            started_at=datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
            document_url="https://outline.example/doc/a",
            tracks=(Track(ANNA, "anna", 600.0, 90.0, 7),),
        ),
        attended(
            2,
            started_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
            tracks=(Track(ANNA, "anna", 300.0, 10.0, 2),),
        ),
    )
    transcripts = ['{"language": "de", "segments": [{"start": 0, "end": 1, "text": "a b c"}]}']
    client = await signed_in(aiohttp_client, FakeReads(sessions, transcripts))

    body = await (await client.get("/api/dashboard")).json()

    assert body["total_speech_seconds"] == 100.0
    assert body["sessions_attended"] == 2
    assert body["sessions_with_protocol"] == 1
    assert body["people_spoken_with"] == 1
    assert body["words_transcribed"] == 3
    assert body["longest_session"]["id"] == "1"
    assert body["first_session"]["id"] == "1"
    assert body["most_recent_session"]["id"] == "2"


async def test_a_dashboard_for_somebody_with_no_recordings_is_still_a_dashboard(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Somebody who ran `/link` and has not been in a meeting yet is the
    ordinary first visit, and an empty console is the correct answer.
    """
    client = await signed_in(aiohttp_client, FakeReads())
    response = await client.get("/api/dashboard")
    assert response.status == 200
    assert (await response.json())["sessions_attended"] == 0


# ---------------------------------------------------------------------------
# The sessions
# ---------------------------------------------------------------------------


async def test_the_session_list_carries_who_else_was_there_and_what_was_measured(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    session = attended(
        3,
        started_at=T0,
        ended_at=T0 + timedelta(minutes=45),
        channel_name="standup",
        document_url="https://outline.example/doc/x",
        tracks=(Track(ANNA, "anna", 2700.0, 120.0, 9),),
    )
    client = await signed_in(aiohttp_client, FakeReads((session,)))

    body = await (await client.get("/api/sessions")).json()

    assert len(body["sessions"]) == 1
    listed = body["sessions"][0]
    assert listed["id"] == "3"
    assert listed["channel_name"] == "standup"
    assert listed["duration_seconds"] == 2700.0
    assert listed["document_url"] == "https://outline.example/doc/x"
    assert listed["other_participants"] == [{"discord_user_id": "200", "display_name": "ben"}]
    assert listed["tracks"][0]["speech_seconds"] == 120.0


async def test_every_id_in_a_session_is_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A Discord snowflake exceeds JavaScript's safe integer range, and a
    JSON number silently loses its last digits there.
    """
    speaker = 386950399101370374
    session = attended(
        9,
        channel_id=386950399101370375,
        participants=(Participant(speaker, "ben"),),
        tracks=(Track(speaker, "ben", 1.0, 1.0, 1),),
    )
    client = await signed_in(aiohttp_client, FakeReads((session,)))

    listed = (await (await client.get("/api/sessions")).json())["sessions"][0]

    assert listed["id"] == "9"
    assert listed["channel_id"] == "386950399101370375"
    assert listed["other_participants"][0]["discord_user_id"] == str(speaker)
    assert listed["tracks"][0]["discord_user_id"] == str(speaker)


async def test_one_session_is_readable_on_its_own(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, FakeReads((attended(4),)))
    response = await client.get("/api/sessions/4")
    assert response.status == 200
    assert (await response.json())["id"] == "4"


async def test_a_session_you_were_not_in_is_not_reachable(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404 rather than 403, and the same 404 an unknown id gets.

    A different answer for "exists but not yours" would let somebody walk
    the id space and learn which sessions the system holds -- which is
    itself something they were never in.
    """
    client = await signed_in(aiohttp_client, FakeReads((attended(4),)))
    assert (await client.get("/api/sessions/5")).status == 404


async def test_a_session_id_that_is_not_a_number_is_not_a_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, FakeReads((attended(4),)))
    assert (await client.get("/api/sessions/four")).status == 404


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


async def test_a_year_comes_back_as_one_entry_per_day_that_had_recordings(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    sessions = (
        attended(
            1,
            started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 21, 9, 30, tzinfo=UTC),
        ),
        attended(
            2,
            started_at=datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 21, 17, 30, tzinfo=UTC),
        ),
        attended(
            3,
            started_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 22, 9, 15, tzinfo=UTC),
        ),
    )
    reads = FakeReads(sessions)
    client = await signed_in(aiohttp_client, reads)

    body = await (await client.get("/api/calendar?year=2026")).json()

    assert reads.years == [2026]
    assert body["year"] == 2026
    assert body["days"] == [
        {
            "date": "2026-08-21",
            "sessions": 2,
            "total_duration_seconds": 3600.0,
            "participants": 2,
        },
        {
            "date": "2026-08-22",
            "sessions": 1,
            "total_duration_seconds": 900.0,
            "participants": 2,
        },
    ]


async def test_a_calendar_without_a_year_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """No default. Guessing "this year" would show an empty heatmap to
    somebody whose meetings were all last year, with nothing on the page
    to say which year they are looking at.
    """
    client = await signed_in(aiohttp_client, FakeReads())
    assert (await client.get("/api/calendar")).status == 400


@pytest.mark.parametrize("year", ["last", "2026.5", "0", "-1", ""])
async def test_a_year_that_is_not_a_year_is_refused(
    aiohttp_client: AiohttpClientFactory, year: str
) -> None:
    """`0` and `-1` are here because they parse as integers and are not
    dates -- `datetime(0, 1, 1)` raises, and a 500 is not an answer to a
    malformed query string.
    """
    client = await signed_in(aiohttp_client, FakeReads())
    assert (await client.get(f"/api/calendar?year={year}")).status == 400


async def test_a_day_comes_back_as_a_timeline(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    sessions = (
        attended(
            1,
            started_at=datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 21, 17, 30, tzinfo=UTC),
            channel_name="retro",
        ),
        attended(
            2,
            started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 21, 9, 15, tzinfo=UTC),
            channel_name="standup",
        ),
    )
    reads = FakeReads(sessions)
    client = await signed_in(aiohttp_client, reads)

    body = await (await client.get("/api/calendar/2026-08-21")).json()

    assert reads.days == [datetime(2026, 8, 21, tzinfo=UTC).date()]
    assert body["date"] == "2026-08-21"
    assert [entry["channel_name"] for entry in body["sessions"]] == ["standup", "retro"]
    assert body["sessions"][0]["started_at"] == "2026-08-21T09:00:00+00:00"
    assert body["sessions"][0]["duration_seconds"] == 900.0


@pytest.mark.parametrize("day", ["yesterday", "2026-13-01", "2026-08", "21-08-2026"])
async def test_a_day_that_is_not_a_date_is_refused(
    aiohttp_client: AiohttpClientFactory, day: str
) -> None:
    client = await signed_in(aiohttp_client, FakeReads())
    assert (await client.get(f"/api/calendar/{day}")).status == 400


async def test_a_refusal_never_repeats_what_was_asked_for(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The same rule the callback endpoint follows: no user input is
    reflected into a response, ever. It is the only way to keep an
    endpoint from becoming an XSS sink for whoever renders its errors.
    """
    client = await signed_in(aiohttp_client, FakeReads())
    response = await client.get("/api/calendar?year=<script>alert(1)</script>")
    assert response.status == 400
    assert "script" not in await response.text()
