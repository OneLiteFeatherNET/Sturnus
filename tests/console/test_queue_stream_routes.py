"""What an open queue stream sends, what it refuses to send, and when it stops.

The property this file exists for is the one the change was made for: **an
unchanged queue produces no event.** A stream that re-sent an identical
snapshot every five seconds would be the browser's timer moved one process
to the left and nothing else, and no type check, lint pass or successful
request can tell the two apart. So the tests count events.

The other three are failure modes rather than features:

- A stream that outlives its guild's work is a socket held open for news
  that cannot arrive, so it says it is done and hangs up.
- A stream with no ceiling is a task the server keeps after the browser
  has gone, so it hangs up on itself and lets the client reconnect.
- A stream a proxy is allowed to buffer is a stream that never arrives,
  which is invisible everywhere except in production.

Authorisation is asserted here too, and not because it is new: it is
deliberately the *same* rule as the polling endpoint's, reached by calling
the same method, and a test that only covered the polling endpoint would
let a second copy of the rule appear here unnoticed.
"""

from __future__ import annotations

from datetime import timedelta

from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import (
    GuildQueue,
    QueuedSession,
    QueueSnapshot,
    QueueSpeaker,
)
from sturnus.console.routes_queue import QUEUE_STREAM_TIMING, StreamTiming
from sturnus.console.session import SessionCookie, SignedSession
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    SESSION,
    T0,
    AiohttpClientFactory,
    FakeQueue,
    FakeQueueOverview,
    build_test_api,
)

#: Fast enough that a whole stream's life fits in a test run, and still
#: ordered the way the real ones are: re-read, then heartbeat, then give
#: up. The defaults are five seconds, fifteen and ten minutes.
FAST = StreamTiming(poll_seconds=0.005, heartbeat_seconds=0.02, max_seconds=2.0)


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def guild_stream_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/queue/stream"


def session_stream_url(session_id: int | str = SESSION) -> str:
    return f"/api/sessions/{session_id}/queue/stream"


# ---------------------------------------------------------------------------
# Queues to hand out, and the fakes that hand them out in a scripted order
# ---------------------------------------------------------------------------


def queued(**over: object) -> QueuedSession:
    base: dict[str, object] = {
        "id": SESSION,
        "channel_id": 555,
        "channel_name": "standup",
        "started_at": T0,
        "ended_at": T0,
        "status": "closed",
        "document_url": None,
        "pending": 2,
        "running": 0,
        "done": 0,
        "dead": 0,
    }
    base.update(over)
    return QueuedSession(**base)  # type: ignore[arg-type]


def guild_queue(**over: object) -> GuildQueue:
    base: dict[str, object] = {
        "pending": 2,
        "running": 1,
        "done": 40,
        "dead": 1,
        "running_past_lease": 0,
        "oldest_pending_session_ended_at": T0,
        "closed_undocumented": 0,
        "lease_seconds": 1800.0,
        "sessions": (queued(),),
        "truncated": False,
    }
    base.update(over)
    return GuildQueue(**base)  # type: ignore[arg-type]


def at_rest(**over: object) -> GuildQueue:
    """A guild with nothing pending and nothing running.

    `dead` is left at one deliberately: a dead job never changes on its
    own, so a queue carrying one is still at rest as far as a stream is
    concerned, and a stream that stayed open for it would wait for ever.
    """
    return guild_queue(pending=0, running=0, sessions=(), **over)


def snapshot(**over: object) -> QueueSnapshot:
    base: dict[str, object] = {
        "session_status": "closed",
        "document_url": None,
        "speakers": (
            QueueSpeaker(ANNA, "anna", "running", 1, None),
            QueueSpeaker(BEN, "ben", "pending", 0, None),
        ),
        "can_requeue": False,
        "refusal": "a re-queue is already running",
    }
    base.update(over)
    return QueueSnapshot(**base)  # type: ignore[arg-type]


def finished() -> QueueSnapshot:
    return snapshot(
        session_status="documented",
        document_url="https://outline.example/doc/1",
        speakers=(
            QueueSpeaker(ANNA, "anna", "done", 1, None),
            QueueSpeaker(BEN, "ben", "done", 1, None),
        ),
        can_requeue=True,
        refusal=None,
    )


