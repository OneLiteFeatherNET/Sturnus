"""Playing back a track, end to end through the real route.

The design (section 1.1) says audio playback is a wider use of a recording
than a transcript is, and names the three things that make it defensible.
The first of those is the only one code can carry, and it is what most of
this file is about: **only participants of a session may play its audio**,
checked against `session_participant` for the signed-in user, on every
request.

A person who was not in the session is answered 404 rather than 403. The
existence of a meeting somebody was not in is not information they are
owed, and 403 tells them the session is real, when it happened, and who
they might ask about it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from moto import mock_aws

from sturnus.console.app import build_api
from sturnus.console.audio import WAV_HEADER_BYTES, AudioDelivery, wav_header
from sturnus.console.ports import (
    EncryptedAudioSource,
    KeyUnwrapper,
    Track,
    TrackDirectory,
)
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.infrastructure.crypto import CHUNK_SIZE, KeyWrapper, encrypt_file
from sturnus.infrastructure.objectstore import S3AudioStore
from tests.console.conftest import (
    ANNA,
    BEN,
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
    now_at,
    sealed,
)

SESSION_COOKIE = "sturnus_session"
WRAPPED = b"wrapped-data-key"

#: Two chunks and a bit, so a range can start inside the third chunk and
#: the "did not download the beginning" assertion has something to measure.
TRACK = bytes(range(241)) * 40_000


@pytest.fixture
def source(tmp_path: Path) -> FakeAudioSource:
    return FakeAudioSource({S3_KEY: sealed(TRACK, tmp_path)})


@pytest.fixture
def tracks() -> FakeTracks:
    """Anna and Ben were both in session 4711; only Anna's track exists."""
    return FakeTracks(
        tracks={(SESSION, ANNA): Track(S3_KEY, KEY_ID, WRAPPED)},
        participants={SESSION: {ANNA, BEN}},
    )


def build(
    tracks: TrackDirectory,
    source: EncryptedAudioSource,
    keys: KeyUnwrapper | None = None,
) -> web.Application:
    return build_api(
        oauth=FakeOAuth(),
        states=FakeStates(),
        links=FakeLinks(),
        admins=FakeAdmins(),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
        schema_ready=lambda: True,
        console_origin="https://sturnus.example",
        audio=AudioDelivery(tracks=tracks, source=source, keys=keys or FakeKeys()),
    )


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


def url(session_id: int = SESSION, speaker_id: int = ANNA) -> str:
    return f"/api/sessions/{session_id}/tracks/{speaker_id}/audio"


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    # Carried across by hand for the reason `test_auth_routes` gives: the
    # cookie is `Secure` and aiohttp's jar correctly refuses to store one
    # over the test server's plain http.
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


# ---------------------------------------------------------------------------
# Who may listen
# ---------------------------------------------------------------------------


async def test_a_participant_hears_the_track_they_asked_for(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url())
    assert response.status == 200
    assert await response.read() == wav_header(len(TRACK)) + TRACK


