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

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from moto import mock_aws

from sturnus.application.spectrogram import BINS, COLUMNS, Spectrogram, encode_artefact
from sturnus.console.audio import AudioDelivery
from sturnus.console.ports import (
    EncryptedAudioSource,
    KeyUnwrapper,
    Track,
    TrackDirectory,
)
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.infrastructure.audio import TARGET_RATE
from sturnus.infrastructure.crypto import CHUNK_SIZE, KeyWrapper, encrypt_file
from sturnus.infrastructure.objectstore import S3AudioStore
from sturnus.infrastructure.recording_adapters import FileAudioWriterFactory
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
    build_test_api,
    now_at,
    sealed,
)

SESSION_COOKIE = "sturnus_session"
WRAPPED = b"wrapped-data-key"

#: Where a stored spectrogram lives: beside the recording it was drawn
#: from. Spelled out rather than derived from `S3_KEY`, so a change to the
#: naming rule shows up here as a test somebody has to read.
SPECTROGRAM_KEY = "sessions/4711/speakers/1.spectrogram.enc"

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
    return build_test_api(
        oauth=FakeOAuth(),
        states=FakeStates(),
        links=FakeLinks(),
        admins=FakeAdmins(),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
        schema_ready=True,
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
    assert await response.read() == TRACK


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
    assert int(response.headers["Content-Length"]) == len(TRACK)


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
    total = len(TRACK)
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": "bytes=1000-1999"})
    assert response.status == 206
    assert response.headers["Content-Range"] == f"bytes 1000-1999/{total}"
    assert int(response.headers["Content-Length"]) == 1_000
    # An offset into the stored file, with nothing subtracted from it: the
    # resource and the plaintext are the same bytes.
    assert await response.read() == TRACK[1_000:2_000]


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
    first = len(TRACK) - 4_000
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(url(), headers={"Range": f"bytes={first}-"})
    assert await response.read() == TRACK[-4_000:]
    assert source.streamed_bytes < len(TRACK) // 2


async def test_an_unsatisfiable_range_is_refused_with_the_real_length(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """416 carries the length so the client can ask again correctly rather
    than guess a second time."""
    total = len(TRACK)
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
    assert await response.read() == TRACK


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
        assert await whole.read() == TRACK

        first = CHUNK_SIZE - 1_000
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
    assert int(response.headers["Content-Length"]) == len(TRACK)
    assert response.headers["Accept-Ranges"] == "bytes"
    assert await response.read() == b""


# ---------------------------------------------------------------------------
# The spectrogram, behind the same gate
# ---------------------------------------------------------------------------


def spectrogram_url(session_id: int = SESSION, speaker_id: int = ANNA) -> str:
    return f"/api/sessions/{session_id}/tracks/{speaker_id}/spectrogram"


def _real_track(tmp_path: Path) -> bytes:
    """A track the writer could actually have produced, with sound on it."""
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    writer = FileAudioWriterFactory(tmp_path / "recordings").open(SESSION, ANNA, epoch)
    t = np.arange(48_000 * 3) / 48_000
    mono = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
    stereo = np.repeat(mono[:, None], 2, axis=1)
    for index in range(len(mono) // 960):
        writer.write(
            epoch + timedelta(seconds=index * 960 / 48_000),
            stereo[index * 960 : (index + 1) * 960].reshape(-1).astype("<i2").tobytes(),
        )
    writer.close()
    return writer.path.read_bytes()


async def test_a_participant_can_see_the_shape_of_a_track(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, tmp_path: Path
) -> None:
    """The picture describes the track the writer wrote, read from its own
    header rather than assumed -- which is the whole lesson of the format
    defect that preceded this view."""
    source = FakeAudioSource({S3_KEY: sealed(_real_track(tmp_path), tmp_path)})
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(spectrogram_url())
    assert response.status == 200

    body = await response.json()
    assert body["sample_rate"] == TARGET_RATE
    assert body["duration_seconds"] == pytest.approx(3.0, abs=0.05)
    assert len(base64.b64decode(body["magnitudes"])) == body["bins"] * body["columns"]
    assert body["hz_per_bin"] == pytest.approx(TARGET_RATE / 2 / body["bins"])


async def test_a_stranger_cannot_see_the_shape_of_a_track_either(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, source: FakeAudioSource
) -> None:
    """A spectrogram shows when somebody spoke and for how long.

    It is less than the audio; it is not nothing, and the rule that governs
    the audio is the right one for it. If this ever answered 200 where the
    audio answers 404, the console would have grown a way to confirm the
    existence of a meeting somebody was not in.
    """
    stranger = 999
    client = await signed_in(aiohttp_client, build(tracks, source), as_user=stranger)
    response = await client.get(spectrogram_url())
    assert response.status == 404


async def test_the_spectrogram_of_a_recording_that_is_gone_is_not_found(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks
) -> None:
    client = await signed_in(aiohttp_client, build(tracks, FakeAudioSource({})))
    response = await client.get(spectrogram_url())
    assert response.status == 404


async def test_a_spectrogram_is_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, tmp_path: Path
) -> None:
    """Same reason as the audio: a shared cache holding a copy would hand
    it to the next person through the same proxy."""
    source = FakeAudioSource({S3_KEY: sealed(_real_track(tmp_path), tmp_path)})
    client = await signed_in(aiohttp_client, build(tracks, source))
    response = await client.get(spectrogram_url())
    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# The picture the worker already drew
#
# A guild that switched `spectrograms_by_default` on has an artefact in the
# bucket, and this endpoint prefers it. What must not change is anything
# else: the same rule decides who may see it, decided again on this
# request, and a track whose audio is gone has no picture either.
# ---------------------------------------------------------------------------


def _stored_artefact(sample_rate: int, tmp_path: Path) -> bytes:
    """One artefact in the real on-disk format, sealed like the audio."""
    cells = bytes(range(256)) * (COLUMNS * BINS // 256)
    return sealed(
        encode_artefact(
            Spectrogram(
                columns=COLUMNS,
                bins=BINS,
                sample_rate=sample_rate,
                duration_seconds=61.5,
                magnitudes=base64.b64encode(cells).decode("ascii"),
            )
        ),
        tmp_path,
    )


@pytest.fixture
def drawn() -> FakeTracks:
    """Anna's track, with a spectrogram the worker stored beside it."""
    return FakeTracks(
        tracks={(SESSION, ANNA): Track(S3_KEY, KEY_ID, WRAPPED, spectrogram_key=SPECTROGRAM_KEY)},
        participants={SESSION: {ANNA, BEN}},
    )


async def test_a_stored_picture_is_answered_without_reading_the_recording(
    aiohttp_client: AiohttpClientFactory, drawn: FakeTracks, tmp_path: Path
) -> None:
    """The whole point of storing one: a view stops costing a full decrypt.

    Asserted on which object was opened rather than on how long it took:
    the saving is that the recording's body is never streamed at all, and
    on a three-hour workshop that body is the entire cost. A reader that
    drew the track anyway would name the recording's key here.
    """
    source = FakeAudioSource(
        {
            S3_KEY: sealed(_real_track(tmp_path), tmp_path),
            SPECTROGRAM_KEY: _stored_artefact(TARGET_RATE, tmp_path),
        }
    )
    client = await signed_in(aiohttp_client, build(drawn, source))

    response = await client.get(spectrogram_url())

    assert response.status == 200
    body = await response.json()
    assert body["duration_seconds"] == 61.5
    assert body["sample_rate"] == TARGET_RATE
    assert len(base64.b64decode(body["magnitudes"])) == BINS * COLUMNS
    assert source.streamed_keys == [SPECTROGRAM_KEY]


async def test_a_track_drawn_before_the_setting_was_on_is_still_answered(
    aiohttp_client: AiohttpClientFactory, tracks: FakeTracks, tmp_path: Path
) -> None:
    """No backfill, and therefore no gap in the interface.

    Every job transcribed before a guild switched the setting on has no
    artefact and never will unless it is re-queued. The endpoint's
    contract does not depend on that: it draws the track, exactly as it
    did before artefacts existed.
    """
    source = FakeAudioSource({S3_KEY: sealed(_real_track(tmp_path), tmp_path)})
    client = await signed_in(aiohttp_client, build(tracks, source))

    response = await client.get(spectrogram_url())

    assert response.status == 200
    assert (await response.json())["duration_seconds"] == pytest.approx(3.0, abs=0.05)


async def test_a_picture_that_is_missing_falls_back_to_drawing_the_track(
    aiohttp_client: AiohttpClientFactory, drawn: FakeTracks, tmp_path: Path
) -> None:
    """A row naming an object that is not there is a possible state.

    The worker writes the key down before it writes the object, on purpose
    -- an artefact nothing names is one the retention sweep can never
    delete -- so a failed upload leaves exactly this. It must cost a
    recomputation and not a refusal.
    """
    source = FakeAudioSource({S3_KEY: sealed(_real_track(tmp_path), tmp_path)})
    client = await signed_in(aiohttp_client, build(drawn, source))

    response = await client.get(spectrogram_url())

    assert response.status == 200
    assert (await response.json())["duration_seconds"] == pytest.approx(3.0, abs=0.05)


async def test_a_picture_this_build_cannot_read_falls_back_to_drawing_it(
    aiohttp_client: AiohttpClientFactory, drawn: FakeTracks, tmp_path: Path
) -> None:
    """A stored artefact is not a recording: losing one loses nothing.

    So an artefact that will not decode is answered by redrawing, where a
    *recording* that will not decrypt is answered with an error -- the
    recording is the only copy of what somebody said, and the picture is a
    convenience the next line of the handler recreates.
    """
    source = FakeAudioSource(
        {
            S3_KEY: sealed(_real_track(tmp_path), tmp_path),
            SPECTROGRAM_KEY: sealed(b'{"version": 1, "columns": 3}', tmp_path),
        }
    )
    client = await signed_in(aiohttp_client, build(drawn, source))

    response = await client.get(spectrogram_url())

    assert response.status == 200
    assert (await response.json())["duration_seconds"] == pytest.approx(3.0, abs=0.05)


async def test_a_stranger_cannot_see_a_stored_picture_either(
    aiohttp_client: AiohttpClientFactory, drawn: FakeTracks, tmp_path: Path
) -> None:
    """The saving is in the payload and must never reach the permission.

    A cheap answer is exactly the kind of answer that grows a cache in
    front of it, and the rule this endpoint enforces -- participants of
    the session, nobody else -- is decided on every request against
    `session_participant`, before anything looks in the bucket.
    """
    source = FakeAudioSource(
        {
            S3_KEY: sealed(_real_track(tmp_path), tmp_path),
            SPECTROGRAM_KEY: _stored_artefact(TARGET_RATE, tmp_path),
        }
    )
    client = await signed_in(aiohttp_client, build(drawn, source), as_user=999)

    response = await client.get(spectrogram_url())

    assert response.status == 404


async def test_a_stored_picture_is_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory, drawn: FakeTracks, tmp_path: Path
) -> None:
    """Same header whichever source answered. A shared cache holding this
    one would hand somebody's voice activity to the next person through
    the same proxy."""
    source = FakeAudioSource(
        {
            S3_KEY: sealed(_real_track(tmp_path), tmp_path),
            SPECTROGRAM_KEY: _stored_artefact(TARGET_RATE, tmp_path),
        }
    )
    client = await signed_in(aiohttp_client, build(drawn, source))

    response = await client.get(spectrogram_url())

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_a_recording_that_is_gone_has_no_picture_either(
    aiohttp_client: AiohttpClientFactory, drawn: FakeTracks, tmp_path: Path
) -> None:
    """After the sweep, the track is neither playable nor visualisable.

    The sweep deletes both objects in one pass, so this state should not
    outlive a sweep -- but it is reachable for as long as one runs, and it
    is the state the whole retention rule is about. An artefact that
    answered here would be a rendering of somebody's voice surviving the
    deletion of the recording it was drawn from, reachable through the
    console, which is precisely what storing one is not allowed to create.
    """
    source = FakeAudioSource({SPECTROGRAM_KEY: _stored_artefact(TARGET_RATE, tmp_path)})
    client = await signed_in(aiohttp_client, build(drawn, source))

    assert (await client.get(spectrogram_url())).status == 404
    assert (await client.get(url())).status == 404