class ScriptedOverview:
    """A guild queue that answers a written-down sequence of readings.

    The last entry repeats for ever, so a script says what changes and how
    it ends without also having to say how many times the stream will get
    round to reading it -- which is a fact about a sleep, not about the
    behaviour under test.
    """

    def __init__(self, *readings: GuildQueue | None) -> None:
        self.readings = list(readings)
        #: Who asked, and about which guild. The stream cannot be seen to
        #: pass the signed-in id rather than one from the URL by reading
        #: its body.
        self.asked: list[tuple[int, int]] = []

    async def for_guild(self, guild_id: int, *, requested_by: int) -> GuildQueue | None:
        self.asked.append((guild_id, requested_by))
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0] if self.readings else None


class ScriptedControl:
    """The per-session counterpart of `ScriptedOverview`."""

    def __init__(self, *readings: QueueSnapshot | None) -> None:
        self.readings = list(readings)
        self.asked: list[tuple[int, int]] = []

    async def status_for(self, session_id: int, *, requested_by: int) -> QueueSnapshot | None:
        self.asked.append((session_id, requested_by))
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0] if self.readings else None

    async def requeue(self, session_id: int, *, requested_by: int) -> None:
        del session_id, requested_by
        return None


def streaming_api(**collaborators: object) -> web.Application:
    """The console API with the stream timings turned down to test speed."""
    app = build_test_api(**collaborators)  # type: ignore[arg-type]
    app[QUEUE_STREAM_TIMING] = FAST
    return app


def blocks(body: str) -> list[str]:
    """The stream cut into its events, comments included."""
    return [block for block in body.split("\n\n") if block.strip()]


def data_blocks(body: str) -> list[str]:
    """Only the `data:` events -- the ones that carry a snapshot."""
    return [block for block in blocks(body) if block.startswith("data: ")]


# ---------------------------------------------------------------------------
# What a stream sends
# ---------------------------------------------------------------------------


