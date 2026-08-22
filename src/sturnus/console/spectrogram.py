"""Turning one encrypted track into a picture of itself, without a file.

A listener who opens a recording wants to know where the speech is before
they decide what to play. A waveform answers that badly -- a constant
noise floor and a spoken sentence reach a similar peak -- and a
spectrogram answers it well, because speech has a shape nothing else in a
voice channel has: harmonic stacks under about 4 kHz, moving at syllable
rate.

It also answers a question the operator scripts were invented for. The
defect that made every recording sound like noise (see
`sturnus.console.audio`, fact 1) is *visible* here: audio played at six
times speed puts its energy in the wrong bands, and a track that is
genuinely empty is flat. Being able to see that in the console is the
difference between "the capture is broken" and "nobody spoke", which cost
a production investigation to tell apart by ear.

**Computed in one pass with bounded memory, never buffered whole.** An
hour of one speaker is about 115 MB of 16 kHz mono, and a handful of
concurrent viewers holding that in a pod would be an outage. The output
size is fixed up front -- `COLUMNS` by `BINS` -- so the arithmetic runs
the other way: the hop between windows is derived from the track's length,
each window is read as it streams past, and everything behind it is
dropped. Peak memory is one chunk of ciphertext plus one window.

**Nothing here writes plaintext to disk**, for the same reason
`sturnus.console.audio` does not, and pinned by the same static test in
`tests/console/test_audio.py`, which lists this module on the serving
path.

The response is deliberately a picture rather than numbers to interpret:
one byte per cell, 0 for the noise floor and 255 for the loudest cell in
*this* track, base64 over the whole matrix. That normalisation is a
decision. Absolute dBFS would render every quiet-but-fine recording as an
empty rectangle, and the question this view exists to answer is "where is
the speech in this track", not "how loud was it".
"""

from __future__ import annotations

import base64
import struct
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass

import numpy as np

from sturnus.console.audio import ByteRange, CorruptRecording, stream_wav
from sturnus.console.ports import EncryptedAudioSource

#: Time slices across the whole track. Fixed rather than proportional, so
#: the response is the same size for a two-minute stand-up and a
#: three-hour workshop, and so the client can size a canvas before it has
#: the data.
COLUMNS = 600

#: Samples per FFT window. At 16 kHz this is 64 ms -- long enough to
#: resolve a voiced fundamental (about 85 Hz for a low voice, and 1024
#: samples is twelve of its cycles), short enough that a syllable is not
#: smeared across the window.
WINDOW = 1024

#: Frequency rows per slice. **`WINDOW // 2` must be an exact multiple of
#: this**, and that is the whole reason for the number: 512 usable FFT
#: points over 128 rows is exactly 4 points each, so every row spans the
#: same width in Hz and row `r` starts at `r * sample_rate / WINDOW * 4`.
#: An axis a client can label with arithmetic instead of a lookup table.
#:
#: An earlier 96 divided 512 into a mix of five- and six-point rows, which
#: renders identically and makes the frequency axis a lie by up to half a
#: row -- the sort of quiet inaccuracy this file exists to make visible in
#: other people's data, so it does not get to have one of its own.
BINS = 128

#: The floor, in dB below this track's loudest cell. Everything at or
#: under it renders as 0. Sixty dB is the range a spectrogram is
#: conventionally drawn over: below that is the dither and the room, and
#: including it turns the picture grey.
DYNAMIC_RANGE_DB = 60.0

#: The magnitude a full-scale sine produces in one window: half the 16-bit
#: range, times the window length, over the Hann window's coherent gain of
#: one half.
_FULL_SCALE = 32768.0 * WINDOW / 4.0

#: The quietest peak still normalised against itself. Below this the track
#: is normalised against *this* value instead, so it renders as the empty
#: picture it is.
#:
#: Without it, "brightest cell becomes 255" has one catastrophic failure
#: mode: a track carrying nothing but resampler dither at -90 dBFS has a
#: brightest cell too, and stretching it over the full range draws a
#: convincing picture of a meeting that never happened. That is precisely
#: the wrong answer for the question this view exists to answer, and it is
#: how "the capture is broken" and "nobody spoke" became indistinguishable
#: in the first place.
#:
#: Set 60 dB below full scale, which is where `sturnus.domain.silence` also
#: puts the line (`SILENCE_PEAK_AMPLITUDE = 32`, about -60 dBFS): the bot
#: and the console then agree on what counts as a silent recording.
_SILENCE_FLOOR = _FULL_SCALE * 10.0 ** (-DYNAMIC_RANGE_DB / 20.0)

