"""Assembling one whole session's transcript from its jobs (Spec 5.3, Spec 8.3).

`assemble` is the seam between persistence and the domain: it reads what a
session's participants and their jobs actually produced, turns each
speaker's relative-offset transcription into absolute segments with
`to_absolute`, attaches whatever external identity a link provides, and
hands the flattened list to `build_transcript` from Plan 1, which owns the
ordering and block-merging rules. `assemble` itself makes exactly one
substantive decision: a speaker with no audio epoch never actually spoke,
so they contribute no segments rather than being placed, wrongly, at the
session's start -- and, for the same reason, is not counted as an attendee.
Everyone who *does* have an epoch is passed to `build_transcript` as the
recorded roster, separately from their segments, because a recorded speaker
can produce no text at all (see `sturnus.infrastructure.whisper`) and the
attendee list must not read that as absence.

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

`BoundLinks` and `merge_gap_from` are here for the same reason and are
newer: `assemble` has two callers now, not one. The worker builds the
published protocol with it, and the console serves
`GET /api/sessions/{id}/transcript` with it -- and the two must produce
the same reading of the same meeting. Anything either caller has to do
*around* `assemble` to get there therefore belongs next to `assemble`
rather than in one of them, because a second copy is a second place for
the console to disagree with the document.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, tzinfo
from typing import Protocol

from sturnus.application.transcription import TranscribedSegment, TranscriptionResult, to_absolute
from sturnus.domain.transcript import (
    DEFAULT_MERGE_GAP,
    Segment,
    SpeakerIdentity,
    Transcript,
    build_transcript,
)


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
    merge_gap: timedelta = DEFAULT_MERGE_GAP,
) -> Transcript:
    """Builds the whole session's `Transcript` from its jobs' stored transcriptions.

    Every absolute time handed to `build_transcript` -- the session bounds
    and each speaker's segments alike -- is localised to `tz` first, so the
    `Transcript` this returns already carries the timezone its reader
    lives in rather than leaving that conversion to whichever caller
    consumes it next.

    `merge_gap` is forwarded to `build_transcript` unchanged, defaulting to
    the same `DEFAULT_MERGE_GAP` `build_transcript` itself would use if
    called directly. How long a pause may be before one speaker's blocks
    split is a judgement, not a fact (Spec 11's `merge_gap_seconds`), so
    the real caller (`sturnus.application.worker._create_session_document`)
    reads it from per-guild configuration and passes it in here instead of
    relying on this default.
    """
    started_at, ended_at = await sessions.session_bounds(session_id)
    names = await sessions.participant_names(session_id)
    transcripts = await jobs.transcripts_for(session_id)

    segments: list[Segment] = []
    recorded: list[SpeakerIdentity] = []
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
        # Collected whether or not the transcription produced anything, and
        # handed to `build_transcript` separately from the segments. A speaker
        # can reach this point with an empty transcript because
        # faster-whisper judged every one of their decoded windows to be
        # silence (`sturnus.infrastructure.whisper`), and deriving the
        # attendee list from the segments alone would then let the protocol
        # report that they were not at the meeting. The audio epoch just
        # checked above is the evidence they were: it is written when their
        # first packet arrives, so having one means audio of them exists,
        # which is a different question from whether any of it became text.
        recorded.append(speaker)
        segments.extend(to_absolute(result, epoch.astimezone(tz), speaker))

    return build_transcript(
        segments,
        started_at.astimezone(tz),
        ended_at.astimezone(tz),
        merge_gap,
        recorded=recorded,
    )


class LinkRepository(Protocol):
    """Where a speaker's external identity is read from, keyed by provider.

    Unlike `LinkReader` above, `provider` is a parameter of
    `external_identity` rather than something fixed at construction: one
    process serves every guild, and which provider's account-link mapping
    applies is itself per-guild configuration (Spec 11's
    `document_provider`) that cannot be resolved until a session's guild
    is known. `BoundLinks` below adapts one resolved provider back down to
    the narrower `LinkReader` shape `assemble` actually calls.
    """

    async def external_identity(
        self, discord_user_id: int, provider: str
    ) -> tuple[str, str] | None: ...


class BoundLinks:
    """A `LinkReader` for one provider, over a repository that serves many.

    `assemble` asks "who is this person elsewhere" and has no business
    knowing that the answer depends on a guild's `document_provider`; the
    repository cannot answer without one. This binds the two, and it lives
    here rather than in either caller because both the worker writing a
    protocol and the console serving `/api/sessions/{id}/transcript` need
    the same binding -- and a second copy of it is a second place for the
    console to resolve a speaker's identity differently from the document
    that was published about the same meeting.
    """

    def __init__(self, links: LinkRepository, provider: str) -> None:
        self._links = links
        self._provider = provider

    async def external_identity(self, discord_user_id: int) -> tuple[str, str] | None:
        return await self._links.external_identity(discord_user_id, self._provider)


def merge_gap_from(configured: str | None) -> timedelta:
    """A guild's `merge_gap_seconds` as a `timedelta`, always.

    Total rather than strict, for the reason `sturnus.infrastructure.db.
    queue._parallel_track_limit` and `sturnus.application.worker.
    _configured_language` are both total: `ConfigStore.set` refuses a
    non-integer, but `docs/operations.md` section 4.1 tells operators they
    may edit `guild_config` with SQL, and that write never sees the
    validation. An unusable value falls back to `DEFAULT_MERGE_GAP` --
    blocks merged by the default rule are a smaller loss than a protocol
    that is never written and a transcript tab that answers 500.

    One function rather than one expression in the worker and another in
    the console adapter, because the two must agree: how long a pause may
    be before a speaker's blocks split decides where the paragraph breaks
    fall, and a console showing different paragraphs from the published
    document is a console nobody trusts.
    """
    if configured is None:
        return DEFAULT_MERGE_GAP
    try:
        seconds = int(configured.strip())
    except (AttributeError, ValueError):
        return DEFAULT_MERGE_GAP
    if seconds < 0:
        return DEFAULT_MERGE_GAP
    return timedelta(seconds=seconds)
