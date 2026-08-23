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

**A session's share of the pool is bounded, and the bound is part of the
claim.** The unit of work is one job per speaker track, so a meeting is as
many jobs as it had people talking; they are independent and two workers
may take two of them. What that does not license is one meeting taking
*every* worker while another guild's meeting waits behind all of it, so a
candidate is rejected when its session already has as much work
outstanding ahead of it as its guild's `max_parallel_tracks` allows — in
the same statement that selects the row, under the same `FOR UPDATE SKIP
LOCKED`, because once a claim has returned the row is `running` and there
is no spelling for giving it back. `_outstanding_before` and
`_parallel_track_limit` are the two halves of that, and the first carries
the argument for why it is a *rank* rather than a count of running
siblings: only one of those two survives two workers claiming at the same
instant.

**The lease is a fencing token as well as a reclaim deadline.** Nothing
renews it while a job runs, so a job that outlives `lease_seconds` is
claimable by a second worker while the first is still transcribing it.
With one worker that was latent; with a pool it is reachable, and both
workers then arrive at `complete` for the same job — which, before this
was fenced, applied both completions, counted zero remaining jobs twice
and reported the session's last job twice, creating its protocol twice
from whichever transcript landed last. `complete` and `fail` therefore
take back the `claimed_at` their caller was handed and refuse to act on a
job that no longer carries it. See `complete`'s docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, Select, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from sturnus.domain import settings
from sturnus.domain.measurements import JobMeasurements, RecordedAudio
from sturnus.infrastructure.db.models import GuildConfig, Session, TranscriptionJob
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

#: The two statuses a job never comes back from. Everything else --
#: `pending`, `running` -- is a track its session still owes, and that is
#: the distinction `_outstanding_before` is built on.
TERMINAL_STATUSES: tuple[str, str] = ("done", "dead")

#: Matches a decimal integer of at least one. Deliberately not `\d`:
#: PostgreSQL's `\d` matches any Unicode digit, and `cast(... AS INTEGER)`
#: does not accept most of them.
_POSITIVE_INTEGER = "^[1-9][0-9]*$"


def _parallel_track_limit() -> ColumnElement[int]:
    """The candidate job's guild's `max_parallel_tracks`, resolved in SQL.

    **How a per-guild setting reaches a guild-agnostic query.** `claim`
    serves every guild from one statement and must not read configuration
    per call: a `ConfigStore.get` before the claim would be a second round
    trip resolving the wrong guild's value (the guild is not known until a
    candidate is in hand), and a value cached at worker start would be a
    setting that quietly stops applying. `guild_config` is an ordinary
    table, so the honest answer is to join to it: the value is resolved
    inside the same statement that selects the row, per candidate, from
    the row an administrator's last write left behind. No extra round
    trip, and nothing to go stale.

    **A value the database cannot read falls back to the default rather
    than raising.** `ConfigStore.set` refuses a non-integer, but
    `docs/operations.md` section 4.1 tells operators they may edit
    `guild_config` with SQL, and this expression runs inside *every*
    claim: a bare `cast(value AS INTEGER)` would turn one guild's typo
    into a worker that can no longer claim anything for anyone. The
    regular expression is what keeps the cast total.
    """
    stored = (
        select(GuildConfig.value)
        .select_from(GuildConfig)
        .join(Session, Session.guild_id == GuildConfig.guild_id)
        .where(
            Session.id == TranscriptionJob.session_id,
            GuildConfig.key == settings.MAX_PARALLEL_TRACKS,
        )
        .correlate(TranscriptionJob)
        .scalar_subquery()
    )
    return case(
        (stored.regexp_match(_POSITIVE_INTEGER), cast(stored, Integer)),
        else_=literal(settings.DEFAULT_MAX_PARALLEL_TRACKS),
    )