#: Below this, a cell is drawn as empty no matter what the rest of the
#: track looks like. The relative floor above is not enough on its own:
#: it moves the *reference*, and a cell can still sit within
#: `DYNAMIC_RANGE_DB` of a reference that is itself the floor.
#:
#: Measured, not guessed. `to_mono_16k` turns digital silence into
#: ±1 LSB of resampler dither -- audible to nothing, but it reaches about
#: -111 dBFS in a window, which is only 51 dB under the relative floor and
#: therefore *visible* without this. A -40 dBFS tone, far quieter than any
#: speech worth keeping, sits at -41 dBFS. Eighty decibels is comfortably
#: between the two: thirty above the dither, forty below the quietest
#: thing anybody meant to record.
_NOISE_MAGNITUDE = _FULL_SCALE * 10.0 ** (-80.0 / 20.0)

#: The canonical header `SpeakerWriter` writes, and the smallest prefix
#: that can describe a track at all.
_MIN_HEADER = 44


@dataclass(frozen=True)
class TrackFormat:
    """What a track's own RIFF header says it is.

    Read rather than assumed, and that is the whole lesson of the format
    defect this module was written after: `sturnus.console.audio` used to
    *state* the sample rate and was wrong by a factor of three. A file
    that describes itself is only useful to a reader that asks.
    """

    sample_rate: int
    channels: int
    sample_width: int
    data_offset: int
    data_bytes: int

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width

    @property
    def frames(self) -> int:
        return self.data_bytes // self.frame_bytes

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


@dataclass(frozen=True)
class Spectrogram:
    """One track as a picture, plus the axes needed to label it."""

    columns: int
    bins: int
    sample_rate: int
    duration_seconds: float
    #: Row-major, `bins` rows of `columns` bytes, row 0 the lowest
    #: frequency. Base64 because the client's destination is an
    #: `ImageData` buffer, and a JSON array of 76 800 numbers is several
    #: times the bytes to say the same thing.
    magnitudes: str

    @property
    def hz_per_bin(self) -> float:
        """The width of one row, which is what labels the frequency axis."""
        return self.sample_rate / 2 / self.bins


