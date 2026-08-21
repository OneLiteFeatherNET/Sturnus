"""Retention sweep selection and orchestration (Spec 12.2).

Audio outlives its transcription by `audio_retention_days` so a poor
transcription can be redone within that window. `expired_jobs` is the pure
selection at the heart of the periodic sweep: given jobs a caller already
read from the database, it returns the subset now due for deletion, without
touching S3 or the database itself.

`sweep_expired_audio` is the periodic sweep that actually calls it: it reads
candidates through `JobStore`, filters them with `expired_jobs`, then --
for each -- deletes the object through `AudioDeleter` and stamps
`audio_deleted_at` as the durable evidence that the deletion happened. The
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
        """
        ...

    async def mark_audio_deleted(self, job_id: int, now: datetime) -> None: ...


class AudioDeleter(Protocol):
    """Where an expired recording's object is actually removed."""

    async def delete(self, key: str) -> None: ...


async def sweep_expired_audio(jobs: JobStore, store: AudioDeleter, now: datetime) -> None:
    """Deletes every expired job's audio object and stamps `audio_deleted_at`.

    Survives its own errors per job: a failure deleting one job's audio
    (or stamping it afterwards) is logged and does not stop the sweep from
    handling the rest -- one unreachable object must not block every other
    job's retention from being enforced.

    `audio_deleted_at` is stamped only after `store.delete` actually
    succeeds, so a failed deletion is retried on the next sweep instead of
    being silently recorded as done. The reverse order (stamp then delete)
    would risk exactly the outcome `audio_deleted_at` exists to rule out --
    a stamp claiming deletion happened when it did not. The cost of the
    chosen order is a delete that succeeds but whose stamp then fails
    being retried once more next sweep; `store.delete` on an
    already-missing key is idempotent (an S3 `DELETE` on a missing object
    still succeeds), so that retry costs nothing.
    """
    for job in expired_jobs(await jobs.candidates_for_retention(), now):
        job_id = cast(int, job["id"])
        try:
            await store.delete(cast(str, job["s3_key"]))
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
