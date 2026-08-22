"""Turning an encrypted recording in S3 into a WAV stream, in flight.

Everything here is deliberately free of aiohttp and of boto3, because the
hard parts of this endpoint are not HTTP and not S3. They are arithmetic:
where a chunk starts, how long a track is, and which byte a `Range` header
actually asked for. Each of those is wrong in a way that is invisible in a
player -- a seek bar that will not move, a last byte quietly missing, a
listener downloading half an hour to hear the thirty-first minute -- so
each of them is tested here without a server.

**Nothing on this path writes plaintext to disk.** Decrypted voice exists
only in the buffer below on its way to the socket. That is the single
property the whole envelope-encryption scheme (Spec 12.1) buys, and a temp
file here -- for a seek, for a cache, for convenience -- would undo it for
every recording the console has ever served, invisibly.
`tests/console/test_audio.py` checks it statically over this module and
`routes_audio`, because reviewing for it once is not the same as being
unable to do it.

**No transcoding, on purpose** (design section 5.1). Opus would be a tenth
of the bytes and needs `ffmpeg` in the image and a CPU budget in a process
that has neither. WAV over a compressed HTTP response is enough for
playback; the alternative is a decode pipeline in the request path.

Three facts hold this file together:

1. What the bot writes is raw interleaved PCM straight off Discord's
   decoder -- 48 kHz, stereo, signed 16-bit little-endian, no container.
   Nothing in the encrypted file records that; it is a convention between
   `FileAudioWriterFactory`, `scripts/audio_sample.py` and this module.
2. The ciphertext is framed: `MAGIC`, an 8-byte per-file nonce prefix, then
   repeated `[4-byte big-endian length][sealed chunk]`, every chunk full
   except the last. So chunk `n` starts at a computable offset, and the
   plaintext length follows from the object's size without decrypting a
   byte of it.
3. Every sealed chunk is exactly 16 bytes longer than its plaintext, which
   is what makes (2) exact rather than approximate.
"""

from __future__ import annotations

import re
import struct
from collections.abc import AsyncGenerator
from dataclasses import dataclass

# Importing the AEAD here rather than through an adapter is the same call
# `sturnus.console.ports` already makes for `ExternalIdentity`: the chunk
# format is not a collaborator that could plausibly be swapped, it is the
# bytes in the bucket.
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sturnus.console.ports import EncryptedAudioSource, KeyUnwrapper, TrackDirectory
from sturnus.infrastructure.crypto import (
    CHUNK_SIZE,
    FILE_PREFIX_BYTES,
    FRAME_BYTES,
    HEADER_BYTES,
    LENGTH_BYTES,
    MAGIC,
    TAG_BYTES,
    nonce,
)

#: What `FileAudioWriterFactory` writes. See fact (1) in the docstring.
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

#: The canonical RIFF/WAVE header: `RIFF`, `fmt ` and `data` and nothing
#: else. Canonical rather than merely valid, because the offset of the
#: first sample has to be a constant this server can add to a byte range,
#: and an optional `LIST` or `fact` chunk would move it.
WAV_HEADER_BYTES = 44

#: RIFF sizes are unsigned 32-bit. At 192000 bytes a second that runs out
#: after about six hours and twelve minutes of meeting.
_RIFF_MAX = 0xFFFFFFFF

#: `bytes=<first>-<last>`, `bytes=<first>-`, `bytes=-<suffix>`. One range
#: only: a multi-range request is answered with the whole resource, which
#: RFC 9110 explicitly permits and which no audio element ever sends.
_RANGE = re.compile(r"^\s*bytes\s*=\s*(?:(\d+)\s*-\s*(\d*)|-\s*(\d+))\s*$")


class CorruptRecording(Exception):
    """The object in the bucket is not a recording this reader understands.

    Raised before any plaintext is produced -- a wrong magic, a truncated
    upload, an object too short to be a recording at all. The alternative
    to refusing is plausible-looking noise on somebody's speakers.
    """


class UnsatisfiableRange(Exception):
    """The `Range` was understood and cannot be answered: 416, not 400."""


