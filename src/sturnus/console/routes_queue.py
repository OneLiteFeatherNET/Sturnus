"""A guild's transcription queue, and re-running one session's part of it.

- `GET  /api/guilds/{guild_id}/queue`
- `GET  /api/guilds/{guild_id}/queue/stream`
- `GET  /api/sessions/{session_id}/queue`
- `GET  /api/sessions/{session_id}/queue/stream`
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

**Why each queue endpoint has a `/stream` twin.** The console used to ask
these two endpoints the same question every few seconds and be told
"nothing changed" almost every time. The timer has moved to the server:
`/stream` re-reads the *same* snapshot the polling endpoint serves,
serialises it with the *same* function, and sends a `data:` event only when
that serialisation differs from the one it last sent. An unchanged queue
costs the browser nothing and the network nothing.

The polling endpoints are unchanged and stay. A client that cannot hold an
`EventSource` — an old browser, a proxy that eats event streams, a script —
must still be able to ask, and a stream that became the only way to learn
the answer would be a feature that takes one away.

**No decision is made here.** Whether a session may be re-queued is
`sturnus.application.requeue.plan_requeue`, and the write is
`sturnus.infrastructure.db.requeue.apply_requeue` — the same function, and
the same row lock, that the Discord command uses. This module is the shape
of two HTTP responses and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
_STATUS_STREAM_PATH = "/api/sessions/{session_id}/queue/stream"
_REQUEUE_PATH = "/api/sessions/{session_id}/queue/requeue"
_GUILD_PATH = "/api/guilds/{guild_id}/queue"
_GUILD_STREAM_PATH = "/api/guilds/{guild_id}/queue/stream"


@dataclass(frozen=True)
class StreamTiming:
    """How often an open stream re-reads, breathes, and gives up.

    Every one of these three is a cost or a failure mode rather than a
    preference, so each says what it is paying for.

    Held on the application rather than written in as constants, for one
    reason: they *are* the behaviour of a stream, and a test that had to
    wait fifteen real seconds to watch a heartbeat arrive is a test nobody
    runs. So `register` installs these defaults and a test replaces them.
    """

    #: **Two seconds, and here is the bill.** One database read per
    #: interval per *open* stream: two administrators watching two guilds
    #: is one query a second, and a guild whose queue is at rest costs
    #: nothing at all, because the stream ends rather than waiting for
    #: news that cannot arrive. That is what makes two seconds affordable
    #: where the browser's five were not. The browser paid a TLS
    #: handshake, a Cloudflare Tunnel hop, a cookie signature check and a
    #: whole request cycle for each of its reads, and it paid them whether
    #: or not the answer had changed; here the only thing that crosses the
    #: wire is a change.
    poll_seconds: float = 2.0
    #: A comment line when nothing has changed, so that nothing in the
    #: middle decides the connection is dead. Fifteen seconds sits well
    #: inside both an nginx `proxy_read_timeout` (sixty by default) and a
    #: Cloudflare Tunnel's idle timeout, with room for a slow hop.
    heartbeat_seconds: float = 15.0
    #: The server hangs up after ten minutes and the client reconnects on
    #: its own -- which is what `EventSource` does without being asked. An
    #: unbounded server-side loop is how a process quietly accumulates
    #: tasks belonging to browsers that were closed hours ago.
    max_seconds: float = 600.0


#: The timings an open stream runs on. See `StreamTiming` on why they are
#: injected rather than written in.
QUEUE_STREAM_TIMING: web.AppKey[StreamTiming] = web.AppKey("queue_stream_timing")

#: Job statuses that mean a worker may still act on this session. The same
#: "is it moving" predicate the console used to drive its own timer with --
#: one definition of it, now applied on the side that does the reading.
_IN_FLIGHT = frozenset({"pending", "running"})

#: A `data:` event carries a snapshot; the two named events below are
#: terminal and mean "stop, and do not reconnect". A named event still
#: needs a `data:` line of its own, or a browser will not dispatch it.
_REST_EVENT = b'event: rest\ndata: {"reason": "at rest"}\n\n'
_GONE_EVENT = b'event: gone\ndata: {"reason": "no longer readable"}\n\n'

#: A comment line. It reaches no listener and exists only to put bytes on
#: an idle connection before something in the middle reclaims it.
_HEARTBEAT = b": keep-alive\n\n"


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


async def guild_queue_stream(request: web.Request) -> web.StreamResponse:
    """The same answer as `guild_queue`, sent again whenever it changes.

    Same authorisation, and expressed by making the same call: the reader
    below is `QUEUE_OVERVIEW.for_guild` with this request's signed-in id,
    exactly as the polling handler above calls it, and `None` still means
    404 for "no such guild" and "not yours" alike. A second copy of the
    rule here is a second rule, and the copy is the one that would be left
    behind when the original changed.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        return _no_such_guild()

    async def read() -> _Reading | None:
        queue = await request.app[QUEUE_OVERVIEW].for_guild(guild_id, requested_by=viewer)
        if queue is None:
            return None
        # `isQueueMoving` in the console, said here instead: `pending` and
        # `running`, and deliberately not `dead`. A dead job never changes
        # on its own, so a stream held open for one would be a page
        # waiting for ever on news that cannot arrive.
        return _Reading(
            payload=_guild_queue_json(guild_id, queue),
            moving=queue.pending > 0 or queue.running > 0,
        )

    return await _stream_queue(request, read=read, refuse=_no_such_guild)


