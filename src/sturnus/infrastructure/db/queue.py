"""The transcription job queue.

The correctness of this module lives in one rule: `complete` marks a job
done and counts the session's jobs that are neither `done` nor `dead` —
that count is what decides whether this was the session's last job.

Doing both inside one transaction is *not*, on its own, enough to make
this safe. Under PostgreSQL's default READ COMMITTED isolation, a
transaction does not see another transaction's UPDATE until that other
transaction commits — "it's all one transaction" only protects each
`complete()` call's own view of the world, it says nothing about what two
concurrent calls see of each other. Without an explicit lock, two jobs of
the same session finishing at the same time each count the other's job as
still outstanding, so neither ever sees zero and the session's document
is never created. This was reproduced directly: driving two `complete()`
calls concurrently on the two jobs of one session, 49 of 50 runs returned
`(False, False)`. `complete` therefore takes a row lock
(`SELECT ... FOR UPDATE`) on the session's sibling jobs before
recomputing the count, forcing the second caller to wait for the first to
commit and then recompute against the already-applied update. See
`complete`'s docstring for the details.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.infrastructure.db.models import TranscriptionJob


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    session_id: int
    discord_user_id: int
    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes


class JobQueue:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(self) -> ClaimedJob | None:
        """Claims one pending job for a worker.

        Selection, status update, and commit happen in one transaction with
        `FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the same
        job.
        """
        async with self._session_factory() as session:
            job = await session.scalar(
                select(TranscriptionJob)
                .where(TranscriptionJob.status == "pending")
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = "running"
            await session.commit()
            return ClaimedJob(
                id=job.id,
                session_id=job.session_id,
                discord_user_id=job.discord_user_id,
                s3_key=job.s3_key,
                encryption_key_id=job.encryption_key_id,
                wrapped_data_key=job.wrapped_data_key,
            )

    async def complete(self, job_id: int, transcript: str) -> bool:
        """Stores the transcript, marks the job done, and reports whether it
        was the session's last job.

        Being in one transaction does *not*, by itself, make the
        remaining-jobs count safe: under READ COMMITTED, this transaction
        would simply not see a sibling job's `done` update until that
        sibling's transaction commits, so two jobs of the same session
        completing at the same time could each count the other as still
        outstanding and neither would ever report the session complete.

        To close that gap we take a `SELECT ... FOR UPDATE` lock on every
        job belonging to the session *before* updating anything, ordered
        by id. That lock is what actually serialises concurrent
        completions of the same session: the second caller blocks on it
        until the first commits, and then re-reads the count against the
        already-applied update rather than a stale snapshot. The `ORDER
        BY id` keeps lock-acquisition order identical across transactions
        so that concurrent completions of *different* sessions can never
        deadlock against each other.
        """
        async with self._session_factory() as session:
            job = await session.get(TranscriptionJob, job_id)
            assert job is not None, f"job {job_id} does not exist"
            session_id = job.session_id

            # Lock the session's jobs (this one included) before touching
            # anything. A concurrent `complete()` for a sibling job of the
            # same session will block on this exact statement until this
            # transaction commits, instead of computing its count from a
            # snapshot that predates our update.
            await session.execute(
                select(TranscriptionJob.id)
                .where(TranscriptionJob.session_id == session_id)
                .order_by(TranscriptionJob.id)
                .with_for_update()
            )

            job.transcript = transcript
            job.status = "done"
            await session.flush()
            remaining = await session.scalar(
                select(func.count())
                .select_from(TranscriptionJob)
                .where(
                    TranscriptionJob.session_id == session_id,
                    TranscriptionJob.status.not_in(("done", "dead")),
                )
            )
            await session.commit()
            return remaining == 0

    async def fail(self, job_id: int, error: str, max_attempts: int) -> None:
        """Records the error and either returns the job to `pending` or, once
        `attempts` reaches `max_attempts`, marks it `dead`.

        A `dead` job is excluded from `complete`'s remaining-jobs count, so
        one unreadable recording never blocks its session's completion.
        """
        async with self._session_factory() as session:
            job = await session.get(TranscriptionJob, job_id)
            assert job is not None, f"job {job_id} does not exist"
            job.attempts += 1
            job.error = error
            job.status = "dead" if job.attempts >= max_attempts else "pending"
            await session.commit()

    async def last_error(self, job_id: int) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(TranscriptionJob.error).where(TranscriptionJob.id == job_id)
            )
