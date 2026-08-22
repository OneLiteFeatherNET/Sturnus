"""The parts of audio delivery that need neither S3 nor HTTP to be wrong.

Three properties are being pinned here, and each one is load-bearing for a
different reason:

- **The declared length is right.** A browser's seek bar is drawn from the
  WAV header, so a header that lies produces a player that cannot seek --
  and the only way to know the length without decrypting the whole track
  first is to derive it from the ciphertext's framing.
- **A range starts where it was asked to start.** "A listener who wants
  minute 30 must not download minutes 0 to 29" is a statement about which
  bytes are fetched, which is why the fake source records them.
- **Nothing is ever written to disk.** The last test in this file is a
  static rule over the two modules on the serving path, in the same spirit
  as `tests/test_architecture.py`: the property the whole encryption
  scheme exists for is not one to leave to review.
"""

from __future__ import annotations

import ast
import io
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sturnus.console.audio import (
    ByteRange,
    CorruptRecording,
    UnsatisfiableRange,
    parse_range,
    stored_length,
    stream_wav,
)
from sturnus.infrastructure.audio import SOURCE_RATE, TARGET_RATE
from sturnus.infrastructure.crypto import CHUNK_SIZE
from sturnus.infrastructure.recording_adapters import FileAudioWriterFactory
from tests.console.conftest import (
    ANNA,
    DATA_KEY,
    S3_KEY,
    FakeAudioSource,
    collect,
    sealed,
)

TOTAL = 1_000

#: One Discord voice frame: 20 ms at 48 kHz.
_SAMPLES_PER_FRAME = 960


# ---------------------------------------------------------------------------
# Parsing a Range header
# ---------------------------------------------------------------------------


def test_no_range_header_asks_for_the_whole_resource() -> None:
    assert parse_range(None, TOTAL) is None


def test_an_open_ended_range_runs_to_the_last_byte() -> None:
    assert parse_range("bytes=0-", TOTAL) == ByteRange(0, TOTAL - 1)


def test_a_closed_range_includes_both_of_its_ends() -> None:
    """RFC 9110 ranges are inclusive at both ends, which is one byte more
    than every slice in this language. Getting it wrong truncates the last
    byte of every partial response, which no player complains about and no
    casual test notices."""
    span = parse_range("bytes=100-200", TOTAL)
    assert span == ByteRange(100, 200)
    assert span is not None and span.length == 101


def test_a_range_that_runs_past_the_end_stops_at_the_last_byte() -> None:
    assert parse_range("bytes=900-5000", TOTAL) == ByteRange(900, TOTAL - 1)


def test_a_suffix_range_counts_back_from_the_end() -> None:
    """`bytes=-500` is the last 500 bytes, not the first 500. A player
    checking for a trailing tag asks exactly this."""
    assert parse_range("bytes=-500", TOTAL) == ByteRange(TOTAL - 500, TOTAL - 1)


def test_a_suffix_longer_than_the_track_is_the_whole_track() -> None:
    assert parse_range("bytes=-5000", TOTAL) == ByteRange(0, TOTAL - 1)


def test_whitespace_around_the_range_is_tolerated() -> None:
    assert parse_range("bytes = 100 - 200", TOTAL) == ByteRange(100, 200)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "bytes=",
        "bytes=abc",
        "bytes=-",
        "bytes=1-2-3",
        "bytes=5-3",
        "items=0-100",
        "0-100",
        "bytes=0-10, 20-30",
    ],
)
def test_a_range_this_server_does_not_understand_is_ignored(header: str) -> None:
    """RFC 9110 says an unsatisfiable *unit* is ignored, not refused, and a
    multi-range request may be answered with the whole resource. Answering
    416 to a header we merely did not parse would break a client that could
    have played the track perfectly well from a plain 200."""
    assert parse_range(header, TOTAL) is None


