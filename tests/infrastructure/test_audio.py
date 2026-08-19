import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sturnus.infrastructure.audio import TARGET_RATE, SpeakerWriter, to_mono_16k

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
