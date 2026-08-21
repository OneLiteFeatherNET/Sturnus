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

The decoder's output is filtered afterwards as well, on the model's own
`no_speech_prob`. Those parameters alone were not enough: they are built for
long repetition cascades, and the failure that reached production was a
single short, fluent subtitle credit invented on room tone. The comment on
`_NO_SPEECH_LIMIT` and the one beside the filter carry that reasoning.

Silence is cut out before the decoder sees it, but *not* by faster-whisper's
own `vad_filter`. That option runs Silero, whose recurrent state collapses on
the bit-exact zero padding `SpeakerWriter` writes between packets — it
reported about one second of speech in two minutes of a real recording and
every transcript this project produced came back empty or hallucinated.
`sturnus.infrastructure.speech_gate` does the same job with a stateless
amplitude test; its module docstring carries the full reasoning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from faster_whisper import WhisperModel  # type: ignore[import-untyped]
from faster_whisper.audio import decode_audio  # type: ignore[import-untyped]

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
)
from sturnus.infrastructure.speech_gate import speech_clips

_SAMPLE_RATE = 16_000

# Whisper's own estimate that a decoded window contained no speech at all.
#
# One constant for one judgement, passed to the library as
# `no_speech_threshold` *and* applied by us to the segments it hands back.
# Those are the same question -- "did the model believe there was anything
# there?" -- and giving them two numbers would be two thresholds that have to
# be reasoned about together and will drift apart the first time either moves.
#
# The value sits between the two classes measured on real audio through this
# exact call path: every genuine decode scored at most 0.453 (an isolated
# 0.4 s word, alone in its window, the hardest real case found), and every
# reproduced hallucination scored at least 0.711. 0.6 is near the midpoint,
# 0.147 above the worst real case and 0.111 below the mildest invented one,
# and adjacent to neither.
#
# The honest limits of that evidence: one speech fixture, one speaker, one
# language, and the `tiny` model only. 0.147 of margin is real but narrow, and
# it rests on a single observation of the hardest real case. Whoever moves this
# number next should move it against a second real utterance, not against
# intuition.
_NO_SPEECH_LIMIT = 0.6


