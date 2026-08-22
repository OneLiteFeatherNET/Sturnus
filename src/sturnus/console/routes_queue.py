"""Re-running a session's transcription from the console, and watching it.

- `GET  /api/sessions/{session_id}/queue`
- `POST /api/sessions/{session_id}/queue/requeue`

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

from sturnus.console.ports import QueueControl, QueueSnapshot, RequeueOutcome
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Where the collaborator is found. Declared here rather than in `app`
#: because it belongs to these routes and nothing else reads it.
QUEUE_CONTROL = web.AppKey("queue_control", QueueControl)

_STATUS_PATH = "/api/sessions/{session_id}/queue"
_REQUEUE_PATH = "/api/sessions/{session_id}/queue/requeue"


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


def _no_such_session() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such session" and "you do not administer that guild" are
    deliberately indistinguishable; see the module docstring.
    """
    return web.json_response({"error": "no such session"}, status=404)


def register(app: web.Application) -> None:
    """Adds the queue routes to an application that already has its control."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_STATUS_PATH, require_session(queue_status)),
            web.post(_REQUEUE_PATH, require_session(requeue_session)),
        ]
    )