async def queue_status_stream(request: web.Request) -> web.StreamResponse:
    """The same answer as `queue_status`, sent again whenever it changes.

    Same authorisation as the polling endpoint, by calling the same
    method: `QUEUE_CONTROL.status_for` with the signed-in id, `None`
    answering 404 for both of the reasons it answers `None`.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    async def read() -> _Reading | None:
        snapshot = await request.app[QUEUE_CONTROL].status_for(session_id, requested_by=viewer)
        if snapshot is None:
            return None
        # Read from the jobs and not from `session_status`, because the
        # session flips to `documented` only after the document is
        # written -- a stream that ended at the last `done` would end one
        # step early and never show the finished document.
        return _Reading(
            payload=_snapshot_json(snapshot),
            moving=any(speaker.status in _IN_FLIGHT for speaker in snapshot.speakers),
        )

    return await _stream_queue(request, read=read, refuse=_no_such_session)


@dataclass(frozen=True)
class _Reading:
    """One re-read of a queue: what to send, and whether to stay."""

    payload: dict[str, object]
    #: Whether a worker may still act on this queue. The stream ends when
    #: this goes false, which is the whole reason it is carried alongside
    #: the payload rather than derived from the JSON: the predicate reads
    #: a typed value, not a dictionary of `object`.
    moving: bool


async def _stream_queue(
    request: web.Request,
    *,
    read: Callable[[], Awaitable[_Reading | None]],
    refuse: Callable[[], web.Response],
) -> web.StreamResponse:
    """Server-sent events for one queue, until it stops moving.

    **The first read happens before the response is prepared**, and that
    ordering is not incidental: once headers are on the wire there is no
    status left to send, so a stream that prepared first and read second
    would have to answer "you do not administer this guild" with a 200 and
    an empty stream. Read first, and a refusal is the same 404 the polling
    endpoint gives.
    """
    first = await read()
    if first is None:
        return refuse()

    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    # It names when a guild met and who spoke, and it is stale the moment
    # a worker claims a job. `no-store` for the same reason the polling
    # endpoints send it.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Connection"] = "keep-alive"
    # **The header this deployment cannot do without.** Sturnus sits behind
    # a Cloudflare Tunnel and a reverse proxy, and a proxy that buffers a
    # response holds every event until the response ends -- which for a
    # stream is ten minutes later, all at once, long after anybody cared.
    # A buffered event stream is a stream that never arrives. nginx and
    # everything that copied its conventions turn buffering off for a
    # response carrying this header.
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    timing = request.app[QUEUE_STREAM_TIMING]
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + timing.max_seconds

    # Sent immediately rather than on the first change, so that a page
    # renders the moment it connects. A client that had to wait for
    # something to happen before it could draw anything would be worse
    # than the timer this replaces.
    sent = _encode(first.payload)
    if not await _send(response, _data_event(sent)):
        return response
    spoke_at = loop.time()

    reading: _Reading | None = first
    while True:
        if reading is None:
            # The guild or session stopped being readable underneath us --
            # deleted, or this person is no longer an administrator. Not an
            # error and not a refusal at this point; the stream simply has
            # nothing further to say, and says so rather than reconnecting
            # forever into a 404.
            await _send(response, _GONE_EVENT)
            break
        if not reading.moving:
            # Nothing pending and nothing running. A stream that stayed
            # open for a finished queue is the polling problem with extra
            # sockets, so it says it is done and hangs up -- and the named
            # event is what stops the client reconnecting, since a browser
            # cannot tell a deliberate close from a dropped one.
            await _send(response, _REST_EVENT)
            break
        if loop.time() >= give_up_at:
            # No terminal event on purpose: the queue is still moving, so
            # the client *should* reconnect, and `EventSource` does that on
            # its own when the connection ends without being told to stop.
            break

        await asyncio.sleep(timing.poll_seconds)
        reading = await read()
        if reading is None:
            continue

        payload = _encode(reading.payload)
        if payload != sent:
            sent = payload
            if not await _send(response, _data_event(payload)):
                return response
            spoke_at = loop.time()
        elif loop.time() - spoke_at >= timing.heartbeat_seconds:
            if not await _send(response, _HEARTBEAT):
                return response
            spoke_at = loop.time()

    await _finish(response)
    return response


def _encode(payload: dict[str, object]) -> str:
    """The snapshot as one line of JSON.

    `sort_keys` is what makes "has it changed" a string comparison rather
    than a structural one. Two dictionaries built from the same rows must
    encode identically or every re-read would look like a change, which is
    precisely the traffic this endpoint exists to stop sending.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _data_event(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode()


async def _send(response: web.StreamResponse, chunk: bytes) -> bool:
    """Writes one event, and reports whether the reader is still there.

    A browser closing a tab, navigating away or losing its connection ends
    a stream mid-write, and that is the *ordinary* end of one rather than a
    fault. Unhandled, aiohttp logs the `ConnectionResetError` at ERROR with
    a traceback -- which is how leaving an admin page became an exception
    in the log of a service that is working perfectly. DEBUG, no traceback,
    and the loop ends rather than writing into a closed transport for the
    remaining nine minutes.

    `asyncio.CancelledError` is deliberately not caught: it descends from
    `BaseException`, and swallowing it would break graceful shutdown.
    """
    try:
        await response.write(chunk)
    except ConnectionError:
        log.debug("A queue stream's reader disconnected before it was finished")
        return False
    return True


async def _finish(response: web.StreamResponse) -> None:
    """Ends the response, forgiving a reader who left first."""
    try:
        await response.write_eof()
    except ConnectionError:
        log.debug("A queue stream's reader disconnected before it could be closed")


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

    # The defaults. A test that wants to watch a heartbeat inside a second
    # replaces this on the application before it starts; see
    # `StreamTiming` on why they are not constants.
    app[QUEUE_STREAM_TIMING] = StreamTiming()
    app.add_routes(
        [
            web.get(_GUILD_PATH, require_session(guild_queue)),
            web.get(_GUILD_STREAM_PATH, require_session(guild_queue_stream)),
            web.get(_STATUS_PATH, require_session(queue_status)),
            web.get(_STATUS_STREAM_PATH, require_session(queue_status_stream)),
            web.post(_REQUEUE_PATH, require_session(requeue_session)),
        ]
    )
