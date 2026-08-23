"""Downloading a track, which is a different act from playing one.

Playback is `test_audio_routes.py`, and its rule is unchanged: only
participants of a session may play its audio. This file is about the
second route, which the owner of this repository decided deliberately to
add and which the design document has been amended to say (section 1.1):
**an administrator of a guild may download any recording of that guild,
including sessions they were not in.**

Three things are checked here and each of them is the whole point:

* The capability does not exist until a guild switches it on. While
  `admin_audio_download_offered` is false the route refuses *everyone*,
  participants included -- turning it on is an administrator asserting
  something about a policy document that software cannot read.
* Every refusal is the same 404 with the same body as every other refusal
  on this path. "You are not an administrator" and "there is no such
  recording" must stay indistinguishable.
* The download is audited at WARNING and the line says whether the person
  who took the copy was in the room, because an administrator downloading
  a meeting they were not in is a different event from a participant
  keeping a copy of their own.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.audio import AudioDelivery
from sturnus.console.ports import EncryptedAudioSource, Track, TrackDirectory
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.observability.events import Event
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    KEY_ID,
    S3_KEY,
    SECRET,
    SESSION,
    T0,
    AiohttpClientFactory,
    FakeAdmins,
    FakeAudioSource,
    FakeKeys,
    FakeLinks,
    FakeOAuth,
    FakeStates,
    FakeTracks,
    build_test_api,
    now_at,
    sealed,
)

SESSION_COOKIE = "sturnus_session"
WRAPPED = b"wrapped-data-key"

#: Somebody who administers the guild and was in none of its meetings.
#: The whole reason this route exists, and the person the old rule refused.
CARA = 300

#: Two chunks and a bit, so a `Range` can start inside the third one.
TRACK = bytes(range(241)) * 40_000


@pytest.fixture
def source(tmp_path: Path) -> FakeAudioSource:
    return FakeAudioSource({S3_KEY: sealed(TRACK, tmp_path)})


def tracks(*, offered: bool) -> FakeTracks:
    """Anna and Ben were in session 4711; Cara administers the guild."""
    return FakeTracks(
        tracks={(SESSION, ANNA): Track(S3_KEY, KEY_ID, WRAPPED)},
        participants={SESSION: {ANNA, BEN}},
        administrators={CARA},
        download_offered=offered,
        guild_id=GUILD,
    )


def build(directory: TrackDirectory, audio_source: EncryptedAudioSource) -> web.Application:
    return build_test_api(
        oauth=FakeOAuth(),
        states=FakeStates(),
        links=FakeLinks(),
        admins=FakeAdmins(),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
        schema_ready=True,
        audio=AudioDelivery(tracks=directory, source=audio_source, keys=FakeKeys()),
    )


def token(discord_user_id: int) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


def url(session_id: int = SESSION, speaker_id: int = ANNA) -> str:
    return f"/api/sessions/{session_id}/tracks/{speaker_id}/download"


def play_url(session_id: int = SESSION, speaker_id: int = ANNA) -> str:
    return f"/api/sessions/{session_id}/tracks/{speaker_id}/audio"


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    # Carried across by hand for the reason `test_auth_routes` gives: the
    # cookie is `Secure` and aiohttp's jar correctly refuses to store one
    # over the test server's plain http.
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


# ---------------------------------------------------------------------------
# The capability arrives switched off
# ---------------------------------------------------------------------------


async def test_a_guild_that_has_not_switched_it_on_offers_no_download_to_anybody(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    """Not "administrators only" -- nobody, participants included.

    While the setting is false the guild has made no assertion about what
    its policy document says, and a route that quietly worked for
    participants would be a capability that exists before anybody claimed
    it does.
    """
    client = await signed_in(aiohttp_client, build(tracks(offered=False), source), as_user=ANNA)

    response = await client.get(url())

    assert response.status == 404
    assert await response.json() == {"error": "no such recording"}


async def test_an_administrator_is_refused_while_the_guild_has_not_switched_it_on(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=False), source), as_user=CARA)

    assert (await client.get(url())).status == 404


# ---------------------------------------------------------------------------
# What the route hands over once a guild has switched it on
# ---------------------------------------------------------------------------


async def test_an_administrator_may_download_a_meeting_they_were_not_in(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    """The widening, stated as one assertion.

    Cara was in no session of this guild. Under the rule that governs
    playback she is nobody, and `/audio` still answers her 404 (below).
    """
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    response = await client.get(url())

    assert response.status == 200
    assert await response.read() == TRACK


async def test_downloading_does_not_widen_playing(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    """The two routes are two rules, and only one of them moved."""
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    assert (await client.get(play_url())).status == 404


async def test_a_participant_may_take_a_copy_of_their_own_meeting(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=BEN)

    response = await client.get(url())

    assert response.status == 200
    assert await response.read() == TRACK


async def test_somebody_who_is_neither_gets_the_same_answer_as_a_stranger(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    """404 and the same body, so a refusal carries no fact about a meeting."""
    stranger = 999
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=stranger)

    response = await client.get(url())

    assert response.status == 404
    assert await response.json() == {"error": "no such recording"}


async def test_a_recording_that_does_not_exist_answers_the_same_way(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    response = await client.get(url(speaker_id=BEN))

    assert response.status == 404
    assert await response.json() == {"error": "no such recording"}


# ---------------------------------------------------------------------------
# The headers, which are the other half of what makes this a download
# ---------------------------------------------------------------------------


async def test_the_response_asks_the_browser_to_save_it_rather_than_play_it(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    response = await client.get(url())

    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="sturnus-session-{SESSION}-speaker-{ANNA}.wav"'
    )
    assert response.headers["Content-Type"] == "audio/wav"


async def test_the_filename_names_the_speaker_by_snowflake_and_never_by_name(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    """A filename outlives the page that produced it.

    The file lands in a Downloads folder, gets attached to a mail, gets
    read by whoever is looking over a shoulder -- with none of the context
    the console gave it and none of its access control. A display name
    there would be a disclosure the console never made; a snowflake is
    meaningless to a bystander and exact to anybody entitled to resolve it.
    """
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    disposition = (await client.get(url())).headers["Content-Disposition"]

    assert str(ANNA) in disposition
    assert "user-" not in disposition
    assert "Anna" not in disposition


async def test_a_downloaded_copy_is_never_held_by_a_shared_cache(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    response = await client.get(url())

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_a_download_resumes_where_it_broke_off(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    """The same `Range` machinery playback uses, not a second copy of it.

    A download of a long meeting is exactly the transfer that gets
    interrupted, so the route that hands over a whole file is the one that
    most needs to be resumable.
    """
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    response = await client.get(url(), headers={"Range": "bytes=100-199"})

    assert response.status == 206
    assert response.headers["Content-Range"] == f"bytes 100-199/{len(TRACK)}"
    assert await response.read() == TRACK[100:200]


# ---------------------------------------------------------------------------
# The audit line
# ---------------------------------------------------------------------------


def _events(caplog: pytest.LogCaptureFixture, event: Event) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "sturnus_event", None) == str(event)]


def _fields(record: logging.LogRecord) -> dict[str, object]:
    fields = getattr(record, "sturnus_fields", None)
    assert isinstance(fields, dict)
    return fields


async def test_an_administrator_taking_a_copy_is_logged_as_a_read_from_outside_the_room(
    aiohttp_client: AiohttpClientFactory,
    source: FakeAudioSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING, and `by_participant` false.

    This is the one read in the system that reaches another person's voice
    without the reader having been in the room with them. Nothing else
    records that it happened.
    """
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=CARA)

    with caplog.at_level(logging.INFO):
        # Read to the end: the audit line is emitted after the last byte
        # has left, because it records what was actually delivered.
        await (await client.get(url())).read()

    lines = _events(caplog, Event.CONSOLE_TRACK_DOWNLOADED)
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    fields = _fields(lines[0])
    assert fields["session_id"] == SESSION
    assert fields["guild_id"] == GUILD
    assert fields["discord_user_id"] == ANNA
    assert fields["requested_by"] == CARA
    assert fields["by_participant"] is False


