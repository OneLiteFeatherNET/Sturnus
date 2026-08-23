"""The recording page's own endpoints, over real HTTP.

What is tested here is the half that only exists at the HTTP boundary:
the status codes, the shapes a body may and may not have, and -- the
thing that would be invisible anywhere else -- that **the transcript is
reached only through the session read that authorises it.** A session id
in a path is a number a caller chose, and the rule for these three
endpoints is not a copy of the participant rule but the very call
`/api/sessions/{id}` is served from.

Whether that statement scopes is a property of SQL and lives in
`tests/console/test_queries.py` and `tests/console/test_adapters.py`;
what a title may be is pure and lives in `tests/console/test_naming.py`;
the shape of a transcript body is pure and lives in
`tests/console/test_statistics.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.naming import MAX_DESCRIPTION_CHARS, MAX_TITLE_CHARS
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.console.statistics import (
    AttendedSession,
    Participant,
    SessionName,
    SessionTranscript,
)
from sturnus.domain.transcript import SpeakerIdentity, TranscriptBlock
from tests.console.conftest import (
    ANNA,
    BEN,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeNaming,
    FakeReads,
    FakeTranscripts,
    build_test_api,
    now_at,
)

SESSION_COOKIE = "sturnus_session"
SESSION = 4711
CHANNEL = 555

TRANSCRIPT_PATH = f"/api/sessions/{SESSION}/transcript"
NAME_PATH = f"/api/sessions/{SESSION}/name"

ANNA_SPEAKING = SpeakerIdentity(discord_user_id=ANNA, discord_display_name="anna")


def attended(
    session_id: int = SESSION,
    *,
    title: str | None = None,
    description: str | None = None,
) -> AttendedSession:
    return AttendedSession(
        id=session_id,
        channel_id=CHANNEL,
        channel_name="meeting",
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        document_url=None,
        participants=(Participant(ANNA, "anna"),),
        tracks=(),
        title=title,
        description=description,
    )


def a_transcript(
    *,
    ended_at: datetime | None = T0 + timedelta(hours=1),
    audio_available: bool = True,
    pending_tracks: int = 0,
    blocks: tuple[TranscriptBlock, ...] = (TranscriptBlock(ANNA_SPEAKING, T0, "we agreed"),),
) -> SessionTranscript:
    return SessionTranscript(
        session_id=SESSION,
        started_at=T0,
        ended_at=ended_at,
        audio_available=audio_available,
        pending_tracks=pending_tracks,
        participants=(ANNA_SPEAKING,),
        blocks=blocks,
    )


def app(
    reads: FakeReads | None = None,
    transcripts: FakeTranscripts | None = None,
    naming: FakeNaming | None = None,
) -> web.Application:
    return build_test_api(
        reads=reads if reads is not None else FakeReads(sessions=(attended(),)),
        transcripts=(
            transcripts if transcripts is not None else FakeTranscripts({SESSION: a_transcript()})
        ),
        naming=naming if naming is not None else FakeNaming(participants={SESSION: {ANNA}}),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
    )


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory,
    reads: FakeReads | None = None,
    transcripts: FakeTranscripts | None = None,
    naming: FakeNaming | None = None,
    as_user: int = ANNA,
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app(reads, transcripts, naming))
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


# ---------------------------------------------------------------------------
# Nothing here is readable without a session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [TRANSCRIPT_PATH, NAME_PATH])
async def test_a_read_without_a_session_is_refused(
    aiohttp_client: AiohttpClientFactory, path: str
) -> None:
    client = await aiohttp_client(app())
    assert (await client.get(path)).status == 401


async def test_a_rename_without_a_session_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app())
    assert (await client.put(NAME_PATH, json={"title": "retro"})).status == 401


# ---------------------------------------------------------------------------
# The transcript, and the read that authorises it
# ---------------------------------------------------------------------------


async def test_a_participant_reads_the_transcript_of_their_own_meeting(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.get(TRANSCRIPT_PATH)
    assert response.status == 200
    body = await response.json()
    assert body["session_id"] == str(SESSION)
    assert [block["text"] for block in body["blocks"]] == ["we agreed"]


async def test_a_transcript_is_only_reached_through_the_scoped_session_read(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The whole authorisation of this endpoint, and the one thing no
    response body could show.

    `TranscriptReader` carries no `requested_by` -- it cannot scope
    itself -- so the handler must ask `SessionReads.session_for` for the
    signed-in person first. This asserts it did, and with whose id.
    """
    reads = FakeReads(sessions=(attended(),))
    client = await signed_in(aiohttp_client, reads=reads)
    await client.get(TRANSCRIPT_PATH)
    assert reads.asked_for == [ANNA]


