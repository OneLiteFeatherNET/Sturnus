"""faster-whisper behind the transcription port.

The library is synchronous and CPU-bound, so every call runs in a worker
thread. The model is loaded once and reused; jobs are processed one at a
time (Spec 5.3), so no locking is required around it.
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

    async def transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        return await asyncio.to_thread(self._transcribe, path, language)

    def _transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        segments, info = self._model.transcribe(
            str(path),
            language=language,
            # Skips the padded silence, which is most of a speaker's file and
            # would otherwise cost real time and invite hallucinated text.
            vad_filter=True,
            # Guards against the repetition cascades Whisper can fall into on
            # long audio (Spec 7).
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
        )
        collected = tuple(
            TranscribedSegment(start=s.start, end=s.end, text=s.text) for s in segments
        )
        detected = getattr(info, "language", None) or self._default_language
        return TranscriptionResult(segments=collected, language=detected)
