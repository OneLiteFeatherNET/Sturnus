"""The transcription job queue.

The correctness of this module lives in two rules.

**`complete` marks a job done and reports whether it was the session's
last job.** "Last" requires two things together: every job of the session
is now `done` or `dead` (its own remaining-jobs count), *and* the session
itself is `closed`. The count alone is not enough -- see Defect 5 below.

Doing both inside one transaction is *not*, on its own, enough to make
the count safe. Under PostgreSQL's default READ COMMITTED isolation, a
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

**Defect 5: the remaining-jobs count alone can go to zero too early.**
`sturnus.application.recording.RecordingService.close` uploads and
enqueues one speaker at a time, and each `enqueue` commits on its own —
so a worker can claim and complete a session's *first* speaker's job
before a second speaker has even been enqueued yet, let alone before the
session itself closes. At that instant the remaining-jobs count is
genuinely zero (only one job exists), so without the session-closed check
`complete` would report the session done from one speaker alone — the
same failure the assembly fix (`sturnus.application.assembly.assemble`)
was meant to end, one level up. Requiring `session.status == "closed"` as
well closes that window: `close_session` only ever runs after every
speaker has already been uploaded and enqueued, so this can never fire
before that has actually happened. It is a strict extra condition, never
a substitute for the remaining-jobs count — a `closed` session can still
have jobs pending. The residual case this cannot catch — every job
happens to complete a moment *before* `close_session` commits, so no
`complete()` call ever runs again afterwards to notice the session is now
closed — is what `sturnus.application.worker.retry_pending_documents`
exists to sweep up after the fact.

**Defect 4: `claim` reclaims a `running` job whose lease has expired.** A worker
killed mid-job (SIGKILL, an evicted pod) leaves its claimed job `running`
forever otherwise: `claim` only ever selected `pending` jobs, and nothing
else ever moves a job out of `running`, so it is never in a terminal
state and `complete`'s remaining-jobs count never reaches zero for that
session either. `claim` therefore also selects a `running` job whose
`claimed_at` is older than `lease_seconds`, exactly as if it were
`pending` — see `claim`'s docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.infrastructure.db.models import Session, TranscriptionJob
from sturnus.infrastructure.telemetry import JOB_OUTCOME, record
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: How long a claimed job may stay `running` before `claim` treats it as
#: abandoned and reclaims it. Generous on purpose: a large-v3 Whisper model
#: transcribing a long recording on CPU (Spec 7 sizes the deployment for
#: CPU, not GPU) can legitimately run for many minutes, and reclaiming a
#: job that is still genuinely being worked on would have two workers
#: processing the same recording at once.
DEFAULT_LEASE_SECONDS = 1800.0


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    session_id: int
    discord_user_id: int
    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes


class JobQueue:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds

    async def claim(self, now: datetime | None = None) -> ClaimedJob | None:
        """Claims one pending (or lease-expired running) job for a worker.

        `now` defaults to the wall clock; a test passes it explicitly to
        make lease expiry deterministic. Selection, status update, and
        commit happen in one transaction with `FOR UPDATE SKIP LOCKED`, so
        concurrent workers never claim the same job.

        A `running` job whose `claimed_at` is older than `lease_seconds`
        is selected exactly as if it were `pending` -- see the module
        docstring's "Defect 4" note on why this exists. `claimed_at` is
        then stamped with `now` regardless of which branch matched, so a
        reclaim starts a fresh lease rather than an immediately-expired one.
        """
        now = now if now is not None else datetime.now(UTC)
        lease_cutoff = now - timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            job = await session.scalar(
                select(TranscriptionJob)
                .where(
                    or_(
                        TranscriptionJob.status == "pending",
                        and_(
                            TranscriptionJob.status == "running",
                            TranscriptionJob.claimed_at < lease_cutoff,
                        ),
                    )
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = "running"
            job.claimed_at = now
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

        The remaining-jobs count reaching zero is still not sufficient on
        its own: the session must also be `closed` (see the module
        docstring's "Defect 5" note), or a worker completing a session's
        first speaker before a second speaker has even been enqueued yet
        would see zero remaining jobs -- because no sibling exists yet --
        and wrongly report the session done from one speaker's job alone.
        This last read needs no extra lock: under READ COMMITTED it can
        only ever see `status == "closed"` once `close_session`'s own
        transaction has actually committed, so a stale read here is always
        the safe direction (a false "not yet last", never a false "last").
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
            session_status = await session.scalar(
                select(Session.status).where(Session.id == session_id)
            )
            await session.commit()

        # **Where `sturnus.job.outcome` is counted, and the reason it is
        # counted here.** The worker loop used to derive the label from
        # `process_one`'s return value, which is `True` after `queue.fail`
        # just as it is after `queue.complete` -- it means "work was
        # attempted", not "work succeeded" -- so every failed job was
        # published as `outcome="done"`. A metric that reports failures as
        # successes is worse than no metric, because it is believed.
        #
        # This method and `fail` below are the two transitions that decide
        # a job's terminal state, so they are the two places that can say
        # what happened without inferring it. Recorded after the commit:
        # the counter must not claim a `done` that a failed transaction
        # rolled back.
        record(JOB_OUTCOME, 1, outcome="done")
        return remaining == 0 and session_status == "closed"

    async def fail(self, job_id: int, error: str, max_attempts: int) -> bool:
        """Records the error and either returns the job to `pending` or, once
        `attempts` reaches `max_attempts`, marks it `dead`. Returns whether
        it is now dead.

        A `dead` job is excluded from `complete`'s remaining-jobs count, so
        one unreadable recording never blocks its session's completion.

        The return value exists because the caller otherwise cannot tell
        permanent loss from a retry -- `process_one` returns `True` for both
        -- and the distinction is the whole point of the outcome metric and
        of the `job.process` span's `outcome` attribute.
        """
        async with self._session_factory() as session:
            job = await session.get(TranscriptionJob, job_id)
            assert job is not None, f"job {job_id} does not exist"
            job.attempts += 1
            job.error = error
            job.status = "dead" if job.attempts >= max_attempts else "pending"
            session_id = job.session_id
            attempts = job.attempts
            dead = job.status == "dead"
            await session.commit()

        if dead:
            # A speaker's audio will never be transcribed. This method has
            # set `status="dead"` since it was written and said nothing at
            # all about it -- permanent loss, expressed as silence.
            #
            # `error` is **not** logged: it is `str(exc)` from
            # `process_one`, which is fine in the database column an
            # operator queries deliberately and is exactly what must not go
            # into a retained, indexed store. The column still has it.
            log_event(
                log,
                logging.ERROR,
                Event.JOB_DEAD,
                "Job exhausted its attempts and is now dead; this recording will never "
                "be transcribed",
                job_id=job_id,
                session_id=session_id,
                attempts=attempts,
                max_attempts=max_attempts,
            )
            record(JOB_OUTCOME, 1, outcome="dead")
        else:
            log_event(
                log,
                logging.WARNING,
                Event.JOB_FAILED,
                "Job returned to the queue for another attempt",
                job_id=job_id,
                session_id=session_id,
                attempt=attempts,
                max_attempts=max_attempts,
            )
            # The measurement that was missing entirely. `dead` was counted
            # from the moment this metric existed; a retryable failure was
            # not counted at all, and the worker loop then counted the same
            # job as `done`. So the two labels an operator most needs --
            # "is this job pipeline failing" and "is it failing
            # permanently" -- were one lie and one silence.
            record(JOB_OUTCOME, 1, outcome="failed")

        return dead

    async def last_error(self, job_id: int) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(TranscriptionJob.error).where(TranscriptionJob.id == job_id)
            )