def _outstanding_before() -> ColumnElement[int]:
    """How many of this session's tracks are still owed and sort ahead of
    the candidate -- the candidate's rank among its own session's work.

    **Why a rank and not a count of `running` siblings.** Both express the
    cap, and only one of them survives two workers claiming at the same
    instant. Under PostgreSQL's READ COMMITTED isolation a statement sees
    one snapshot, and `FOR UPDATE` re-checks the search condition only for
    the row it actually locks -- never for other rows the condition
    mentions. A count of `running` siblings is therefore read from a
    snapshot that a rival claimer's just-committed `UPDATE` may not be in
    yet: both claimers count the same zero, both claim, and the cap is
    exceeded. That failure is not fixable by locking harder, because the
    stale read is of a *different* row than the one being locked.

    A rank is immune to exactly that, and for one reason: it is computed
    over `status NOT IN ('done', 'dead')`, a predicate a claim does not
    change. `pending` and `running` are both outstanding, so a concurrent
    claim moves no sibling into or out of the set being counted and cannot
    change any other candidate's rank. The one transition that does change
    it -- a sibling finishing -- only ever *shrinks* the set, so a claimer
    reading a stale snapshot ranks its candidate too high and claims less
    than it could. The error is always in the safe direction and is gone
    by the next poll.

    (`sturnus.infrastructure.db.requeue` moves finished jobs back to
    `pending`, which grows the set. It runs only against a `documented`
    session, whose every job is `done` and none of which is running, so it
    cannot race a claim of the same session.)
    """
    sibling = aliased(TranscriptionJob)
    return (
        select(func.count())
        .select_from(sibling)
        .where(
            sibling.session_id == TranscriptionJob.session_id,
            sibling.id < TranscriptionJob.id,
            sibling.status.not_in(TERMINAL_STATUSES),
        )
        .correlate(TranscriptionJob)
        .scalar_subquery()
    )


