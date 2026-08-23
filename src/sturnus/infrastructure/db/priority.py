"""Reading a guild's queue order, and writing a new one.

The decision this module acts on is not made here. `sturnus.application.
priorities` holds all of it -- what order was asked for, and which
integers express it -- and this module is the transaction that reads the
rows those functions need, hands them over, and writes back what comes
out. The same arrangement `sturnus.infrastructure.db.requeue` has with
`sturnus.application.requeue`, and for the same reason: the rule then has
one definition and can be tested without a database at all.

**Why the decision is a callable rather than an argument.** `reorder`
takes a function from "the queue as it is right now" to "the order it
should be in". A drag and a quick action are then the same write with two
different decisions -- and, more importantly, the decision is taken
*inside* the lock, from rows this transaction has already locked. An API
that took a finished list of session ids would be taking one computed
from whatever the browser was showing, which is precisely the stale
snapshot two administrators dragging at once produce.

**The lock, and why it is the guild's rows rather than one session's.**
An order is a statement about a queue, not about a session: deciding
where one meeting goes means reading where all the others are. So the
guild's outstanding jobs are locked before any of them is read, which
makes two concurrent reorders of one guild serialise -- the second sees
what the first wrote and decides against it. Rows are locked in ascending
`id`, identically to `JobQueue.complete` and `apply_requeue`, which is
what stops any two of the three deadlocking: every one of them takes its
locks in the same direction, so no cycle can form.

While a reorder holds those locks a worker's `claim` cannot take the
guild's jobs -- it runs `FOR UPDATE SKIP LOCKED`, so it walks past them
and claims somebody else's work instead. That is the correct behaviour
and it lasts for one short transaction, but it is worth knowing that a
reorder is momentarily also a hold.

**`FOR UPDATE OF transcription_job`**, not a bare `FOR UPDATE`. The
statement joins to `session` only to name the guild, and locking a
session row would collide with `apply_requeue`, which locks jobs and then
writes the session. Only the rows that are about to be written are
locked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.priorities import QueuedSession, priorities_for
from sturnus.infrastructure.db.models import Session, SessionParticipant, TranscriptionJob

#: Job statuses that mean a worker may still act on this row -- the same
#: set `JobQueue.claim` selects from and `_outstanding_before` counts.
#: These are the only rows a priority means anything on, so they are the
#: only rows this module reads a priority from or writes one to.
OUTSTANDING_STATUSES: tuple[str, str] = ("pending", "running")

#: What a caller wants done with the queue: given it as it stands, the
#: order it should be in, or `None` to refuse and write nothing. `None` is
#: how a drag says that the session it names, or the session it was
#: dropped beside, is not in this queue any more.
Decision = Callable[[Sequence[QueuedSession]], "tuple[int, ...] | None"]


@dataclass(frozen=True)
class QueueOrder:
    """A guild's queue after a reorder, and what the reorder wrote.

    `changed` is separate from `sessions` and is not derivable from it: a
    reorder that changed nothing and a reorder that changed everything
    both return the whole queue, and the difference is exactly what an
    administrator's page needs in order to say whether anything happened.
    """

    #: Every outstanding session of the guild, in the order a claim would
    #: now reach them, with the priority each one now carries.
    sessions: tuple[QueuedSession, ...]
    #: The sessions whose priority this call wrote, ascending. Empty when
    #: the order asked for was the order that already held.
    changed: tuple[int, ...]

    @property
    def order(self) -> tuple[int, ...]:
        return tuple(session.id for session in self.sessions)


async def load_queued_sessions(
    session_factory: async_sessionmaker[AsyncSession], guild_id: int
) -> tuple[QueuedSession, ...]:
    """This guild's outstanding sessions, in the order a claim would reach them.

    Lock-free, because this is the read behind a page: holding a lock
    across the time a human spends looking at a list would block every
    worker completing one of that guild's jobs meanwhile. The order that
    actually gets written is re-derived inside `reorder`'s lock.
    """
    async with session_factory() as db:
        return await _queued_sessions(db, guild_id)


async def reorder(
    session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    decide: Decision,
) -> QueueOrder | None:
    """Applies one decision to one guild's queue, in one locked transaction.

    Returns the queue as it now stands, or `None` when `decide` refused --
    which for a drag means the session named, or the session it was
    dropped beside, is no longer in this queue. `None` is a refusal to act
    and never a partial write: nothing is written on that path at all.

    A session's number goes onto **every one of its outstanding jobs**, and
    onto none of its finished ones. The unit an administrator moves is a
    meeting; the rows are one per speaker; a write that moved four of five
    speakers would leave a queue half-reordered in a way no page renders
    and nobody could see. A `done` or `dead` job is not going to be
    claimed again, so a queue position on it would be an intention
    recorded about work that is over.
    """
    async with session_factory() as db:
        # Before anything is read. A decision taken outside this lock is a
        # decision about a queue that may already have moved -- which is
        # exactly what two administrators dragging at once produce.
        await db.execute(
            select(TranscriptionJob.id)
            .select_from(TranscriptionJob)
            .join(Session, Session.id == TranscriptionJob.session_id)
            .where(
                Session.guild_id == guild_id,
                TranscriptionJob.status.in_(OUTSTANDING_STATUSES),
            )
            .order_by(TranscriptionJob.id)
            .with_for_update(of=TranscriptionJob)
        )
        queued = await _queued_sessions(db, guild_id)
        wanted = decide(queued)
        if wanted is None:
            await db.rollback()
            return None

        changes = priorities_for(queued, wanted)
        for session_id, priority in changes.items():
            await db.execute(
                update(TranscriptionJob)
                .where(
                    TranscriptionJob.session_id == session_id,
                    TranscriptionJob.status.in_(OUTSTANDING_STATUSES),
                )
                .values(priority=priority)
            )
        await db.commit()

    written = {session.id: changes.get(session.id, session.priority) for session in queued}
    return QueueOrder(
        # Rebuilt from what was decided rather than re-read, so the answer
        # describes this transaction's own result. A second read would be
        # a different moment, and could show a session that a worker
        # finished in between as having left the queue this call just
        # ordered.
        sessions=tuple(
            _at(session, written[session.id])
            for session in sorted(queued, key=lambda row: wanted.index(row.id))
        ),
        changed=tuple(sorted(changes)),
    )


def _at(session: QueuedSession, priority: int) -> QueuedSession:
    if priority == session.priority:
        return session
    return QueuedSession(
        id=session.id,
        priority=priority,
        participants=session.participants,
        audio_seconds=session.audio_seconds,
    )


async def _queued_sessions(db: AsyncSession, guild_id: int) -> tuple[QueuedSession, ...]:
    """The guild's outstanding sessions, with everything a rule reads.

    Two statements, and each aggregates over a different set of rows on
    purpose.

    **Priority is read from the outstanding jobs only**, as the *minimum*
    over them. A reorder writes one number to all of them, so the minimum
    is that number -- but a session that closed a moment later has jobs
    enqueued at the ordinary `0`, and the minimum is then `0`, which is
    genuinely where a claim would reach that session next. The alternative
    readings would report a place the queue does not actually have it in.

    **Length is read from every job of the session**, outstanding or not.
    It is a fact about the recording rather than about the queue, and a
    re-queued session keeps the measurements its first pass produced --
    which is the one case where a queued session has a length at all,
    since `audio_seconds` is not written until a job completes. Null where
    nothing has measured anything, never zero: see
    `sturnus.application.priorities`.
    """
    outstanding = TranscriptionJob.status.in_(OUTSTANDING_STATUSES)
    rows = (
        await db.execute(
            select(
                TranscriptionJob.session_id,
                func.min(TranscriptionJob.priority).filter(outstanding).label("priority"),
                func.sum(TranscriptionJob.audio_seconds).label("audio_seconds"),
            )
            .select_from(TranscriptionJob)
            .join(Session, Session.id == TranscriptionJob.session_id)
            .where(Session.guild_id == guild_id)
            .group_by(TranscriptionJob.session_id)
            # A session with nothing outstanding is not in the queue and
            # cannot be given a place in it. Expressed as a `HAVING` over
            # the same aggregate the priority comes from, so the two can
            # never disagree about which sessions those are.
            .having(func.count().filter(outstanding) > 0)
        )
    ).all()
    if not rows:
        return ()

    counted = await db.execute(
        select(SessionParticipant.session_id, func.count())
        .where(SessionParticipant.session_id.in_([row.session_id for row in rows]))
        .group_by(SessionParticipant.session_id)
    )
    participants: dict[int, int] = {session_id: int(count) for session_id, count in counted.all()}
    return tuple(
        sorted(
            (
                QueuedSession(
                    id=row.session_id,
                    priority=row.priority,
                    # Zero for a session whose participant rows are gone,
                    # rather than absent: a job exists for it, so it is in
                    # the queue and has to be orderable. A rule that reads
                    # participants ranks it last, which is the truthful
                    # place for a meeting nobody is recorded as attending.
                    participants=participants.get(row.session_id, 0),
                    audio_seconds=None if row.audio_seconds is None else float(row.audio_seconds),
                )
                for row in rows
            ),
            key=lambda session: (session.priority, session.id),
        )
    )
