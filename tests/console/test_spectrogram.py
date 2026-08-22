"""What the picture of a track has to get right to be worth showing.

A spectrogram that renders is not the same as a spectrogram that is true,
and the difference matters more here than in most views: this one exists
so that a human can look at a recording and tell "nobody spoke" from "the
capture is broken" — the two things a production investigation could not
separate by ear. A picture that is merely plausible would answer that
question wrongly and confidently.

So the tests below drive the real writer, the real encryptor and the real
reader, and assert on where the energy lands: a tone at a known frequency
must appear in the row that frequency belongs to, and silence must be
empty rather than noise stretched to fill the range.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from sturnus.console.audio import CorruptRecording, stored_length
from sturnus.console.spectrogram import (
    BINS,
    COLUMNS,
    WINDOW,
    parse_track_format,
    spectrogram,
)
from sturnus.infrastructure.audio import SOURCE_RATE, TARGET_RATE
from sturnus.infrastructure.recording_adapters import FileAudioWriterFactory
from tests.console.conftest import (
    ANNA,
    DATA_KEY,
    S3_KEY,
    FakeAudioSource,
    sealed,
)

#: One Discord voice frame: 20 ms at 48 kHz.
_SAMPLES_PER_FRAME = 960


def _write_track(tmp_path: Path, samples: np.ndarray) -> bytes:
    """`samples` (48 kHz mono) through the real writer, as stored bytes.

    Fed 20 ms at a time as stereo, which is what the sink hands over, so
    the track under test is one the bot could actually have produced.
    """
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    writer = FileAudioWriterFactory(tmp_path / "recordings").open(1, ANNA, epoch)
    stereo = np.repeat(samples[:, None], 2, axis=1)
    for index in range(len(samples) // _SAMPLES_PER_FRAME):
        block = stereo[index * _SAMPLES_PER_FRAME : (index + 1) * _SAMPLES_PER_FRAME]
        writer.write(
            epoch + timedelta(seconds=index * _SAMPLES_PER_FRAME / SOURCE_RATE),
            block.reshape(-1).astype("<i2").tobytes(),
        )
    writer.close()
    return writer.path.read_bytes()


def _tone(seconds: float, hz: float, *, at: tuple[float, float] | None = None) -> np.ndarray:
    """A sine over `at` seconds of an otherwise silent track."""
    t = np.arange(int(SOURCE_RATE * seconds)) / SOURCE_RATE
    signal = np.zeros_like(t)
    window = (t >= at[0]) & (t < at[1]) if at else np.ones_like(t, dtype=bool)
    signal[window] = np.sin(2 * np.pi * hz * t[window])
    return (signal * 0.6 * 32767).astype(np.int16)


async def _draw(track: bytes, tmp_path: Path) -> tuple[np.ndarray, float, float]:
    """The rendered matrix, its Hz-per-row and its duration."""
    ciphertext = sealed(track, tmp_path)
    picture = await spectrogram(
        FakeAudioSource({S3_KEY: ciphertext}),
        S3_KEY,
        DATA_KEY,
        stored_length(len(ciphertext)),
    )
    matrix = np.frombuffer(base64.b64decode(picture.magnitudes), np.uint8)
    return (
        matrix.reshape(picture.bins, picture.columns),
        picture.hz_per_bin,
        picture.duration_seconds,
    )


# ---------------------------------------------------------------------------
# Reading the format from the file rather than assuming it
# ---------------------------------------------------------------------------


def test_the_format_is_read_from_the_track_itself(tmp_path: Path) -> None:
    """The lesson of the six-times-speed defect, as a test.

    `sturnus.console.audio` used to state the sample rate and was wrong.
    Nothing here states it: the header the writer wrote is the only source.
    """
    fmt = parse_track_format(_write_track(tmp_path, _tone(1.0, 440))[:64])
    assert fmt.sample_rate == TARGET_RATE
    assert fmt.channels == 1
    assert fmt.sample_width == 2
    assert fmt.data_offset == 44


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"RIFF" + b"\x00" * 60,
        b"NOTARIFF" + b"\x00" * 60,
    ],
)
def test_something_that_is_not_a_track_is_refused(head: bytes) -> None:
    with pytest.raises(CorruptRecording):
        parse_track_format(head)


# ---------------------------------------------------------------------------
# Where the energy lands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hz", [250, 500, 1000, 3000])
async def test_a_tone_appears_in_the_row_its_frequency_belongs_to(hz: int, tmp_path: Path) -> None:
    """The frequency axis is arithmetic, so it is checked as arithmetic.

    `WINDOW // 2` divides evenly by `BINS`, which is what makes row `r`
    exactly `[r * hz_per_bin, (r + 1) * hz_per_bin)` and lets a client
    label the axis without a lookup table. A row layout that only looked
    right would put this assertion off by up to half a row.
    """
    matrix, hz_per_bin, _ = await _draw(_write_track(tmp_path, _tone(4.0, hz)), tmp_path)
    hottest = int(matrix.mean(axis=1).argmax())
    assert hottest * hz_per_bin <= hz <= (hottest + 1) * hz_per_bin


async def test_speech_shows_up_where_it_was_spoken_and_nowhere_else(tmp_path: Path) -> None:
    """The question this view exists to answer: *where* is the sound?"""
    track = _write_track(tmp_path, _tone(20.0, 800, at=(5.0, 10.0)))
    matrix, _, duration = await _draw(track, tmp_path)

    per_column = matrix.mean(axis=0)
    loud = [c for c in range(COLUMNS) if per_column[c] > per_column.max() * 0.5]
    first, last = min(loud) / COLUMNS * duration, max(loud) / COLUMNS * duration
    assert 4.5 <= first <= 5.5, f"sound starts at {first:.1f}s, expected 5s"
    assert 9.5 <= last <= 10.5, f"sound ends at {last:.1f}s, expected 10s"


async def test_a_silent_track_draws_as_empty_rather_than_as_noise(tmp_path: Path) -> None:
    """Normalising by the loudest cell has one failure mode, and this is it.

    A track of digital silence has no loudest cell. Dividing by it anyway
    would amplify the dither to fill the whole range and render a
    convincing picture of a meeting that never happened -- which is
    exactly the wrong answer for the one question this view is for.
    """
    matrix, _, _ = await _draw(
        _write_track(tmp_path, np.zeros(SOURCE_RATE * 3, np.int16)), tmp_path
    )
    assert matrix.max() == 0


# ---------------------------------------------------------------------------
# Size, shape and the lengths that break arithmetic
# ---------------------------------------------------------------------------


async def test_the_picture_is_the_same_size_whatever_the_meeting_was(tmp_path: Path) -> None:
    """A client sizes its canvas before it has the data, and a two-minute
    stand-up and a three-hour workshop must not need different code."""
    short, _, short_seconds = await _draw(_write_track(tmp_path, _tone(2.0, 440)), tmp_path)
    long, _, long_seconds = await _draw(_write_track(tmp_path, _tone(40.0, 440)), tmp_path)
    assert short.shape == long.shape == (BINS, COLUMNS)
    assert short_seconds == pytest.approx(2.0, abs=0.05)
    assert long_seconds == pytest.approx(40.0, abs=0.05)


async def test_a_track_shorter_than_one_window_still_draws(tmp_path: Path) -> None:
    """Under 64 ms there is not a full window to transform.

    Padding rather than dropping, because an empty rectangle reads as a
    failure and a very short recording is not one.
    """
    samples = _tone(WINDOW / TARGET_RATE / 2, 440)
    matrix, _, _ = await _draw(_write_track(tmp_path, samples), tmp_path)
    assert matrix.shape == (BINS, COLUMNS)


async def test_an_object_too_short_to_be_a_track_is_refused(tmp_path: Path) -> None:
    ciphertext = sealed(b"RIFF", tmp_path)
    with pytest.raises(CorruptRecording):
        await spectrogram(
            FakeAudioSource({S3_KEY: ciphertext}), S3_KEY, DATA_KEY, stored_length(len(ciphertext))
        )


async def test_the_whole_track_is_never_held_in_memory_at_once(tmp_path: Path) -> None:
    """The property that keeps a long meeting from being an outage.

    Asserted through the source rather than by measuring memory: the
    reader is handed the object in small pieces and must never accumulate
    them, so the largest buffer it can be holding is bounded by the chunk
    size and the window -- not by the length of the recording. What is
    checked here is that a track many times the size of a window still
    produces a fixed-size result from a single forward pass.
    """
    track = _write_track(tmp_path, _tone(60.0, 700))
    ciphertext = sealed(track, tmp_path)
    source = FakeAudioSource({S3_KEY: ciphertext})
    picture = await spectrogram(source, S3_KEY, DATA_KEY, stored_length(len(ciphertext)))

    assert len(base64.b64decode(picture.magnitudes)) == BINS * COLUMNS
    # One forward pass: the body was opened exactly once, at the first
    # chunk. A reader that re-read to seek would show several starts here.
    assert len(source.streamed_from) == 1
