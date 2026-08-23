"""Reading a session's jobs, and resetting them, in one place.

Extracted from `sturnus.infrastructure.discord.queue_cog`, which was its
only caller until the console grew a re-queue button. The alternative was
a second implementation of the write, and the write is the one part of
this feature that must not have two: it takes a row lock in a specific
order, spans two tables in one transaction, and every one of those
decisions exists because getting it wrong strands a session with no
document and no error anywhere. Two copies of that would agree today and
diverge on the first change to either.

What stays behind in `queue_cog` is the rendering -- Discord's message
limit, its embeds, its confirmation view. What moved here is everything
that touches the database, so both callers ask the same questions and
perform the same write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.publishing import DOCUMENTED_STATUS
from sturnus.application.requeue import TERMINAL_STATUSES, RequeuePlan, plan_requeue
from sturnus.infrastructure.db.models import Session, SessionParticipant, TranscriptionJob
from sturnus.infrastructure.db.priority import OUTSTANDING_STATUSES

#: Job statuses reported by a queue summary, in lifecycle order rather
#: than alphabetically: a reader looks at this to see where work is piling
#: up, and `pending -> running -> done | dead` is the order that makes
#: that legible.
REPORTED_STATUSES = ("pending", "running", "done", "dead")


@dataclass(frozen=True)
class SessionSummary:
    """The session-level facts every `/queue` reply is rendered from.

    `channel_id` rather than the stored `channel_name`: `<#id>` renders as
    the channel's *current* name in Discord and stays a working link if it
    was renamed since, whereas `session.channel_name` is deliberately
    frozen at the moment the session opened so old protocols do not get
    rewritten by a later rename.
    """

    id: int
    channel_id: int
    status: str
    ended_at: datetime | None
    end_reason: str | None
    document_url: str | None
    announced_at: datetime | None


@dataclass(frozen=True)
class SessionView:
    """One session, its re-queue plan, and the names to render it with.

    Read and returned as a unit so the reply cannot be built from a
    session row read at one moment and job rows read at another.
    """

    summary: SessionSummary
    plan: RequeuePlan
    #: `discord_user_id` -> display name, from `session_participant`.
    names: dict[int, str]

    @property
    def is_settled(self) -> bool:
        """Whether the pipeline has finished with this session and let go of it.

        `documented` is the only status from which a re-queue is safe, and
        each of the other two is unsafe for its own reason.

        An `open` session is the window `JobQueue.complete`'s "Defect 5"
        guard exists to refuse. `RecordingService.close` uploads and
        enqueues one speaker at a time, every `enqueue` committing on its
        own, and calls `close_session` only after the last upload -- so a
        long multi-speaker session spends a long time `open` with early
        speakers already enqueued (and possibly already `done`) while
        later ones do not exist as rows yet. `apply_requeue` writes
        `status="closed"` unconditionally, so re-queueing in that window
        hands `complete` exactly the state its guard is there to prevent:
        no outstanding jobs plus a `closed` session, therefore "this was
        the session's last job", therefore a document assembled from the
        speakers that happened to exist at that moment. That is this
        command causing the failure it exists to repair.

        A `closed` session is still owned by
        `sturnus.application.worker.retry_pending_documents`, which
        documents any closed session whose jobs are all terminal and may
        be between its read and its `mark_documented` write right now.
        Re-queueing into that sweep gives it a session whose transcripts
        this command has just cleared: it publishes that near-empty
        document and flips the session to `documented`, after which
        `complete`'s last-job rule (which requires `closed`) never fires
        again and the sweep never selects it again either -- the redo runs
        to completion and no document is ever made from it.

        Waiting costs nothing: a session that is genuinely finished
        reaches `documented` on its own, and `/queue session` shows when.
        """
        return self.summary.status == DOCUMENTED_STATUS

    @property
    def is_refused(self) -> bool:
        """Whether this session must be refused instead of re-queued.

        The three reasons in the order `render_requeue_refusal` reports
        them, which is the order of what an administrator can do about it:
        a blocked session only needs the queue to go idle, an unsettled
        one needs the pipeline to finish, and an empty one will never
        change.
        """
        return self.plan.is_blocked or not self.is_settled or self.plan.is_empty


@dataclass(frozen=True)
class JobLine:
    """One speaker's row in the `/queue session` readout."""

    discord_user_id: int
    status: str
    attempts: int
    audio_present: bool
    error: str | None
    #: The *length* of the stored transcript, never the transcript. This
    #: reply has to be enough to decide whether a re-queue is warranted --
    #: a 100-minute recording whose transcript is 24 characters is the
    #: tell -- without a slash command becoming a way to read meeting
    #: content out of the document system.
    transcript_length: int | None


