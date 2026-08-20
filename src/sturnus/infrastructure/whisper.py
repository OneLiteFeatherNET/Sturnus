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
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from faster_whisper import WhisperModel  # type: ignore[import-untyped]

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
)


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
        segments, info = self._model.transcribe(
            str(path),
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
            # Skips the padded silence, which is most of a speaker's file and
            # would otherwise cost real time and invite hallucinated text.
            vad_filter=True,
            # Guards against the repetition cascades Whisper can fall into on
            # long audio (Spec 7).
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
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
        collected = tuple(
            TranscribedSegment(start=s.start, end=s.end, text=s.text) for s in segments
        )
        detected = getattr(info, "language", None) or self._default_language
        return TranscriptionResult(segments=collected, language=detected)