async def test_the_transcript_of_a_meeting_you_were_not_in_is_not_found(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`FakeReads` answers `None` for a session it does not hold, which is
    what the real query answers for both "no such session" and "not
    yours". The handler must not go on to fetch the transcript anyway."""
    transcripts = FakeTranscripts({SESSION: a_transcript()})
    client = await signed_in(aiohttp_client, reads=FakeReads(sessions=()), transcripts=transcripts)
    response = await client.get(TRANSCRIPT_PATH)
    assert response.status == 404
    assert transcripts.asked == []


async def test_a_session_id_that_is_not_a_number_is_not_found(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The same 404 rather than a 400: it names no session either, and a
    distinct answer only tells a caller which ids are well formed."""
    client = await signed_in(aiohttp_client)
    assert (await client.get("/api/sessions/nonsense/transcript")).status == 404


async def test_a_transcript_whose_audio_retention_expired_is_still_served(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The retention window is about the recording, not about the minutes.

    So this endpoint answers 200 for a session whose audio endpoints
    answer 404, and says which of the two happened -- a tab that could
    not would look broken for a system behaving exactly as designed.
    """
    client = await signed_in(
        aiohttp_client,
        transcripts=FakeTranscripts({SESSION: a_transcript(audio_available=False)}),
    )
    response = await client.get(TRANSCRIPT_PATH)
    assert response.status == 200
    body = await response.json()
    assert body["audio_available"] is False
    assert [block["text"] for block in body["blocks"]] == ["we agreed"]


async def test_a_meeting_still_being_transcribed_says_so_rather_than_looking_empty(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(
        aiohttp_client,
        transcripts=FakeTranscripts(
            {SESSION: a_transcript(ended_at=None, pending_tracks=3, blocks=())}
        ),
    )
    body = await (await client.get(TRANSCRIPT_PATH)).json()
    assert body["ended_at"] is None
    assert body["pending_tracks"] == 3
    assert body["blocks"] == []


async def test_a_transcript_is_never_stored_by_a_shared_cache(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """It is the protected content itself. A cache holding it is a copy of
    a meeting outside every rule this system applies to one."""
    client = await signed_in(aiohttp_client)
    response = await client.get(TRANSCRIPT_PATH)
    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# What a meeting is called
# ---------------------------------------------------------------------------


async def test_a_participant_reads_the_name_of_their_own_meeting(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(
        aiohttp_client,
        reads=FakeReads(sessions=(attended(title="Sprint 34", description="agenda"),)),
    )
    body = await (await client.get(NAME_PATH)).json()
    assert body == {"title": "Sprint 34", "description": "agenda"}


async def test_the_name_of_a_meeting_you_were_not_in_is_not_found(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, reads=FakeReads(sessions=()))
    assert (await client.get(NAME_PATH)).status == 404


async def test_a_meeting_nobody_has_named_answers_with_nulls(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    assert await (await client.get(NAME_PATH)).json() == {"title": None, "description": None}


async def test_a_participant_may_name_a_meeting_they_were_in(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    naming = FakeNaming(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, naming=naming)
    response = await client.put(
        NAME_PATH, json={"title": "Sprint 34 planning", "description": "what we decided"}
    )
    assert response.status == 200
    assert await response.json() == {
        "title": "Sprint 34 planning",
        "description": "what we decided",
    }
    assert naming.stored[SESSION] == SessionName("Sprint 34 planning", "what we decided")


async def test_a_rename_is_written_for_the_signed_in_person_and_nobody_else(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A session id in a path is a number a caller chose, and no handler
    may turn it into a write that is not scoped. The id the write was
    made under cannot be seen in the response body."""
    naming = FakeNaming(participants={SESSION: {ANNA, BEN}})
    client = await signed_in(aiohttp_client, naming=naming, as_user=BEN)
    await client.put(NAME_PATH, json={"title": "retro"})
    assert naming.written == [(SESSION, BEN, "retro", None)]


async def test_somebody_who_was_not_in_the_meeting_cannot_name_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404 and not 403: a 403 would confirm that a meeting exists to
    somebody just established as having no part in it."""
    client = await signed_in(
        aiohttp_client, naming=FakeNaming(participants={SESSION: {BEN}}), as_user=ANNA
    )
    assert (await client.put(NAME_PATH, json={"title": "retro"})).status == 404


async def test_a_title_left_out_of_the_body_clears_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A `PUT` replaces the name. A client that sent only a description
    while leaving a title in place would be doing a `PATCH` under a
    `PUT`'s name."""
    naming = FakeNaming(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, naming=naming)
    body = await (await client.put(NAME_PATH, json={"description": "only this"})).json()
    assert body == {"title": None, "description": "only this"}


async def test_clearing_a_name_is_a_write_and_not_a_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    naming = FakeNaming(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, naming=naming)
    response = await client.put(NAME_PATH, json={"title": None, "description": None})
    assert response.status == 200
    assert naming.written == [(SESSION, ANNA, None, None)]


async def test_a_title_is_stored_as_it_was_trimmed_and_not_as_it_was_sent(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A client shown its own input back would keep displaying a title the
    database does not have."""
    naming = FakeNaming(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, naming=naming)
    body = await (await client.put(NAME_PATH, json={"title": "  weekly \n retro "})).json()
    assert body["title"] == "weekly retro"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not an object", id="a bare string"),
        pytest.param(["retro"], id="a list"),
        pytest.param({"title": 42}, id="a title that is not text"),
        pytest.param({"description": ["one", "two"]}, id="a description that is not text"),
    ],
)
async def test_a_body_that_is_not_a_name_is_refused(
    aiohttp_client: AiohttpClientFactory, body: object
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(NAME_PATH, json=body)
    assert response.status == 400
    assert "title" in (await response.json())["error"]


async def test_a_body_that_is_not_json_at_all_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(NAME_PATH, data="{", headers={"Content-Type": "application/json"})
    assert response.status == 400


async def test_a_title_longer_than_the_limit_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    naming = FakeNaming(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, naming=naming)
    response = await client.put(NAME_PATH, json={"title": "a" * (MAX_TITLE_CHARS + 1)})
    assert response.status == 400
    assert naming.written == []


async def test_a_description_longer_than_the_limit_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(NAME_PATH, json={"description": "a" * (MAX_DESCRIPTION_CHARS + 1)})
    assert response.status == 400


async def test_a_refusal_never_repeats_what_was_sent(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """No user input is reflected into a response: an endpoint that echoes
    what it was handed is an XSS sink for whatever renders its errors."""
    client = await signed_in(aiohttp_client)
    payload = "<script>alert(1)</script>" + "a" * MAX_TITLE_CHARS
    response = await client.put(NAME_PATH, json={"title": payload})
    assert response.status == 400
    assert "<script>" not in (await response.json())["error"]


async def test_naming_a_session_whose_id_is_not_a_number_is_not_found(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    naming = FakeNaming(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, naming=naming)
    assert (await client.put("/api/sessions/nonsense/name", json={"title": "x"})).status == 404
    assert naming.written == []