@dataclass(frozen=True)
class QueueStatus:
    """The guild-wide counts behind `/queue status`."""

    counts: dict[str, int]
    running_past_lease: int
    #: When the session owning the oldest `pending` job ended. See
    #: `load_status` for why that is the closest thing to an enqueue time
    #: this schema has.
    oldest_pending_session_ended_at: datetime | None
    closed_undocumented: int


# ---------------------------------------------------------------------------
# Reads. Plain ORM queries, all joined to `session` and filtered by guild.
# ---------------------------------------------------------------------------


def _summary_of(row: Session) -> SessionSummary:
    return SessionSummary(
        id=row.id,
        channel_id=row.channel_id,
        status=row.status,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        document_url=row.document_url,
        announced_at=row.announced_at,
    )


async def _participant_names(db: AsyncSession, session_id: int) -> dict[int, str]:
    rows = await db.execute(
        select(SessionParticipant.discord_user_id, SessionParticipant.discord_display_name).where(
            SessionParticipant.session_id == session_id
        )
    )
    return {user_id: name for user_id, name in rows}


async def load_status(
    session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    now: datetime,
    lease_seconds: float,
) -> QueueStatus:
    """Counts this guild's jobs, its expired leases and its stuck sessions.

    The "oldest pending job" figure is derived from `session.ended_at`
    rather than from the job row, because `transcription_job` has no
    enqueue timestamp at all. `RecordingService.close` uploads and enqueues
    every speaker and only then calls `close_session`, so `ended_at` is
    within seconds of when the job was created -- close enough to answer
    "has something been sitting in the queue for hours?", which is the only
    question this line exists for. It is *not* the age of a re-queued job:
    a reset job keeps its session's original `ended_at`, so a re-queue
    makes this number read older than the job really is. `render_status`
    therefore names the session's end rather than calling it the job's age,
    and says outright that a re-queue skews it.
    """
    lease_cutoff = now - timedelta(seconds=lease_seconds)
    async with session_factory() as db:
        rows = await db.execute(
            select(TranscriptionJob.status, func.count())
            .join(Session, Session.id == TranscriptionJob.session_id)
            .where(Session.guild_id == guild_id)
            .group_by(TranscriptionJob.status)
        )
        counts = {status: 0 for status in REPORTED_STATUSES}
        for status, count in rows:
            counts[status] = counts.get(status, 0) + int(count)

        past_lease = await db.scalar(
            select(func.count())
            .select_from(TranscriptionJob)
            .join(Session, Session.id == TranscriptionJob.session_id)
            .where(
                Session.guild_id == guild_id,
                TranscriptionJob.status == "running",
                TranscriptionJob.claimed_at < lease_cutoff,
            )
        )
        oldest = await db.scalar(
            select(func.min(Session.ended_at))
            .join(TranscriptionJob, TranscriptionJob.session_id == Session.id)
            .where(Session.guild_id == guild_id, TranscriptionJob.status == "pending")
        )
        # The same condition `SessionRepository.closed_undocumented_sessions`
        # uses for `retry_pending_documents`, restated here scoped to one
        # guild: that method is deliberately guild-blind because the worker
        # serves every guild from one process, and a status readout must
        # not be.
        has_jobs = select(TranscriptionJob.session_id).distinct()
        unfinished = select(TranscriptionJob.session_id).where(
            TranscriptionJob.status.not_in(tuple(TERMINAL_STATUSES))
        )
        stuck = await db.scalar(
            select(func.count())
            .select_from(Session)
            .where(
                Session.guild_id == guild_id,
                Session.status == "closed",
                Session.id.in_(has_jobs),
                Session.id.not_in(unfinished),
            )
        )
    return QueueStatus(
        counts=counts,
        running_past_lease=int(past_lease or 0),
        oldest_pending_session_ended_at=oldest,
        closed_undocumented=int(stuck or 0),
    )


#: How many unfinished sessions a queue overview reports before it stops.
#: A guild that has been broken for a month has hundreds, and a page of
#: hundreds is a page nobody reads -- while the twenty newest are where
#: whatever is wrong right now actually shows. The reader is told when the
#: list was cut, so "twenty" never reads as "twenty exist".
ACTIVE_SESSION_LIMIT = 20


