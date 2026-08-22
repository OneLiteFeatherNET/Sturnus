"""faster-whisper behind the transcription port.

The library is synchronous and CPU-bound, so every call runs in a worker
thread. The model is loaded once and reused; jobs are processed one at a
time (Spec 5.3), so no locking is required around it.

Every decoding parameter below is set explicitly rather than left to the
library's default, and each one is set against a specific way a meeting
protocol goes wrong: silence turning into invented speech, one bad segment
poisoning the segments after it, a project name coming out as a common
word. `tests/infrastructure/test_whisper.py` pins each of them against a
fake model, because none of them is visible in the output of a passing
two-second fixture.

One of them is set to `None` rather than to a number, which reads like an
omission and is not: `log_prob_threshold` is a veto on faster-whisper's
own silence check, not a quality floor, and leaving it at the library
default is what let a subtitle credit invented on room tone reach a
production protocol. The comment beside it carries the mechanism.

Silence is cut out before the decoder sees it, but *not* by faster-whisper's
own `vad_filter`. That option runs Silero, whose recurrent state collapses on
the bit-exact zero padding `SpeakerWriter` writes between packets — it
reported about one second of speech in two minutes of a real recording and
every transcript this project produced came back empty or hallucinated.
`sturnus.infrastructure.speech_gate` does the same job with a stateless
amplitude test; its module docstring carries the full reasoning.

What the gate finds is then concatenated here and handed to the model as one
short array, with `clip_timestamps` re-expressed on that timeline and the
segment times mapped back onto the recording's afterwards. Handing over the
padded file and only marking the speech with `clip_timestamps` — which is what
this did until now — makes faster-whisper run its log-mel extraction over the
padding as well, because the array is shrunk only in the `vad_filter` branch
that setting clips skips: 4.94 GB of frames for a 100-minute track against
1.99 GB for the 41 minutes of speech in it, and the worker's memory limit had
to be raised from 6Gi to 12Gi in production because of it. The clip boundaries
stay, on the new timeline, because they are what keeps an encoder window from
spanning two utterances spoken minutes apart; `_on_the_original_timeline`
below carries the rest of that reasoning.

This adapter is not a pure consumer of documented API and should not be read
as one. `clip_timestamps` is a documented argument, but what it is used *for*
here — and how a returned segment is put back on the recording's timeline —
rests on three internals of `generate_segments`: that `segment_size =
min(nb_max_frames, content_frames - seek, seek_clip_end - seek)` keeps an
encoder window inside one clip, that `Segment.seek` is that window's first
log-mel frame, and that clip boundaries reach the seek loop as `round(ts *
frames_per_second)`. None of the three is documented, the loop around them
carries faster-whisper's own note that it should be rewritten as a nested
loop, and `pyproject.toml` pins `faster-whisper>=1.1` with no upper bound
while Renovate merges on a green build.
`tests/infrastructure/test_whisper.py::test_a_window_never_spans_two_clips`
drives the real `generate_segments` — no weights, no download — and fails if
any of the three stops holding. It is the only upper bound there is.

**This module is where a transcription becomes observable at all**, and it
is instrumented against one specific way of being wrong. The failure that
cost this project two days produced an *empty transcript*, which is
indistinguishable from a participant who never spoke -- and was read as
exactly that for a day. Two things separate them, and neither is visible
anywhere else in the codebase:

- `speech_seconds` against `audio_seconds` on `transcription.decoded`,
  which is the speech gate's own signature. `sturnus.application.worker`
  emits `job.transcribed` with segment counts and a realtime factor, but it
  cannot see the gate.
- `sturnus.transcription.decoded_seconds` divided by wall time, which is a
  real-time factor computed from the audio the model was *given* rather
  than from the segments it returned. A hundred minutes "decoded" in
  forty-three seconds is 140x, which is impossible; the same job measured
  by its (nonexistent) segments would have contributed nothing at all and
  said nothing.

See `sturnus.infrastructure.telemetry.TranscriptionProgress` for the live
half -- position, denominator and the stall clock -- and why they are
observable instruments rather than gauges this module sets.
"""

