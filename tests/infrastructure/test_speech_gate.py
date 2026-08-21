"""What the amplitude gate does with the input `SpeakerWriter` actually produces.

The defect this module exists to prevent was history-dependent: Silero VAD is
a recurrent network, and `SpeakerWriter` pads every gap between packets with
bit-exact digital zero, so a quiet speaker feeds it thousands of identical
windows in a row. Its hidden state settles at the extreme low-confidence end
and marginal speech afterwards no longer crosses threshold — measured on a
real recording, 1.1 seconds of speech reported out of 120.

The gate replaces it with a per-frame amplitude test that carries no state
across frames, so every one of these tests can be written without a model and
without fixture audio: the position of a burst inside the file cannot change
the verdict, and `test_a_burst_is_judged_the_same_after_ten_minutes_of_zero`
is the assertion that says so. That is the whole reason the gate lives in its
own pure-numpy module rather than inside `WhisperEngine`.
"""

from __future__ import annotations

import numpy as np
import pytest

from sturnus.infrastructure.speech_gate import (
    _BLOCK_FRAMES,
    _FRAME_SAMPLES,
    _HANGOVER_SECONDS,
    _frame_peaks,
    speech_clips,
)

SAMPLE_RATE = 16_000


def _zeros(seconds: float) -> np.ndarray:
    """Exactly what `SpeakerWriter` writes into a gap: bit-exact digital zero."""
    return np.zeros(round(seconds * SAMPLE_RATE), dtype=np.float32)


def _tone(seconds: float, amplitude: float = 0.05) -> np.ndarray:
    """A stand-in for speech: loud enough to pass the gate, quiet enough to be marginal.

    0.05 is about -26 dBFS — a soft speaker, well under the level of the
    clean speech that Silero handled correctly even after a long zero run.
    220 Hz puts several full cycles inside every 30 ms frame, so the
    per-frame peak reaches the amplitude in every frame rather than only in
    some of them; a test tone slow enough to be near a zero crossing for a
    whole frame would be testing the tone, not the gate.
    """
    samples = round(seconds * SAMPLE_RATE)
    t = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)


def test_empty_audio_yields_no_clips() -> None:
    assert speech_clips(np.zeros(0, dtype=np.float32)) == ()


@pytest.mark.parametrize("seconds", [0.5, 60.0, 600.0])
def test_padding_alone_yields_no_clips(seconds: float) -> None:
    """The gate's contract, stated as a test: what we wrote ourselves is cut out.

    The padding's peak is exactly 0.0, so any positive floor excludes it. If
    this ever fails, `WhisperEngine` would hand the decoder an hours-long
    clip of silence and Whisper would invent text for it — the exact
    outcome `test_silence_yields_no_segments` guards against downstream.
    """
    assert speech_clips(_zeros(seconds)) == ()


def test_a_burst_surrounded_by_padding_is_found_with_room_around_it() -> None:
    """Finds the burst, and widens it by the hangover rather than trimming to it.

    The expected edges are written out as numbers instead of derived from
    `_HANGOVER_SECONDS`, deliberately. A test that computes its expectation
    from the constant it is checking passes for every value of that constant,
    including zero — and a hangover of zero clips word onsets and final
    consonants off mid-syllable, which is a real transcript defect that would
    then reach production untested.
    """
    audio = np.concatenate([_zeros(5.0), _tone(4.0), _zeros(5.0)])

    clips = speech_clips(audio)

    assert len(clips) == 1
    start, end = clips[0]
    assert start == pytest.approx(4.75, abs=0.05)
    assert end == pytest.approx(9.25, abs=0.05)


