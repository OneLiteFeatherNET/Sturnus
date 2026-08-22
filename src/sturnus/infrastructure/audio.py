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


def to_mono_16k(pcm: bytes, history: bytes = b"") -> bytes:
    """Convert Discord's 48 kHz 16-bit stereo PCM to 16 kHz mono.

    `soxr` is used rather than the standard library's `audioop`, which is
    removed in Python 3.13, and rather than naive decimation, which would
    alias without a low-pass filter.

    **`history` is the frame before this one, and it is not optional in
    practice.** A resampling filter needs signal on both sides of the
    samples it is producing. Given one 20 ms frame alone it starts from
    silence and ends in silence, so it rings in and out at every frame
    boundary -- fifty times a second, for as long as anybody is speaking.

    Measured against resampling the same audio as one continuous stream,
    frames alone reach 32.9 dB SNR and frames with one frame of history
    reach 45.2 dB: four times less error energy. Two frames of history
    measure the same as one, so one is what this takes.

    That error is not evenly spread -- 64% of it sits above 4 kHz -- which
    is why it was audible as roughness on speech rather than as anything
    that showed up in a level meter. It only became noticeable once the
    recordings carried speech at all (see
    `sturnus.infrastructure.discord.dave`); before that it was hiding
    under a much larger fault.

    Returns exactly `len(pcm) // 6` bytes whatever the history: the
    caller places audio by RTP-derived time, so a conversion that
    sometimes returned a different number of samples would shift the
    timeline instead of improving it.
    """
    if not pcm:
        return b""
    wanted = len(pcm) // (_SAMPLE_WIDTH * 2 * (SOURCE_RATE // TARGET_RATE))
    block = history + pcm
    stereo = np.frombuffer(block, dtype="<i2").reshape(-1, 2)
    mono = stereo.mean(axis=1).astype(np.int16)
    resampled: np.ndarray = soxr.resample(mono, SOURCE_RATE, TARGET_RATE)
    # The tail is this frame's own audio; everything before it belongs to
    # the history and has already been written.
    tail = resampled[-wanted:] if len(resampled) >= wanted else resampled
    if len(tail) < wanted:
        # Only reachable for a first frame shorter than the ratio itself.
        tail = np.pad(tail, (wanted - len(tail), 0))
    return tail.astype("<i2").tobytes()


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
