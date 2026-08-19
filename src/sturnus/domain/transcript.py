"""Target-neutral transcript model.

Deliberately contains no markup: which parts of a speaker identity show
up in the result, and in what form, is decided solely by the respective
adapter via its template.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_MERGE_GAP = timedelta(seconds=15)


@dataclass(frozen=True)
class SpeakerIdentity:
    discord_user_id: int
    discord_display_name: str
    external_user_id: str | None = None
    external_display_name: str | None = None


@dataclass(frozen=True)
class Segment:
    speaker: SpeakerIdentity
    start: datetime
    end: datetime
    text: str


@dataclass(frozen=True)
class TranscriptBlock:
    speaker: SpeakerIdentity
    start: datetime
    text: str


@dataclass(frozen=True)
class Transcript:
    session_started_at: datetime
    session_ended_at: datetime
    participants: tuple[SpeakerIdentity, ...]
    blocks: tuple[TranscriptBlock, ...]


def build_transcript(
    segments: Iterable[Segment],
    session_started_at: datetime,
    session_ended_at: datetime,
    merge_gap: timedelta = DEFAULT_MERGE_GAP,
) -> Transcript:
    """Orders segments from all speakers chronologically and merges them into blocks."""
    usable = sorted(
        (s for s in segments if s.text.strip()),
        key=lambda s: (s.start, s.speaker.discord_user_id),
    )

    blocks: list[TranscriptBlock] = []
    participants: list[SpeakerIdentity] = []
    open_speaker: SpeakerIdentity | None = None
    open_start: datetime | None = None
    open_end: datetime | None = None
    open_parts: list[str] = []

    def flush() -> None:
        nonlocal open_speaker, open_start, open_end, open_parts
        if open_speaker is not None and open_start is not None:
            blocks.append(TranscriptBlock(open_speaker, open_start, " ".join(open_parts)))
        open_speaker, open_start, open_end, open_parts = None, None, None, []

    for segment in usable:
        # Track participants by discord_user_id; prefer richer variants (with external fields).
        participant_idx = next(
            (
                i
                for i, p in enumerate(participants)
                if p.discord_user_id == segment.speaker.discord_user_id
            ),
            None,
        )
        if participant_idx is None:
            participants.append(segment.speaker)
        elif (
            segment.speaker.external_user_id is not None
            and participants[participant_idx].external_user_id is None
        ):
            participants[participant_idx] = segment.speaker

        continues = (
            open_speaker == segment.speaker
            and open_end is not None
            and segment.start - open_end <= merge_gap
        )
        if not continues:
            flush()
            open_speaker = segment.speaker
            open_start = segment.start

        open_parts.append(segment.text.strip())
        open_end = max(open_end, segment.end) if open_end is not None else segment.end

    flush()

    return Transcript(
        session_started_at=session_started_at,
        session_ended_at=session_ended_at,
        participants=tuple(participants),
        blocks=tuple(blocks),
    )