def test_a_burst_is_judged_the_same_after_ten_minutes_of_zero() -> None:
    """The regression test for the defect itself.

    Ten minutes of bit-exact zero is what collapsed Silero's recurrent state,
    after which the identical burst recovered 0 of 5 seconds. The gate has no
    state to collapse, so the clip it finds must be the same shape no matter
    how much padding precedes it. Comparing the two placements rather than
    asserting one absolute number is what makes this a test of statelessness
    and not merely of thresholding.
    """
    burst = _tone(5.0)
    # Both leads are whole multiples of the 30 ms frame (32 frames and 20 000
    # frames). Otherwise the burst would begin part-way into a frame in one
    # case and on a boundary in the other, and the clip edges would differ by
    # up to one frame for a reason that has nothing to do with what this test
    # is about.
    short_lead = 32 * _FRAME_SAMPLES / SAMPLE_RATE
    long_lead = 20_000 * _FRAME_SAMPLES / SAMPLE_RATE

    after_short = speech_clips(np.concatenate([_zeros(short_lead), burst, _zeros(10.0)]))
    after_long = speech_clips(np.concatenate([_zeros(long_lead), burst, _zeros(10.0)]))

    assert len(after_short) == 1
    assert len(after_long) == 1
    assert after_long[0][0] - long_lead == pytest.approx(after_short[0][0] - short_lead, abs=1e-6)
    assert after_long[0][1] - after_long[0][0] == pytest.approx(
        after_short[0][1] - after_short[0][0], abs=1e-6
    )


def test_bursts_separated_by_a_short_gap_become_one_clip() -> None:
    """A turn-taking pause must not cost an extra encoder window.

    Whisper pads any clip shorter than 30 seconds up to a full 30-second
    window, so two clips 1 second apart cost twice what one clip spanning
    both costs. Merging is why `_MERGE_GAP_SECONDS` is generous.

    The gap is the literal 1.5 s an ordinary turn-taking pause lasts, not
    `_MERGE_GAP_SECONDS / 2.0`. A gap derived from the constant is always half
    of it, so the old form merged for every value and pinned nothing -- and it
    was worse than merely vacuous, because `_HANGOVER_SECONDS` widens each
    burst by 0.25 s at both ends and so eats 0.5 s of whatever gap is written
    here. 1.5 s leaves 1.0 s of real separation after the hangover, which must
    still come back as one clip. Verified by mutation: `_MERGE_GAP_SECONDS =
    0.6` -- nowhere near the "deliberately generous" its comment claims --
    passed the old form and fails this one.
    """
    gap = 1.5
    audio = np.concatenate([_zeros(2.0), _tone(1.0), _zeros(gap), _tone(1.0), _zeros(2.0)])

    clips = speech_clips(audio)

    assert len(clips) == 1
    start, end = clips[0]
    assert start <= 2.0
    assert end >= 2.0 + 1.0 + gap + 1.0


def test_bursts_separated_by_a_long_gap_stay_separate() -> None:
    """The other half of the merge rule: real silence is still dropped.

    Without this, the merge would eventually swallow the whole file and the
    gate would be doing nothing at all.

    A literal 6 s, for the same reason as the merge case above: a gap written
    as `_MERGE_GAP_SECONDS * 4.0` stays four times the constant and separates
    for every value of it. 6 s leaves 5.5 s after the hangover, which is real
    silence by any reading. Together the two tests bound the constant into
    roughly (1.0, 5.5) seconds, which is what "a couple of seconds" means.
    """
    gap = 6.0
    audio = np.concatenate([_zeros(2.0), _tone(1.0), _zeros(gap), _tone(1.0), _zeros(2.0)])

    clips = speech_clips(audio)

    assert len(clips) == 2
    assert clips[0][1] < clips[1][0]


def test_a_single_loud_frame_is_dropped() -> None:
    """One 30 ms frame is a click or a pop, and it is expensive.

    Admitting it costs a whole 30-second encoder window for material Whisper
    is known to hallucinate on, so `_MIN_RUN_FRAMES` rejects it.
    """
    blip = _tone(_FRAME_SAMPLES / SAMPLE_RATE, amplitude=0.9)
    audio = np.concatenate([_zeros(3.0), blip, _zeros(3.0)])

    assert speech_clips(audio) == ()


def test_a_short_word_is_kept() -> None:
    """The other side of `_MIN_RUN_FRAMES`: 150 ms is "ja", not a click.

    The duration is a literal rather than `_MIN_RUN_FRAMES * _FRAME_SAMPLES`,
    for the same reason the hangover test uses literals: an expectation
    derived from the constant under test survives every value of it, so
    raising the minimum run to a quarter of a second — which would silently
    drop the shortest real words a speaker says — would pass unnoticed.
    """
    audio = np.concatenate([_zeros(3.0), _tone(0.15), _zeros(3.0)])

    assert len(speech_clips(audio)) == 1


