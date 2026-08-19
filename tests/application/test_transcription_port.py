from datetime import UTC, datetime, timedelta

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
    to_absolute,
)
from sturnus.domain.transcript import SpeakerIdentity

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA = SpeakerIdentity(1, "anna")


def result(*pairs: tuple[float, float, str]) -> TranscriptionResult:
    return TranscriptionResult(
        segments=tuple(TranscribedSegment(s, e, t) for s, e, t in pairs),
        language="de",
    )


def test_offsets_are_anchored_to_the_audio_epoch() -> None:
    """The epoch is sample zero of the recording, not when the speaker joined."""
    epoch = T0 + timedelta(seconds=30)
    segments = to_absolute(result((0.0, 1.5, "hello")), epoch, ANNA)
    assert segments[0].start == epoch
    assert segments[0].end == epoch + timedelta(seconds=1.5)


def test_several_segments_keep_their_spacing() -> None:
    segments = to_absolute(result((0.0, 1.0, "a"), (10.0, 11.0, "b")), T0, ANNA)
    assert segments[1].start - segments[0].start == timedelta(seconds=10)


def test_the_speaker_is_attached_to_every_segment() -> None:
    segments = to_absolute(result((0.0, 1.0, "a"), (2.0, 3.0, "b")), T0, ANNA)
    assert all(s.speaker == ANNA for s in segments)


def test_an_empty_result_yields_no_segments() -> None:
    assert to_absolute(result(), T0, ANNA) == []


def test_sub_second_offsets_survive_the_conversion() -> None:
    """Whisper reports fractional seconds; rounding them would misorder speakers."""
    segments = to_absolute(result((0.12, 0.34, "x")), T0, ANNA)
    assert segments[0].start == T0 + timedelta(milliseconds=120)
