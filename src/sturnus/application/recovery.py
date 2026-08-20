"""Recovery of recordings a crash left behind (Spec 6.4).

A session is unsplittable: the bot writes to a volume for the whole
session and only encrypts, uploads and enqueues once it closes. A hard
kill (`SIGKILL`, an evicted pod) therefore leaves a complete recording on
disk that nothing has finished handing off. Losing hours of audio because
the process merely restarted would be the worst failure this system has,
so every start scans the recording directory for what a previous process
left behind and finishes the job before the client ever connects.

`find_orphans` is pure and read-only -- no file is touched -- so it can be
tested exhaustively without a filesystem full of side effects. The actual
recovery routine, `recover_orphans`, reuses `RecordingService.close` for
the plaintext-`.wav` case instead of re-implementing encrypt-upload-enqueue
as a parallel copy, which would only drift from the real thing the next
time `close` changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sturnus.application.ports import AudioStore, Encryptor
from sturnus.application.recording import JobQueue, SessionRecorder, audio_key
from sturnus.application.recording import RecordingService as _RecordingService
from sturnus.domain.session import EndReason, SessionTimeouts

log = logging.getLogger(__name__)

_SESSION_DIR_RE = re.compile(r"^session-(\d+)$")

# Recovery only ever runs because the previous process ended without going
# through `SessionMachine` at all -- a hard kill, an evicted pod -- so
# every session it closes ends with this honest reason rather than one of
# the machine's own timeout reasons, none of which actually fired.
RECOVERY_END_REASON = EndReason.CRASHED


@dataclass(frozen=True)
class OrphanRecording:
    """One leftover recording found on disk after a restart.

    `path` points at whichever file was actually found -- the plaintext
    `.wav` if encryption never ran, or the `.enc` if encryption finished
    but the upload did not.
    """

    session_id: int
    discord_user_id: int
    path: Path
    encrypted: bool


def find_orphans(root: Path) -> list[OrphanRecording]:
    """Scans `<root>/session-<id>/<discord_user_id>.wav|.enc` for leftovers.

    Anything that doesn't match the layout -- a stray file directly under
    `root`, a directory not named `session-<digits>`, a file whose stem
    isn't a plain integer -- is silently ignored rather than reported as an
    orphan; only the exact layout `FileAudioWriterFactory` and the
    encryptor produce is ever written here.

    When both a `.wav` and a `.enc` exist for the same speaker -- possible
    if a crash landed exactly between encrypting and deleting the
    plaintext -- the `.enc` wins: encryption already completed, so only the
    upload is still owed.
    """
    orphans: dict[tuple[int, int], OrphanRecording] = {}
    if not root.is_dir():
        return []

    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        match = _SESSION_DIR_RE.match(session_dir.name)
        if match is None:
            continue
        session_id = int(match.group(1))

        for candidate in sorted(session_dir.iterdir()):
            if not candidate.is_file():
                continue
            if candidate.suffix not in (".wav", ".enc"):
                continue
            if not candidate.stem.isdigit():
                continue
            discord_user_id = int(candidate.stem)
            encrypted = candidate.suffix == ".enc"

            key = (session_id, discord_user_id)
            existing = orphans.get(key)
            if existing is not None and existing.encrypted:
                continue  # a `.enc` was already recorded; a stale `.wav` loses
            orphans[key] = OrphanRecording(session_id, discord_user_id, candidate, encrypted)

    return list(orphans.values())


class _AlreadyWrittenFile:
    """Adapts a completed, on-disk recording to the `AudioWriter` port.

    The file is whole -- it was finished by a process that then crashed or
    was killed before it could hand the file off -- so recovery never
    appends to it. `write` therefore must never be called; `close` has
    nothing left to flush.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, _at: datetime, _pcm: bytes) -> None:
        raise RuntimeError("recovery does not append to an already-complete recording")

    def close(self) -> None:
        pass


class _UnusedWriterFactory:
    """Satisfies `RecordingService`'s constructor without ever opening a writer.

    Recovery pre-populates the service's writers from files already on
    disk (`_AlreadyWrittenFile`); if this factory is ever asked to open a
    new one, something has gone wrong in the wiring above.
    """

    def open(self, session_id: int, discord_user_id: int, epoch: datetime) -> _AlreadyWrittenFile:
        raise RuntimeError(
            f"recovery must not open a new writer (session {session_id}, "
            f"speaker {discord_user_id}, epoch {epoch})"
        )


def _service_for_recovery(
    sessions: SessionRecorder,
    jobs: JobQueue,
    store: AudioStore,
    encryptor: Encryptor,
    retention_days: int,
) -> _RecordingService:
    """Builds a `RecordingService` whose `close` recovery can call directly.

    `guild_id`, `channel_id` and the timeouts are irrelevant here -- they
    only matter for `open_session` and `tick`, and recovery calls neither.
    """
    return _RecordingService(
        guild_id=0,
        channel_id=0,
        timeouts=SessionTimeouts(),
        sessions=sessions,
        jobs=jobs,
        store=store,
        writers=_UnusedWriterFactory(),
        encryptor=encryptor,
        retention_days=retention_days,
    )