def test_a_one_frame_dip_inside_a_word_does_not_split_it() -> None:
    """A syllable boundary drops below the floor for a frame; that is not two words.

    Step 2 rejects each half as too short on its own, so without the merge in
    step 4 the whole utterance would vanish rather than merely be split.
    """
    dip = _zeros(_FRAME_SAMPLES / SAMPLE_RATE)
    audio = np.concatenate([_zeros(2.0), _tone(0.3), dip, _tone(0.3), _zeros(2.0)])

    assert len(speech_clips(audio)) == 1


def test_the_hangover_clamps_to_the_recording() -> None:
    """Audio at the very edges must not produce a negative start or an end past the file.

    `clip_timestamps` is fed straight to faster-whisper, which turns each
    value into a seek frame; a negative one would seek before the file.
    """
    audio = _tone(4.0)

    clips = speech_clips(audio)

    assert len(clips) == 1
    start, end = clips[0]
    assert start == 0.0
    assert end == pytest.approx(len(audio) / SAMPLE_RATE, abs=1e-6)


def test_audio_shorter_than_the_minimum_run_yields_no_clips() -> None:
    """A deliberate decision, not an oversight.

    A file under `_MIN_RUN_FRAMES` frames — 120 ms — holds no transcribable
    word, and admitting it would spend a full 30-second encoder window on it.
    The rule is applied uniformly: a run too short to keep is dropped
    wherever it sits, including when it is the entire file. The alternative
    considered was a special case that returns one clip covering a loud
    sub-frame file; it was rejected because it would put exactly the input
    Whisper hallucinates on in front of the decoder, and because a special
    case here would be the one code path no real recording ever exercises.
    """
    assert speech_clips(_tone(0.02, amplitude=0.9)) == ()


def test_the_floor_sits_just_above_the_padding_and_far_below_speech() -> None:
    """The threshold is a boundary test, so pin both sides of it.

    Below the floor the gate must still exclude; the padding is at exactly 0.0
    so it clears this by an enormous margin, and that margin is the reason a
    threshold this crude is safe at all.

    **Both levels are literals chosen independently of `_SILENCE_PEAK`, and
    must stay that way.** This test used to probe `_SILENCE_PEAK / 2.0` and
    `_SILENCE_PEAK * 2.0`, which every positive threshold whatsoever passes by
    construction -- one level is below it and the other above it however absurd
    it is. Verified by mutation: `_SILENCE_PEAK = 0.049`, fifty times too high
    and well up inside ordinary speech, left the old form green. Refactoring
    these numbers back into expressions over the constant restores that.

    The two levels encode what the constant's own comment claims:

    * 0.0009 is a shade under -60 dBFS, which is where the floor says it sits,
      and must be excluded.
    * 0.005 is about -46 dBFS. The real German fixture attenuated by 40 dB
      peaks near 0.007 and still gates correctly, so this level is inside the
      range of quiet-but-real speech and must be kept.
    """
    quiet = np.full(SAMPLE_RATE, 0.0009, dtype=np.float32)
    loud = np.full(SAMPLE_RATE, 0.005, dtype=np.float32)

    assert speech_clips(quiet) == ()
    assert len(speech_clips(loud)) == 1


def test_clips_are_ascending_and_do_not_overlap() -> None:
    """What `clip_timestamps` requires: faster-whisper walks the clips in order.

    Its seek loop advances monotonically and skips any clip whose end it has
    already passed, so an out-of-order or overlapping pair would silently
    drop audio instead of failing loudly.

    The 6.0 s separator is a literal, and the same literal
    `test_bursts_separated_by_a_long_gap_stay_separate` uses, for the same
    reason: written as `_MERGE_GAP_SECONDS * 2.0` it stayed twice the constant
    for every value of it, so `assert len(clips) == 6` held whatever the merge
    gap was and the test pinned nothing. 6.0 s leaves 5.5 s of real separation
    after `_HANGOVER_SECONDS` widens each burst by 0.25 s at both ends, which
    must come back as six clips and not fewer. Verified by mutation:
    `_MERGE_GAP_SECONDS = 6.5` merges all six into one and fails this; under
    the old form it passed.
    """
    pieces = [_zeros(1.0)]
    for _ in range(6):
        pieces.append(_tone(0.5))
        pieces.append(_zeros(6.0))
    audio = np.concatenate(pieces)

    clips = speech_clips(audio)
    duration = len(audio) / SAMPLE_RATE

    assert len(clips) == 6
    for start, end in clips:
        assert 0.0 <= start < end <= duration
    for (_, earlier_end), (later_start, _) in zip(clips, clips[1:], strict=False):
        assert earlier_end < later_start


