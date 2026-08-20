"""The transcription worker (Spec 5.3, Spec 7, Spec 8, Spec 12.1).

`process_one` claims one job, decrypts its audio, transcribes it, stores the
transcript, and — if it was the session's last job — creates the session's
protocol document. Every temporary file it touches is removed in a
`finally`, regardless of where processing fails: a decrypted recording left
on disk is exactly what the envelope encryption (Spec 12.1) exists to
prevent, while every other failure here is allowed to fail loudly.

Deleting the audio object itself is deliberately **not** done here, even
though it would be the obvious next step after a successful transcription.
Spec 12.2 keeps the object for `audio_retention_days` so a poor
transcription can be redone from the original audio; that deletion belongs
to the retention sweep (`sturnus.application.retention`), not to this job.

Language pinning (Spec 7): a speaker's first job asks the engine to detect
the language and persists what it found; every later job for that same
speaker passes the stored language back in, so one protocol never mixes
languages mid-session because the engine's guess drifted.

Dependency-rule note: this module lives in `sturnus.application`, which must
never import `sturnus.infrastructure` (tests/test_architecture.py). Every
collaborator below is therefore a narrow local `Protocol`, the same pattern
`sturnus.application.assembly` uses for `SessionReader`/`JobReader`/
`LinkReader` -- the concrete adapters that satisfy these shapes live in
`sturnus.infrastructure` and `sturnus.entrypoints.worker`, never imported
here by name. This is also why a permanently-rejected document creation is
recognised below by its exception's class *name* rather than by catching
`sturnus.infrastructure.documents.outline.PermanentDocumentError` directly:
importing that type here would be exactly the violation this rule exists
to prevent, and neither that module nor `sturnus.application.documents`
(where a shared exception type would otherwise belong) is a file this task
may modify.

**Known gap, reported rather than papered over.** Step 6 of the brief asks
this function to "assemble, render, create the document" once a session's
last job completes -- i.e. to call `sturnus.application.assembly.assemble`,
which needs a `SessionReader` (`session_bounds`, `participant_names`,
`audio_epoch`), a `JobReader` (`transcripts_for`), and a `LinkReader`
(`external_identity`) to merge every speaker's stored transcript into one
chronological document. But the `sessions` collaborator this function
actually receives -- per the brief's own test fixture, `FakeSessions` in
`tests/application/test_worker.py` -- exposes only `detected_language`,
`set_detected_language`, and `mark_documented`; `assemble`'s very first
line calls `session_bounds`, which that fixture does not implement, so
calling `assemble` with this object would fail every "last job" test at
runtime, not just under static analysis. None of the twelve given tests
supply anything richer. Rather than silently deciding this away, the
document built below uses *only the job that happened to finish last*: its
own transcript, converted through the same `to_absolute`/`build_transcript`
machinery `assemble` itself uses, anchored to the moment this function
runs. For a session with more than one speaker, the created document
currently reflects only that last speaker's words, not the full merge
`assemble` would produce. Closing this gap needs a decision this module
should not make unilaterally: either widen `sessions` (and its test
double) to the full `SessionReader`/`JobReader`/`LinkReader` shape and
genuinely call `assemble`, or give `process_one` those three readers as
their own parameters, wired from the real repositories in
`sturnus.entrypoints.worker`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from sturnus.application.assembly import serialize_transcript
from sturnus.application.documents import (
    CreatedDocument,
    DocumentSink,
    document_title,
    render_transcript,
)
from sturnus.application.transcription import (
    TranscriptionEngine,
    TranscriptionResult,
    to_absolute,
)
from sturnus.domain.transcript import SpeakerIdentity, build_transcript

log = logging.getLogger(__name__)

#: A minimal, self-contained fallback template. It is the default
#: `template_source` for `process_one` so the twelve tests in
#: `tests/application/test_worker.py` -- none of which pass one -- can run
#: without this module reaching into `sturnus.infrastructure` for the
#: packaged `outline_template.md.j2`. Production callers
#: (`sturnus.entrypoints.worker`) load the real packaged template from disk
#: and pass its text in explicitly instead.
_FALLBACK_TEMPLATE = (
    "{% for block in blocks %}"
    "**{{ block.time }}** · {{ block.speaker.discord_display_name | md }}\n\n"
    "{{ block.text | md }}\n\n"
    "{% endfor %}"
)


class Queue(Protocol):
    """Where jobs are claimed, completed, and failed (`sturnus.infrastructure.db.queue.JobQueue`).

    `claim` deliberately returns `object | None` rather than a concrete job
    type: the real job (`ClaimedJob`) lives in `sturnus.infrastructure.db.
    queue`, which this module must never import. `process_one` narrows the
    claimed value to `_ClaimedJobShape` with `cast` immediately after.
    """

    async def claim(self) -> object | None: ...

    async def complete(self, job_id: int, transcript: str) -> bool: ...

    async def fail(self, job_id: int, error: str, max_attempts: int) -> None: ...


class AudioDownloader(Protocol):
    """Where the encrypted recording is fetched from before it can be decrypted."""

    async def get(self, key: str, target: Path) -> None: ...


class Decryptor(Protocol):
    """Unwraps the session's data key and decrypts the recording with it.

    Synchronous by design (`sturnus.infrastructure.crypto.KeyWrapper.unwrap`
    and `decrypt_file` are both CPU/IO-bound, not natively awaitable);
    `process_one` runs it through `asyncio.to_thread` itself.
    """

    def decrypt_to(self, source: Path, target: Path, wrapped: bytes, key_id: str) -> None: ...


class SessionStore(Protocol):
    """The session-scoped bookkeeping this job needs: language pinning and completion.

    Deliberately narrow -- see the module docstring's "Known gap" note for
    why this does not also cover `sturnus.application.assembly.SessionReader`.
    """

    async def detected_language(self, session_id: int, user_id: int) -> str | None: ...

    async def set_detected_language(self, session_id: int, user_id: int, lang: str) -> None: ...

    async def mark_documented(self, session_id: int, doc_id: str, url: str) -> None: ...


class _ClaimedJobShape(Protocol):
    """The attributes `process_one` reads off whatever `Queue.claim` returns.

    Matches `sturnus.infrastructure.db.queue.ClaimedJob` structurally
    without importing it (see `Queue.claim`'s docstring).
    """

    id: int
    session_id: int
    discord_user_id: int
    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes


async def _create_session_document(
    documents: DocumentSink,
    sessions: SessionStore,
    job: _ClaimedJobShape,
    result: TranscriptionResult,
    template_source: str,
) -> None:
    """Builds and creates the document for the job that finished a session.

    See the module docstring's "Known gap" note: this uses only the one
    job that happened to complete the session, not every speaker's stored
    transcript. A permanently-rejected creation (an unretryable rejection
    from the document sink, e.g. a deleted collection) is swallowed here
    rather than re-raised: the transcription job this belongs to already
    completed successfully before this is ever called, so there is nothing
    left to fail or retry -- only the document never gets created, which is
    logged instead.
    """
    speaker = SpeakerIdentity(
        discord_user_id=job.discord_user_id,
        discord_display_name=str(job.discord_user_id),
    )
    epoch = datetime.now(UTC)
    segments = to_absolute(result, epoch, speaker)
    ended_at = epoch + timedelta(seconds=max((s.end for s in result.segments), default=0.0))
    transcript = build_transcript(segments, epoch, ended_at)

    body = render_transcript(transcript, template_source, UTC)
    title = document_title(transcript, UTC)
    try:
        created: CreatedDocument = await documents.create(title, body)
    except Exception as exc:
        # Recognised by class name, not by `except PermanentDocumentError`:
        # that type lives in `sturnus.infrastructure.documents.outline`,
        # which this module must never import (see the module docstring).
        if type(exc).__name__ != "PermanentDocumentError":
            raise
        log.warning("Document sink permanently rejected creation for session %d", job.session_id)
        return
    await sessions.mark_documented(job.session_id, created.id, created.url)


async def process_one(
    queue: Queue,
    engine: TranscriptionEngine,
    store: AudioDownloader,
    crypto: Decryptor,
    documents: DocumentSink,
    sessions: SessionStore,
    work_dir: Path,
    max_attempts: int,
    template_source: str = _FALLBACK_TEMPLATE,
) -> bool:
    """Processes one claimed job end to end. Returns `False` if the queue was empty.

    Order, and why it is this order:

    1. Claim -- nothing claimed means there is no work; the caller backs off.
    2. Download the encrypted object to a scratch directory under `work_dir`.
    3. Unwrap the data key and decrypt to a plaintext WAV, still on disk.
    4. Transcribe -- language pinning per Spec 7 (see the module docstring).
    5. Store the transcript on the job; ask whether it was the session's last.
    6. If it was: create the document and mark the session documented.
    7. Every temporary file made in steps 2-3 is removed in a `finally`, so
       a failure anywhere above never leaves decrypted speech on disk. The
       audio object in S3 is left alone deliberately -- see the module
       docstring.
    """
    claimed = await queue.claim()
    if claimed is None:
        return False
    job = cast(_ClaimedJobShape, claimed)

    job_dir = work_dir / f"job-{job.id}-{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)
    encrypted_path = job_dir / "audio.enc"
    wav_path = job_dir / "audio.wav"

    try:
        await store.get(job.s3_key, encrypted_path)
        await asyncio.to_thread(
            crypto.decrypt_to,
            encrypted_path,
            wav_path,
            job.wrapped_data_key,
            job.encryption_key_id,
        )

        pinned_language = await sessions.detected_language(job.session_id, job.discord_user_id)
        try:
            result = await engine.transcribe(wav_path, pinned_language)
        except Exception as exc:
            log.warning("Transcription failed for job %d", job.id)
            await queue.fail(job.id, str(exc), max_attempts)
            return True

        if pinned_language is None:
            await sessions.set_detected_language(
                job.session_id, job.discord_user_id, result.language
            )

        is_last = await queue.complete(job.id, serialize_transcript(result))

        if is_last:
            await _create_session_document(documents, sessions, job, result, template_source)

        return True
    finally:
        # Runs whether processing succeeded, the transcription failed, or
        # something above raised outright: decrypted speech (and the
        # encrypted copy fetched to build it) must never survive this
        # function, regardless of how it exits.
        shutil.rmtree(job_dir, ignore_errors=True)