def claim_statement(lease_cutoff: datetime) -> Select[tuple[TranscriptionJob]]:
    """The one statement a claim runs, built apart from running it.

    Lifted out of `claim` for a single reason: the ordering below makes a
    claim about a *plan*, and a claim about a plan can only be settled by
    asking PostgreSQL for one. A test that hand-copied this query into an
    `EXPLAIN` would settle it about the copy, so the query
    `test_no_index_this_schema_has_can_order_a_claim_by_priority`
    explains is this one.

    **What the planner does with this, which is not what migration 0013
    predicted.** That migration added
    `ix_job_claim_order (status, priority, id)` and said the claim's
    `ORDER BY priority, id` would be one forward scan of it. It is not,
    and it cannot be. `status` leads that index and this statement matches
    *two* values of it -- a `pending` job and a `running` one whose lease
    expired -- so a scan of it yields the pending rows in `(priority, id)`
    order and then the running ones in `(priority, id)` order: two ordered
    runs, not one. PostgreSQL puts a `Sort` on top. Measured on PostgreSQL
    17, with sequential and bitmap scans disabled so that the ordered
    index scan was the only plan left: it still sorted. The claim is
    therefore no worse off than it was before this ordering existed -- it
    sorted by `id` for the same reason -- but the index is earning
    nothing, and nobody should read 0013's paragraph and believe
    otherwise.

    Two ways to earn it were considered and both rejected. Saying why here
    is the point of this paragraph, because both look obvious.

    `ORDER BY status, priority, id` does produce one forward scan with no
    sort -- measured, not assumed. It also silently means "no expired
    lease is ever reclaimed while any pending job is claimable", because
    `pending` sorts before `running`. That is the Defect 4 hazard put
    back: a job whose worker was killed would wait for the whole queue to
    drain before anybody picked it up, and its session would stay
    undocumented for exactly that long. A plan node is not worth that.

    A partial index -- `(priority, id) WHERE status IN ('pending',
    'running')` -- gives the ordering *and* keeps the semantics, because
    the status predicate moves into the index's `WHERE` and stops being a
    key column. It is the right answer, and it is a migration, which this
    branch deliberately is not. Whoever writes the next one should write
    it, and may drop `ix_job_claim_order` in the same breath.
    """
    return (
        select(TranscriptionJob)
        .where(
            or_(
                TranscriptionJob.status == "pending",
                and_(
                    TranscriptionJob.status == "running",
                    TranscriptionJob.claimed_at < lease_cutoff,
                ),
            ),
            _outstanding_before() < _parallel_track_limit(),
        )
        # **Lower first, then oldest first.** `priority` is what an
        # administrator said should happen sooner (see
        # `TranscriptionJob.priority` and `sturnus.application.priorities`);
        # `id` is the age it has always been ordered by, and it still
        # breaks every tie, so a queue nobody has touched claims in exactly
        # the first-in-first-out order it did before this clause grew a
        # column.
        #
        # Both ascending. That is what the lower-first convention was
        # chosen for and it stays right whatever the plan does with it:
        # `priority DESC, id ASC` would need an index with a descending
        # column before it could ever be scanned in order, so it forecloses
        # the partial index the docstring above recommends.
        .order_by(TranscriptionJob.priority, TranscriptionJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def _claim_is_current(job: TranscriptionJob, lease: datetime | None) -> bool:
    """Whether the caller may still act on this job.

    Two ways to have lost it, and a worker that outran its lease can hit
    either depending on how far the worker that took the job over has got:
    the job has since been finished or written off (`TERMINAL_STATUSES`),
    or it has been re-claimed and no longer carries the `claimed_at` this
    caller was handed.

    A caller that presents no lease is not a rival worker and is taken at
    its word -- see `complete`'s docstring for why the token is optional
    -- but even then a job already in a terminal state is never acted on
    twice.
    """
    if job.status in TERMINAL_STATUSES:
        return False
    return lease is None or job.claimed_at == lease


def _report_lost_claim(job_id: int, session_id: int, status: str) -> None:
    """One place to say that a worker's work has been thrown away.

    A warning rather than an error: nothing is lost that the worker
    holding the job will not produce again, and the recording is still
    going to be transcribed. It is worth a line all the same, because it
    is the only visible symptom of a lease that is too short for the
    material -- the job took longer than `STURNUS_JOB_LEASE_SECONDS` and
    a second worker started it over. Seeing this repeatedly means raising
    the lease, not raising the worker count.

    Counted as `stale`, never as `done`: a transcription really did run
    and really did produce nothing that will ever be read, and reporting
    it as a success is the exact failure `sturnus.job.outcome` was rebuilt
    to end.

    The counter and this line are where a lost claim shows up; the
    `job.process` span is not. Its `outcome` is stamped by
    `sturnus.infrastructure.traced.TracedQueue`, which sees only the
    boolean these methods return -- and it is a decorator around the
    enclosing span rather than this one, so this module cannot correct it
    from here without writing to the wrong span. Said out loud rather than
    left to be discovered: a trace of a lost claim reads `outcome="done"`.
    """
    log_event(
        log,
        logging.WARNING,
        Event.JOB_CLAIM_LOST,
        "A job was taken over by another worker while this one was still "
        "processing it; the work done here is discarded",
        job_id=job_id,
        session_id=session_id,
        status=status,
    )
    record(JOB_OUTCOME, 1, outcome="stale")


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    session_id: int
    discord_user_id: int
    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes
    #: The instant this claim was stamped on the row, and the worker's
    #: proof that the job is still its own. Presented back to `complete`
    #: and `fail` as `lease=`; a reclaim overwrites it, which is how those
    #: two recognise a worker whose job has been taken away from it.
    claimed_at: datetime
    #: The model this job was re-queued with, or `None` for the worker's
    #: own default -- which is every job nobody asked a question about.
    requested_model: str | None = None


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

        **The cap, and why it is in this statement.** A candidate is
        rejected when its session already owes more work ahead of it than
        its guild's `max_parallel_tracks` allows to run at once
        (`_outstanding_before`, `_parallel_track_limit`). It is part of the
        search condition rather than a check the caller makes afterwards:
        by the time a claim has returned, the row is `running`, and handing
        it back would be an unclaim that the lease logic has no spelling
        for. As a `WHERE` clause it costs nothing -- the scan simply walks
        past a session that is at its limit and takes the next session's
        work.

        **Lower priority first, then oldest first.** `ORDER BY priority,
        id` is the ordering guarantee this method makes; see
        `claim_statement`, which holds the clause and the argument for its
        directions. `id` is the age it has always sorted by: jobs are
        enqueued when a session closes, so ascending id is the order the
        meetings ended in, and a guild that keeps meeting gets ids that
        sort *behind* everything already waiting, which is what stops it
        from starving a quieter guild. `priority` in front of it is the
        only thing that can override that, it is written by nobody except
        `sturnus.infrastructure.db.priority` on an administrator's
        instruction, and it defaults to the same value for everybody -- so
        a deployment where nobody has ever asked for anything claims in
        exactly the order it did before.

        The cap and the ordering answer opposite halves of the same
        question: the cap stops one session monopolising the pool, the
        ordering stops the sessions it makes room for being served out of
        turn.
        """
        now = now if now is not None else datetime.now(UTC)
        lease_cutoff = now - timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            job = await session.scalar(claim_statement(lease_cutoff))
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
                claimed_at=now,
                requested_model=job.requested_model,
            )

    async def complete(
        self,
        job_id: int,
        transcript: str,
        measurements: JobMeasurements | None = None,
        *,
        lease: datetime | None = None,
        audio: RecordedAudio | None = None,
    ) -> bool:
        """Stores the transcript, marks the job done, and reports whether it
        was the session's last job.

        **`lease` is a fencing token, and without it this method is not
        safe to call from more than one worker.** Nothing renews a lease
        while a job runs, so a transcription that outlives
        `lease_seconds` leaves its job claimable by a second worker while
        the first is still decoding it -- and both then arrive here for the
        same `job_id`. Before this parameter existed both completions
        applied: each stored its own transcript over the other's, each
        counted zero jobs remaining, and each reported the session's last
        job, so the session's protocol was created twice from whichever
        transcript happened to land last. Passing back the `claimed_at`
        this job's claim returned is what tells the two apart -- a reclaim
        overwrites it, so the worker that lost the job presents a value
        the row no longer holds, and its completion is refused and counted
        as `stale` rather than as `done`. A job that is already `done` or
        `dead` is refused the same way, which is the same race in the other
        order.

        The token is optional because a caller that never claimed the job
        has nothing to present: a test completing a `pending` row is not a
        rival worker, and there is no claim for it to have lost. Every
        production caller goes through `sturnus.application.worker.
        process_one`, which always holds a `ClaimedJob` and always passes
        its `claimed_at`.

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

        `audio` is what the recording is as a file -- its sample rate, its
        channel count, the size of the stored object. It is written here
        rather than at enqueue because this is the one moment both files
        exist: the worker has the encrypted object and the plaintext WAV
        it decrypted out of it, and deletes both a few lines later. It is
        fenced by the same `lease` as everything else in this method, so a
        worker that has lost its job cannot stamp the row with a
        measurement nobody is waiting for.
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

            # Re-read *after* that lock, because the row this transaction
            # loaded a moment ago is exactly what a rival worker may have
            # been rewriting: the lock above covers this job too, so once
            # it is held nothing else can be mid-completion of it, and a
            # fresh read here is the last word on whose claim this is.
            await session.refresh(job)
            if not _claim_is_current(job, lease):
                # Read before the rollback, which expires every attribute
                # this object has and would send the report below back to
                # a database it no longer has a transaction on.
                status = job.status
                await session.rollback()
                _report_lost_claim(job_id, session_id, status)
                return False

            job.transcript = transcript
            job.status = "done"
            # Written in the same transaction as the transcript, because
            # they describe the same act of decoding: a row carrying one
            # without the other would be a job whose transcript cannot be
            # interpreted -- an empty one means "said nothing" or "nothing
            # decoded" depending entirely on these three numbers.
            #
            # Left null when absent rather than defaulted to zero. Null is
            # "never measured", zero is "measured, and it was nothing", and
            # only one of those is a claim about the recording. An engine
            # that cannot measure says so by passing nothing.
            if measurements is not None:
                job.audio_seconds = measurements.audio_seconds
                job.speech_seconds = measurements.speech_seconds
                job.segment_count = measurements.segment_count
                # What actually ran, which is not always what was asked
                # for: a job re-queued with no model runs on the worker's
                # default, and the three numbers above mean nothing
                # without knowing which.
                job.model = measurements.model
            # The same rule, one layer out: absent means nobody could
            # read the header, and null is how a row says so. Zero
            # channels at zero hertz would be a claim about a recording
            # that nothing ever looked at -- and `sturnus.console.
            # statistics` insists on that distinction for `audio_seconds`
            # already, for the same reason.
            if audio is not None:
                job.sample_rate = audio.sample_rate
                job.channels = audio.channels
                job.stored_bytes = audio.stored_bytes
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

    async def fail(
        self, job_id: int, error: str, max_attempts: int, *, lease: datetime | None = None
    ) -> bool:
        """Records the error and either returns the job to `pending` or, once
        `attempts` reaches `max_attempts`, marks it `dead`. Returns whether
        it is now dead.

        A `dead` job is excluded from `complete`'s remaining-jobs count, so
        one unreadable recording never blocks its session's completion.

        The return value exists because the caller otherwise cannot tell
        permanent loss from a retry -- `process_one` returns `True` for both
        -- and the distinction is the whole point of the outcome metric and
        of the `job.process` span's `outcome` attribute.

        `lease` fences this exactly as it fences `complete`, and for a
        worse failure. A worker whose lease expired mid-job, reporting a
        failure it hit afterwards, would otherwise pull the job out from
        under the worker that is decoding it right now -- back to
        `pending` for a third worker to pick up in parallel, or, if this
        was the last attempt, straight to `dead`: a recording written off
        while a healthy worker was busy producing its transcript. A
        refused failure changes nothing and reports "not dead", which is
        the truth about a job somebody else still holds.
        """
        async with self._session_factory() as session:
            # Locked rather than merely read, so that a `complete` for this
            # same job -- which holds a lock on every job of its session,
            # this one included -- cannot be halfway through when the check
            # below decides whose claim this is.
            job = await session.get(TranscriptionJob, job_id, with_for_update=True)
            assert job is not None, f"job {job_id} does not exist"
            if not _claim_is_current(job, lease):
                # Read before the rollback expires them; see `complete`.
                session_id, status = job.session_id, job.status
                await session.rollback()
                _report_lost_claim(job_id, session_id, status)
                return False
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