@dataclass(frozen=True)
class ActiveSession:
    """One session the pipeline has not finished with, and where its jobs are.

    `channel_name` as well as `channel_id`, unlike `SessionSummary`: that
    one is rendered into Discord, where `<#id>` resolves to the channel's
    current name and stays a working link. A web page has no such
    rendering, so it needs the name the session opened under -- the same
    name every other console view shows.
    """

    id: int
    channel_id: int
    channel_name: str | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    document_url: str | None
    #: One entry per `REPORTED_STATUSES`, zero-filled, so a caller can
    #: render the lifecycle in order without checking for absent keys.
    counts: dict[str, int]
    #: Where this session sits in its guild's queue -- the priority its
    #: outstanding jobs carry, lower first. **`None` when it has none**,
    #: which is a meeting that is still recording, or one whose every job
    #: has finished and which is only still listed because one of them
    #: died. Deliberately not `0`: zero is the ordinary priority and a
    #: real place in the queue, and a session with nothing queued does not
    #: have a place in it. A page that read the two as the same would
    #: offer a drag handle on a row that nothing can be reordered about.
    priority: int | None


async def load_active_sessions(
    session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    limit: int = ACTIVE_SESSION_LIMIT,
) -> tuple[list[ActiveSession], bool]:
    """This guild's unfinished sessions, newest first, and whether there are more.

    **What counts as unfinished**, and why it is two conditions rather than
    one. Anything that is not `documented` is obviously unfinished: it is
    recording now, waiting for a worker, being transcribed, or stuck. But a
    session can reach `documented` with a `dead` job in it -- the document
    is written once every job is terminal, and `dead` is terminal -- so a
    speaker whose transcription failed permanently would vanish from the
    queue view at exactly the moment somebody needs to notice them. A dead
    job keeps its session on this list however finished the session claims
    to be.

    An `open` session with no jobs at all is included on purpose: it is a
    recording in progress, which is the one thing an administrator looking
    at a queue most wants confirmed.

    Guild-scoped in the statement rather than filtered afterwards, the same
    rule the rest of this module follows: a `WHERE` that names the guild
    cannot be forgotten, because without it the query returns nothing
    rather than everything.
    """
    dead_jobs = select(TranscriptionJob.session_id).where(TranscriptionJob.status == "dead")
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Session)
                .where(
                    Session.guild_id == guild_id,
                    or_(Session.status != DOCUMENTED_STATUS, Session.id.in_(dead_jobs)),
                )
                # By id as well as by time, so two sessions that opened in
                # the same instant do not swap places between two refreshes.
                .order_by(Session.started_at.desc(), Session.id.desc())
                # One more than asked for, which is how "there are more"
                # is learned without a second `COUNT(*)` over the same
                # predicate -- a count that could disagree with the page it
                # describes, having been taken a moment later.
                .limit(limit + 1)
            )
        ).scalars()
        found = list(rows)
        truncated = len(found) > limit
        found = found[:limit]
        if not found:
            return [], False

        counted = await db.execute(
            # The priority comes back from the same grouped read as the
            # counts rather than from a statement of its own, because the
            # two are read for one page and a second query would be a
            # second moment: a row could show a job that the counts say is
            # still pending sitting at a priority its reorder has already
            # moved on from.
            select(
                TranscriptionJob.session_id,
                TranscriptionJob.status,
                func.count(),
                func.min(TranscriptionJob.priority),
            )
            .where(TranscriptionJob.session_id.in_([row.id for row in found]))
            .group_by(TranscriptionJob.session_id, TranscriptionJob.status)
        )
        per_session: dict[int, dict[str, int]] = {
            row.id: dict.fromkeys(REPORTED_STATUSES, 0) for row in found
        }
        priorities: dict[int, int] = {}
        for session_id, status, count, priority in counted:
            # `setdefault` rather than assignment: a status this build does
            # not know about is still work somebody has to account for, and
            # dropping it would make the counts silently fail to add up.
            per_session[session_id][status] = per_session[session_id].get(status, 0) + int(count)
            if status in OUTSTANDING_STATUSES:
                # The lowest number over the jobs a worker may still take,
                # which is where a claim would next reach this session. A
                # reorder writes one number to all of them, so this is
                # normally that number; it differs only while a session
                # that was reordered has since enqueued more speakers, and
                # then the lower one is the honest answer.
                current = priorities.get(session_id)
                priorities[session_id] = priority if current is None else min(current, priority)

    return [
        ActiveSession(
            id=row.id,
            channel_id=row.channel_id,
            channel_name=row.channel_name,
            started_at=row.started_at,
            ended_at=row.ended_at,
            status=row.status,
            document_url=row.document_url,
            counts=per_session[row.id],
            priority=priorities.get(row.id),
        )
        for row in found
    ], truncated