def parse_track_format(head: bytes) -> TrackFormat:
    """Reads the RIFF header a track begins with.

    Walks the chunk list rather than trusting the canonical 44-byte
    layout. `SpeakerWriter` writes exactly that layout today, but a reader
    that hardcodes an offset is how this system got a six-times-speed
    playback bug in the first place, and walking costs a dozen lines.
    """
    if len(head) < _MIN_HEADER or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise CorruptRecording("track does not begin with a RIFF/WAVE header")

    offset = 12
    fmt: tuple[int, int, int] | None = None
    while offset + 8 <= len(head):
        chunk_id = head[offset : offset + 4]
        (size,) = struct.unpack_from("<I", head, offset + 4)
        body = offset + 8
        if chunk_id == b"fmt " and body + 16 <= len(head):
            _, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", head, body)
            fmt = (rate, channels, bits // 8)
        elif chunk_id == b"data":
            if fmt is None:
                raise CorruptRecording("track has audio before it says what format it is in")
            rate, channels, width = fmt
            if rate <= 0 or channels <= 0 or width <= 0:
                raise CorruptRecording("track declares a format nothing can be decoded from")
            return TrackFormat(rate, channels, width, body, size)
        # Chunk bodies are padded to an even length; the size field is not.
        offset = body + size + (size & 1)
    raise CorruptRecording("track header has no data chunk")


async def spectrogram(
    source: EncryptedAudioSource,
    key: str,
    data_key: bytes,
    stored_bytes: int,
) -> Spectrogram:
    """Streams one encrypted track past an FFT and returns the picture.

    The track is read once, forwards, and never held: `_windows` yields
    one window per output column as the bytes go by, so peak memory does
    not grow with the length of the meeting.
    """
    if stored_bytes < _MIN_HEADER:
        raise CorruptRecording("object is too short to hold a track")

    pieces = stream_wav(source, key, data_key, ByteRange(0, stored_bytes - 1))
    columns: list[np.ndarray] = []
    fmt: TrackFormat | None = None
    try:
        async for window, track_format in _windows(pieces):
            fmt = track_format
            columns.append(_column(window, track_format.sample_rate))
    finally:
        await pieces.aclose()

    if fmt is None:
        raise CorruptRecording("track ended before its header was complete")
    return _render(columns, fmt)


async def _windows(
    pieces: AsyncIterator[bytes],
) -> AsyncGenerator[tuple[np.ndarray, TrackFormat], None]:
    """Yields `(window, format)` once per output column, in file order.

    The hop is derived from the declared data length, so the columns span
    the whole track regardless of how long it is. A track shorter than one
    window yields a single, zero-padded column rather than nothing -- a
    two-second recording still has a picture, and an empty rectangle would
    read as a failure it is not.
    """
    head = bytearray()
    fmt: TrackFormat | None = None
    consumed = 0  # frames of audio already passed
    buffer = np.empty(0, dtype=np.int16)
    wanted = 0  # index of the next column
    hop = 0
    total = 0

    async for piece in pieces:
        if fmt is None:
            head += piece
            if len(head) < _MIN_HEADER:
                continue
            fmt = parse_track_format(bytes(head))
            if fmt.sample_width != 2:
                raise CorruptRecording("only 16-bit tracks can be drawn")
            total = fmt.frames
            hop = max(1, (total - WINDOW) // max(1, COLUMNS - 1)) if total > WINDOW else WINDOW
            body = bytes(head[fmt.data_offset :])
            head = bytearray()
        else:
            body = piece

        if not body:
            continue
        buffer = np.concatenate([buffer, _mono(body, fmt)])

        # Emit every column whose window is now fully inside the buffer.
        while wanted < COLUMNS:
            start = wanted * hop
            if start + WINDOW > consumed + len(buffer):
                break
            if start >= total:
                wanted = COLUMNS
                break
            local = start - consumed
            yield buffer[local : local + WINDOW].astype(np.float32), fmt
            wanted += 1

        # Drop everything no future column can reach.
        keep_from = max(0, wanted * hop - consumed)
        if keep_from > 0:
            buffer = buffer[keep_from:]
            consumed += keep_from
        if wanted >= COLUMNS:
            break

    if fmt is None:
        return
    # A track shorter than one window, or a last column that ran past the
    # end: pad rather than drop, so short recordings still draw.
    while wanted < COLUMNS and wanted * hop < max(total, 1):
        start = wanted * hop
        local = max(0, start - consumed)
        tail = buffer[local : local + WINDOW]
        if len(tail) == 0:
            break
        padded = np.zeros(WINDOW, dtype=np.float32)
        padded[: len(tail)] = tail
        yield padded, fmt
        wanted += 1


def _mono(body: bytes, fmt: TrackFormat) -> np.ndarray:
    """The samples in `body` as one channel, whatever the track has."""
    usable = len(body) - (len(body) % fmt.frame_bytes)
    if usable <= 0:
        return np.empty(0, dtype=np.int16)
    samples = np.frombuffer(body[:usable], dtype="<i2")
    if fmt.channels == 1:
        return samples
    return samples.reshape(-1, fmt.channels).mean(axis=1).astype(np.int16)


def _column(window: np.ndarray, sample_rate: int) -> np.ndarray:
    """One time slice as `BINS` magnitudes, lowest frequency first.

    Hann-windowed, because a rectangular window on a voiced frame spreads
    its harmonics across the whole picture and the harmonic stack is
    precisely what makes speech recognisable here.

    The frequency axis is linear rather than mel or log. A mel axis is
    better for showing a *word*; a linear one is better for showing the
    fault this view exists to expose, because a track played at the wrong
    rate is a stack that has moved bodily up the axis, and only a linear
    axis makes that a translation rather than a distortion.
    """
    del sample_rate
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    # `rfft` returns `WINDOW/2 + 1` points; dropping DC leaves exactly
    # `WINDOW/2`, which `BINS` divides evenly (see its comment). Each row
    # is the *peak* of its group rather than the mean, which keeps a
    # narrow harmonic visible instead of averaging it into the floor
    # beside it -- and the harmonic stack is what identifies speech here.
    group = WINDOW // 2 // BINS
    return np.maximum.reduceat(
        spectrum[1 : 1 + WINDOW // 2], np.arange(0, WINDOW // 2, group)
    ).astype(np.float32)


def _render(columns: list[np.ndarray], fmt: TrackFormat) -> Spectrogram:
    """Normalises the collected slices into one byte per cell."""
    matrix = np.stack(columns, axis=1) if columns else np.zeros((BINS, 1), dtype=np.float32)

    # Normalised against the loudest cell *or* the silence floor, whichever
    # is louder. For any real recording the peak wins and nothing changes;
    # for a track with no signal in it the floor wins, every cell lands
    # more than `DYNAMIC_RANGE_DB` below it, and the picture is empty --
    # which is the truth about that track.
    reference = max(float(matrix.max()), _SILENCE_FLOOR)
    decibels = 20.0 * np.log10(np.maximum(matrix, 1e-12) / reference)
    clipped = np.clip(decibels, -DYNAMIC_RANGE_DB, 0.0)
    scaled = ((clipped + DYNAMIC_RANGE_DB) / DYNAMIC_RANGE_DB * 255.0).astype(np.uint8)
    # The absolute cut, applied last so it wins over the relative scale.
    scaled[matrix < _NOISE_MAGNITUDE] = 0

    # Padded to the promised width so the client can treat the payload as
    # a fixed-size image. A short track is short because it is short, and
    # the columns it does not have are floor.
    if scaled.shape[1] < COLUMNS:
        scaled = np.pad(scaled, ((0, 0), (0, COLUMNS - scaled.shape[1])))

    return Spectrogram(
        columns=COLUMNS,
        bins=BINS,
        sample_rate=fmt.sample_rate,
        duration_seconds=round(fmt.duration_seconds, 3),
        magnitudes=base64.b64encode(scaled.tobytes()).decode("ascii"),
    )
