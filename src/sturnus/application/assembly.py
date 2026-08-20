"""Assembling one whole session's transcript from its jobs (Spec 5.3, Spec 8.3).

`assemble` is the seam between persistence and the domain: it reads what a
session's participants and their jobs actually produced, turns each
speaker's relative-offset transcription into absolute segments with
`to_absolute`, attaches whatever external identity a link provides, and
hands the flattened list to `build_transcript` from Plan 1, which owns the
ordering and block-merging rules. `assemble` itself makes exactly one
substantive decision: a speaker with no audio epoch never actually spoke,
so they contribute no segments rather than being placed, wrongly, at the
session's start.

`sessions`, `jobs` and `links` are typed as local `Protocol`s rather than
the concrete repositories, for the same reason `sturnus.application.
recording.SessionRecorder` is: this module must not import
`sturnus.infrastructure` (tests/test_architecture.py), and a protocol lets
both the real repositories and the fakes in tests/application/
test_assembly.py satisfy the same shape.

`serialize_transcript`/`deserialize_transcript` below are the single
definition of how a `TranscriptionResult` is encoded as the plain-text
`transcription_job.transcript` column. They live here, in `application`,
rather than in `sturnus.infrastructure.db.repositories`, because
`infrastructure` may import `application` but never the reverse (the same
resolution `sturnus.application.documents.escape_markdown` uses) -- so this
is the one place both the worker that writes the column (Task 9,
`sturnus.application.worker`) and `JobRepository.transcripts_for`, which
reads it back, can agree on the format without either layer reaching into
the other.
"""

from __future__ import annotations

import json
from datetime import datetime, tzinfo
from typing import Protocol

from sturnus.application.transcription import TranscribedSegment, TranscriptionResult, to_absolute
from sturnus.domain.transcript import Segment, SpeakerIdentity, Transcript, build_transcript


class SessionReader(Protocol):
    """What `assemble` needs to know about a session's participants."""

    async def participant_names(self, session_id: int) -> dict[int, str]: ...

    async def audio_epoch(self, session_id: int, user_id: int) -> datetime | None: ...

    async def session_bounds(self, session_id: int) -> tuple[datetime, datetime]: ...


class JobReader(Protocol):
    """Where `assemble` reads each speaker's finished transcription from."""

    async def transcripts_for(self, session_id: int) -> dict[int, TranscriptionResult]: ...


class LinkReader(Protocol):
    """Where `assemble` reads a speaker's external identity from, if linked."""

    async def external_identity(self, discord_user_id: int) -> tuple[str, str] | None: ...


def serialize_transcript(result: TranscriptionResult) -> str:
    """The one encoding of a `TranscriptionResult` for `transcription_job.transcript`.

    Plain JSON:
    `{"language": "de", "segments": [{"start": 0.0, "end": 2.0, "text": "..."}]}`.
    JSON was chosen over pickling for safety (the column is later read back
    by a different process) and readability (it is inspectable directly in
    the database while debugging a stuck session).
    """
    return json.dumps(
        {
            "language": result.language,
            "segments": [
                {"start": segment.start, "end": segment.end, "text": segment.text}
                for segment in result.segments
            ],
        }
    )


def deserialize_transcript(text: str) -> TranscriptionResult:
    """The paired reader for `serialize_transcript`."""
    data = json.loads(text)
    return TranscriptionResult(
        segments=tuple(
            TranscribedSegment(segment["start"], segment["end"], segment["text"])
            for segment in data["segments"]
        ),
        language=data["language"],
    )


async def assemble(
    session_id: int,
    sessions: SessionReader,
    jobs: JobReader,
    links: LinkReader,
    tz: tzinfo,
) -> Transcript:
    """Builds the whole session's `Transcript` from its jobs' stored transcriptions.

    Every absolute time handed to `build_transcript` -- the session bounds
    and each speaker's segments alike -- is localised to `tz` first, so the
    `Transcript` this returns already carries the timezone its reader
    lives in rather than leaving that conversion to whichever caller
    consumes it next.
    """
    started_at, ended_at = await sessions.session_bounds(session_id)
    names = await sessions.participant_names(session_id)
    transcripts = await jobs.transcripts_for(session_id)

    segments: list[Segment] = []
    for discord_user_id, result in transcripts.items():
        epoch = await sessions.audio_epoch(session_id, discord_user_id)
        if epoch is None:
            # No audio epoch means no audio was ever recorded for this
            # speaker; defaulting to the session start would place their
            # words at a time they demonstrably did not speak.
            continue

        external = await links.external_identity(discord_user_id)
        speaker = SpeakerIdentity(
            discord_user_id=discord_user_id,
            discord_display_name=names.get(discord_user_id, str(discord_user_id)),
            external_user_id=external[0] if external is not None else None,
            external_display_name=external[1] if external is not None else None,
        )
        segments.extend(to_absolute(result, epoch.astimezone(tz), speaker))

    return build_transcript(segments, started_at.astimezone(tz), ended_at.astimezone(tz))