async def load_session(
    session_factory: async_sessionmaker[AsyncSession], guild_id: int, session_id: int
) -> tuple[SessionSummary, list[JobLine], dict[int, str]] | None:
    """Reads one session's detail, or `None` if it is not this guild's."""
    async with session_factory() as db:
        row = await db.scalar(
            select(Session).where(Session.id == session_id, Session.guild_id == guild_id)
        )
        if row is None:
            return None
        jobs = await db.execute(
            select(TranscriptionJob)
            .where(TranscriptionJob.session_id == session_id)
            .order_by(TranscriptionJob.id)
        )
        lines = [
            JobLine(
                discord_user_id=job.discord_user_id,
                status=job.status,
                attempts=job.attempts,
                audio_present=job.audio_deleted_at is None,
                error=job.error,
                transcript_length=None if job.transcript is None else len(job.transcript),
            )
            for job in jobs.scalars()
        ]
        return _summary_of(row), lines, await _participant_names(db, session_id)


async def load_requeue_view(
    session_factory: async_sessionmaker[AsyncSession], guild_id: int, session_id: int
) -> SessionView | None:
    """Builds the plan shown in the confirmation, without locking anything.

    Deliberately lock-free: this read only decides what to *offer*, and
    holding a row lock across the seconds a human takes to read a prompt
    would block every worker completing a sibling job of the same session
    meanwhile. The plan that actually gets applied is re-derived inside the
    lock by `apply_requeue`, so a session that changes while the prompt is
    on screen is refused rather than acted on from a stale snapshot.
    """
    async with session_factory() as db:
        row = await db.scalar(
            select(Session).where(Session.id == session_id, Session.guild_id == guild_id)
        )
        if row is None:
            return None
        return SessionView(
            summary=_summary_of(row),
            plan=plan_requeue(await _job_dicts(db, session_id)),
            names=await _participant_names(db, session_id),
        )


