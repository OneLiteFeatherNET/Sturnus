"""What a finished job measured, as a value rather than five loose numbers.

These are the figures the console's dashboard is built from -- how long
somebody actually spoke, across every session they were in. Until now the
worker computed all of them and persisted none: they went into log lines
and metrics, both of which are retained for weeks while a session's own
row lives for as long as the guild does.
"""

from dataclasses import FrozenInstanceError

import pytest

from sturnus.domain.measurements import JobMeasurements


def test_it_carries_the_three_numbers_a_finished_job_produced() -> None:
    m = JobMeasurements(audio_seconds=521.0, speech_seconds=88.5, segment_count=42)
    assert m.audio_seconds == 521.0
    assert m.speech_seconds == 88.5
    assert m.segment_count == 42


def test_speech_can_never_exceed_the_audio_it_was_taken_from() -> None:
    """The gate removes silence; it cannot invent speech.

    A violation here means the two numbers were measured against different
    things -- which is exactly the confusion that made a track look
    'finished in 43 seconds': one figure was the concatenated speech and
    the other the padded file, and nothing compared them.
    """
    with pytest.raises(ValueError, match="speech"):
        JobMeasurements(audio_seconds=10.0, speech_seconds=11.0, segment_count=1)


def test_a_track_that_was_all_silence_is_a_valid_measurement() -> None:
    """Zero speech in a real recording is a fact worth storing, not an error.

    It is the difference between 'said nothing' and 'was never transcribed',
    and the console has to be able to show it.
    """
    m = JobMeasurements(audio_seconds=521.0, speech_seconds=0.0, segment_count=0)
    assert m.speech_seconds == 0.0


def test_negative_durations_are_refused() -> None:
    with pytest.raises(ValueError):
        JobMeasurements(audio_seconds=-1.0, speech_seconds=0.0, segment_count=0)
    with pytest.raises(ValueError):
        JobMeasurements(audio_seconds=1.0, speech_seconds=-1.0, segment_count=0)


def test_a_negative_segment_count_is_refused() -> None:
    with pytest.raises(ValueError):
        JobMeasurements(audio_seconds=1.0, speech_seconds=1.0, segment_count=-1)


def test_the_share_of_a_track_that_was_speech() -> None:
    """The one derived figure worth having: it separates a quiet participant
    from a broken microphone at a glance.
    """
    m = JobMeasurements(audio_seconds=200.0, speech_seconds=50.0, segment_count=5)
    assert m.speech_share == pytest.approx(0.25)


def test_the_speech_share_of_an_empty_track_is_zero_rather_than_undefined() -> None:
    """A zero-length track divides by zero. Reporting 0.0 keeps every caller
    from having to guard the same division.
    """
    assert JobMeasurements(0.0, 0.0, 0).speech_share == 0.0


def test_it_is_immutable() -> None:
    """A measurement is what was observed. Nothing downstream may adjust it.

    `FrozenInstanceError` specifically, not any exception: the point is
    that the dataclass is frozen, and a test that accepts an
    `AttributeError` from a typo would keep passing after somebody
    unfroze it.
    """
    m = JobMeasurements(1.0, 1.0, 1)
    with pytest.raises(FrozenInstanceError):
        m.audio_seconds = 2.0  # type: ignore[misc]
