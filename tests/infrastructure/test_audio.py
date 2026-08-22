import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import soxr  # type: ignore[import-untyped]

from sturnus.infrastructure.audio import (
    SOURCE_RATE,
    TARGET_RATE,
    SpeakerWriter,
    to_mono_16k,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def samples(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == TARGET_RATE
        assert w.getsampwidth() == 2
        return w.getnframes()


def tone(frames: int) -> bytes:
    """`frames` samples of non-silence at 16 kHz mono, 16-bit."""
    return b"\x10\x27" * frames


def test_resampling_reduces_stereo_48k_to_mono_16k() -> None:
    # 4800 stereo frames at 48 kHz = 100 ms; expect ~1600 mono frames at 16 kHz.
    src = b"\x00\x10" * 2 * 4800
    out = to_mono_16k(src)
    frames = len(out) // 2
    assert abs(frames - 1600) <= 8  # resampler edge effects


def test_first_write_defines_no_leading_silence(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(1600))
    w.close()
    assert samples(path) == 1600


def test_a_gap_is_filled_with_silence(tmp_path: Path) -> None:
    path = tmp_path / "b.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(1600))  # 0.0 - 0.1 s
    w.write(T0 + timedelta(seconds=5), tone(1600))  # 5.0 - 5.1 s
    w.close()
    # 5.1 seconds of timeline at 16 kHz.
    assert samples(path) == pytest.approx(int(5.1 * TARGET_RATE), abs=8)


def test_silence_is_actually_silent(tmp_path: Path) -> None:
    path = tmp_path / "c.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(160))
    w.write(T0 + timedelta(seconds=1), tone(160))
    w.close()
    with wave.open(str(path), "rb") as f:
        f.readframes(160)
        assert set(f.readframes(int(0.5 * TARGET_RATE))) == {0}


def test_a_late_packet_does_not_rewind(tmp_path: Path) -> None:
    """An out-of-order packet must not corrupt the file.

    The signed-delta fix in the speaker clock means a timestamp can precede
    one already written. Seeking backwards in a sequential file is not
    possible, so such a packet is appended at the current position rather
    than dropped — losing its exact placement but never its content.
    """
    path = tmp_path / "d.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0 + timedelta(seconds=1), tone(1600))
    before = w.samples_written
    w.write(T0 + timedelta(seconds=0.5), tone(1600))
    w.close()
    assert w.samples_written == before + 1600
    assert samples(path) == before + 1600


def test_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "e.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(160))
    w.close()
    w.close()
    assert samples(path) == 160


def test_a_writer_that_never_receives_audio_yields_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "f.wav"
    SpeakerWriter(path, epoch=T0).close()
    assert samples(path) == 0


def test_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    w = SpeakerWriter(tmp_path / "g.wav", epoch=T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        w.write(datetime(2026, 8, 19, 20, 0, 0), tone(160))
    w.close()


def test_a_frame_converts_to_the_same_length_with_or_without_history() -> None:
    """The property the writer depends on.

    Audio is placed by RTP-derived time, so a conversion that sometimes
    returned a different number of samples would shift the timeline
    instead of improving it -- and `soxr`'s own streaming API, which
    returns 0 samples for one chunk and 490 for the next, would do exactly
    that.
    """
    frame = _stereo_48k(960)
    assert len(to_mono_16k(frame)) == len(to_mono_16k(frame, _stereo_48k(960)))
    assert len(to_mono_16k(frame)) == len(frame) // 6


def test_history_removes_the_boundary_artefact_between_frames() -> None:
    """Why the history exists, as a measurement rather than a claim.

    A resampling filter needs signal on both sides of what it produces.
    Given one 20 ms frame alone it rings in from silence and out into
    silence, fifty times a second. Compared against resampling the same
    audio as one continuous stream, that costs about 12 dB -- and two
    thirds of the error sits above 4 kHz, which is why it was audible as
    roughness rather than visible on a level meter.
    """
    rate, frames = SOURCE_RATE, 40
    samples = int(rate * 0.02) * frames
    t = np.arange(samples) / rate
    tone = sum(np.sin(2 * np.pi * 180 * k * t) / k for k in range(1, 12))
    mono = (tone / np.abs(tone).max() * 0.3 * 32767).astype(np.int16)
    stereo = np.repeat(mono[:, None], 2, axis=1)

    reference = soxr.resample(mono, SOURCE_RATE, TARGET_RATE).astype(float)

    def convert(with_history: bool) -> np.ndarray:
        out, history = b"", b""
        for index in range(frames):
            block = stereo[index * 960 : (index + 1) * 960].reshape(-1)
            frame = block.astype("<i2").tobytes()
            out += to_mono_16k(frame, history if with_history else b"")
            history = frame
        return np.frombuffer(out, "<i2").astype(float)

    def snr(produced: np.ndarray) -> float:
        count = min(len(produced), len(reference))
        error = produced[:count] - reference[:count]
        return float(10 * np.log10((reference[:count] ** 2).mean() / max((error**2).mean(), 1e-9)))

    without, with_it = snr(convert(False)), snr(convert(True))
    assert with_it > without + 8, f"history bought only {with_it - without:.1f} dB"


def _stereo_48k(samples: int) -> bytes:
    """`samples` frames of 48 kHz 16-bit stereo, as the decoder returns them."""
    t = np.arange(samples) / SOURCE_RATE
    mono = (np.sin(2 * np.pi * 300 * t) * 8000).astype(np.int16)
    return np.repeat(mono[:, None], 2, axis=1).reshape(-1).astype("<i2").tobytes()