async def _job_dicts(db: AsyncSession, session_id: int) -> list[dict[str, object]]:
    """One session's jobs, shaped for `plan_requeue`.

    Deliberately unfiltered by status or `audio_deleted_at`: both checks
    are that pure function's job alone, so there is exactly one definition
    of the rule -- the same reasoning
    `JobRepository.candidates_for_retention` follows for `expired_jobs`.
    """
    rows = await db.execute(
        select(
            TranscriptionJob.id,
            TranscriptionJob.discord_user_id,
            TranscriptionJob.status,
            TranscriptionJob.audio_deleted_at,
        ).where(TranscriptionJob.session_id == session_id)
    )
    return [
        {
            "id": row.id,
            "discord_user_id": row.discord_user_id,
            "status": row.status,
            "audio_deleted_at": row.audio_deleted_at,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# The write.
# ---------------------------------------------------------------------------


async def apply_requeue(
    session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    session_id: int,
    *,
    model: str,
) -> SessionView | None:
    """Resets a session's recoverable jobs, in one transaction and one commit.

    Returns the session as it was *decided* on -- the plan here is the one
    that was actually applied, or the one that caused a refusal. `None`
    means the session does not belong to this guild (or does not exist),
    which the caller renders as `NO_SUCH_SESSION`. Whether the write
    happened is derivable: it did exactly when the returned view is not
    `is_refused`.

    **`model` is required, and it is a name, never `None`.** It began as
    an optional argument that both callers omitted, so every re-queue this
    system had ever performed stored `NULL` -- which meant "whichever model
    the worker that happens to claim this job was configured with", a
    value that is not a record of anything and can differ between two
    workers of one fleet. It is a registered name from
    `sturnus.domain.transcription_models`; turning a request (or the
    absence of one) into such a name is `resolve`'s job, at the boundary
    where a caller can still be told to fix it. This function does not
    re-check, because a second copy of that rule is how the two drift --
    `WhisperEngine._model_for` is the backstop for a name that got past
    the boundary by some other route.

    Three properties matter and none is incidental.

    **The session's own status is checked here too, not only in front of
    the prompt.** `SessionView.is_settled` spells out why only a
    `documented` session may be reset; what matters at *this* point is
    that the check is made against the row this transaction locked. The
    prompt holds nothing but ids while an administrator reads it, and the
    session can move in that time -- so a status read before the lock
    would be exactly the stale snapshot the lock exists to rule out.

    **The lock comes first.** `SELECT TranscriptionJob.id WHERE session_id
    = ... ORDER BY id FOR UPDATE` is the same statement, with the same
    ordering, that `JobQueue.complete` takes before recomputing its
    remaining-jobs count. Taking it here is what serialises a re-queue
    against a worker completing a sibling job of the same session instead
    of letting the two interleave -- under READ COMMITTED this transaction
    would otherwise happily build a plan from a snapshot that predates a
    `complete()` already in flight, decide the session is entirely `done`,
    and reset a job whose worker is about to write its old transcript back
    over the reset. Keeping `ORDER BY id` identical to `complete`'s is what
    stops the two statements deadlocking against each other.

    **One transaction, one commit, spanning both tables.** If the job
    resets committed and the session reset did not, the redo would finish
    against a still-`documented` session: `complete()` would return `False`
    forever, because its last-job rule requires `status == "closed"`, and
    `retry_pending_documents` -- which also looks for `"closed"` -- would
    not sweep it either. The session would be stranded with no document and
    no error anywhere.
    """
    async with session_factory() as db:
        # Before reading anything: a plan built outside this lock is a plan
        # about a session that may already have moved.
        await db.execute(
            select(TranscriptionJob.id)
            .where(TranscriptionJob.session_id == session_id)
            .order_by(TranscriptionJob.id)
            .with_for_update()
        )
        row = await db.scalar(
            select(Session).where(Session.id == session_id, Session.guild_id == guild_id)
        )
        if row is None:
            return None
        view = SessionView(
            summary=_summary_of(row),
            plan=plan_requeue(await _job_dicts(db, session_id)),
            names=await _participant_names(db, session_id),
        )
        if view.is_refused:
            return view

        await db.execute(
            update(TranscriptionJob)
            .where(TranscriptionJob.id.in_(view.plan.resettable_job_ids))
            .values(
                status="pending",
                # A lease timestamp on a `pending` row means nothing and
                # would only make `/queue status` report a job as past its
                # lease before any worker has looked at it.
                claimed_at=None,
                # A full budget: this is a new attempt at a new code path,
                # not a continuation of the old one.
                attempts=0,
                # The old error described the old run.
                error=None,
                # What to transcribe with this time. Always a name, never
                # `NULL`: a re-queue nobody asked a question about carries
                # the registry's fallback, so the column says what was
                # asked for rather than that nothing was.
                #
                # It is stored rather than passed, because the worker that
                # eventually claims this job is not the process handling
                # this request -- there may be none running right now. A
                # question about a model has to survive in the row until
                # somebody picks the job up.
                requested_model=model,
                # `assemble` reads every job of the session, not only the
                # one that finished last, so a reset job that kept its old
                # text would put the very hallucinations this command
                # exists to remove into the new document if the session
                # were re-documented before the redo finished. Clearing it
                # makes a half-done redo visibly incomplete instead of
                # plausibly wrong. Losing the old text is intended: it
                # being wrong is why we are here, and `complete`
                # overwrites it on success anyway.
                transcript=None,
            )
        )
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(
                # "closed", never "open": "open" would make
                # `find_open_session` believe this guild has a live
                # recording. "closed" reproduces the post-`close_session`,
                # pre-documentation state exactly, which is what makes
                # `complete`'s last-job rule fire again -- and while it
                # sits there, `candidates_for_announcement`'s
                # `status == "documented"` filter excludes it, so nothing
                # announces mid-redo.
                status="closed",
                # Required, not optional. `sessions_to_announce` selects
                # only sessions whose `announced_at` is still null, and
                # `mark_documented` never touches this column -- so a
                # re-queue that left it set would produce a session that
                # transcribes, re-documents with a fresh URL, and is then
                # never announced, with nothing logged and nothing raised.
                # Clearing it is the deliberate choice to post again,
                # exactly once, the same not-null guard preventing any
                # further repeats.
                #
                # Clearing the column is not by itself enough to make that
                # second post happen: an announcement sweep can already be
                # inside `announcer.post` for this session right now, and
                # its `mark_announced` afterwards would stamp the column
                # we have just cleared. `SessionRepository.mark_announced`
                # is a compare-and-set on `status = 'documented' AND
                # announced_at IS NULL` for that reason -- the `closed`
                # written above is what makes the late stamp miss.
                announced_at=None,
                # `document_provider`/`document_id`/`document_url` are
                # deliberately untouched: the next `mark_documented`
                # overwrites them, and clearing them now would only stop
                # `/queue session` showing which document is superseded.
            )
        )
        await db.commit()
        return view


# ---------------------------------------------------------------------------
# Rendering. Pure functions over the values above, so the wording is
# testable without an `Interaction` -- the same reasoning `config_cog`'s
# `render_write_result` follows.
# ---------------------------------------------------------------------------
