"""A guild's transcription queue, what runs first, and re-running one session.

- `GET  /api/guilds/{guild_id}/queue`
- `GET  /api/guilds/{guild_id}/queue/stream`
- `POST /api/guilds/{guild_id}/queue/priority`
- `GET  /api/sessions/{session_id}/queue`
- `GET  /api/sessions/{session_id}/queue/stream`
- `POST /api/sessions/{session_id}/queue/priority`
- `POST /api/sessions/{session_id}/queue/requeue`
- `GET  /api/models`

**Why a reorder is expressed relative to another session and never as a
number.** A drag-and-drop list produces "this one goes here", and here is
a neighbour: `{"place": "before", "session": "512"}`. It does not produce
an integer, and an API that asked for one would be asking a browser to
invent the queue's arithmetic -- with a stale copy of the queue, and with
no way to agree with the other browser doing the same thing a second
later. So the console names a session it can see and the server works out
the numbers (`sturnus.application.priorities`).

That choice is what makes two administrators dragging at once produce
something sensible. Each request is decided inside the same lock that
writes it, against the queue as it stands at that instant, so the second
is applied to what the first left rather than to the list its browser was
showing. The result is always one of the two orders those two drags could
serialise into -- never a blend of both, and never a lost write. The
second administrator may well see an order they did not picture, because
the list moved under them; the stream tells them so immediately, and an
anchor still means something after somebody else's move landed in a way
that "put it at index 3" would not.

**A reorder names a session, and applies to that session's jobs.** The
rows are one per speaker, so a request that took job ids could reorder
four of a meeting's five speakers -- a queue that is half moved, that no
page renders and that nobody would ever notice. Nothing in this module
can express that.

**Why the guild-wide view is here rather than in a module of its own.**
It is the same subject asked at a different scale: the per-session
endpoints answer "where has this one got to", and the guild one answers
"what is outstanding, and which sessions is it outstanding in". Splitting
them across two files would have put one authorisation rule in two places
and invited them to drift.

**Why `/api/models` is here too**, despite naming no guild and no session.
It exists for exactly one caller: the dropdown beside the re-queue button.
A module of its own would have been a file holding one handler that reads
a domain constant, and it would have separated the list of what may be
asked for from the endpoint that decides whether a request may ask for it.

**Which model a re-queue runs, and who may say.** A re-queue may name a
transcription model; it is validated against
`sturnus.domain.transcription_models` here, at the boundary, and an
unknown name is a 400. That refusal is the point of the registry: an
unknown name used to travel unchecked into `WhisperModel(...)`, which
raises rather than falling back, so a typo cost four failed attempts and
one speaker left `dead` with no transcript, minutes later and nowhere near
the request that caused it.

Naming a model is not a permission of its own. It is part of a re-queue,
and re-queueing already takes an administrator of the session's guild — so
an administrator may name any registered model, and everybody else has
their request refused whole rather than obeyed with the model discarded.
Silently dropping an instruction is how a caller learns to distrust an
API; the rule stays in one place by being the rule that was already there.

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

It does not, however, cost the *database* nothing, and `StreamTiming`
spells out what it does cost. The interval was chosen so that a stream
reads no more often than the polling it replaces; what is saved is the
per-request overhead of asking, and what is gained is that a change
arrives when it happens rather than on the client's next tick.

The polling endpoints are unchanged and stay. A client that cannot hold an
`EventSource` — an old browser, a proxy that eats event streams, a script —
must still be able to ask, and a stream that became the only way to learn
the answer would be a feature that takes one away.

**No decision is made here.** Whether a session may be re-queued is
`sturnus.application.requeue.plan_requeue`, and the write is
`sturnus.infrastructure.db.requeue.apply_requeue` — the same function, and
the same row lock, that the Discord command uses. What a model name means
is `sturnus.domain.transcription_models`, which the Discord command and
the worker read too. This module turns those answers into HTTP responses
and holds none of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from sturnus.application.priorities import (
    PLACEMENTS,
    Placement,
    UnknownPriorityRule,
    resolve_rule,
)
from sturnus.console.ports import (
    GuildQueue,
    QueueControl,
    QueuedSession,
    QueueOrder,
    QueueOverview,
    QueueSnapshot,
    RequeueOutcome,
)
from sturnus.domain import transcription_models
from sturnus.domain.transcription_models import UnknownTranscriptionModel
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
_PLACE_PATH = "/api/sessions/{session_id}/queue/priority"
_GUILD_PATH = "/api/guilds/{guild_id}/queue"
_GUILD_STREAM_PATH = "/api/guilds/{guild_id}/queue/stream"
_GUILD_PRIORITY_PATH = "/api/guilds/{guild_id}/queue/priority"
#: Deliberately not under `/api/guilds/{guild_id}/`. The registry is a
#: property of this deployment's build, not of a guild -- putting a guild
#: in the path would promise a per-guild answer that does not exist and
#: that a console would then be tempted to cache per guild.
_MODELS_PATH = "/api/models"

#: Refusal reasons for a malformed request. Fixed strings, so the console
#: can key off them without the offending input being echoed back. The
#: unknown-model refusal is not here: it is the registry's own message,
#: which names both what was asked for and what there is.
_MALFORMED_BODY = "malformed request body"
_MODEL_MUST_BE_A_STRING = "model must be a string naming a transcription model"
_PLACE_MUST_BE_KNOWN = "place must be one of " + ", ".join(PLACEMENTS)
_ANCHOR_MUST_BE_A_SESSION_ID = "session must be a string naming the session to sit beside"
_ANCHOR_ONLY_WITH_BEFORE_OR_AFTER = "session may only be given with place before or after"
_ANCHOR_IS_THE_SESSION_ITSELF = "a session cannot be placed relative to itself"
_RULE_MUST_BE_A_STRING = "rule must be a string naming a queue rule"


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

    #: **Five seconds, and here is the actual bill.** A "read" here is not
    #: one query. `ConsoleQueueOverview.for_guild`, which the guild stream
    #: calls, issues **seven** statements over three pooled connections --
    #: `is_admin` is one, `load_status` is four (the status counts, the
    #: expired leases, the oldest pending session, the stuck-closed count)
    #: and `load_active_sessions` is two. `ConsoleQueueControl.status_for`,
    #: which the session stream calls, issues **eight** over four --
    #: `_administered_guild` is two, and `load_requeue_view` and
    #: `load_session` are three each. So two administrators watching two
    #: guilds is not "one query a second": it is about 2.8 statements a
    #: second, and one administrator watching one guild is about 1.4.
    #:
    #: Five rather than the two this shipped with, because two was 2.5x
    #: *more* database work than the polling it replaced, not less. The
    #: guild page polled every five seconds and the panel every three, so
    #: at five the guild stream costs exactly what its polling cost and the
    #: panel's stream costs rather less. What is genuinely saved is
    #: per-*request* overhead, which the browser paid on every tick whether
    #: or not the answer had changed: a TLS handshake, a Cloudflare Tunnel
    #: hop, a cookie signature check and a whole request cycle. That saving
    #: is real and worth having; it is not a saving in reads, and the
    #: argument for this endpoint must not be made as though it were.
    #:
    #: The improvement in *latency* is not paid for at all: a change is
    #: sent when it happens rather than on the client's next tick, so five
    #: seconds here is not five seconds of staleness the way five seconds
    #: of polling was.
    #:
    #: A queue at rest costs nothing, because the stream ends rather than
    #: waiting for news that cannot arrive.
    poll_seconds: float = 5.0
    #: A comment line when nothing has changed, so that nothing in the
    #: middle decides the connection is dead. Fifteen seconds sits well
    #: inside both an nginx `proxy_read_timeout` (sixty by default) and a
    #: Cloudflare Tunnel's idle timeout, with room for a slow hop.
    heartbeat_seconds: float = 15.0
    #: The server hangs up after ten minutes and the client reconnects on
    #: its own -- which is what `EventSource` does without being asked. An
    #: unbounded server-side loop is how a process quietly accumulates
    #: tasks belonging to browsers that were closed hours ago.
    #:
    #: **This is also the worst case for an abandoned stream**, and the
    #: reason it is not longer. A client that falls back to polling closes
    #: its `EventSource` -- `openQueueStream` closes the source in the same
    #: breath as it announces `polling`, so the browser is not paying for
    #: both -- but this loop learns nothing from that until its next write
    #: fails. On a direct connection that is at most `heartbeat_seconds`
    #: away, fifteen seconds, and sooner if the queue changes meanwhile.
    #: Behind the buffering proxy that *caused* the fallback there is no
    #: such luck: the proxy holds its own connection to this process open
    #: and swallows the writes, so the loop keeps re-reading for the full
    #: ten minutes. That is the ceiling on what one abandoned stream can
    #: cost -- 120 reads, about 840 statements for a guild stream, once --
    #: and it is why the ceiling exists rather than being left to the
    #: client to enforce by hanging up.
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

    A model nobody has is the *other* kind of wrong and gets **400**: the
    request itself is unusable and no state will ever make it work. That
    refusal is issued here, before the session is even looked at, because
    the failure it prevents is expensive and silent — an unknown name used
    to travel into `WhisperModel(...)`, which raises rather than falling
    back, so the job failed, retried, and left the speaker `dead` with no
    transcript some minutes later and nowhere near this request.

    **Body optional, and the model within it optional.** No body at all is
    the console's existing button and means "no choice"; the fallback is
    substituted at this line rather than deeper, so nothing below has to
    know what an absent choice means.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    try:
        model = transcription_models.resolve(await _requested_model(request))
    except UnknownTranscriptionModel as exc:
        # The exception's own message names the offending value, which is
        # unbounded text a caller sent, so it goes into the response and
        # never into a log line. There is nothing an operator would do
        # with the news that a browser asked for a model this build does
        # not have -- the same trade `routes_me._store` makes.
        return _bad_request(str(exc))

    outcome = await request.app[QUEUE_CONTROL].requeue(session_id, requested_by=viewer, model=model)
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
        # Safe to log precisely because the registry is closed: this is
        # one of a fixed set of literals from this repository's own
        # source, which is the standard `observability.fields` holds
        # `model` to. An unvalidated name would not have met it.
        model=outcome.model,
    )
    return web.json_response(_outcome_json(outcome))


async def place_session(request: web.Request) -> web.Response:
    """Moves one session to where an administrator dropped it in the queue.

    **404 for everybody who is not an administrator of the session's
    guild, and the same 404 for a session that does not exist.** Expressed
    by calling `QueueControl.place`, which is the same object and the same
    check the re-queue endpoint above uses -- a second copy of that rule
    here would be a second rule, and the copy is the one that gets left
    behind when the original changes. A 403 would confirm that the session
    exists and roughly when it ran, to somebody just established as having
    no business knowing.

    **409 for a drag the queue has moved out from under.** The session, or
    the session it was dropped beside, has finished since the page was
    drawn. That is not a malformed request -- it was perfectly good a few
    seconds ago -- and it is not a permission problem, so it is neither
    400 nor 404: it is the state having changed, which is exactly what
    409 says and exactly what the re-queue refusal above uses it for. The
    body carries the queue as it now is, so the page can redraw rather
    than replay the drag it has just been told is stale.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    placement = await _requested_placement(request, session_id)
    order = await request.app[QUEUE_CONTROL].place(
        session_id, requested_by=viewer, placement=placement
    )
    if order is None:
        return _no_such_session()
    return _order_response(order, session_id=session_id, requested_by=viewer)