async def recover_orphans(
    root: Path,
    sessions: SessionRecorder,
    jobs: JobQueue,
    store: AudioStore,
    encryptor: Encryptor,
    retention_days: int,
    now: datetime,
) -> list[OrphanRecording]:
    """Finishes every recording a previous process left unfinished.

    Grouped by session, because a session's speakers share one data key --
    but the two kinds of orphan need that key from different places.

    A plain `.wav` was never encrypted, so a freshly generated key is not
    just acceptable but the only option: the key that would have encrypted
    it only ever lived in the crashed process's memory and was never
    persisted anywhere recovery can reach. This reuses
    `RecordingService.close` for the encrypt-upload-enqueue sequence,
    exactly as an orderly shutdown would. `close` does not delete the
    `.enc` it produces, so recovery removes it afterwards -- otherwise the
    same file would be rediscovered as an orphan on the next restart and
    enqueued a second time.

    Belt and braces against `close()`'s own cleanup: a session whose row
    is already `closed` (or further along, `documented`) has, by
    definition, already been through `close()` to completion -- every
    speaker it had was uploaded and enqueued before the row was ever
    marked closed. A `.enc` still on disk for such a session can only mean
    a crash landed between that upload/enqueue and `close()`'s own local
    cleanup, never that the work is still outstanding. Reprocessing it
    would enqueue the same `(session_id, discord_user_id)` job a second
    time and hit `uq_job_per_speaker`'s `IntegrityError` -- so recovery
    skips it, logs it, and just finishes the interrupted cleanup instead.

    A `.enc` was already encrypted with the session's *original* key, and
    only that key can ever decrypt it -- a freshly generated one is
    guaranteed to fail. That key is read back from the `session` row
    (`SessionRepository.session_key`), the source of truth written once
    when the session opened. When a session has no stored key -- one that
    predates this column, or one that crashed before the row was ever
    written -- its `.enc` orphans are skipped and logged rather than
    enqueued as jobs that could never succeed: a loud skip is honest,
    where a doomed job would hide the loss behind work that looks like
    progress.
    """
    orphans = find_orphans(root)
    if not orphans:
        return []

    by_session: dict[int, list[OrphanRecording]] = {}
    for orphan in orphans:
        by_session.setdefault(orphan.session_id, []).append(orphan)

    for session_id, group in by_session.items():
        status = await sessions.session_status(session_id)
        if status is not None and status != "open":
            # The row already closed (or was fully documented), so every
            # speaker it had was already uploaded and enqueued before that
            # happened -- these are stale local copies of work already
            # done, not work still owed. See `recover_orphans`'s docstring.
            log.warning(
                "Session %d's row is already %s; removing %d leftover file(s) on disk "
                "instead of reprocessing them, which would duplicate already-enqueued jobs",
                session_id,
                status,
                len(group),
            )
            for orphan in group:
                orphan.path.unlink(missing_ok=True)
            continue

        log.warning("Recovering %d orphaned recording(s) for session %d", len(group), session_id)
        stored_key = await sessions.session_key(session_id)

        plain = [o for o in group if not o.encrypted]
        already_encrypted = [o for o in group if o.encrypted]

        if plain:
            service = _service_for_recovery(sessions, jobs, store, encryptor, retention_days)
            # Accessing RecordingService's private state directly is the
            # point: its public API can only ever start a *new* session,
            # never resume an existing one, and duplicating its close()
            # logic on top of that is exactly the drift this function
            # exists to avoid.
            service._session_id = session_id  # noqa: SLF001
            service._data_key = encryptor.new_session_key()  # noqa: SLF001
            service._writers = {  # noqa: SLF001
                o.discord_user_id: _AlreadyWrittenFile(o.path) for o in plain
            }
            await service.close(RECOVERY_END_REASON, now)
            for o in plain:
                enc_path = o.path.with_suffix(".enc")
                enc_path.unlink(missing_ok=True)
        else:
            # No plaintext left to close through -- but the session row
            # still needs closing, since `close` is the only thing that
            # does it and it was never reached in this branch.
            await sessions.close_session(session_id, now, RECOVERY_END_REASON.value)

        if stored_key is None:
            for o in already_encrypted:
                log.warning(
                    "Cannot recover session %d speaker %d: session has no stored data "
                    "key, so the file at %s cannot be decrypted -- skipping instead of "
                    "enqueuing a job that could never succeed",
                    session_id,
                    o.discord_user_id,
                    o.path,
                )
            continue

        encryption_key_id, wrapped_data_key = stored_key
        for o in already_encrypted:
            key = audio_key(session_id, o.discord_user_id)
            await store.put(key, o.path)
            await jobs.enqueue(
                session_id=session_id,
                discord_user_id=o.discord_user_id,
                s3_key=key,
                encryption_key_id=encryption_key_id,
                wrapped_data_key=wrapped_data_key,
                retention_until=now + timedelta(days=retention_days),
            )
            o.path.unlink(missing_ok=True)

    return orphans
