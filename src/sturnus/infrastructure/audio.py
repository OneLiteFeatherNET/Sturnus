"""Writing one speaker's stream as a continuous 16 kHz mono WAV file.

Discord sends no packets while a participant is silent, so silence has to be
inserted deliberately. Without it the file would be that speaker's utterances
spliced together with every pause removed, and each offset the transcription
returns would point at the wrong moment.

Audio is converted to Whisper's own format on arrival — 16 kHz mono — so no
resampling is needed later.
"""

from __future__ import annotations

import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import soxr  # type: ignore[import-untyped]

SOURCE_RATE = 48_000
TARGET_RATE = 16_000
_SAMPLE_WIDTH = 2


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value


def to_mono_16k(pcm: bytes) -> bytes:
    """Convert Discord's 48 kHz 16-bit stereo PCM to 16 kHz mono.

    `soxr` is used rather than the standard library's `audioop`, which is
    removed in Python 3.13, and rather than naive decimation, which would
    alias without a low-pass filter.
    """
    if not pcm:
        return b""
    stereo = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
    mono = stereo.mean(axis=1).astype(np.int16)
    resampled: np.ndarray = soxr.resample(mono, SOURCE_RATE, TARGET_RATE)
    return resampled.astype("<i2").tobytes()


class SpeakerWriter:
    """Appends one speaker's audio, padding the gaps between packets."""

    def __init__(self, path: Path, epoch: datetime) -> None:
        self.epoch = _require_aware(epoch)
        self.samples_written = 0
        self._wave = wave.open(str(path), "wb")  # noqa: SIM115 — handle spans write()/close()
        self._wave.setnchannels(1)
        self._wave.setsampwidth(_SAMPLE_WIDTH)
        self._wave.setframerate(TARGET_RATE)
        self._closed = False

    def write(self, at: datetime, pcm_16k_mono: bytes) -> None:
        _require_aware(at)
        if self._closed:
            raise RuntimeError("writer is closed")

        expected = int((at - self.epoch).total_seconds() * TARGET_RATE)
        gap = expected - self.samples_written
        if gap > 0:
            self._wave.writeframes(b"\x00" * (gap * _SAMPLE_WIDTH))
            self.samples_written += gap
        # A negative gap means a packet arrived out of order. The file is
        # written sequentially, so its exact placement cannot be recovered;
        # appending keeps the words and loses only sub-second accuracy,
        # which is preferable to discarding speech.

        self._wave.writeframes(pcm_16k_mono)
        self.samples_written += len(pcm_16k_mono) // _SAMPLE_WIDTH

    def close(self) -> None:
        if not self._closed:
            self._wave.close()
            self._closed = True