def test_frame_peaks_are_block_independent() -> None:
    """The blocked pass must produce what the naive one-shot pass would.

    A 100-minute file is 96 M samples; `np.abs(audio).reshape(-1, 480).max(1)`
    would allocate a second ~384 MB array on top of the first, which the
    worker pod's memory limit does not have room for. Blocking is what
    avoids that, and this test is what proves the block boundaries do not
    move a clip edge: the audio here is many blocks long and the bursts are
    placed at second boundaries that fall inside, on, and across them.
    """
    pieces = [_zeros(59.5), _tone(1.0), _zeros(59.0), _tone(1.0), _zeros(30.0)]
    audio = np.concatenate(pieces)

    clips = speech_clips(audio)

    assert len(clips) == 2
    assert clips[0][0] == pytest.approx(59.5 - _HANGOVER_SECONDS, abs=0.05)
    assert clips[0][1] == pytest.approx(60.5 + _HANGOVER_SECONDS, abs=0.05)
    assert clips[1][0] == pytest.approx(119.5 - _HANGOVER_SECONDS, abs=0.05)
    assert clips[1][1] == pytest.approx(120.5 + _HANGOVER_SECONDS, abs=0.05)


def test_a_non_default_sample_rate_is_honoured() -> None:
    """The frame length is defined in milliseconds, not in samples.

    `WhisperEngine` always decodes at 16 kHz, so this is not a production
    path today — but a gate whose frame length silently meant something
    different at another rate would be a trap for the next caller.

    The burst is 150 ms, which is what makes the test discriminating: at
    8 kHz a correctly rescaled 30 ms frame is 240 samples, so the burst is
    five frames and clears `_MIN_RUN_FRAMES`. Taking `_FRAME_SAMPLES`
    literally would make each frame 60 ms instead, the burst two and a half
    frames, and the word would vanish. A longer burst would survive either
    way and the test would prove nothing.
    """
    rate = 8_000
    lead = 34 * 240  # a whole number of 30 ms frames, so the run starts on a boundary
    samples = round(0.15 * rate)
    t = np.arange(samples, dtype=np.float32) / rate
    audio = np.concatenate(
        [
            np.zeros(lead, dtype=np.float32),
            (0.05 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32),
            np.zeros(rate, dtype=np.float32),
        ]
    )

    clips = speech_clips(audio, sample_rate=rate)

    assert len(clips) == 1
    assert clips[0][0] == pytest.approx(lead / rate - 0.25, abs=0.02)
    assert clips[0][1] == pytest.approx((lead + samples) / rate + 0.25, abs=0.02)


def test_the_blocked_pass_equals_the_one_shot_pass() -> None:
    """The blocking is an optimisation, so it must be invisible in the result.

    `_frame_peaks` walks the array in blocks only to avoid allocating a second
    copy of it — a 100-minute recording decodes to ~384 MB, and the obvious
    `np.abs(audio).reshape(-1, 480).max(axis=1)` would need another. An
    off-by-one at a block boundary would move a clip edge by 30 ms in
    production and never in a test written with seconds of audio, so this
    compares the blocked pass against the naive one it stands in for, over a
    length that is neither a whole number of blocks nor a whole number of
    frames.
    """
    rng = np.random.default_rng(seed=20260819)
    samples = _BLOCK_FRAMES * _FRAME_SAMPLES * 3 + _FRAME_SAMPLES * 7 + 123
    audio = rng.uniform(-1.0, 1.0, samples).astype(np.float32)

    peaks = _frame_peaks(audio, _FRAME_SAMPLES)

    whole = (samples // _FRAME_SAMPLES) * _FRAME_SAMPLES
    naive = np.abs(audio[:whole]).reshape(-1, _FRAME_SAMPLES).max(axis=1)
    assert peaks.shape[0] == samples // _FRAME_SAMPLES + 1
    assert np.array_equal(peaks[:-1], naive)
    assert peaks[-1] == np.abs(audio[whole:]).max()
