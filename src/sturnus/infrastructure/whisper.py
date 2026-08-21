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
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from faster_whisper import WhisperModel  # type: ignore[import-untyped]
from faster_whisper.audio import decode_audio  # type: ignore[import-untyped]

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
)
from sturnus.infrastructure.speech_gate import speech_clips

_SAMPLE_RATE = 16_000

log = logging.getLogger(__name__)


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

        collected = tuple(
            TranscribedSegment(start=s.start, end=s.end, text=s.text) for s in segments
        )

        # Count what the guard cost, every time, because from in here a track
        # of room tone correctly rejected and a track of quiet speech wrongly
        # rejected are the same event. Nothing else in the system can see this
        # either: `log_prob_threshold=None` makes faster-whisper drop the
        # window internally, and the only trace it leaves is a DEBUG line on
        # its own `faster_whisper` logger, which nothing here configures. So a
        # transcript that came back empty would otherwise be indistinguishable
        # from a speaker who never spoke -- and that is exactly the failure
        # this branch exists to make impossible in the document, so it must not
        # be reintroduced in the logs.
        #
        # The seconds matter more than the counts and are what the message
        # leads with: 0.9 s of room tone dropped is the guard working, forty
        # minutes dropped is an incident, and only the duration tells them
        # apart. The text itself is never logged -- the worker's logs are not
        # access-controlled the way the Outline collection is.
        gated_seconds = sum(end - start for start, end in clips)
        if collected:
            log.debug(
                "%s: gate passed %d clip(s)/%.1f s, decoder kept %d segment(s)/%.1f s",
                path,
                len(clips),
                gated_seconds,
                len(collected),
                sum(s.end - s.start for s in collected),
            )
        else:
            log.warning(
                "%s: gate passed %d clip(s)/%.1f s of audio above the silence floor "
                "but the decoder judged every window to be silence, so this speaker "
                "contributes nothing to the protocol",
                path,
                len(clips),
                gated_seconds,
            )

        # With `clip_timestamps` set, detection starts at the first clip
        # instead of at second 0, so a speaker whose file opens with twenty
        # minutes of padding no longer has their language guessed from it.
        detected = getattr(info, "language", None) or self._default_language
        return TranscriptionResult(segments=collected, language=detected)
