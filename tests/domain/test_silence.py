"""Tests for the amplitude watch that tells a quiet meeting from a dead microphone.

Every case here is about the distinction the feature exists to make:
audio that *arrived and decoded* but carries nothing audible is a fault,
while audio that carries something -- however quiet -- is a meeting.
Nothing in this module touches Discord, a database or a file; the watch is
pure arithmetic over bytes, which is exactly why it lives in the domain.
"""

from sturnus.domain.silence import (
    BYTES_PER_SAMPLE_FRAME,
    SILENCE_EVIDENCE_BYTES,
    SILENCE_PEAK_AMPLITUDE,
    SOURCE_SAMPLE_RATE_HZ,
    SilentAudioWatch,
    peak_amplitude,
)

ANNA, BEN = 100, 200


def _pcm(seconds: float, sample: int) -> bytes:
    """`seconds` of 48 kHz 16-bit stereo PCM in which every sample is `sample`."""
    frames = int(seconds * SOURCE_SAMPLE_RATE_HZ)
    return int(sample & 0xFFFF).to_bytes(2, "little") * 2 * frames


def silent(seconds: float) -> bytes:
    return _pcm(seconds, 0)


def audible(seconds: float) -> bytes:
    return _pcm(seconds, 10_000)


def test_digital_silence_peaks_at_zero() -> None:
    assert peak_amplitude(silent(0.02)) == 0


def test_the_peak_is_the_loudest_sample_in_the_packet() -> None:
    assert peak_amplitude(b"\x00\x00" + b"\x10\x27" + b"\x00\x00") == 10_000


def test_a_negative_sample_counts_by_its_magnitude() -> None:
    """A waveform is symmetric around zero; only distance from it is loudness.

    Reading the peak off the signed values alone would report a loud
    negative half-cycle as quieter than digital silence, and a speaker
    whose packet happened to land on one would be warned about mid-
    sentence.
    """
    assert peak_amplitude((-10_000 & 0xFFFF).to_bytes(2, "little")) == 10_000


def test_an_empty_packet_has_no_peak() -> None:
    """The decoder hands back `b""` for a frame it could not conceal."""
    assert peak_amplitude(b"") == 0


def test_a_trailing_half_sample_is_ignored_rather_than_raising() -> None:
    """`peak_amplitude` is called on every frame of every speaker, and the
    packet length comes from outside this process. A byte count that is not
    a whole number of samples must cost the last half-sample, never the
    session.
    """
    assert peak_amplitude(b"\x10\x27\x00") == 10_000


def test_nothing_is_reported_before_the_evidence_threshold() -> None:
    """People are silent for most of a meeting. That is never news."""
    watch = SilentAudioWatch()
    fired = [watch.observe(ANNA, silent(1.0)) for _ in range(29)]
    assert not any(fired)


def test_the_threshold_reports_exactly_once_and_then_never_again() -> None:
    """One message per speaker per session -- the second one only annoys."""
    watch = SilentAudioWatch()
    for _ in range(29):
        assert watch.observe(ANNA, silent(1.0)) is False
    assert watch.observe(ANNA, silent(1.0)) is True
    for _ in range(60):
        assert watch.observe(ANNA, silent(1.0)) is False


def test_audible_audio_discards_the_evidence_collected_so_far() -> None:
    """A single audible frame means the microphone works, whatever came before.

    The evidence has to be *continuous*: someone who spoke twenty seconds
    ago and has been quiet since is a person in a meeting, not a fault.
    """
    watch = SilentAudioWatch()
    for _ in range(29):
        watch.observe(ANNA, silent(1.0))
    assert watch.observe(ANNA, audible(0.02)) is False
    for _ in range(29):
        assert watch.observe(ANNA, silent(1.0)) is False
    assert watch.observe(ANNA, silent(1.0)) is True


def test_each_speaker_is_watched_separately() -> None:
    """Ben talking says nothing about whether Anna's microphone works."""
    watch = SilentAudioWatch()
    for _ in range(29):
        watch.observe(ANNA, silent(1.0))
        watch.observe(BEN, audible(1.0))
    assert watch.observe(BEN, audible(1.0)) is False
    assert watch.observe(ANNA, silent(1.0)) is True


def test_the_noise_floor_counts_as_silence() -> None:
    """Opus does not always decode digital silence back to exact zeros.

    A hard `== 0` test would therefore report nothing at all on the very
    recordings this feature was written for -- the threshold is what makes
    it detect a muted microphone rather than only a zeroed buffer.

    The amplitude here is a literal, deliberately not
    `SILENCE_PEAK_AMPLITUDE` itself: a test written against the constant
    passes for *any* value of it, including the `0` this case exists to
    rule out. `16` is about -66 dBFS, the order of magnitude a lossy codec
    leaves behind on a silent input.
    """
    watch = SilentAudioWatch()
    for _ in range(29):
        assert watch.observe(ANNA, _pcm(1.0, 16)) is False
    assert watch.observe(ANNA, _pcm(1.0, 16)) is True


def test_quiet_speech_is_audio_and_is_never_warned_about() -> None:
    """The other side of the same boundary, pinned with a literal for the
    same reason: a threshold raised far enough to swallow a softly spoken
    word would put somebody who *is* being recorded on the spot in front of
    the room, which is the failure this feature must never produce.

    `1000` is about -30 dBFS -- quiet, distant, unmistakably sound.
    """
    watch = SilentAudioWatch()
    for _ in range(29):
        watch.observe(ANNA, silent(1.0))
    assert watch.observe(ANNA, _pcm(0.02, 1_000)) is False
    assert watch.observe(ANNA, silent(1.0)) is False, "and the evidence started over"


def test_one_step_above_the_threshold_is_audio() -> None:
    """`SILENCE_PEAK_AMPLITUDE` is the loudest peak still counted as
    silence, not the quietest counted as audio -- the boundary is inclusive
    on the silent side, and a change of that direction would shift every
    detection by one sample value without any other test noticing.
    """
    watch = SilentAudioWatch()
    for _ in range(29):
        watch.observe(ANNA, silent(1.0))
    assert watch.observe(ANNA, _pcm(0.02, SILENCE_PEAK_AMPLITUDE + 1)) is False


def test_the_evidence_threshold_is_thirty_seconds_of_received_audio() -> None:
    """Pins the constant itself, since the whole design rests on its unit.

    It is a byte count of *received* PCM, not wall-clock time: a speaker
    who transmits nothing for half an hour has produced no evidence of
    anything, and warning them would be the false positive that makes the
    feature unusable.
    """
    assert SILENCE_EVIDENCE_BYTES == 30 * SOURCE_SAMPLE_RATE_HZ * BYTES_PER_SAMPLE_FRAME