async def test_someone_who_was_not_in_the_session_is_told_it_does_not_exist(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """404 rather than 403, and the difference is the whole point.

    403 would confirm that the session exists, that it has a recording of
    this person, and roughly when -- to somebody the system has just
    established has no business knowing any of it.
    """
    stranger = 999
    client = await signed_in(aiohttp_client, build(tracks, source), as_user=stranger)
    assert (await client.get(url())).status == 404


async def test_a_request_without_a_session_is_refused_before_anything_is_read(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    client = await aiohttp_client(build(tracks, source))
    assert (await client.get(url())).status == 401
    assert tracks.asked == []
    assert source.streamed_bytes == 0


async def test_the_query_is_scoped_by_the_signed_in_user_not_by_the_url(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """The path names whose voice is wanted; the cookie names who is
    asking. Only the second may decide."""
    client = await signed_in(aiohttp_client, build(tracks, source), as_user=BEN)
    await client.get(url(speaker_id=ANNA))
    assert tracks.asked == [(SESSION, ANNA, BEN)]


async def test_authorisation_is_a_fresh_query_on_every_request(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """Never cached, never inferred from an earlier request. A revoked
    participation that a cache outlives is a recording still being served
    to somebody who may no longer hear it.
    """
    client = await signed_in(aiohttp_client, build(tracks, source))
    await client.get(url())
    await client.get(url())
    assert tracks.asked == [(SESSION, ANNA, ANNA)] * 2


async def test_a_session_that_does_not_exist_is_not_found(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, source))
    assert (await client.get(url(session_id=1))).status == 404


async def test_a_speaker_with_no_recording_in_that_session_is_not_found(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """Ben was in the meeting and never spoke, or his audio was erased by
    the retention sweep. Either way there is nothing to play."""
    client = await signed_in(aiohttp_client, build(tracks, source))
    assert (await client.get(url(speaker_id=BEN))).status == 404


@pytest.mark.parametrize(
    "path", ["/api/sessions/x/tracks/100/audio", "/api/sessions/1/tracks//audio"]
)
async def test_an_id_that_is_not_a_number_is_not_found(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource, path: str
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, source))
    assert (await client.get(path)).status == 404


async def test_a_recording_whose_object_is_gone_is_not_found(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks
) -> None:
    """A row outliving its object is what the retention sweep leaves behind
    between erasing the audio and stamping the row. It is a 404, not a
    500: nothing is broken, the audio is simply gone.
    """
    client = await signed_in(aiohttp_client, build(tracks, FakeAudioSource({})))
    assert (await client.get(url())).status == 404


async def test_a_recording_wrapped_by_another_master_key_is_not_served(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """A key id that disagrees with this process's is a deployment holding
    the wrong master key. Refusing on the id says so; going ahead would
    fail as an authentication-tag error mid-stream, after a 200 had already
    promised a playable track.
    """
    client = await signed_in(
        aiohttp_client, build(tracks, source, keys=FakeKeys(key_id="some-other-key"))
    )
    assert (await client.get(url())).status == 500


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


async def test_the_response_is_a_wav_a_browser_will_play(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url())
    assert response.headers["Content-Type"] == "audio/wav"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert int(response.headers["Content-Length"]) == WAV_HEADER_BYTES + len(TRACK)


async def test_nothing_in_between_is_allowed_to_keep_a_copy(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """Someone's voice, decrypted, on the way to one specific listener. A
    shared cache holding it would serve it to the next person through the
    same proxy, which is exactly the audience the authorisation check
    exists to exclude."""
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url())
    assert "no-store" in response.headers["Cache-Control"]
    assert "private" in response.headers["Cache-Control"]


# ---------------------------------------------------------------------------
# Range
# ---------------------------------------------------------------------------


async def test_a_range_is_answered_with_partial_content(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    total = WAV_HEADER_BYTES + len(TRACK)
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": "bytes=44-1043"})
    assert response.status == 206
    assert response.headers["Content-Range"] == f"bytes 44-1043/{total}"
    assert int(response.headers["Content-Length"]) == 1_000
    assert await response.read() == TRACK[:1_000]


async def test_a_suffix_range_returns_the_end_of_the_track(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": "bytes=-500"})
    assert response.status == 206
    assert await response.read() == TRACK[-500:]


async def test_a_range_starting_late_does_not_download_what_came_before(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """The reason `Range` is implemented at all: a listener who wants
    minute 30 must not pay for minutes 0 to 29."""
    first = WAV_HEADER_BYTES + len(TRACK) - 4_000
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": f"bytes={first}-"})
    assert await response.read() == TRACK[-4_000:]
    assert source.streamed_bytes < len(TRACK) // 2


async def test_an_unsatisfiable_range_is_refused_with_the_real_length(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """416 carries the length so the client can ask again correctly rather
    than guess a second time."""
    total = WAV_HEADER_BYTES + len(TRACK)
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": f"bytes={total}-"})
    assert response.status == 416
    assert response.headers["Content-Range"] == f"bytes */{total}"


async def test_a_range_this_server_cannot_parse_yields_the_whole_track(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": "kilobytes=0-1"})
    assert response.status == 200
    assert await response.read() == wav_header(len(TRACK)) + TRACK


async def test_a_stranger_asking_for_a_range_still_learns_nothing(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """416 would leak the length of a recording to somebody who may not
    hear it; the authorisation check therefore runs first."""
    client = await signed_in(aiohttp_client, build(tracks, source), as_user=999)
    response = await client.get(url(), headers={"Range": "bytes=99999999-"})
    assert response.status == 404


# ---------------------------------------------------------------------------
# The whole path, with nothing standing in for anything
# ---------------------------------------------------------------------------


async def test_a_real_recording_survives_the_whole_round_trip(
    aiohttp_client: AiohttpClientFactory, tmp_path: Path
) -> None:
    """The pieces agree, or they do not.

    Every other test here replaces the object store and the master key with
    a double, which is the only way to assert on which bytes were fetched.
    This one replaces neither: a real `KeyWrapper` wraps a real data key, a
    real `encrypt_file` writes the real on-disk format, `moto` stands in
    for S3, and what comes back out of the socket is compared with what
    went in -- across a chunk boundary, which is the seam every one of
    those pieces has to agree about.
    """
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sturnus-audio")
        store = S3AudioStore(
            endpoint=None, bucket="sturnus-audio", access_key="ak", secret_key="sk"
        )
        wrapper = KeyWrapper(b"m" * 32, "master-1")
        data_key = wrapper.new_data_key()

        plain = tmp_path / "track.pcm"
        plain.write_bytes(TRACK)
        encrypted = tmp_path / "track.enc"
        encrypt_file(plain, encrypted, data_key.plaintext)
        await store.put(S3_KEY, encrypted)

        tracks = FakeTracks(
            tracks={(SESSION, ANNA): Track(S3_KEY, "master-1", data_key.wrapped)},
            participants={SESSION: {ANNA}},
        )
        client = await signed_in(aiohttp_client, build(tracks, store, keys=wrapper))

        whole = await client.get(url())
        assert await whole.read() == wav_header(len(TRACK)) + TRACK

        first = WAV_HEADER_BYTES + CHUNK_SIZE - 1_000
        partial = await client.get(url(), headers={"Range": f"bytes={first}-"})
        assert partial.status == 206
        assert await partial.read() == TRACK[CHUNK_SIZE - 1_000 :]


async def test_a_player_can_ask_for_the_length_without_the_audio(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """A `HEAD` is how an audio element learns the length and whether it may
    seek, before it decides to fetch anything. Answering it must cost no
    decryption at all."""
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.head(url())
    assert response.status == 200
    assert int(response.headers["Content-Length"]) == WAV_HEADER_BYTES + len(TRACK)
    assert response.headers["Accept-Ranges"] == "bytes"
    assert await response.read() == b""