async def prioritise_guild_queue(request: web.Request) -> web.Response:
    """Reorders a whole guild's queue by one named rule.

    The quick actions beside the queue: run the meetings with the most
    people in them first, or the shortest recordings first. Each is a sort
    key in `sturnus.application.priorities` and nothing more, so the third
    one somebody asks for is one function there and one name in a
    registry -- not another endpoint, and not another way to write a
    priority.

    **The name is validated here, at the boundary**, and an unknown one is
    a 400 naming both what was asked for and what there is. The same trade
    the `model` parameter makes on the re-queue endpoint, and for the same
    reason: below this line an unknown rule does not exist, so nothing
    deeper has to decide what to do about one -- and silently running a
    different rule than the one asked for would reorder a guild's queue in
    a way the administrator could not tell from the feature working.

    404 for a guild this person does not administer and for one that does
    not exist alike, as everywhere else in this module.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        return _no_such_guild()

    rule = await _requested_rule(request)
    try:
        resolve_rule(rule)
    except UnknownPriorityRule as exc:
        # The message names the value a caller sent, which is unbounded
        # text, so it goes into the response and never into a log line --
        # the same trade the unknown-model refusal makes above.
        return _bad_request(str(exc))

    order = await request.app[QUEUE_OVERVIEW].reprioritise(guild_id, requested_by=viewer, rule=rule)
    if order is None:
        return _no_such_guild()
    return _order_response(order, guild_id=guild_id, requested_by=viewer, rule=rule)


def _order_response(
    order: QueueOrder,
    *,
    requested_by: int,
    session_id: int | None = None,
    guild_id: int | None = None,
    rule: str | None = None,
) -> web.Response:
    """One answer for both writes, refusal included.

    The audit line is here rather than in each handler because it is the
    same event: somebody changed the order work will be done in. It is
    logged at WARNING when something was actually written, for the reason
    the re-queue audit line is -- a queue that reordered itself under a
    team needs a name attached to why -- and nothing is logged for a
    reorder that changed nothing, which is what a page re-sending its
    current order produces.
    """
    if not order.accepted:
        # INFO: a stale drag is this endpoint working. Nothing was written
        # and there is nothing for an operator to do.
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_QUEUE_REORDER_REFUSED,
            "Refused a queue reorder asked for from the console",
            session_id=session_id,
            guild_id=guild_id,
            requested_by=requested_by,
        )
        return web.json_response(_order_json(order), status=409)

    if order.changed:
        log_event(
            log,
            logging.WARNING,
            Event.CONSOLE_QUEUE_REORDERED,
            "Reordered a guild's transcription queue from the console",
            session_id=session_id,
            guild_id=guild_id,
            requested_by=requested_by,
            # One of a fixed set of literals from this repository's own
            # source, which is the standard `observability.fields` holds
            # `model` to. `None` for a drag, which names no rule.
            rule=rule,
            sessions=len(order.changed),
        )
    return web.json_response(
        _order_json(order),
        # It names which meetings a guild has outstanding, and it is stale
        # the moment a worker claims a job.
        headers={"Cache-Control": "private, no-store"},
    )


async def known_models(request: web.Request) -> web.Response:
    """Every transcription model a re-queue may name, and which is the default.

    This is the dropdown's source. It exists because nothing in the system
    could previously *list* the models — a name was a free string, so no
    interface could have offered a choice even if it wanted to, and an
    administrator picking one would have been typing from memory into a
    field that fails four attempts later.

    Each entry carries its approximate size and the trade it makes,
    because the choice is between hours of worker time and whether the
    transcript is worth reading, and neither is legible from a name like
    `large-v3-turbo`. The order is the registry's own and means something:
    fastest and roughest first.

    **403, where the rest of this module answers 404.** Those 404s exist so
    that a refusal cannot confirm a fact about somebody else — that a
    session ran, that a guild exists. There is no such fact here: the body
    is seven literals from this repository's own source, identical for
    every caller. The only thing this refusal discloses is whether the
    caller administers anything, which `/api/me` already tells them about
    themselves.

    Administrator-gated all the same, and not merely as tidiness: a person
    who administers nothing can never re-queue anything, so the list would
    be an offer this API would then refuse to honour.
    """
    from sturnus.console.app import _ADMINS, current_user

    viewer = current_user(request).discord_user_id
    if not await request.app[_ADMINS].is_admin_anywhere(viewer):
        return web.json_response({"error": "not an administrator"}, status=403)

    return web.json_response(
        {
            "fallback": transcription_models.FALLBACK,
            "models": [
                {
                    "name": model.name,
                    "approximate_size": model.approximate_size,
                    "summary": model.summary,
                }
                for model in transcription_models.KNOWN_MODELS
            ],
        },
        # The one response in this module that is the same for every
        # caller and changes only when this repository does. It is still
        # not cached: it is served from behind a session cookie, and a
        # shared cache holding a body that took a cookie to obtain is a
        # habit worth not starting for seven lines of JSON.
        headers={"Cache-Control": "private, no-store"},
    )


async def _requested_model(request: web.Request) -> str | None:
    """The `model` a request named, as a string and only as a string.

    An absent body and an absent key both mean "no choice" — the console's
    re-queue button sends no body at all, and must keep working. Anything
    else is refused rather than interpreted: `{"model": 3}` is not coerced
    to `"3"`, and `{"model": null}` is refused rather than read as the
    fallback, because sending `null` is what a client does with an unset
    form field and quietly accepting it would hide that bug for as long as
    the fallback happened to be what they wanted.
    """
    if not request.body_exists:
        return None
    try:
        body = await request.json()
    except ValueError:
        raise _bad_request_exception(_MALFORMED_BODY) from None
    if not isinstance(body, dict):
        raise _bad_request_exception(_MALFORMED_BODY)
    if "model" not in body:
        return None
    model = body["model"]
    if not isinstance(model, str):
        raise _bad_request_exception(_MODEL_MUST_BE_A_STRING)
    return model


async def _requested_placement(request: web.Request, session_id: int) -> Placement:
    """Where a drag says a session goes, checked before anything is read.

    Strict about types rather than forgiving, exactly as
    `_requested_model` is: `{"place": 1}` is not coerced and
    `{"session": 512}` is refused rather than read as `"512"`. Session ids
    travel as strings everywhere in this API -- see `_queued_session_json`
    -- so a number here is a client that has started parsing ids as
    numbers somewhere, which is a bug worth failing loudly rather than
    accommodating until it reaches an id that does not survive the round
    trip.

    The anchor's presence is checked against the placement rather than
    ignored when it does not apply. `{"place": "first", "session": "512"}`
    is somebody who believes they said where; obeying half of that and
    discarding the rest is how a caller learns to distrust an API.
    """
    body = await _body(request)
    place = body.get("place")
    if not isinstance(place, str) or place not in PLACEMENTS:
        raise _bad_request_exception(_PLACE_MUST_BE_KNOWN)

    anchor = _anchor_id(body)
    placement = Placement(place, anchor)
    if not placement.is_valid:
        raise _bad_request_exception(
            _ANCHOR_MUST_BE_A_SESSION_ID if anchor is None else _ANCHOR_ONLY_WITH_BEFORE_OR_AFTER
        )
    if anchor == session_id:
        # "Before itself" names no position at all. Refused rather than
        # treated as a no-op, because a client that sent it is computing
        # the anchor wrongly and a silent success hides that for ever.
        raise _bad_request_exception(_ANCHOR_IS_THE_SESSION_ITSELF)
    return placement


def _anchor_id(body: dict[str, object]) -> int | None:
    if "session" not in body:
        return None
    anchor = body["session"]
    if not isinstance(anchor, str):
        raise _bad_request_exception(_ANCHOR_MUST_BE_A_SESSION_ID)
    try:
        return int(anchor)
    except ValueError:
        raise _bad_request_exception(_ANCHOR_MUST_BE_A_SESSION_ID) from None


async def _requested_rule(request: web.Request) -> str:
    """The quick action a request named. Required, and only ever a string.

    No default. A body without a rule is not "the usual one" -- there is
    no usual one, and guessing would reorder a guild's queue by a rule
    nobody chose.
    """
    body = await _body(request)
    rule = body.get("rule")
    if not isinstance(rule, str):
        raise _bad_request_exception(_RULE_MUST_BE_A_STRING)
    return rule


async def _body(request: web.Request) -> dict[str, object]:
    """The request's JSON object, or a 400.

    Unlike the re-queue's body, this one is required: a re-queue with no
    body is the console's button and means "no choice", while a reorder
    with no body has not said what to do.
    """
    if not request.body_exists:
        raise _bad_request_exception(_MALFORMED_BODY)
    try:
        body = await request.json()
    except ValueError:
        raise _bad_request_exception(_MALFORMED_BODY) from None
    if not isinstance(body, dict):
        raise _bad_request_exception(_MALFORMED_BODY)
    return body


def _bad_request(reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=400)


def _bad_request_exception(reason: str) -> web.HTTPException:
    """A 400 with a JSON body, raised rather than returned.

    aiohttp deprecated returning an `HTTPException` from a handler, and
    raising lets `_requested_model` read as a straight line instead of
    threading an optional response back to its caller — the same trade
    `routes_me._refusal` makes.
    """
    return web.HTTPBadRequest(text=json.dumps({"error": reason}), content_type="application/json")


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
        # Present-and-null rather than absent on a refusal, so a client
        # never has to tell "nothing was written" from "this API predates
        # the field" -- it would guess, and it would guess wrong on one of
        # them. The same shape `/api/me` gives `display_name`.
        "model": outcome.model,
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
        # Lower first, `0` ordinary. **Present-and-null rather than absent**
        # for a session with nothing outstanding -- still recording, or
        # listed only because a job of it died -- so a client never has to
        # tell "no place in the queue" from "this API predates the field".
        # It would guess, and it would guess wrong on one of them. The same
        # shape `/api/me` gives `display_name`.
        #
        # Null is not zero here and the difference is what a page acts on:
        # zero is the ordinary priority and a real place in the queue, null
        # is a row with nothing to reorder, which is a row that must not
        # offer a drag handle.
        "priority": session.priority,
    }


def _order_json(order: QueueOrder) -> dict[str, object]:
    """A guild's queue order, in the shape both writes answer with.

    The whole queue every time, refusal included, because this is what a
    page redraws from. It is deliberately not the *difference*: a client
    that applied a diff to a list it had would be applying it to the list
    that may have caused the refusal.

    `changed` is sent beside it and is the one thing the order itself does
    not say -- whether this request did anything. An administrator who
    dragged a session two pixels and put it back gets `[]` and can be told
    "nothing to do" instead of "done".
    """
    return {
        "accepted": order.accepted,
        # Present-and-null on success rather than absent; see `_outcome_json`.
        "refusal": order.refusal,
        "changed": [str(session_id) for session_id in order.changed],
        "order": [
            {
                # A string, like every other id in this API. Session ids do
                # not need it and follow anyway: two id shapes in one
                # payload is how the one that matters gets parsed with the
                # wrong one.
                "session_id": str(position.session_id),
                # Never null here, unlike the queue listing's field of the
                # same name: everything in this list has outstanding work
                # by construction, which is what having a place means.
                "priority": position.priority,
            }
            for position in order.sessions
        ],
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
            web.post(_GUILD_PRIORITY_PATH, require_session(prioritise_guild_queue)),
            web.get(_STATUS_PATH, require_session(queue_status)),
            web.get(_STATUS_STREAM_PATH, require_session(queue_status_stream)),
            web.post(_PLACE_PATH, require_session(place_session)),
            web.post(_REQUEUE_PATH, require_session(requeue_session)),
            web.get(_MODELS_PATH, require_session(known_models)),
        ]
    )
