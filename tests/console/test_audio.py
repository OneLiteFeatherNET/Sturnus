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
import struct
from pathlib import Path

import pytest

from sturnus.console.audio import (
    BYTES_PER_SECOND,
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    WAV_HEADER_BYTES,
    ByteRange,
    CorruptRecording,
    UnsatisfiableRange,
    parse_range,
    pcm_length,
    stream_wav,
    wav_header,
)
from sturnus.infrastructure.crypto import CHUNK_SIZE
from tests.console.conftest import (
    DATA_KEY,
    S3_KEY,
    FakeAudioSource,
    collect,
    sealed,
)

TOTAL = 1_000


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


def test_the_header_is_the_canonical_forty_four_bytes() -> None:
    """Canonical rather than merely valid: the offset of the first sample
    has to be a constant this server can add to a byte range, and an
    optional `LIST` or `fact` chunk would move it."""
    assert len(wav_header(1_000)) == WAV_HEADER_BYTES


def test_the_header_describes_what_discord_actually_handed_us() -> None:
    """48 kHz, stereo, signed 16-bit, little-endian -- what
    `FileAudioWriterFactory` writes and what `scripts/audio_sample.py`
    reads. Nothing in the encrypted file records it; it is a convention
    between the writer and every reader, and this is now one of those."""
    header = wav_header(0)
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    assert header[12:16] == b"fmt "
    assert struct.unpack_from("<I", header, 16)[0] == 16
    assert struct.unpack_from("<H", header, 20)[0] == 1
    assert struct.unpack_from("<H", header, 22)[0] == CHANNELS
    assert struct.unpack_from("<I", header, 24)[0] == SAMPLE_RATE
    assert struct.unpack_from("<I", header, 28)[0] == BYTES_PER_SECOND
    assert struct.unpack_from("<H", header, 32)[0] == CHANNELS * SAMPLE_WIDTH
    assert struct.unpack_from("<H", header, 34)[0] == SAMPLE_WIDTH * 8


def test_the_header_declares_the_length_of_the_whole_track() -> None:
    """The seek bar is drawn from this number, so it describes the track --
    never the slice a `Range` asked for."""
    header = wav_header(500_000)
    assert header[36:40] == b"data"
    assert struct.unpack_from("<I", header, 40)[0] == 500_000
    assert struct.unpack_from("<I", header, 4)[0] == 500_000 + 36


def test_a_track_too_long_for_the_header_declares_the_most_it_can() -> None:
    """RIFF sizes are unsigned 32-bit, so a track past about six hours
    cannot be described honestly. Clamping keeps the header well-formed --
    the alternative is `struct.error` at the moment somebody finally
    records a long meeting."""
    header = wav_header(5 * 1024**3)
    assert struct.unpack_from("<I", header, 40)[0] == 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Deriving the track's length from the ciphertext
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    [0, 1, 5_000, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, CHUNK_SIZE * 2 + 7],
)
def test_the_track_length_is_derived_without_decrypting_anything(size: int, tmp_path: Path) -> None:
    """`plaintext + 16` per chunk, `+4` per length prefix, `+13` per file.

    Exercised against files the real encryptor wrote, at every boundary
    that could be off by one: empty, part of a chunk, exactly a chunk, and
    a chunk plus a remainder.
    """
    assert pcm_length(len(sealed(b"\x01" * size, tmp_path))) == size


@pytest.mark.parametrize("ciphertext_bytes", [0, 5, 12, 14, 20])
def test_an_object_too_short_to_be_a_recording_is_refused(ciphertext_bytes: int) -> None:
    with pytest.raises(CorruptRecording):
        pcm_length(ciphertext_bytes)


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
    span = ByteRange(0, WAV_HEADER_BYTES + len(track) - 1)
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, span, len(track)))
    assert body == wav_header(len(track)) + track


async def test_the_header_comes_before_a_single_sample(
    source: FakeAudioSource, track: bytes
) -> None:
    """A player reads the format before it reads audio; a stream that sent
    PCM first would be noise until the header arrived, which it never
    would."""
    pieces = stream_wav(
        source, S3_KEY, DATA_KEY, ByteRange(0, WAV_HEADER_BYTES + len(track) - 1), len(track)
    )
    first = await anext(aiter(pieces))
    assert first == wav_header(len(track))


