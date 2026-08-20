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

Once a session's last job completes, `process_one` calls
`sturnus.application.assembly.assemble` to merge *every* participant's
stored transcript -- not just the job that happened to finish last -- into
one chronological `Transcript`, then renders it through
`sturnus.application.documents.render_transcript`/`document_title` before
handing the result to `documents.create`. `assemble` needs a `SessionReader`
(`session_bounds`, `participant_names`, `audio_epoch`), a `JobReader`
(`transcripts_for`), and a `LinkReader` (`external_identity`). `SessionStore`
below is widened to be structurally a `SessionReader` as well as its
original language-pinning/completion shape, so the one `sessions`
collaborator satisfies both; `jobs` and `links` are threaded through
`process_one` as their own parameters, typed with `assembly`'s own
`JobReader`/`LinkReader` protocols rather than duplicating them here.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from sturnus.application.assembly import JobReader, LinkReader, assemble, serialize_transcript
from sturnus.application.documents import (
    CreatedDocument,
    DocumentSink,
    document_title,
    render_transcript,
)
from sturnus.application.transcription import TranscriptionEngine

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
    """The session-scoped bookkeeping this job needs.

    Covers the original language-pinning/completion bookkeeping
    (`detected_language`/`set_detected_language`/`mark_documented`) *and*
    everything `sturnus.application.assembly.assemble` needs to know about
    a session's participants (`participant_names`/`audio_epoch`/
    `session_bounds`) -- widened to that full shape so the one `sessions`
    collaborator `process_one` already receives can also be passed to
    `assemble` as its `SessionReader`, structurally, without a separate
    parameter.
    """

    async def detected_language(self, session_id: int, user_id: int) -> str | None: ...

    async def set_detected_language(self, session_id: int, user_id: int, lang: str) -> None: ...

    async def mark_documented(self, session_id: int, doc_id: str, url: str) -> None: ...

    async def participant_names(self, session_id: int) -> dict[int, str]: ...

    async def audio_epoch(self, session_id: int, user_id: int) -> datetime | None: ...

    async def session_bounds(self, session_id: int) -> tuple[datetime, datetime]: ...


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
    jobs: JobReader,
    links: LinkReader,
    session_id: int,
    template_source: str,
) -> None:
    """Assembles, renders, and creates the document for a session's last job.

    Calls `sturnus.application.assembly.assemble` to merge every
    participant's stored transcript -- not just the job that happened to
    complete the session -- into one chronological `Transcript`, then
    renders it through `render_transcript`/`document_title`. Both are
    localised to UTC: no timezone configuration exists anywhere in this
    codebase yet (`sturnus.entrypoints.worker.WorkerSettings` has no such
    field), so this keeps the same UTC anchor the previous, single-speaker
    version of this function already used rather than inventing one.

    A permanently-rejected creation (an unretryable rejection from the
    document sink, e.g. a deleted collection) is swallowed here rather than
    re-raised: the transcription job this belongs to already completed
    successfully before this is ever called, so there is nothing left to
    fail or retry -- only the document never gets created, which is logged
    instead.
    """
    transcript = await assemble(session_id, sessions, jobs, links, UTC)

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
        log.warning("Document sink permanently rejected creation for session %d", session_id)
        return
    await sessions.mark_documented(session_id, created.id, created.url)


async def process_one(
    queue: Queue,
    engine: TranscriptionEngine,
    store: AudioDownloader,
    crypto: Decryptor,
    documents: DocumentSink,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkReader,
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
    6. If it was: assemble every participant's stored transcript into one
       document (`_create_session_document`, `sturnus.application.assembly.
       assemble`) and mark the session documented.
    7. Every temporary file made in steps 2-3 is removed in a `finally`, so
       a failure anywhere above never leaves decrypted speech on disk. The
       audio object in S3 is left alone deliberately -- see the module
       docstring.

    `jobs` and `links` are only ever read from in step 6, but are accepted
    as parameters up front (rather than constructed lazily) so every
    collaborator `process_one` needs is visible in its signature, matching
    `sessions`/`documents`/`queue` and the rest.
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
            await _create_session_document(
                documents, sessions, jobs, links, job.session_id, template_source
            )

        return True
    finally:
        # Runs whether processing succeeded, the transcription failed, or
        # something above raised outright: decrypted speech (and the
        # encrypted copy fetched to build it) must never survive this
        # function, regardless of how it exits.
        shutil.rmtree(job_dir, ignore_errors=True)
