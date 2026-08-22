"""A guild's transcription queue, and re-running one session's part of it.

- `GET  /api/guilds/{guild_id}/queue`
- `GET  /api/sessions/{session_id}/queue`
- `POST /api/sessions/{session_id}/queue/requeue`

**Why the guild-wide view is here rather than in a module of its own.**
It is the same subject asked at a different scale: the per-session
endpoints answer "where has this one got to", and the guild one answers
"what is outstanding, and which sessions is it outstanding in". Splitting
them across two files would have put one authorisation rule in two places
and invited them to drift.

**Why this exists at all.** The first pass over a recording can be wrong —
a model that hallucinated, a worker that died, a bug since fixed — and
until now the only way to ask for another one was `/queue requeue` in
Discord. Somebody looking at a bad protocol in the console had to leave
the console to do anything about it.

**Why the rule is administrator rather than participant.** Everywhere else
in the console the question is "was this your meeting", because playing
your own recording back is a use of your own data. This is not that: a
re-queue spends worker time, clears and rewrites transcripts, replaces a
shared document and re-announces it. It is an operation on the system, so
it takes an administrator of the guild the session belongs to — and
`ConsoleQueueControl` makes that check part of every call rather than
something a handler applies.

Somebody who is not an administrator gets **404, not 403**, for the same
reason the audio endpoint does: a 403 confirms that the session exists and
when it ran, to somebody just established as having no business knowing.

**Why a progress endpoint and not just a button.** A re-queue is not
instantaneous — the jobs go back to `pending` and a worker picks them up
when it gets to them. A button that reports nothing after being pressed is
a button people press twice, and pressing this one twice is not harmless:
the second press lands while the first redo is `running`, which is exactly
the state `plan_requeue` refuses. Better to show the queue moving.

**No decision is made here.** Whether a session may be re-queued is
`sturnus.application.requeue.plan_requeue`, and the write is
`sturnus.infrastructure.db.requeue.apply_requeue` — the same function, and
the same row lock, that the Discord command uses. This module is the shape
of two HTTP responses and nothing else.
"""

from __future__ import annotations

import logging

from aiohttp import web

from sturnus.console.ports import (
    GuildQueue,
    QueueControl,
    QueuedSession,
    QueueOverview,
    QueueSnapshot,
    RequeueOutcome,
)
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Where the collaborator is found. Declared here rather than in `app`
#: because it belongs to these routes and nothing else reads it.
QUEUE_CONTROL = web.AppKey("queue_control", QueueControl)

#: The guild-wide overview's collaborator. A second key rather than one
#: object with both shapes, because the two answer different questions at
#: different scales and a protocol that offered both would let a handler
#: reach for the wide one where the narrow one was meant.
QUEUE_OVERVIEW: web.AppKey[QueueOverview] = web.AppKey("queue_overview")

_STATUS_PATH = "/api/sessions/{session_id}/queue"
_REQUEUE_PATH = "/api/sessions/{session_id}/queue/requeue"
_GUILD_PATH = "/api/guilds/{guild_id}/queue"


async def guild_queue(request: web.Request) -> web.Response:
    """What this guild's transcription pipeline still owes, and where.

    404 for a guild this person does not administer, and the same 404 for
    one that does not exist. The list names when a guild met, in which
    channel, and how many people spoke; a 403 would confirm that such a
    list exists here to somebody just established as having no business
    with it.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        # A path segment that is not a number names no guild, which is the
        # same answer as naming one that does not exist.
        return _no_such_guild()

    queue = await request.app[QUEUE_OVERVIEW].for_guild(guild_id, requested_by=viewer)
    if queue is None:
        return _no_such_guild()
    return web.json_response(
        _guild_queue_json(guild_id, queue),
        # It names when a guild met and in which channel, and it is stale
        # the moment a worker claims a job.
        headers={"Cache-Control": "private, no-store"},
    )


async def queue_status(request: web.Request) -> web.Response:
    """Where this session's transcription has got to."""
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    snapshot = await request.app[QUEUE_CONTROL].status_for(session_id, requested_by=viewer)
    if snapshot is None:
        return _no_such_session()
    return web.json_response(
        _snapshot_json(snapshot),
        # It names who spoke in a meeting, and it goes stale the moment a
        # worker picks a job up.
        headers={"Cache-Control": "private, no-store"},
    )