async def test_a_stream_sends_the_queue_as_it_stands_the_moment_it_is_opened(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Immediately, and not on the first change.

    A client that had to wait for something to happen before it could draw
    anything would be worse than the timer this replaces -- a quiet server
    would render as a blank page for as long as it stayed quiet.
    """
    app = streaming_api(queues=ScriptedOverview(at_rest()))
    client = await signed_in(aiohttp_client, app)

    response = await client.get(guild_stream_url())
    assert response.status == 200
    body = await response.text()

    first = data_blocks(body)[0]
    # The same serialiser the polling endpoint uses, so the same payload
    # reaches the same parser -- a stream with a shape of its own would be
    # a second contract to keep in step with the first.
    assert f'"guild_id":"{GUILD}"' in first
    assert '"pending":0' in first


async def test_a_queue_that_has_not_changed_produces_no_event_at_all(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The whole point of the change, and the only way to see it is to count.

    The stream re-reads several times over the life of this test and every
    reading is the same queue. One event on connect, one when the queue
    finally comes to rest, and nothing in between -- where the timer this
    replaces would have sent the same payload over and over.
    """
    moving = guild_queue()
    overview = ScriptedOverview(moving, moving, moving, moving, moving, at_rest())
    client = await signed_in(aiohttp_client, streaming_api(queues=overview))

    body = await (await client.get(guild_stream_url())).text()

    assert len(data_blocks(body)) == 2
    # It really did re-read more often than it spoke; otherwise the count
    # above would pass for a stream that never polled at all.
    assert len(overview.asked) > 2


async def test_a_change_in_the_queue_arrives_as_an_event(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    overview = ScriptedOverview(
        guild_queue(pending=2, running=1),
        guild_queue(pending=1, running=2),
        at_rest(),
    )
    client = await signed_in(aiohttp_client, streaming_api(queues=overview))

    body = await (await client.get(guild_stream_url())).text()
    sent = [block.replace(" ", "") for block in data_blocks(body)]

    assert len(sent) == 3
    assert '"pending":2,"running":1' in sent[0]
    assert '"pending":1,"running":2' in sent[1]


async def test_a_stream_with_nothing_to_say_keeps_the_connection_alive_with_a_comment(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Silence and a dead connection look identical to a reverse proxy.

    A comment line reaches no listener and exists only to put bytes on the
    wire before something in the middle reclaims a connection it has
    decided is idle.
    """
    moving = guild_queue()
    overview = ScriptedOverview(*([moving] * 20), at_rest())
    client = await signed_in(aiohttp_client, streaming_api(queues=overview))

    body = await (await client.get(guild_stream_url())).text()

    assert any(block.startswith(":") for block in blocks(body))


async def test_a_stream_says_it_is_done_and_closes_when_the_queue_comes_to_rest(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The terminal event is what stops the client reconnecting.

    A browser cannot tell a deliberate close from a dropped one and
    reconnects after both, so a stream that simply hung up on a finished
    queue would be reopened for ever.
    """
    client = await signed_in(
        aiohttp_client, streaming_api(queues=ScriptedOverview(guild_queue(), at_rest()))
    )

    body = await (await client.get(guild_stream_url())).text()

    assert blocks(body)[-1].startswith("event: rest")


async def test_a_stream_whose_queue_stops_being_readable_says_so_rather_than_ending_quietly(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Deleted underneath, or the reader stopped administering the guild.

    Neither is an error at this point -- the stream simply has nothing
    further to say. Ending without saying it would leave a client
    reconnecting into a 404 for the life of the tab.
    """
    client = await signed_in(
        aiohttp_client, streaming_api(queues=ScriptedOverview(guild_queue(), None))
    )

    body = await (await client.get(guild_stream_url())).text()

    assert blocks(body)[-1].startswith("event: gone")


async def test_a_stream_that_reaches_its_ceiling_ends_without_telling_the_client_to_stop(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The bound exists so the server does not keep a task per closed tab.

    And it ends *without* a terminal event on purpose: the queue is still
    moving, so the client should reconnect -- which `EventSource` does on
    its own when a connection ends without being told otherwise.
    """
    app = streaming_api(queues=ScriptedOverview(guild_queue()))
    app[QUEUE_STREAM_TIMING] = StreamTiming(
        poll_seconds=0.005, heartbeat_seconds=5.0, max_seconds=0.05
    )
    client = await signed_in(aiohttp_client, app)

    body = await (await client.get(guild_stream_url())).text()

    assert data_blocks(body)
    assert not any(block.startswith("event:") for block in blocks(body))


# ---------------------------------------------------------------------------
# One session's stream
# ---------------------------------------------------------------------------


async def test_a_session_stream_sends_the_speakers_and_stops_when_they_have_all_finished(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Read from the jobs, not from `session_status`.

    A session flips to `documented` only after the document is written, so
    a stream that ended at the last `done` would end one step early and
    never show the finished document.
    """
    control = ScriptedControl(snapshot(), snapshot(), finished())
    client = await signed_in(aiohttp_client, streaming_api(queue=control))

    body = await (await client.get(session_stream_url())).text()
    sent = data_blocks(body)

    assert len(sent) == 2
    assert '"status":"running"' in sent[0]
    assert "https://outline.example/doc/1" in sent[1]
    assert blocks(body)[-1].startswith("event: rest")


async def test_a_session_stream_names_speakers_by_a_string_id(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The same serialiser as the polling endpoint, so the same rule holds.

    A snowflake exceeds JavaScript's safe integer range, where a JSON
    number silently loses its last digits and produces an id that looks
    right and names nobody.
    """
    client = await signed_in(aiohttp_client, streaming_api(queue=ScriptedControl(finished())))

    body = await (await client.get(session_stream_url())).text()

    assert f'"discord_user_id":"{ANNA}"' in data_blocks(body)[0]


# ---------------------------------------------------------------------------
# Who may open one
# ---------------------------------------------------------------------------


async def test_somebody_who_does_not_administer_the_guild_is_told_it_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404 before a single byte of stream, and the same 404 either way.

    The refusal has to happen before the response is prepared: once headers
    are on the wire there is no status left to send, and a stream that
    prepared first would have to answer "you do not administer this guild"
    with a 200 and an empty body.
    """
    client = await signed_in(
        aiohttp_client,
        streaming_api(queues=FakeQueueOverview(), queue=FakeQueue()),
    )

    assert (await client.get(guild_stream_url())).status == 404
    assert (await client.get(session_stream_url())).status == 404


async def test_a_stream_asks_about_the_signed_in_person_and_never_about_a_name_in_the_url(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    overview = ScriptedOverview(at_rest())
    client = await signed_in(aiohttp_client, streaming_api(queues=overview), as_user=BEN)

    await (await client.get(guild_stream_url())).text()

    assert overview.asked[0] == (GUILD, BEN)


async def test_a_request_without_a_session_cannot_open_a_stream(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(streaming_api(queues=ScriptedOverview(at_rest())))

    assert (await client.get(guild_stream_url())).status == 401
    assert (await client.get(session_stream_url())).status == 401


async def test_a_guild_that_is_not_a_number_names_nothing_and_opens_nothing(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, streaming_api(queues=ScriptedOverview(at_rest())))

    assert (await client.get(guild_stream_url("nonsense"))).status == 404
    assert (await client.get(session_stream_url("nonsense"))).status == 404


# ---------------------------------------------------------------------------
# What the headers promise
# ---------------------------------------------------------------------------


async def test_a_stream_forbids_the_buffering_that_would_make_it_arrive_all_at_once(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`X-Accel-Buffering: no`, and this deployment cannot do without it.

    Sturnus sits behind a Cloudflare Tunnel and a reverse proxy. A proxy
    that buffers holds every event until the response ends -- which for a
    stream is ten minutes later, all at once, long after anybody cared.
    """
    client = await signed_in(aiohttp_client, streaming_api(queues=ScriptedOverview(at_rest())))

    response = await client.get(guild_stream_url())
    await response.text()

    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.headers["X-Accel-Buffering"] == "no"
    assert "no-store" in response.headers["Cache-Control"]


async def test_a_session_stream_carries_the_same_headers_as_a_guild_one(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, streaming_api(queue=ScriptedControl(finished())))

    response = await client.get(session_stream_url())
    await response.text()

    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert response.headers["X-Accel-Buffering"] == "no"


async def test_a_stream_re_reads_no_more_often_than_the_polling_it_replaces() -> None:
    """The interval is an operating cost, so it is pinned rather than assumed.

    A "read" here is not one query. One read of a guild's overview is seven
    SQL statements over three pooled connections -- `is_admin`,
    `load_status`'s four, and `load_active_sessions`'s two -- and one read
    of a session's queue is eight over four. At five seconds that is about
    1.4 statements a second per open guild stream, which is exactly what
    the five-second browser poll this replaces already cost; the panel's
    three-second poll cost rather more than its stream now does.

    Two seconds, which this shipped with, was 2.5x *more* database work
    than polling rather than less. What streaming genuinely saves is the
    per-request overhead the browser paid on every tick whether or not the
    answer had changed -- a TLS handshake, a tunnel hop, a cookie signature
    check -- and what it genuinely gains is that a change is sent when it
    happens rather than on the client's next tick. Neither of those is a
    reason to read more often, so the number is written down here with the
    reasoning attached to it.
    """
    timing = StreamTiming()

    assert timing.poll_seconds == 5.0
    # Well inside an nginx `proxy_read_timeout` (sixty by default) and a
    # Cloudflare Tunnel's idle timeout, and comfortably longer than a
    # re-read, so a heartbeat is never what an idle connection is waiting
    # on.
    assert timing.heartbeat_seconds == 15.0
    # The ceiling on what one abandoned stream can cost. A client that
    # falls back to polling closes its `EventSource`, but this loop learns
    # that only when its next write fails -- and behind the buffering proxy
    # that caused the fallback the writes are swallowed rather than
    # refused, so nothing but this number ends it.
    assert timing.max_seconds == 600.0
    assert timing.max_seconds / timing.poll_seconds == 120