async def test_a_participant_taking_a_copy_is_the_same_event_told_apart_by_one_field(
    aiohttp_client: AiohttpClientFactory,
    source: FakeAudioSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=BEN)

    with caplog.at_level(logging.INFO):
        # Read to the end: the audit line is emitted after the last byte
        # has left, because it records what was actually delivered.
        await (await client.get(url())).read()

    lines = _events(caplog, Event.CONSOLE_TRACK_DOWNLOADED)
    assert len(lines) == 1
    assert _fields(lines[0])["by_participant"] is True
    assert _fields(lines[0])["requested_by"] == BEN


async def test_a_download_is_not_logged_as_a_playback(
    aiohttp_client: AiohttpClientFactory,
    source: FakeAudioSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two acts, two events. An access log that called them one thing
    would be an access log nobody could ask the question of."""
    client = await signed_in(aiohttp_client, build(tracks(offered=True), source), as_user=BEN)

    with caplog.at_level(logging.INFO):
        # Read to the end: the audit line is emitted after the last byte
        # has left, because it records what was actually delivered.
        await (await client.get(url())).read()

    assert not _events(caplog, Event.CONSOLE_TRACK_SERVED)


async def test_a_refused_download_hands_over_nothing_and_says_so(
    aiohttp_client: AiohttpClientFactory,
    source: FakeAudioSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await signed_in(aiohttp_client, build(tracks(offered=False), source), as_user=CARA)

    with caplog.at_level(logging.INFO):
        # Read to the end: the audit line is emitted after the last byte
        # has left, because it records what was actually delivered.
        await (await client.get(url())).read()

    assert not _events(caplog, Event.CONSOLE_TRACK_DOWNLOADED)
    refusals = _events(caplog, Event.CONSOLE_TRACK_REFUSED)
    assert len(refusals) == 1
    assert _fields(refusals[0])["reason"] == "download_not_permitted"


# ---------------------------------------------------------------------------
# The gate is in front of everything, as it is for playback
# ---------------------------------------------------------------------------


async def test_a_visitor_who_is_not_signed_in_is_refused_before_anything_is_read(
    aiohttp_client: AiohttpClientFactory, source: FakeAudioSource
) -> None:
    directory = tracks(offered=True)
    client = await aiohttp_client(build(directory, source))

    response = await client.get(url())

    assert response.status == 401
    assert directory.asked_to_download == []
    assert source.reads == []