async def requeue_session(request: web.Request) -> web.Response:
    """Sends a documented session back through transcription.

    A refusal is **409, not 400**: the request is well formed and the
    session is real, and what is wrong is the state it is in — which is
    the distinction a client needs to decide between "fix your request"
    and "try again when the queue is idle". The reason travels with it,
    because a button that greys out without saying why is a bug report
    waiting to be filed.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    outcome = await request.app[QUEUE_CONTROL].requeue(session_id, requested_by=viewer)
    if outcome is None:
        return _no_such_session()

    if not outcome.accepted:
        # INFO, not WARNING: a refusal is this feature working. The
        # interesting line is the one below it.
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_REQUEUE_REFUSED,
            "Refused a re-queue asked for from the console",
            session_id=session_id,
            requested_by=viewer,
        )
        return web.json_response(_outcome_json(outcome), status=409)

    # The audit line for an operation that clears transcripts and will
    # replace a published document. `requested_by` is the whole point: a
    # document that changed under a team needs a name attached to why.
    log_event(
        log,
        logging.WARNING,
        Event.CONSOLE_REQUEUE_APPLIED,
        "Re-queued a session's transcription from the console",
        session_id=session_id,
        requested_by=viewer,
        speakers=len(outcome.requeued_user_ids),
        skipped=len(outcome.erased_user_ids),
    )
    return web.json_response(_outcome_json(outcome))


def _session_id(request: web.Request) -> int | None:
    try:
        return int(request.match_info["session_id"])
    except ValueError:
        # A path segment that is not a number names nothing, which is the
        # same answer as naming something that does not exist.
        return None


def _snapshot_json(snapshot: QueueSnapshot) -> dict[str, object]:
    return {
        "session_status": snapshot.session_status,
        "document_url": snapshot.document_url,
        "can_requeue": snapshot.can_requeue,
        "refusal": snapshot.refusal,
        "speakers": [
            {
                # A Discord snowflake exceeds JavaScript's safe integer
                # range, where a JSON number silently loses its last digits
                # and produces an id that looks right and names nobody.
                "discord_user_id": str(speaker.discord_user_id),
                "display_name": speaker.display_name,
                "status": speaker.status,
                "attempts": speaker.attempts,
                "error": speaker.error,
            }
            for speaker in snapshot.speakers
        ],
    }


def _outcome_json(outcome: RequeueOutcome) -> dict[str, object]:
    return {
        "accepted": outcome.accepted,
        "requeued": [str(user_id) for user_id in outcome.requeued_user_ids],
        # Named separately and never folded into the count above. An
        # administrator told "3 speakers re-queued" and not told that a
        # fourth was skipped because its audio is erased would reasonably
        # assume the whole document had been regenerated.
        "skipped_erased": [str(user_id) for user_id in outcome.erased_user_ids],
        "refusal": outcome.refusal,
    }


def _guild_queue_json(guild_id: int, queue: GuildQueue) -> dict[str, object]:
    return {
        "guild_id": str(guild_id),
        # The lifecycle in its own order rather than four sibling fields:
        # `pending -> running -> done | dead` is how a reader finds where
        # work is piling up, and a shape that spelled it out flat would
        # leave that ordering to whichever client rendered it.
        "counts": {
            "pending": queue.pending,
            "running": queue.running,
            "done": queue.done,
            "dead": queue.dead,
        },
        "running_past_lease": queue.running_past_lease,
        "oldest_pending_session_ended_at": (
            None
            if queue.oldest_pending_session_ended_at is None
            else queue.oldest_pending_session_ended_at.isoformat()
        ),
        "closed_undocumented": queue.closed_undocumented,
        # Sent with the count it produced. `running_past_lease` is derived
        # from an assumed lease and the one that applies is the worker's,
        # which this process cannot see -- so the console names the number
        # it used instead of presenting the count as a fact.
        "lease_seconds": queue.lease_seconds,
        "truncated": queue.truncated,
        "sessions": [_queued_session_json(session) for session in queue.sessions],
    }


def _queued_session_json(session: QueuedSession) -> dict[str, object]:
    return {
        # A string like every other id in this API. Session ids do not
        # need it and follow anyway: two id shapes in one payload is how
        # the one that matters gets parsed with the wrong one.
        "id": str(session.id),
        "channel_id": str(session.channel_id),
        "channel_name": session.channel_name,
        "started_at": session.started_at.isoformat(),
        "ended_at": None if session.ended_at is None else session.ended_at.isoformat(),
        "status": session.status,
        "document_url": session.document_url,
        "counts": {
            "pending": session.pending,
            "running": session.running,
            "done": session.done,
            "dead": session.dead,
        },
    }


def _no_such_guild() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such guild" and "you do not administer that guild" are
    deliberately indistinguishable, for the reason `_no_such_session`
    gives about sessions.
    """
    return web.json_response({"error": "no such guild"}, status=404)


def _no_such_session() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such session" and "you do not administer that guild" are
    deliberately indistinguishable; see the module docstring.
    """
    return web.json_response({"error": "no such session"}, status=404)


def register(app: web.Application) -> None:
    """Adds the queue routes to an application that already has its collaborators."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_GUILD_PATH, require_session(guild_queue)),
            web.get(_STATUS_PATH, require_session(queue_status)),
            web.post(_REQUEUE_PATH, require_session(requeue_session)),
        ]
    )
