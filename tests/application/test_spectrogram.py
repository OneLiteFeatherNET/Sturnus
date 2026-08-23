"""The stored form of a picture, and everything a reader must refuse.

`tests/console/test_spectrogram.py` covers what the picture *says* -- that
a tone lands in the row it belongs to and that silence draws empty. This
file covers the other half, which only exists because a spectrogram is
stored now: the bytes that go into the bucket, and the reading of them.

The theme throughout is that a stored artefact outlives the process that
wrote it. It survives a deployment, a `COLUMNS` change, a partially
written object and a key that belongs to another recording, and in every
one of those cases the honest answer is to refuse it and draw the track
again -- which the caller can always do, because the audio it was drawn
from is still there. A picture that renders and is wrong is the one
outcome worth failing to prevent.
"""

from __future__ import annotations

import base64
import json

import pytest

from sturnus.application.spectrogram import (
    ARTEFACT_VERSION,
    BINS,
    COLUMNS,
    Spectrogram,
    decode_artefact,
    draw,
    encode_artefact,
)
from sturnus.domain.errors import CorruptRecording


def picture(sample_rate: int = 16_000, duration: float = 12.5) -> Spectrogram:
    """A picture of the shape this build draws, with recognisable cells."""
    cells = bytes(range(256)) * (COLUMNS * BINS // 256)
    return Spectrogram(
        columns=COLUMNS,
        bins=BINS,
        sample_rate=sample_rate,
        duration_seconds=duration,
        magnitudes=base64.b64encode(cells).decode("ascii"),
    )


def artefact(**overrides: object) -> bytes:
    """A stored artefact with fields replaced, for the refusal tests."""
    document = json.loads(encode_artefact(picture()))
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


async def _pieces(*chunks: bytes) -> object:
    for chunk in chunks:
        yield chunk


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_a_stored_picture_reads_back_as_the_picture_that_was_stored() -> None:
    """The console answers from the artefact, so the artefact has to carry
    everything the response has: the axes as well as the cells."""
    original = picture(sample_rate=16_000, duration=3.25)

    restored = decode_artefact(encode_artefact(original))

    assert restored == original


def test_the_frequency_axis_survives_the_round_trip() -> None:
    """`hz_per_bin` is derived from the sample rate, and the sample rate is
    read from the track's own header. A picture that lost it would be
    labelled with whatever the client assumed."""
    restored = decode_artefact(encode_artefact(picture(sample_rate=8_000)))

    assert restored.hz_per_bin == pytest.approx(8_000 / 2 / BINS)


def test_an_artefact_is_about_a_hundred_kilobytes_whatever_the_meeting_was() -> None:
    """The number an operator sizes a bucket with.

    Fixed by construction -- `COLUMNS` by `BINS` cells, base64'd, in a
    small envelope -- so a three-hour workshop costs exactly what a
    two-minute stand-up does. If this ever became proportional to the
    recording, the storage estimate this feature was accepted on would
    stop being true and nothing else would say so.
    """
    stored = encode_artefact(picture())

    assert COLUMNS * BINS == 76_800
    assert len(stored) < 110_000


# ---------------------------------------------------------------------------
# What a reader refuses, and why refusing is cheap
# ---------------------------------------------------------------------------


def test_a_picture_of_another_shape_is_refused() -> None:
    """The failure this check exists for is silent otherwise.

    An artefact drawn when `COLUMNS` was a different number still parses
    and still renders; it describes the track wrongly by however much the
    shape moved, and the client sizes its canvas from the numbers it was
    handed rather than from what arrived.
    """
    with pytest.raises(CorruptRecording):
        decode_artefact(artefact(columns=COLUMNS // 2))

    with pytest.raises(CorruptRecording):
        decode_artefact(artefact(bins=BINS + 8))


def test_a_picture_from_a_future_version_is_refused() -> None:
    with pytest.raises(CorruptRecording):
        decode_artefact(artefact(version=ARTEFACT_VERSION + 1))


def test_a_matrix_with_the_wrong_number_of_cells_is_refused() -> None:
    """A truncated upload is the ordinary cause, and it renders as a
    picture of a meeting that stopped early."""
    short = base64.b64encode(b"\x00" * (COLUMNS * BINS - 1)).decode("ascii")

    with pytest.raises(CorruptRecording):
        decode_artefact(artefact(magnitudes=short))


def test_something_that_is_not_an_artefact_at_all_is_refused() -> None:
    with pytest.raises(CorruptRecording):
        decode_artefact(b"not json")

    with pytest.raises(CorruptRecording):
        decode_artefact(b'["a list is not a document"]')


def test_an_artefact_missing_its_axes_is_refused() -> None:
    """Rather than defaulted. A sample rate this reader guessed is exactly
    the six-times-speed defect that made a spectrogram worth having."""
    with pytest.raises(CorruptRecording):
        decode_artefact(artefact(sample_rate=None))

    with pytest.raises(CorruptRecording):
        decode_artefact(artefact(magnitudes=None))


# ---------------------------------------------------------------------------
# Drawing, over a stream that is not S3
# ---------------------------------------------------------------------------


async def test_a_stream_that_never_carried_a_header_is_refused() -> None:
    """The worker's stream is a file rather than an object body, and an
    empty one is a decrypt that produced nothing -- which must not draw an
    empty picture and call it a recording."""
    with pytest.raises(CorruptRecording):
        await draw(_pieces())  # type: ignore[arg-type]


async def test_a_stream_of_something_that_is_not_a_track_is_refused() -> None:
    with pytest.raises(CorruptRecording):
        await draw(_pieces(b"RIFF" + b"\x00" * 200))  # type: ignore[arg-type]
