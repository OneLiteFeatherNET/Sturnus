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

from sturnus.domain.measurements import JobMeasurements
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
    #: What the engine measured about the recording it was handed, when it
    #: is in a position to know. `None` for an engine that only decodes --
    #: a test double, or a future backend that is handed audio rather than
    #: a file and so never sees its length.
    #:
    #: It rides on the result rather than being recomputed by the caller
    #: because the caller *cannot* recompute it: the gate's figures are not
    #: derivable from the segments. `max(segment.end)` is the end of the
    #: last thing said, which on a track whose speaker fell silent halfway
    #: through is nowhere near the length of the recording -- and a track
    #: that decoded to nothing has no segments to take a maximum of at all,
    #: which is precisely the case worth measuring.
    measurements: JobMeasurements | None = None


class TranscriptionEngine(Protocol):
    async def transcribe(
        self, path: Path, language: str | None, initial_prompt: str | None
    ) -> TranscriptionResult:
        """Transcribe one speaker's recording.

        `language` pins the language; `None` asks the engine to detect it and
        report what it found.

        `initial_prompt` is vocabulary and style for the engine to lean
        towards — an organisation's project names, the words a general
        model has never seen and will otherwise replace with something it
        has. It is deliberately a required argument rather than one with a
        default: the guild's configured prompt (Spec 11) is worth nothing
        if a call site can quietly leave it out, and a caller that really
        has no vocabulary to offer says so by passing `None`.
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