class WhisperEngine:
    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        default_language: str,
    ) -> None:
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._default_language = default_language

    async def transcribe(
        self, path: Path, language: str | None, initial_prompt: str | None
    ) -> TranscriptionResult:
        return await asyncio.to_thread(self._transcribe, path, language, initial_prompt)

    def _transcribe(
        self, path: Path, language: str | None, initial_prompt: str | None
    ) -> TranscriptionResult:
        # Decoded here rather than inside `transcribe()` so the gate and the
        # model measure and seek through the *same* array. Handing the model
        # the path instead would decode a 100-minute file a second time, and
        # clip offsets computed on a different copy of the samples could be
        # misaligned against the one being transcribed.
        audio = decode_audio(str(path), sampling_rate=_SAMPLE_RATE)
        clips = speech_clips(audio, sample_rate=_SAMPLE_RATE)

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
            return TranscriptionResult(segments=(), language=self._default_language)

        segments, info = self._model.transcribe(
            audio,
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
            clip_timestamps=[value for clip in clips for value in clip],
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
            # Silero would have excluded, and these two are the part of the
            # filtering the library itself does. They are not the whole of it
            # -- neither can see a short subtitle credit invented on room
            # tone, which is what the `_NO_SPEECH_LIMIT` filter below the call
            # is for, and the comment there explains why `no_speech_threshold`
            # in particular does not fire on one.
            compression_ratio_threshold=2.4,
            no_speech_threshold=_NO_SPEECH_LIMIT,
            # The library defaults this to `True`, which feeds each
            # segment's own text back in as the prompt for the next one.
            # One hallucinated segment then becomes the context every
            # following segment is decoded against, and the cascade the
            # two thresholds above exist to catch is exactly what that
            # produces. `vad_filter` makes the default worse here rather
            # than better: it cuts one speaker's track into fragments with
            # every silence removed, so the "previous text" is routinely
            # from minutes earlier and about something else entirely --
            # per-speaker recordings of a conversation are the case this
            # default is least suited to.
            condition_on_previous_text=False,
            # Above the library's default of 5. Beam search cost is
            # roughly linear in the width and this deployment transcribes
            # offline, one speaker's file at a time, hours after the
            # meeting -- so the trade is CPU seconds (which the worker has,
            # see `charts/sturnus/values.yaml`) against a wrong word in a
            # document people read instead of having been in the room.
            beam_size=8,
        )
        # No offset arithmetic here, deliberately. Unlike the `vad_filter`
        # path, which concatenates the kept audio and repairs the timestamps
        # afterwards, the `clip_timestamps` path runs the feature extractor
        # over the whole array and makes the seek loop jump between clips, so
        # `time_offset = seek * time_per_frame` is already on the original
        # timeline. These offsets stay file-relative in exactly the sense
        # `sturnus.application.transcription.to_absolute` assumes.

        # The filter is what stops a silent track from producing a document
        # that reads like a result. Given a short fragment of room tone the
        # decoder writes what follows the last line of dialogue in the subtitle
        # files it was trained on -- observed in production as " Untertitelung
        # des ZDF, 2020", and before the language was pinned as " Thank you."
        # and " Copyright WDR 2021". Neither threshold above sees it: a
        # four-word credit compresses like a four-word sentence (0.69 measured,
        # against 0.43 for real speech, so it is *less* repetitive than the
        # thing we want to keep), and `no_speech_threshold` is vetoed before it
        # can fire -- faster-whisper computes `should_skip = no_speech_prob >
        # no_speech_threshold` and then clears it again when `avg_logprob >
        # log_prob_threshold` (`transcribe.py:1215-1233`). A credit's
        # `avg_logprob` is about -0.88, above the library's -1.0 default, so
        # the guard disables itself on exactly the segments it exists for. That
        # is why lowering `no_speech_threshold` to 0.4 was measured to change
        # nothing at all, and why the next reader should not try it.
        #
        # So the same judgement is made here instead, on the segments that
        # survived. We deliberately do not pass `log_prob_threshold=None` to
        # close the veto in the library: that parameter also drives temperature
        # fallback and the silence early-out for *all* audio, and the cost of
        # changing it for genuine long segments could not be measured, only
        # guessed at.
        #
        # `s.no_speech_prob` is read directly and not through `getattr` with a
        # default. If faster-whisper ever renames the field we want an
        # AttributeError in the worker, not a guard that quietly stops
        # guarding -- a guard that vanishes without a test failing is how this
        # reached production in the first place. `info.language` below is read
        # with `getattr` because a missing language has a sane fallback; a
        # missing no-speech probability does not.
        #
        # What this costs: an isolated, short utterance decoded in a window
        # with nothing else in it. Being alone is what drives `no_speech_prob`
        # up -- the full 4.1 s fixture scored 0.014 and the *same audio 34 dB
        # quieter* scored 0.011, while slicing it down scored 0.137 at 1.0 s
        # and 0.453 at 0.4 s -- and `_MERGE_GAP_SECONDS` puts any word spoken
        # near other speech into the same clip and the same window as it. So
        # the realistic loss is a lone "Mhm." on an otherwise silent track. A
        # reader loses nothing to a dropped backchannel; a reader who was not
        # in the room, which is who this feature is for, has no way at all to
        # tell an invented line from a real one.
        collected = tuple(
            TranscribedSegment(start=s.start, end=s.end, text=s.text)
            for s in segments
            if s.no_speech_prob <= _NO_SPEECH_LIMIT
        )
        # With `clip_timestamps` set, detection starts at the first clip
        # instead of at second 0, so a speaker whose file opens with twenty
        # minutes of padding no longer has their language guessed from it.
        detected = getattr(info, "language", None) or self._default_language
        return TranscriptionResult(segments=collected, language=detected)
