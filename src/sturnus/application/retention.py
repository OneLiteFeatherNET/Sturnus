"""Retention sweep selection (Spec 12.2).

Audio outlives its transcription by `audio_retention_days` so a poor
transcription can be redone within that window. `expired_jobs` is the pure
selection at the heart of the periodic sweep: given jobs a caller already
read from the database, it returns the subset now due for deletion, without
touching S3 or the database itself. That I/O -- deleting the object,
stamping `audio_deleted_at` as the durable evidence that the deletion
happened -- belongs to the infrastructure adapter that calls this function;
the bucket lifecycle rule (Spec 12.2) is a second line of defence, never a
substitute for that record.

Kept a pure function over plain data so it is testable without a database.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast


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