from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path
from typing import Any

import numpy as np
from faster_whisper import WhisperModel  # type: ignore[import-untyped]
from faster_whisper.audio import decode_audio  # type: ignore[import-untyped]

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
)
from sturnus.domain.measurements import JobMeasurements
from sturnus.infrastructure.speech_gate import speech_clips
from sturnus.infrastructure.telemetry import TRANSCRIPTION_PROGRESS, set_current_span_fields
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

_SAMPLE_RATE = 16_000

log = logging.getLogger(__name__)


def _on_the_frame_grid(times: list[float], frames_per_second: int) -> list[int]:
    """Seconds, as the log-mel frame numbers faster-whisper's seek loop counts in.

    `generate_segments` converts `clip_timestamps` with `seek_points =
    [round(ts * self.frames_per_second) for ts in options.clip_timestamps]`
    and every seek position it reports afterwards is a number on that grid.
    Attributing a segment to a clip is therefore a comparison of frames, and
    this is the one place the conversion is written down. Doing it in seconds
    instead would put a window that begins exactly on a boundary that rounded
    *up* into the clip before it, and being one clip out is being one removed
    silence out — minutes, not milliseconds.
    """
    return [round(time * frames_per_second) for time in times]


def _on_the_original_timeline(
    start: float,
    end: float,
    seek: int,
    bounds: list[tuple[int, int]],
    clip_starts_in_frames: list[int],
    offsets: list[float],
) -> tuple[float, float]:
    """One segment's times, moved from the concatenated speech back to the file.

    The clip is decided by **where the segment was decoded from, not by the
    times it reports**, and that is the whole of the difference between a
    working restore and the one faster-whisper ships. `seek` is the segment's
    own `Segment.seek`: the first log-mel frame of the encoder window it came
    out of (`yield Segment(seek=previous_seek, ...)`, with `previous_seek`
    set to the window's start immediately before decoding). The seek loop
    enters a clip at `seek = seek_clips[clip_idx][0]` and leaves it the
    moment `seek >= seek_clip_end`, so that frame names one clip and only
    one, whatever the decoder then says about the audio in it.

    Reading it from the times cannot be made safe. A decoded `end` is
    `time_offset + end_timestamp_position * time_precision` and
    `_split_segments_by_timestamps` caps neither it nor the seek positions it
    derives at the window's content, so a tail segment routinely claims a few
    tenths of a second the window never held. Resolve the clip from that
    `end` — or from a midpoint, when the overrun is more than half the
    segment's own length — and the *next* clip's offset is added, moving the
    text across the entire removed silence. Measured on the real engine with
    this branch's own pre-fix code, on the shape the recording that started
    this has — 100 minutes, 187 clips, 41.2 minutes of speech: 57 of 462
    segments reported an `end` past their clip, and for 5 of them the overrun
    was more than half the segment, so a midpoint landed in the next clip.
    Those 5 came back 20 to 31 seconds from where they were spoken, one of
    them carrying 14 seconds of text. 20 to 31 seconds because that is the
    silence removed between two clips *on this shape*; the error is always
    exactly one gap, so on a session with three utterances forty minutes
    apart it is forty minutes. That is the same silently wrong timestamp
    `restore_speech_timestamps` produces, which is the reason this project
    does the restore itself — and moving the arithmetic into our own file is
    not what made it wrong there.

    The guarantee this now gives: **a restored segment always lies inside the
    file-timeline extent of the clip whose encoder window produced it.**
    `seek` is what picks the clip; the clamp is what holds the times to it
    whatever the decoder claimed, at a cost of an overrun's worth off a
    segment that only ever feeds a global sort, a 15-second merge window and
    an %H:%M:%S render. It rests on three properties of `generate_segments`,
    none of them documented and all of them observed on the real library by
    `tests/infrastructure/test_whisper.py::test_a_window_never_spans_two_clips`:
    that `Segment.seek` is the window's first frame, that the loop never
    decodes a window from outside the clip it is walking, and that clip
    boundaries reach it as `round(ts * frames_per_second)`.
    """
    # `bisect_right` on the clip *starts*: the clips are adjacent on the
    # concatenated timeline, so the starts partition it and the window's
    # frame falls in exactly one of them. `max(..., 0)` is for a `seek` below
    # the first boundary, which the loop cannot produce and which would
    # otherwise index the list from the wrong end.
    index = max(bisect_right(clip_starts_in_frames, seek) - 1, 0)
    first, last = bounds[index]
    low, high = first / _SAMPLE_RATE, last / _SAMPLE_RATE
    restored_start = min(max(start + offsets[index], low), high)
    restored_end = min(max(end + offsets[index], restored_start), high)
    return restored_start, restored_end