async def test_a_range_inside_the_header_returns_only_header_bytes(
    source: FakeAudioSource, track: bytes
) -> None:
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(4, 11), len(track)))
    assert body == wav_header(len(track))[4:12]
    assert source.streamed_bytes == 0


async def test_a_range_that_straddles_the_header_joins_the_two(
    source: FakeAudioSource, track: bytes
) -> None:
    body = await collect(
        stream_wav(source, S3_KEY, DATA_KEY, ByteRange(40, WAV_HEADER_BYTES + 9), len(track))
    )
    assert body == wav_header(len(track))[40:] + track[:10]


async def test_a_range_starting_mid_track_yields_the_rest_of_it(
    source: FakeAudioSource, track: bytes
) -> None:
    first = WAV_HEADER_BYTES + CHUNK_SIZE + 12_345
    body = await collect(
        stream_wav(
            source,
            S3_KEY,
            DATA_KEY,
            ByteRange(first, WAV_HEADER_BYTES + len(track) - 1),
            len(track),
        )
    )
    assert body == track[CHUNK_SIZE + 12_345 :]


async def test_a_listener_who_wants_the_end_does_not_download_the_beginning(
    source: FakeAudioSource, track: bytes
) -> None:
    """The reason `Range` is here at all.

    The chunked format allows starting at a chunk boundary, so a request
    for a byte inside chunk two fetches ciphertext from chunk two onwards
    and discards only the remainder of that one chunk -- not the two before
    it.
    """
    first = WAV_HEADER_BYTES + CHUNK_SIZE * 2 + 99
    await collect(
        stream_wav(
            source,
            S3_KEY,
            DATA_KEY,
            ByteRange(first, WAV_HEADER_BYTES + len(track) - 1),
            len(track),
        )
    )
    assert source.streamed_bytes < len(track) - CHUNK_SIZE


async def test_a_range_that_ends_early_stops_reading_there(
    source: FakeAudioSource, track: bytes
) -> None:
    """The other half of the same promise: a player that asked for one
    chunk must not cost a whole track's worth of egress."""
    body = await collect(
        stream_wav(
            source, S3_KEY, DATA_KEY, ByteRange(WAV_HEADER_BYTES, WAV_HEADER_BYTES + 9), len(track)
        )
    )
    assert body == track[:10]
    assert source.streamed_bytes < CHUNK_SIZE * 2


async def test_the_last_byte_of_the_track_is_reachable(
    source: FakeAudioSource, track: bytes
) -> None:
    """An off-by-one at the end is invisible in a player and fatal to a
    client that checksums what it got."""
    last = WAV_HEADER_BYTES + len(track) - 1
    body = await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(last, last), len(track)))
    assert body == track[-1:]


async def test_an_object_that_is_not_a_recording_is_refused() -> None:
    """The magic is checked before a single chunk is decrypted, so a
    truncated upload or a wrong key fails as a refusal rather than as
    plausible-looking noise on somebody's speakers."""
    source = FakeAudioSource({S3_KEY: b"NOTSTRN" + b"\x00" * 200})
    with pytest.raises(CorruptRecording):
        await collect(stream_wav(source, S3_KEY, DATA_KEY, ByteRange(0, 100), 100))


async def test_a_truncated_recording_is_refused(track: bytes, tmp_path: Path) -> None:
    full = sealed(track, tmp_path)
    source = FakeAudioSource({S3_KEY: full[: len(full) // 2]})
    with pytest.raises(CorruptRecording):
        await collect(
            stream_wav(
                source,
                S3_KEY,
                DATA_KEY,
                ByteRange(0, WAV_HEADER_BYTES + len(track) - 1),
                len(track),
            )
        )


# ---------------------------------------------------------------------------
# The property the encryption scheme exists for
# ---------------------------------------------------------------------------

_SERVING_PATH = (
    Path(__file__).parent.parent.parent / "src" / "sturnus" / "console",
    ("audio.py", "routes_audio.py"),
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