def test_a_range_starting_past_the_end_is_unsatisfiable() -> None:
    with pytest.raises(UnsatisfiableRange):
        parse_range(f"bytes={TOTAL}-", TOTAL)


def test_a_zero_length_suffix_is_unsatisfiable() -> None:
    """`bytes=-0` asks for the last nothing, which no response can be."""
    with pytest.raises(UnsatisfiableRange):
        parse_range("bytes=-0", TOTAL)


def test_any_range_over_an_empty_resource_is_unsatisfiable() -> None:
    with pytest.raises(UnsatisfiableRange):
        parse_range("bytes=0-", 0)


# ---------------------------------------------------------------------------
# The WAV header
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Deriving the stored file's length from the ciphertext
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    [0, 1, 5_000, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, CHUNK_SIZE * 2 + 7],
)
def test_the_stored_length_is_derived_without_decrypting_anything(
    size: int, tmp_path: Path
) -> None:
    """`plaintext + 16` per chunk, `+4` per length prefix, `+13` per file.

    Exercised against files the real encryptor wrote, at every boundary
    that could be off by one: empty, part of a chunk, exactly a chunk, and
    a chunk plus a remainder.
    """
    assert stored_length(len(sealed(b"\x01" * size, tmp_path))) == size


@pytest.mark.parametrize("ciphertext_bytes", [0, 5, 12, 14, 20])
def test_an_object_too_short_to_be_a_recording_is_refused(ciphertext_bytes: int) -> None:
    with pytest.raises(CorruptRecording):
        stored_length(ciphertext_bytes)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.fixture
def track() -> bytes:
    return bytes(range(251)) * 40_000  # ~10 MB, not a multiple of any chunk


@pytest.fixture
def source(track: bytes, tmp_path: Path) -> FakeAudioSource:
    return FakeAudioSource({S3_KEY: sealed(track, tmp_path)})


async def test_the_whole_track_comes_back_byte_for_byte(
    source: FakeAudioSource, track: bytes
) -> None:
    """The served resource is the stored file: no header is added to it, so
    nothing is added to its length either."""
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(0, len(track) - 1)))
    assert body == track


async def test_a_range_starting_mid_track_yields_the_rest_of_it(
    source: FakeAudioSource, track: bytes
) -> None:
    first = CHUNK_SIZE + 12_345
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(first, len(track) - 1)))
    assert body == track[first:]


async def test_a_listener_who_wants_the_end_does_not_download_the_beginning(
    source: FakeAudioSource, track: bytes
) -> None:
    """The reason `Range` is here at all.

    The chunked format allows starting at a chunk boundary, so a request
    for a byte inside chunk two fetches ciphertext from chunk two onwards
    and discards only the remainder of that one chunk -- not the two before
    it.
    """
    first = CHUNK_SIZE * 2 + 99
    await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(first, len(track) - 1)))
    assert source.streamed_bytes < len(track) - CHUNK_SIZE


async def test_a_range_that_ends_early_stops_reading_there(
    source: FakeAudioSource, track: bytes
) -> None:
    """The other half of the same promise: a player that asked for one
    chunk must not cost a whole track's worth of egress."""
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(0, 9)))
    assert body == track[:10]
    assert source.streamed_bytes < CHUNK_SIZE * 2


async def test_the_last_byte_of_the_track_is_reachable(
    source: FakeAudioSource, track: bytes
) -> None:
    """An off-by-one at the end is invisible in a player and fatal to a
    client that checksums what it got."""
    last = len(track) - 1
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(last, last)))
    assert body == track[-1:]


async def test_an_object_that_is_not_a_recording_is_refused() -> None:
    """The magic is checked before a single chunk is decrypted, so a
    truncated upload or a wrong key fails as a refusal rather than as
    plausible-looking noise on somebody's speakers."""
    source = FakeAudioSource({S3_KEY: b"NOTSTRN" + b"\x00" * 200})
    with pytest.raises(CorruptRecording):
        await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(0, 100)))