class WhisperEngine:
    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        default_language: str,
    ) -> None:
        self._device = device
        self._compute_type = compute_type
        # One model is loaded eagerly -- the default -- because the first
        # load is slow enough that discovering it lazily would make the
        # first job of every deployment look like a hang. Others join it
        # only if a job asks by name.
        self._models: dict[str, WhisperModel] = {
            model_size: WhisperModel(model_size, device=device, compute_type=compute_type)
        }
        self._default_model = model_size
        # Every measurement below is labelled with the model that produced
        # it, resolved per job rather than fixed at construction. It is the
        # only label these metrics carry: a real-time factor that mixes
        # `large-v3` with `tiny` says nothing, and no id may become a
        # metric label (see `observability.fields.METRIC_LABEL_FIELDS`).
        self._default_language = default_language

    async def transcribe(
        self,
        path: Path,
        language: str | None,
        initial_prompt: str | None,
        model: str | None = None,
    ) -> TranscriptionResult:
        """Transcribes one file, optionally with a model other than the default.

        `model` exists so that two runs over the *same* recording can be
        compared -- which is the only way to answer "is a smaller model
        good enough here" for this deployment's own audio, in its own
        language. Every earlier comparison in this project was made
        against recordings that turned out to be noise (see
        `sturnus.infrastructure.discord.dave`), so none of them said
        anything about a model.
        """
        return await asyncio.to_thread(self._transcribe, path, language, initial_prompt, model)

    def _model_for(self, name: str | None) -> tuple[str, WhisperModel]:
        """The model a job asked for, loading it once if it is new.

        **An unknown name fails the job rather than falling back.** A
        comparison run that silently used the old model would still
        produce a transcript, still record measurements, and look exactly
        like a result -- and this project has already lost days to a
        failure that reported success.

        Cached because loading `large-v3` takes long enough to dominate a
        short job, and a comparison means running the same file twice.
        Unbounded on purpose: the names come from an administrator's
        re-queue, not from user input, and a worker that has loaded three
        models has been asked for three.
        """
        wanted = name or self._default_model
        existing = self._models.get(wanted)
        if existing is not None:
            return wanted, existing
        log.info("Loading transcription model %s, which this worker has not used before", wanted)
        loaded = WhisperModel(wanted, device=self._device, compute_type=self._compute_type)
        self._models[wanted] = loaded
        return wanted, loaded

    def _transcribe(
        self,
        path: Path,
        language: str | None,
        initial_prompt: str | None,
        model: str | None = None,
    ) -> TranscriptionResult:
        # Decoded here rather than inside `transcribe()` so the gate and the
        # model measure and seek through the *same* array. Handing the model
        # the path instead would decode a 100-minute file a second time, and
        # clip offsets computed on a different copy of the samples could be
        # misaligned against the one being transcribed.
        # Resolved before anything is decoded: an unknown model must fail
        # the job before it costs a hundred minutes of audio processing.
        model_name, engine = self._model_for(model)
        audio = decode_audio(str(path), sampling_rate=_SAMPLE_RATE)
        audio_seconds = audio.shape[0] / _SAMPLE_RATE
        clips = speech_clips(audio, sample_rate=_SAMPLE_RATE)
        # The gate's own verdict, in seconds. `speech_gate` stays a pure
        # numpy function with no logger of its own -- it is called once per
        # job and its result is right here, so reporting it from the caller
        # costs nothing and keeps a hot array routine free of I/O.
        speech_seconds = sum(end - start for start, end in clips)

        if not clips:
            # Deliberately returning without touching the model, because
            # `WhisperModel.transcribe` reads an empty `clip_timestamps` list
            # as "transcribe everything": `generate_segments` starts from an
            # empty `seek_points`, appends 0, then appends `content_frames` to
            # make the length even, and ends up with one clip spanning the
            # whole file. Falling through to the call would therefore push an
            # entire recording of nothing but padding through the decoder —
            # slow, and the single most reliable way to make Whisper invent
            # text. A silent participant must produce no segments at all.
            #
            # It guards a second thing since the speech is concatenated here:
            # `np.concatenate([])` raises `ValueError`, and a zero-length
            # array is an input `FeatureExtractor` has never been exercised
            # against. This one line is what stands between an all-padding
            # track and both failures.
            #
            # Announced rather than returned silently, and **not** counted
            # towards `decoded_seconds`. An empty transcript has exactly two
            # causes -- the gate found nothing, or the model was called and
            # produced nothing -- and only the second is a defect. Adding
            # this file's duration to the decode counter would report a
            # whole recording processed in the microseconds the gate took,
            # which is the very signature the counter exists to raise.
            log_event(
                log,
                logging.INFO,
                Event.TRANSCRIPTION_SKIPPED,
                "The speech gate found nothing above the silence floor; the model was "
                "not called and this speaker produced no segments.",
                model=model_name,
                audio_seconds=round(audio_seconds, 3),
                speech_seconds=0.0,
                clips=0,
            )
            return TranscriptionResult(segments=(), language=self._default_language)

        # Seconds are what `speech_clips` speaks in and samples are the only
        # unit in which the arithmetic below is exact, so the conversion
        # happens once, here.
        bounds = [(round(start * _SAMPLE_RATE), round(end * _SAMPLE_RATE)) for start, end in clips]
        # `speech_clips` promises clips inside the array it was handed,
        # ascending, non-overlapping, and none shorter than about 0.37 s
        # (`_MIN_RUN_FRAMES` plus a quarter-second hangover at each end).
        # Every line below is built on those promises and none of the
        # failures would be visible in a transcript: a clip past the end of
        # the array slices short, so every offset after it is wrong by the
        # difference; out-of-order clips make `offsets` decrease, so a
        # speaker's lines come back shuffled and the protocol interleaves two
        # speakers wrongly; and a clip that does not end strictly after it
        # starts contributes a length of zero or less to the cumulative sum
        # below, which is what the concatenated timeline *is* — the
        # boundaries stop increasing, so `clip_timestamps` describes a clip
        # the seek loop can never enter and `clip_starts_in_frames` stops
        # being sorted, at which point the `bisect_right` that attributes a
        # segment to a clip answers by accident. Asserted rather than
        # repaired, and asserted here rather than
        # trusted, because the constants the promises rest on live in another
        # module and this is where breaking them would surface.
        assert bounds[0][0] >= 0 and bounds[-1][1] <= audio.shape[0], (
            "speech_clips returned a clip outside the audio it was given"
        )
        assert all(first < last for first, last in bounds), (
            "speech_clips returned a clip that does not end after it starts"
        )
        assert all(
            before[1] <= after[0] for before, after in zip(bounds, bounds[1:], strict=False)
        ), "speech_clips returned overlapping or out-of-order clips"

        # One allocation for the whole of the speech. Deliberately not
        # `faster_whisper.vad.collect_chunks`, which concatenates inside its
        # own loop and is therefore quadratic in the clip count: 1.71 s
        # against 0.01 s for a bit-identical array on the 100-minute
        # recording, and worse again on a four-hour session.
        speech = np.concatenate([audio[first:last] for first, last in bounds])
        # The padded array is dead the moment those slices are copied, and
        # dropping it *here* rather than letting the frame hold it is worth
        # 271 MB of peak: `WhisperModel.transcribe` allocates the log-mel
        # features while this frame is still alive, so anything still
        # referenced adds to the peak instead of overlapping with it.
        del audio

        # Where each clip lands once the silence between them is gone.
        # `concat_ends` doubles as the boundary list `clip_timestamps` wants:
        # the clips are adjacent on this timeline, so the flat
        # [start, end, start, end, ...] form is just the cumulative
        # boundaries repeated.
        lengths = [last - first for first, last in bounds]
        counted = list(accumulate(lengths))
        concat_ends = [count / _SAMPLE_RATE for count in counted]
        concat_starts = [0.0, *concat_ends[:-1]]
        # Silence removed before each clip, in seconds — a difference of two
        # integer sample counts, so it is exact, and adding it to a segment
        # decoded from that clip is the whole of the restore.
        offsets = [
            first / _SAMPLE_RATE - concat_start
            for (first, _), concat_start in zip(bounds, concat_starts, strict=True)
        ]
        # Which clip a returned segment belongs to is read off the seek
        # position it reports, so the seek loop's own frame grid has to be
        # reproduced here (`_on_the_frame_grid`). Asked for before the call
        # rather than while collecting: a library that stopped publishing
        # `frames_per_second` should fail now, not after a 100-minute decode.
        clip_starts_in_frames = _on_the_frame_grid(concat_starts, engine.frames_per_second)

        started = time.monotonic()
        # Before the call, not after it: `transcribe()` extracts features and
        # detects a language before it yields anything, and a job that wedges
        # in there has to be distinguishable from one that has merely just
        # started. See `TranscriptionProgress.begin`.
        TRANSCRIPTION_PROGRESS.begin(model_name)
        try:
            return self._decode(
                engine,
                model_name,
                speech,
                language,
                initial_prompt,
                clips,
                bounds,
                concat_starts,
                concat_ends,
                clip_starts_in_frames,
                offsets,
                audio_seconds,
                speech_seconds,
                started,
            )
        finally:
            # Runs on the failure path too. Without it a decoder that raised
            # would leave the job "in flight" forever, and
            # `seconds_since_progress` would climb past every threshold
            # while the worker moved on to the next job.
            TRANSCRIPTION_PROGRESS.end()

    def _decode(
        self,
        engine: WhisperModel,
        model_name: str,
        speech: np.ndarray[Any, Any],
        language: str | None,
        initial_prompt: str | None,
        clips: tuple[tuple[float, float], ...],
        bounds: list[tuple[int, int]],
        concat_starts: list[float],
        concat_ends: list[float],
        clip_starts_in_frames: list[int],
        offsets: list[float],
        audio_seconds: float,
        speech_seconds: float,
        started: float,
    ) -> TranscriptionResult:
        """The model call, the loop that reports while it runs, and the restore.

        Split out of `_transcribe` only so that the `try/finally` around it
        is one line and cannot accidentally grow to cover the gate. Everything
        the concatenation produced is handed in rather than recomputed: the
        arithmetic that undoes the join has to be the arithmetic that made it,
        and a second derivation of `offsets` from the same clips is a second
        place for the two to drift apart.
        """
        segments, info = engine.transcribe(
            speech,
            language=language,
            # Biases the decoder towards the vocabulary and the style of
            # this text. It is the only lever Sturnus has on proper nouns,
            # and proper nouns are both what Whisper reliably gets wrong
            # and what a protocol is read for: a decision about "Ducula"
            # is unusable when the sentence says "Dracula". Per-guild
            # configuration (`transcription_prompt`, Spec 11) rather than
            # a constant here -- the vocabulary that matters is the
            # organisation's, and this adapter has no idea whose meeting
            # it is transcribing.
            initial_prompt=initial_prompt,
            # A flat list of seconds — [start0, end0, start1, end1, ...] — not
            # a list of pairs and not the dict form, which belongs to
            # `BatchedInferencePipeline.transcribe`, a different API.
            #
            # Still passed, now that the array is already only speech, for
            # the one thing it does that concatenating cannot: `segment_size
            # = min(nb_max_frames, content_frames - seek, seek_clip_end -
            # seek)` caps an encoder window at the clip it began in, so the
            # decoder is never shown two utterances at once and cannot emit a
            # segment spanning the join between them. Dropping it — plain
            # concatenation, which is what the library's own `vad_filter`
            # path does — was measured emitting a single 258-second segment
            # covering four utterances spoken minutes apart.
            clip_timestamps=[
                value for pair in zip(concat_starts, concat_ends, strict=True) for value in pair
            ],
            # Redundant on paper: faster-whisper's guard is
            # `if vad_filter and clip_timestamps == "0"`, so setting the clips
            # already keeps Silero from ever being loaded. Stated anyway, so
            # that seeing Silero is out of the picture does not depend on the
            # reader knowing that rule.
            vad_filter=False,
            # Guards against the repetition cascades Whisper can fall into on
            # long audio (Spec 7). They matter more now than they did, not
            # less: the gate is an amplitude test with no phonetic
            # discrimination, so it lets through hum and cross-talk that
            # Silero would have excluded, and these are what filter the
            # decoder's output on it.
            #
            # `compression_ratio_threshold` catches repetition and nothing
            # else; a four-word subtitle credit compresses like a four-word
            # sentence (0.69 measured, against 0.43 for real speech, so the
            # invented line is *less* repetitive than the thing we want to
            # keep). `no_speech_threshold` is the one that catches the credit,
            # and only because of the argument below it.
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
            # `None`, deliberately, and this is the fix for the credits.
            #
            # Despite the name, `log_prob_threshold` does not reject
            # low-confidence output anywhere in this library. On the
            # sequential path it is a *veto on the no-speech skip*
            # (`transcribe.py:1215-1233`):
            #
            #     should_skip = result.no_speech_prob > no_speech_threshold
            #     if log_prob_threshold is not None and avg_logprob > log_prob_threshold:
            #         should_skip = False
            #
            # A subtitle credit is a fluent, high-probability token sequence --
            # that is precisely why the model reaches for one when there is
            # nothing to transcribe -- so its `avg_logprob` of about -0.88 sits
            # above the library's -1.0 default and switches off the guard aimed
            # at it. That is how `" Untertitelung des ZDF, 2020"` reached a
            # protocol with `no_speech_threshold` already set. It is also why
            # lowering that threshold to 0.4 was measured to change *nothing*:
            # the veto fires wherever the threshold sits, so do not try it.
            #
            # Measured through this exact call path, `tiny`, German, over 111
            # non-speech inputs the gate let through and 31 real utterances
            # sliced out of `tests/fixtures/hello.wav`: with the veto in place
            # 11 non-speech inputs came back carrying invented text and 28 of
            # 31 real utterances survived; with `None`, 0 invented lines
            # survived and 27 of 31 real ones did. The whole price of the fix,
            # on the whole measured set, is that one: a 0.4 s fragment cut out
            # of the middle of a word, which the model rendered " Ah, ja." at
            # a `no_speech_prob` of 0.607. Decode time over the real set also
            # fell, from 14.0 s to 11.2 s, because `None` stops `avg_logprob`
            # triggering the temperature ladder -- the cost this fix was
            # suspected of having, measured, with the opposite sign.
            #
            # What it costs, stated at the right unit: `no_speech_prob` is one
            # number per decoded 30-second window, not per segment
            # (`transcribe.py:1364` copies it onto every `Segment` cut from the
            # window), so a window that loses this argument loses *all* of it,
            # sentences included. Two mitigations, both measured rather than
            # assumed. `clip_timestamps` bounds a window by its clip --
            # `segment_size = min(nb_max_frames, content_frames - seek,
            # seek_clip_end - seek)` at `transcribe.py:1173-1177` -- so only
            # audio the gate already merged into one clip, less than
            # `speech_gate._MERGE_GAP_SECONDS` apart, can ever share a window.
            # And where real speech and room tone did share a
            # window, the speech dominated: `no_speech_prob` came out at
            # 0.018-0.058 and the model transcribed the sentence instead of
            # inventing anything. The residual loss is a lone short utterance
            # alone in its window, which is what drives the probability up:
            # the full 4.1 s fixture scored 0.014 and the same audio 34 dB
            # quieter scored 0.011, while 0.4 s fragments of it scored
            # 0.39-0.80.
            #
            # Do not read that band as a margin. It is not one: real 0.4 s
            # fragments reached 0.804 while invented lines went as low as
            # 0.639, so the two classes overlap and no threshold separates
            # them. 0.6 is the library's own default, it is where this file has
            # always had `no_speech_threshold`, and on the measured set it
            # rejects every invented line at the cost of four 0.4 s fragments
            # of a real word. Whoever moves it should move it against a second
            # speaker and a second fixture, and should expect to trade, not to
            # find a gap.
            log_prob_threshold=None,
            # The library defaults this to `True`, which feeds each
            # segment's own text back in as the prompt for the next one.
            # One hallucinated segment then becomes the context every
            # following segment is decoded against, and the cascade the
            # two thresholds above exist to catch is exactly what that
            # produces. Cutting the silence out makes the default worse
            # here rather than better, whichever way it is cut: one
            # speaker's track becomes fragments minutes apart, so the
            # "previous text" is routinely about something else entirely --
            # per-speaker recordings of a conversation are the case this
            # default is least suited to. (faster-whisper also resets the
            # prompt at every window boundary of its own accord, so under
            # the clip boundaries above this is belt and braces rather than
            # the load-bearing part. It stays because it is a decision, and
            # because the boundaries are not what it depends on.)
            condition_on_previous_text=False,
            # Above the library's default of 5. Beam search cost is
            # roughly linear in the width and this deployment transcribes
            # offline, one speaker's file at a time, hours after the
            # meeting -- so the trade is CPU seconds (which the worker has,
            # see `charts/sturnus/values.yaml`) against a wrong word in a
            # document people read instead of having been in the room.
            beam_size=8,
        )
        # The model was handed concatenated speech, so every time it
        # reports is a position in *that* array: an utterance spoken at 02:30
        # comes back at about five seconds. `sturnus.application.
        # transcription.to_absolute` adds the speaker's epoch to whatever is
        # here and `domain.transcript` sorts all speakers' segments together
        # on the result, so leaving these alone would not make a
        # slightly-wrong document — it would stack the whole meeting into its
        # first seconds and interleave two speakers into nonsense. The
        # restored times are file-relative in exactly the sense `to_absolute`
        # assumes, to within the 10 ms of frame-grid rounding
        # `_on_the_original_timeline` describes.
        #
        # **A loop, not a tuple comprehension, and that is the change.**
        # `segments` is a lazy generator: a comprehension consumes it inside
        # a single expression, so nothing between the first segment and the
        # last is ever observable and a job is only measurable once it has
        # already finished. Reporting each `end` as it arrives costs one
        # method call per segment -- a few hundred per job -- and is what
        # makes a running transcription's position, and a stalled one's
        # silence, visible at all.
        total_seconds = float(getattr(info, "duration_after_vad", 0.0) or 0.0)
        TRANSCRIPTION_PROGRESS.set_total(total_seconds)
        collected: list[TranscribedSegment] = []
        for segment in segments:
            start, end = _on_the_original_timeline(
                segment.start,
                segment.end,
                segment.seek,
                bounds,
                clip_starts_in_frames,
                offsets,
            )
            collected.append(TranscribedSegment(start=start, end=end, text=segment.text))
            # `segment.end` and not the restored `end`, deliberately. The
            # denominator set just above is `duration_after_vad`, which is the
            # concatenated speech the model was handed; the restored end is on
            # the recording's timeline, up to a whole meeting further along.
            # Reporting one against the other would put a job that has decoded
            # its first clip at several hundred percent and make the real-time
            # factor a number about the removed silence.
            TRANSCRIPTION_PROGRESS.advance(segment.end)
        # The decoder walked to the end of the speech it was handed whether or
        # not the last stretch of it produced a segment, so the counter is
        # topped up to the audio the model was actually given. This is the line
        # that makes a job returning nothing at all show up as an impossible
        # real-time factor rather than as a silent zero.
        TRANSCRIPTION_PROGRESS.advance(total_seconds)

        wall_seconds = time.monotonic() - started
        # The model never saw the padding at all, so a speaker whose file
        # opens with twenty minutes of it no longer has their language guessed
        # from silence.
        detected = getattr(info, "language", None) or self._default_language
        # `job.transcribed` in `sturnus.application.worker` reports a
        # realtime factor too, computed from the last segment's `end`. That
        # is the right number for "how long did this take per minute of
        # speech" and the wrong one for "did this decode anything at all",
        # because a job with no segments has no denominator there. This one
        # divides by the audio handed over -- which since the speech is
        # concatenated is `duration_after_vad`, the gated seconds themselves --
        # so it is defined exactly when the question is worth asking.
        #
        # WARNING rather than INFO when nothing came back, and the message
        # says why it matters rather than restating the count. From in here a
        # track of room tone correctly rejected and a track of quiet speech
        # wrongly rejected are the same event, and only the seconds below tell
        # them apart -- 0.9 s dropped is the guard working, forty minutes
        # dropped is an incident. `log_prob_threshold=None` makes
        # faster-whisper discard the window internally and leaves no trace
        # except a DEBUG line on its own logger, which nothing here
        # configures, so this is the only place it can be seen at all.
        if collected:
            log_event(
                log,
                logging.INFO,
                Event.TRANSCRIPTION_DECODED,
                "Decoded one speaker's recording",
                model=model_name,
                language=detected,
                audio_seconds=round(audio_seconds, 3),
                speech_seconds=round(speech_seconds, 3),
                clips=len(clips),
                segments=len(collected),
                wall_seconds=round(wall_seconds, 3),
                realtime_factor=round(wall_seconds / total_seconds, 4) if total_seconds else None,
            )
        else:
            log_event(
                log,
                logging.WARNING,
                Event.TRANSCRIPTION_DECODED,
                "The gate passed audio above the silence floor but the decoder judged "
                "every window to be silence; this speaker contributes nothing to the "
                "protocol",
                model=model_name,
                language=detected,
                audio_seconds=round(audio_seconds, 3),
                speech_seconds=round(speech_seconds, 3),
                clips=len(clips),
                segments=len(collected),
                wall_seconds=round(wall_seconds, 3),
                realtime_factor=round(wall_seconds / total_seconds, 4) if total_seconds else None,
            )
        # Onto `job.transcribe`, opened by
        # `traced.TracedTranscriptionEngine` around this call --
        # `asyncio.to_thread` copies the context, so the span is the
        # enclosing one rather than an orphan. The wrapper cannot set these
        # two: it sees a `TranscriptionResult`, and the gate's numbers are
        # not in it.
        set_current_span_fields(speech_seconds=speech_seconds, clips=len(clips))
        return TranscriptionResult(
            segments=tuple(collected),
            language=detected,
            # This is the only place all three exist together. `audio_seconds`
            # is the file as written, `speech_seconds` what the gate passed on,
            # and the segment count what came back -- the caller can derive
            # none of them, which is why they travel on the result rather than
            # being recomputed downstream.
            measurements=JobMeasurements(
                model=model_name,
                audio_seconds=audio_seconds,
                speech_seconds=speech_seconds,
                segment_count=len(collected),
            ),
        )
