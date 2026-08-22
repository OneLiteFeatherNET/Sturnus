"""Which of a finished session's jobs may be transcribed a second time.

Re-running a session means returning it to the exact state it was in
immediately after `close_session` and before documentation, so that the
machinery which already exists -- `JobQueue.claim`, `JobQueue.complete`,
`sturnus.application.worker._create_session_document`,
`sturnus.application.publishing.announce_ready_sessions` -- carries it
forward a second time on its own. Nothing orchestrates the redo; it is a
state reset and nothing else.

This module holds the *decision* half of that: given the job rows of one
session, which of them may be reset, which must be left alone, and whether
the session may be touched at all. It is a pure function over plain dicts
and is tested without a database, sitting beside
`sturnus.application.publishing.sessions_to_announce` and
`sturnus.application.retention.expired_jobs` and following their style for
the same reason -- there is then exactly one definition of the rule, and
every sentence `/queue requeue` says to an administrator is derived from
this value rather than re-decided while rendering a reply.

The write that acts on a plan lives in
`sturnus.infrastructure.discord.queue_cog`, which is also the only caller.

Two rules carry all the weight, and both exist because getting them wrong
turns a helpful command into a destructive one.

**A job that is not terminal blocks the whole session.** A `pending` job is
already going to be transcribed, so there is nothing to re-queue; a
`running` job is worse than pointless to reset, because the worker holding
it will still call `complete()` when it finishes, writing the old run's
transcript and flipping the row back to `done` -- silently undoing a reset
the administrator has already been told about. Refusing the session outright
is simple, safe, and easy to explain in a reply.

**A job whose audio has been erased is skipped, not reset.**
`audio_deleted_at` is the authoritative, already-durable record that the S3
object is gone: it is stamped only after `store.delete` actually succeeded,
by either the retention sweep (`sturnus.application.retention`) or an
immediate erasure request (`/audio delete`, `/audio purge`). Re-queueing
such a job hands a worker a key it cannot download, `queue.fail` fires,
`attempts` climbs, the job goes `dead` again, and the only product is noise
in the log. Recoverability is never inferred from `retention_until`, which
is a plan and not a fact -- the hourly sweep may simply not have run yet.

A skipped job keeps its `done` status and its existing transcript, so it
still counts as terminal for `JobQueue.complete` and still contributes its
old text to `sturnus.application.assembly.assemble`. That composes
correctly: the new document is the redone speakers plus the untouched old
text of the speakers whose audio is gone. It also means the reply must say
so plainly -- an administrator told "3 speakers re-queued" and not told
"1 speaker's audio is erased, their old transcript is carried over" would
reasonably assume the whole document had been regenerated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

#: Job statuses `JobQueue` treats as finished. `claim` never selects
#: either of them, so nothing short of an explicit write resurrects such a
#: job -- which is exactly what makes them the ones a re-queue may reset.
#: `dead` belongs here as much as `done` does: it means "gave up after
#: `max_attempts`", never "unusable", and a job that died because the old
#: code path could not make sense of the audio is precisely the job an
#: administrator wants re-run against the new one.
TERMINAL_STATUSES = frozenset({"done", "dead"})


@dataclass(frozen=True)
class RequeuePlan:
    """What a re-queue of one session would do, before anything is written.

    The three tuples are disjoint and, together, cover every job of the
    session exactly once. All three are always computed, even when the
    plan turns out to be blocked: the plan describes the session, and the
    caller decides what to do about it -- which keeps the refusal reply
    able to say what *would* have happened as well as why it did not.
    """

    #: Jobs to reset to `pending`, ascending by id.
    resettable_job_ids: tuple[int, ...]
    #: The speakers those jobs belong to, in the same order. Kept beside
    #: the job ids rather than derived later from `session_participant`:
    #: a job whose participant row is gone still has a job, so the two
    #: lists are not interchangeable and reconstructing one from the other
    #: would quietly drop exactly the speaker a re-queue is most likely to
    #: be about.
    resettable_user_ids: tuple[int, ...]
    #: Speakers left untouched because their audio no longer exists. Their
    #: old transcript is carried into the new document unchanged.
    erased_user_ids: tuple[int, ...]
    #: Speakers whose job is `pending` or `running`. Any at all means the
    #: session must be refused; see the module docstring.
    active_user_ids: tuple[int, ...]

    @property
    def is_blocked(self) -> bool:
        """Whether a worker may still act on this session's jobs."""
        return bool(self.active_user_ids)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing left to reset.

        True for a session with no jobs at all and for one whose every
        recording has been erased alike. Both must be refused rather than
        confirmed: making no change while reporting success is the failure
        mode this command has to avoid.
        """
        return not self.resettable_job_ids


def plan_requeue(jobs: list[dict[str, object]]) -> RequeuePlan:
    """Sorts one session's jobs into reset / skip / blocking.

    `jobs` are the rows of a single session, each carrying `id`,
    `discord_user_id`, `status` and `audio_deleted_at`. Row order is not
    trusted: the result is sorted by job id, because the confirmation text
    built from it is read by a human and two runs against an unchanged
    session must produce the same sentence.

    The classification is checked in the order blocking, then erased, then
    resettable, and that order matters. A job can legitimately be
    `pending` *and* have `audio_deleted_at` set -- the retention sweep does
    not consult job status -- and reporting it as merely "skipped" would
    let the session through while a worker can still claim one of its jobs,
    which is the exact case the refusal exists for.
    """
    resettable: list[int] = []
    resettable_users: list[int] = []
    erased: list[int] = []
    active: list[int] = []
    for candidate in sorted(jobs, key=lambda row: cast(int, row["id"])):
        job_id = cast(int, candidate["id"])
        user_id = cast(int, candidate["discord_user_id"])
        status = cast(str, candidate["status"])
        audio_deleted_at = cast("datetime | None", candidate["audio_deleted_at"])
        if status not in TERMINAL_STATUSES:
            active.append(user_id)
        elif audio_deleted_at is not None:
            erased.append(user_id)
        else:
            resettable.append(job_id)
            resettable_users.append(user_id)
    return RequeuePlan(
        resettable_job_ids=tuple(resettable),
        resettable_user_ids=tuple(resettable_users),
        erased_user_ids=tuple(erased),
        active_user_ids=tuple(active),
    )
