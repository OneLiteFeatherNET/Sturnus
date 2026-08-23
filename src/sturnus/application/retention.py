"""Retention sweep selection and orchestration (Spec 12.2).

Audio outlives its transcription by `audio_retention_days` so a poor
transcription can be redone within that window. `expired_jobs` is the pure
selection at the heart of the periodic sweep: given jobs a caller already
read from the database, it returns the subset now due for deletion, without
touching S3 or the database itself.

`sweep_expired_audio` is the periodic sweep that actually calls it: it reads
candidates through `JobStore`, filters them with `expired_jobs`, then --
for each -- deletes the recording *and its stored spectrogram* through
`AudioDeleter` and stamps `audio_deleted_at` as the durable evidence that
the deletion happened. Both objects, in one pass, because a spectrogram
that outlived the recording it was drawn from would be a rendering of
somebody's voice activity surviving the retention window that voice was
subject to -- see `sweep_expired_audio` and
`sturnus.domain.settings.SPECTROGRAMS_BY_DEFAULT`. The
bucket lifecycle rule (Spec 12.2) is a second line of defence, never a
substitute for that record. `JobStore`/`AudioDeleter` are narrow local
`Protocol`s rather than concrete types, the same pattern
`sturnus.application.worker` uses for its own collaborators -- this module
lives in `sturnus.application`, which must never import
`sturnus.infrastructure` (tests/test_architecture.py); the concrete
adapters (`sturnus.infrastructure.db.repositories.JobRepository`, the S3
store) are wired in by `sturnus.entrypoints.worker`.

`expired_jobs` itself stays a pure function over plain data, exactly as
before, so it is testable without a database.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol, cast

from sturnus.observability.events import Event, log_exception

log = logging.getLogger(__name__)


def expired_jobs(jobs: list[dict[str, object]], now: datetime) -> list[dict[str, object]]:
    """Selects jobs whose retention has passed and whose audio is not yet deleted.

    The boundary is exclusive: a job whose `retention_until` is exactly
    `now` has not expired yet, only once time strictly passes it. A job
    already marked `audio_deleted_at` is excluded regardless of how far
    past its retention it now is -- re-running the sweep after a partial
    failure must not report work it already did as new work.
    """
    selected: list[dict[str, object]] = []
    for candidate in jobs:
        retention_until = cast(datetime, candidate["retention_until"])
        audio_deleted_at = cast("datetime | None", candidate["audio_deleted_at"])
        if retention_until < now and audio_deleted_at is None:
            selected.append(candidate)
    return selected


class JobStore(Protocol):
    """Where retention candidates are read from and `audio_deleted_at` is stamped."""

    async def candidates_for_retention(self) -> list[dict[str, object]]:
        """Every job not yet marked `audio_deleted_at`, shaped for `expired_jobs`.

        Deliberately unfiltered by `retention_until`: that boundary check
        is `expired_jobs`'s job alone, so there is exactly one definition
        of it anywhere in the codebase, the same reasoning
        `sturnus.application.publishing.sessions_to_announce`'s caller
        follows for `status`/`announced_at`/`document_url`.

        Every candidate carries `spectrogram_key` -- `None` for the jobs
        that have no stored picture, which is most of them and all of them
        for a guild that never switched `spectrograms_by_default` on. It
        is selected rather than derived from the job's ids because the
        naming rule may change and the objects already in the bucket
        cannot: this sweep must delete what was written.
        """
        ...

    async def mark_audio_deleted(self, job_id: int, now: datetime) -> None:
        """Stamps the deletion, and forgets where the picture was.

        `spectrogram_key` is cleared in the same statement. The column
        says where this job's artefact is, and once the sweep has deleted
        it there is no artefact for it to point at; leaving the key behind
        would make every later sweep re-delete an object that is already
        gone and would leave the row claiming a picture exists.
        """
        ...


class AudioDeleter(Protocol):
    """Where an expired recording's objects are actually removed.

    One method for both, because both are objects in the same bucket
    under the same credentials, and a port with a second method for the
    artefact would invite a caller to delete one kind and not the other.
    """

    async def delete(self, key: str) -> None: ...


async def sweep_expired_audio(jobs: JobStore, store: AudioDeleter, now: datetime) -> None:
    """Deletes every expired job's audio, and its spectrogram, and stamps the row.

    **A stored spectrogram is deleted when its audio is deleted, in the
    same pass.** That rule is the whole reason storing one is defensible.
    A spectrogram is a rendering of when somebody spoke and for how long;
    it is less than the audio and it is not nothing, and this sweep is the
    only thing in the system that ends a recording's life. If it deleted
    the object and left the picture, `spectrograms_by_default` would be a
    switch that quietly makes something about a person's voice outlive the
    retention window that person's recording was subject to -- which is
    what the window is for. So the picture goes with it, here, rather than
    in a second sweep that could be forgotten, disabled, or fail on its
    own.

    Survives its own errors per job: a failure deleting one job's audio
    (or its picture, or stamping it afterwards) is logged and does not
    stop the sweep from handling the rest -- one unreachable object must
    not block every other job's retention from being enforced.

    `audio_deleted_at` is stamped only after both deletions actually
    succeed, so a failed deletion is retried on the next sweep instead of
    being silently recorded as done. The reverse order (stamp then delete)
    would risk exactly the outcome `audio_deleted_at` exists to rule out --
    a stamp claiming deletion happened when it did not. The cost of the
    chosen order is a delete that succeeds but whose stamp then fails
    being retried once more next sweep; `store.delete` on an
    already-missing key is idempotent (an S3 `DELETE` on a missing object
    still succeeds), so that retry costs nothing.

    The audio goes first and the picture second, which matters only in the
    one case where the sweep is interrupted between them: what is left
    behind is then a picture whose audio is gone, and a row still asking
    to be swept. The console refuses a track whose object has been erased
    before it looks for a picture at all, so that interval is invisible
    from outside and ends on the next sweep.
    """
    for job in expired_jobs(await jobs.candidates_for_retention(), now):
        job_id = cast(int, job["id"])
        try:
            await store.delete(cast(str, job["s3_key"]))
            picture = cast("str | None", job["spectrogram_key"])
            if picture is not None:
                await store.delete(picture)
            await jobs.mark_audio_deleted(job_id, now)
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.RETENTION_FAILED,
                "Failed to delete expired audio; will retry next sweep",
                exc,
                job_id=job_id,
            )
