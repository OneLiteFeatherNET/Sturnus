"""Cutting the padding we wrote ourselves back out of a speaker's recording.

**This is not a voice activity detector and must not be described as one.**
Its contract is narrower and that narrowness is what makes it safe: it removes
the silence `sturnus.infrastructure.audio.SpeakerWriter` inserted, whose exact
value we chose. It makes no attempt to tell speech from noise.

Why a gate at all. Discord sends no packets while a participant is quiet, so
`SpeakerWriter` pads every gap with `b"\\x00"` — bit-exact digital zero — to
keep file time equal to wall-clock time. A speaker in a busy channel is
therefore mostly padding, and handing all of it to Whisper is both slow (the
decoder pays a fixed price per 30-second window regardless of content) and an
invitation to hallucinate, since Whisper reliably invents text for silence.
Something has to cut it out.

Why not Silero, which used to do this job via `vad_filter=True`. Silero is an
LSTM: its hidden state is zeroed once per call and then threaded through every
512-sample window of the whole array, so a 100-minute file is one continuous
recurrent sequence. Fed thousands of consecutive windows of the *identical*
input vector — which is exactly what bit-exact zero padding is — the state
settles into a fixed point at the extreme low-confidence end of its range, and
speech that only weakly exceeds the noise floor afterwards no longer perturbs
the output enough to cross threshold. Measured on the real recording that
started this: a marginal-SNR burst preceded by 5-40 minutes of exact zero
recovered 0 of 5 seconds, while the same burst preceded by merely-quiet dither
recovered 96-100%, and lowering the threshold after the collapse recovered
only 38%. Every transcript this project produced was empty or hallucinated as
a result. Bit-exact zero of that kind essentially never occurs in a genuine
recording — a live mic or a lossy codec always contributes jitter — so the
input shape that breaks Silero is one Sturnus manufactures for itself.

Why an amplitude gate is not merely the previous mistake with a smaller model.
A per-frame peak test carries no state from one frame to the next, so there is
no history for minutes of zero to corrupt: every frame is judged identically
regardless of what preceded it. The gate is structurally immune to the
demonstrated failure rather than tuned away from it. And it is aimed at a
target it cannot miss — the padding's peak amplitude is exactly 0.0, so any
positive floor excludes it with enormous margin.

The accepted cost: with no phonetic discrimination the gate admits hum,
keyboard clatter and cross-talk that Silero would have excluded. That is
decode time wasted, not a wrong transcript, so long as the decoder rejects
what the gate let through — which is `WhisperEngine`'s job and not this
module's. Do not read the existence of that second line as a reason to relax
this one: it was set and it still shipped `" Untertitelung des ZDF, 2020"`
under a participant's name, because `no_speech_threshold` was silently
vetoed by a library default `WhisperEngine` had not overridden. That is
fixed there, and the comments there say how.

This module lives in `infrastructure` and not in `domain` because it needs
numpy, and `tests/test_architecture.py` enforces that `sturnus.domain` imports
nothing but the standard library and itself.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# 30 ms. Shorter than any syllable, so a frame boundary cannot fall in the
# middle of the only loud part of a word and halve its measured peak; long
# enough that a single stray sample cannot open a clip on its own.
#
# Given in samples rather than in seconds because that is the unit every
# operation below works in, and at `_FRAME_RATE` because 16 kHz is the only
# rate `WhisperEngine` ever decodes at. `speech_clips` rescales it for any
# other rate, so the frame stays 30 ms of audio and not 480 samples of it.
_FRAME_SAMPLES = 480
_FRAME_RATE = 16_000

# About -60 dBFS, expressed against the 16-bit scale the audio was written at
# (32 of 32767 full-scale steps) because that is the resolution the samples
# actually have — `SpeakerWriter` writes int16 and `decode_audio` divides by
# 32768, so nothing below one step exists in the input.
#
# The number is chosen to be *unmissably* above the padding rather than to sit
# near speech: the padding's peak is exactly 0.0, so the margin is total. It is
# also far below a whisper, and below what Whisper can transcribe at all, so
# audio it excludes was not going to become text either way. A floor low
# enough to be this safe is the whole point — see this module's docstring on
# why we are not trying to detect speech.
_SILENCE_PEAK = 32 / 32767

# 120 ms. Kills clicks, pops and single-packet artefacts without endangering a
# short word. It matters more than it looks: a clip costs a whole 30-second
# encoder window no matter how short it is (see `_MERGE_GAP_SECONDS`), so
# admitting a 30 ms blip buys a full window of decoding on material Whisper is
# known to hallucinate on. A real word whose middle dips below the floor for a
# frame is split into two too-short runs here and rejoined in step 4.
_MIN_RUN_FRAMES = 4

# A peak test finds the loud middle of a word but misses the quiet onset and
# the trailing consonant, and clipping those turns "spricht" into "richt". A
# quarter of a second at each end covers them; over-including a little silence
# at a clip edge costs nothing, because the clip is padded up to a full
# encoder window anyway.
_HANGOVER_SECONDS = 0.25

# Deliberately generous, and this is a cost decision more than a quality one.
# Whisper's encoder processes a fixed 30-second window, and a clip shorter than
# that is padded up to one (`faster_whisper.audio.pad_or_trim`), so twelve
# 0.3-second clips cost twelve windows while one clip spanning all of them
# costs one. Merging anything closer than 2 seconds stays far below one window,
# so it is nearly free, and it turns ordinary turn-taking pauses and
# between-word breaths into a single clip instead of a dozen.
_MERGE_GAP_SECONDS = 2.0

# Roughly 60 seconds of frames. The blocked pass in `_frame_peaks` exists for
# memory, not speed: a 100-minute recording is 96 M samples, so the decoded
# float32 array is already ~384 MB, and the obvious one-liner
# `np.abs(audio).reshape(-1, _FRAME_SAMPLES).max(axis=1)` allocates a second
# array of the same size. The worker pod's memory limit does not have room for
# both, and the failure would appear only in production and only on long
# sessions — never in a test, where the arrays are seconds long.
_BLOCK_FRAMES = 2_000


def _frame_peaks(audio: np.ndarray[Any, Any], frame_samples: int) -> np.ndarray[Any, Any]:
    """Peak absolute amplitude per fixed-length frame, computed block by block.

    A trailing partial frame is measured over the samples it actually has
    rather than dropped, so audio at the very end of a file can still open a
    run. It is a frame like any other for the run-length rule that follows.
    """
    frame_count = -(-audio.shape[0] // frame_samples)  # ceil, without floats
    peaks = np.empty(frame_count, dtype=np.float32)

    for first in range(0, frame_count, _BLOCK_FRAMES):
        last = min(first + _BLOCK_FRAMES, frame_count)
        block = np.abs(audio[first * frame_samples : last * frame_samples])
        whole = (block.shape[0] // frame_samples) * frame_samples
        if whole:
            peaks[first : first + whole // frame_samples] = (
                block[:whole].reshape(-1, frame_samples).max(axis=1)
            )
        if whole != block.shape[0]:
            # The final partial frame; only ever the last frame of the array,
            # because every earlier block is a whole multiple of the frame.
            peaks[last - 1] = block[whole:].max()

    return peaks


def _loud_runs(loud: np.ndarray[Any, Any]) -> list[tuple[int, int]]:
    """Half-open `[start, stop)` frame ranges of consecutive loud frames.

    The sentinel `False` at each end is what makes a run that touches the
    first or last frame produce an edge like any other, so the two boundary
    cases need no special handling below.
    """
    flags = np.concatenate(([False], loud, [False]))
    edges = np.flatnonzero(np.diff(flags.astype(np.int8)))
    return [
        (int(start), int(stop))
        for start, stop in zip(edges[0::2], edges[1::2], strict=True)
        if stop - start >= _MIN_RUN_FRAMES
    ]


def speech_clips(
    audio: np.ndarray[Any, Any], sample_rate: int = 16_000
) -> tuple[tuple[float, float], ...]:
    """The stretches of `audio` that are not padding, as `(start, end)` in seconds.

    `audio` is mono float in [-1.0, 1.0] — what `faster_whisper.audio.
    decode_audio` produces. The result is non-overlapping and ascending, which
    is what `clip_timestamps` requires: faster-whisper's seek loop walks the
    clips in order and never goes backwards, so an out-of-order pair would
    silently drop audio rather than fail.

    An empty result means "there is nothing here worth transcribing", and the
    caller must treat it as such — `WhisperModel.transcribe` reads an empty
    `clip_timestamps` list as "transcribe the whole file", so passing this
    result straight through without checking it would do the opposite of what
    it says.
    """
    duration = audio.shape[0] / sample_rate
    frame_samples = max(1, round(_FRAME_SAMPLES * sample_rate / _FRAME_RATE))

    peaks = _frame_peaks(audio, frame_samples)
    runs = _loud_runs(peaks > _SILENCE_PEAK)
    if not runs:
        return ()

    merged: list[list[float]] = []
    for first_frame, stop_frame in runs:
        # Clamped, because the hangover on a run touching either edge would
        # otherwise produce a negative start or an end past the file, and both
        # become a seek frame outside the features array.
        start = max(0.0, first_frame * frame_samples / sample_rate - _HANGOVER_SECONDS)
        end = min(duration, stop_frame * frame_samples / sample_rate + _HANGOVER_SECONDS)
        if merged and start - merged[-1][1] < _MERGE_GAP_SECONDS:
            merged[-1][1] = end
        else:
            merged.append([start, end])

    return tuple((start, end) for start, end in merged)