async def test_a_truncated_recording_is_refused(track: bytes, tmp_path: Path) -> None:
    full = sealed(track, tmp_path)
    source = FakeAudioSource({S3_KEY: full[: len(full) // 2]})
    with pytest.raises(CorruptRecording):
        await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(0, len(track) - 1)))


# ---------------------------------------------------------------------------
# The property the encryption scheme exists for
# ---------------------------------------------------------------------------

_SERVING_PATH = (
    Path(__file__).parent.parent.parent / "src" / "sturnus" / "console",
    ("audio.py", "routes_audio.py", "spectrogram.py"),
)

_WRITES_TO_DISK = frozenset(
    {
        "open",
        "write_bytes",
        "write_text",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "TemporaryDirectory",
        "copyfileobj",
    }
)


def test_nothing_on_the_serving_path_can_write_plaintext_to_disk() -> None:
    """The one property the whole envelope-encryption scheme buys.

    Decrypted voice exists only in the chunk buffer on its way to the
    socket. A temp file here -- for a seek, for a cache, for convenience --
    would undo the encryption for every recording the console has ever
    served, and it would do so invisibly. Reviewing for it once is not the
    same as being unable to do it.
    """
    directory, names = _SERVING_PATH
    offenders: list[str] = []
    for name in names:
        tree = ast.parse((directory / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if called in _WRITES_TO_DISK:
                offenders.append(f"{name}:{node.lineno}: calls {called}()")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# The round trip: what the bot recorded is what the console serves
# ---------------------------------------------------------------------------


def _recorded_track(tmp_path: Path, seconds: float = 1.0) -> tuple[bytes, int]:
    """One speaker's track, written by the real writer from real Discord PCM.

    Goes through `FileAudioWriterFactory` rather than assembling a WAV by
    hand, for the same reason `sealed` goes through `encrypt_file`: a
    fixture that restates the format agrees with itself and with nothing
    else, and the format is precisely what was wrong.
    """
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    writer = FileAudioWriterFactory(tmp_path / "recordings").open(1, ANNA, epoch)
    frames = int(seconds * SOURCE_RATE) // _SAMPLES_PER_FRAME
    for index in range(frames):
        # 48 kHz 16-bit stereo, exactly what `ResilientOpusDecoder` returns.
        writer.write(
            epoch + timedelta(seconds=index * _SAMPLES_PER_FRAME / SOURCE_RATE),
            b"\x11\x22" * (_SAMPLES_PER_FRAME * 2),
        )
    writer.close()
    return writer.path.read_bytes(), frames * _SAMPLES_PER_FRAME * TARGET_RATE // SOURCE_RATE


@pytest.mark.asyncio
async def test_the_console_serves_the_track_the_bot_actually_recorded(tmp_path: Path) -> None:
    """The one property no test held: writer and reader agree on the format.

    `SpeakerWriter` writes a RIFF/WAVE container at 16 kHz mono -- Whisper's
    own format, converted on arrival so nothing has to resample later. The
    reader here used to declare 48 kHz stereo and prepend a second header
    of its own, so a listener got the stored header played as samples and
    the rest at six times speed: two channels of alternating mono samples,
    three times the rate. Unintelligible, and indistinguishable from a
    capture that had genuinely failed.

    Asserting through `wave` rather than on bytes is deliberate: it is the
    question a player asks, and it cannot pass while the response carries a
    header that describes something the payload is not.
    """
    track, expected_frames = _recorded_track(tmp_path)
    ciphertext = sealed(track, tmp_path)
    source = FakeAudioSource({S3_KEY: ciphertext})

    total = stored_length(len(ciphertext))
    served = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(0, total - 1)))

    assert served == track, "the served resource is the stored track, byte for byte"
    with wave.open(io.BytesIO(served)) as played:
        assert played.getframerate() == TARGET_RATE
        assert played.getnchannels() == 1
        assert played.getsampwidth() == 2
        assert played.getnframes() == expected_frames