@dataclass(frozen=True)
class ByteRange:
    """A byte range over the served resource, inclusive at both ends.

    Inclusive because HTTP is, and converting at the edges is how the last
    byte of every partial response goes missing -- which no player
    complains about and no casual test notices.
    """

    first: int
    last: int

    @property
    def length(self) -> int:
        return self.last - self.first + 1


@dataclass(frozen=True)
class AudioDelivery:
    """Everything the audio endpoint needs from the world, in one value.

    One parameter to `build_api` rather than three, because that function
    is the seam the whole console is assembled through and every argument
    added to it is a line three parallel changes have to agree on.
    """

    tracks: TrackDirectory
    source: EncryptedAudioSource
    keys: KeyUnwrapper


def parse_range(header: str | None, total: int) -> ByteRange | None:
    """The `Range` header as a byte range, or `None` for the whole resource.

    `None` covers both "no header" and "a header this server does not
    understand", which RFC 9110 treats the same way: an unrecognised range
    unit is ignored, and a server may answer a multi-range request with the
    whole representation. Answering 416 to a header we merely failed to
    parse would break a client that would have played the track perfectly
    well from a plain 200.

    `UnsatisfiableRange` is reserved for the case where the syntax was
    fine and the answer cannot exist: a first byte at or past the end, or a
    suffix of zero bytes. That one is a 416.
    """
    if header is None:
        return None
    match = _RANGE.match(header)
    if match is None:
        return None

    first_text, last_text, suffix_text = match.groups()
    if suffix_text is not None:
        suffix = int(suffix_text)
        if suffix == 0 or total == 0:
            raise UnsatisfiableRange("a suffix of zero bytes cannot be answered")
        # A suffix longer than the resource is the whole resource, not an
        # error: `bytes=-5000` from a client that does not know the length
        # yet is an ordinary first request.
        return ByteRange(max(0, total - suffix), total - 1)

    first = int(first_text)
    if first >= total:
        raise UnsatisfiableRange("the range starts past the end of the recording")
    last = int(last_text) if last_text else total - 1
    if last < first:
        return None
    return ByteRange(first, min(last, total - 1))


def wav_header(pcm_bytes: int) -> bytes:
    """The 44-byte header for a track of `pcm_bytes` of audio.

    The length declared here is the *whole* track's, never the slice a
    `Range` asked for: a browser draws its seek bar from this number, and a
    header that describes the slice gives a player that cannot seek.

    Clamped at `_RIFF_MAX` rather than allowed to overflow. A track past
    six hours cannot be described honestly in this format, and the choice
    is between a header that understates the length and a `struct.error`
    at the moment somebody finally records a long meeting.
    """
    data = min(pcm_bytes, _RIFF_MAX)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        min(data + 36, _RIFF_MAX),
        b"WAVE",
        b"fmt ",
        16,  # the size of the fmt chunk that follows
        1,  # WAVE_FORMAT_PCM
        CHANNELS,
        SAMPLE_RATE,
        BYTES_PER_SECOND,
        CHANNELS * SAMPLE_WIDTH,
        SAMPLE_WIDTH * 8,
        b"data",
        data,
    )


def pcm_length(ciphertext_bytes: int) -> int:
    """How much audio an encrypted object of this size holds.

    Derived rather than measured, which is the point: the WAV header has to
    declare the track's length before the first byte of audio is sent, and
    the alternative is downloading and decrypting the whole recording to
    find out how long it is.

    The arithmetic is exact because the framing is: every chunk but the
    last is exactly `FRAME_BYTES` on disk, and every sealed chunk is
    exactly `TAG_BYTES` longer than its plaintext.
    """
    body = ciphertext_bytes - HEADER_BYTES
    if body < 0:
        raise CorruptRecording("object is too short to hold a recording header")
    full, remainder = divmod(body, FRAME_BYTES)
    if remainder == 0:
        # Either an empty recording or one whose length is an exact
        # multiple of the chunk size -- `encrypt_file`'s loop writes no
        # trailing empty chunk, so there is no partial frame to account for.
        return full * CHUNK_SIZE
    if remainder <= LENGTH_BYTES + TAG_BYTES:
        raise CorruptRecording("the final chunk is too short to be a sealed chunk")
    return full * CHUNK_SIZE + remainder - LENGTH_BYTES - TAG_BYTES


