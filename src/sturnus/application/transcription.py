"""The transcription port and the conversion into domain segments.

Whisper reports offsets relative to the start of the audio file. The file
begins at that speaker's audio epoch (Spec 6.3), so absolute time is the
epoch plus the offset — no in-memory state from the recording process is
needed, which is what lets the worker run in a different process entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from sturnus.domain.transcript import Segment, SpeakerIdentity


@dataclass(frozen=True)
class TranscribedSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    segments: tuple[TranscribedSegment, ...]
    language: str


class TranscriptionEngine(Protocol):
    async def transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        """Transcribe one speaker's recording.

        `language` pins the language; `None` asks the engine to detect it and
        report what it found.
        """
        ...


def to_absolute(
    result: TranscriptionResult, epoch: datetime, speaker: SpeakerIdentity
) -> list[Segment]:
    return [
        Segment(
            speaker=speaker,
            start=epoch + timedelta(seconds=segment.start),
            end=epoch + timedelta(seconds=segment.end),
            text=segment.text,
        )
        for segment in result.segments
    ]