async def stream_wav(
    source: EncryptedAudioSource,
    key: str,
    data_key: bytes,
    span: ByteRange,
    pcm_bytes: int,
) -> AsyncGenerator[bytes, None]:
    """Yields `span` of the WAV representation of one encrypted recording.

    `span` is over the served resource -- header included -- because that
    is the thing the client is addressing. Byte 44 of the response is
    sample zero of the track.

    The seek is what makes `Range` worth having. A request whose first
    audio byte falls in chunk `n` fetches ciphertext from chunk `n`'s
    offset onwards and throws away only the part of *that* chunk which
    precedes it: at most one chunk of waste, rather than every chunk before
    it. AES-GCM will not decrypt half a chunk, so a chunk boundary is the
    finest seek this format allows, and that is the price of authenticating
    what is served.
    """
    if span.first < WAV_HEADER_BYTES:
        header = wav_header(pcm_bytes)
        yield header[span.first : min(span.last + 1, WAV_HEADER_BYTES)]
        if span.last < WAV_HEADER_BYTES:
            return

    first_sample = max(0, span.first - WAV_HEADER_BYTES)
    remaining = span.last - WAV_HEADER_BYTES - first_sample + 1
    if remaining <= 0:
        return

    prefix = await _file_prefix(source, key)
    aead = AESGCM(data_key)
    counter = first_sample // CHUNK_SIZE
    discard = first_sample % CHUNK_SIZE

    # A bytearray rather than repeated concatenation: a 4 MiB frame arrives
    # in pieces, and rebuilding the buffer for each one is quadratic in the
    # number of pieces.
    buffer = bytearray()
    pieces = source.stream(key, HEADER_BYTES + counter * FRAME_BYTES)
    try:
        async for piece in pieces:
            buffer += piece
            while remaining > 0:
                sealed = _take_frame(buffer)
                if sealed is None:
                    break
                plain = aead.decrypt(nonce(prefix, counter), bytes(sealed), None)
                counter += 1
                if discard:
                    plain = plain[discard:]
                    discard = 0
                if len(plain) > remaining:
                    plain = plain[:remaining]
                remaining -= len(plain)
                yield plain
            if remaining <= 0:
                return
    finally:
        # Closed explicitly rather than left to the loop's finalisation
        # hooks: a listener who stops halfway through should stop the
        # transfer out of S3 in the same breath, and a suspended generator
        # nobody closed holds that connection open until something else
        # gets round to it.
        await pieces.aclose()

    if remaining > 0:
        raise CorruptRecording("the recording ended before the requested range did")


async def _file_prefix(source: EncryptedAudioSource, key: str) -> bytes:
    """The per-file nonce prefix, read on its own before the body.

    A second request to the object store, and worth it: the alternative is
    opening the body at offset zero to read thirteen bytes and then
    abandoning it, which fetches the beginning of a recording that a seeking
    listener explicitly did not ask for.
    """
    header = await source.read(key, 0, HEADER_BYTES)
    if len(header) != HEADER_BYTES or not header.startswith(MAGIC):
        raise CorruptRecording("not a sturnus encrypted recording")
    return header[len(MAGIC) : len(MAGIC) + FILE_PREFIX_BYTES]


def _take_frame(buffer: bytearray) -> bytearray | None:
    """Removes and returns one complete sealed chunk, or `None` if partial.

    Returns the chunk rather than a view into the buffer, because the
    buffer is about to be truncated underneath it.
    """
    if len(buffer) < LENGTH_BYTES:
        return None
    (size,) = struct.unpack_from(">I", buffer)
    if size <= TAG_BYTES or size > CHUNK_SIZE + TAG_BYTES:
        raise CorruptRecording("chunk length prefix is not a possible chunk size")
    if len(buffer) < LENGTH_BYTES + size:
        return None
    sealed = buffer[LENGTH_BYTES : LENGTH_BYTES + size]
    del buffer[: LENGTH_BYTES + size]
    return sealed
