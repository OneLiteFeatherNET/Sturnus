"""Who may say what the queue does first, and what they have to say to say it.

Three things are pinned here and each of them is a way this endpoint could
be plausibly wrong while returning 200.

**The rule is administrator-of-the-guild, and the refusal is a 404.**
Reordering a queue is an operation on the system -- it changes the order
everybody in that guild waits in -- so it is the re-queue rule and not the
"was this your meeting" rule the audio endpoints use. 403 would confirm
that a session exists and roughly when it ran, to somebody just
established as having no business knowing.

**The console never sends a number.** A drag says "before that one", and
the arithmetic is the server's. A request shape that accepted an integer
would be an API asking a browser to compute the queue's order from a stale
copy of it.

**A drag the queue has moved out from under is a 409 that carries the
queue.** Not a 400 -- the request was good when it was made -- and not a
silent success, which is the failure that makes a drag-and-drop list
untrustworthy.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.application.priorities import Placement
from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import QueueOrder, QueuePosition
from sturnus.console.session import SessionCookie, SignedSession
from tests.console.conftest import (
    ANNA,
    GUILD,
    SECRET,
    SESSION,
    T0,
    AiohttpClientFactory,
    FakeQueue,
    FakeQueueOverview,
    build_test_api,
)

OTHER_SESSION = 512


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def place_url(session_id: int = SESSION) -> str:
    return f"/api/sessions/{session_id}/queue/priority"


def guild_url(guild_id: int = GUILD) -> str:
    return f"/api/guilds/{guild_id}/queue/priority"


def order(**over: object) -> QueueOrder:
    base: dict[str, object] = {
        "accepted": True,
        "refusal": None,
        "sessions": (
            QueuePosition(session_id=SESSION, priority=0),
            QueuePosition(session_id=OTHER_SESSION, priority=1),
        ),
        "changed": (OTHER_SESSION,),
    }
    base.update(over)
    return QueueOrder(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A drag
# ---------------------------------------------------------------------------


async def test_an_administrator_can_put_a_session_at_the_front_of_the_queue(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queue = FakeQueue(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(place_url(), json={"place": "first"})

    assert response.status == 200
    body = await response.json()
    assert body["accepted"] is True
    assert body["order"] == [
        {"session_id": str(SESSION), "priority": 0},
        {"session_id": str(OTHER_SESSION), "priority": 1},
    ]
    assert body["changed"] == [str(OTHER_SESSION)]


async def test_a_drag_names_a_neighbour_and_the_placement_reaches_the_write(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A handler that dropped the anchor would still return a plausible 200."""
    queue = FakeQueue(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    await client.post(place_url(), json={"place": "before", "session": str(OTHER_SESSION)})

    assert queue.placed == [(SESSION, ANNA, Placement("before", OTHER_SESSION))]


async def test_the_write_is_made_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queue = FakeQueue(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    await client.post(place_url(), json={"place": "last"})

    assert [asked for _, asked, _ in queue.placed] == [ANNA]


async def test_somebody_who_does_not_administer_the_guild_is_told_it_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404, not 403, and the same 404 a session that never existed gets.

    The control answers `None` for both reasons, which is the whole point
    of folding them together there.
    """
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue()))

    response = await client.post(place_url(), json={"place": "first"})

    assert response.status == 404
    assert (await response.json())["error"] == "no such session"


async def test_a_session_id_that_is_not_a_number_gets_the_same_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue(order=order())))

    response = await client.post("/api/sessions/nonsense/queue/priority", json={"place": "first"})

    assert response.status == 404


async def test_a_request_without_a_session_cookie_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(queue=FakeQueue(order=order())))

    response = await client.post(place_url(), json={"place": "first"})

    assert response.status == 401


async def test_a_drag_the_queue_has_moved_out_from_under_is_a_conflict(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """409, and it carries the queue as it now is.

    The request was well formed and the person may make it; what changed
    is the state, which is what 409 says. A page told only "no" would
    redraw from the list that caused the refusal and offer the same drag
    again.
    """
    refused = order(
        accepted=False,
        refusal="that session is no longer in this guild's queue",
        changed=(),
    )
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue(order=refused)))

    response = await client.post(place_url(), json={"place": "first"})

    assert response.status == 409
    body = await response.json()
    assert body["accepted"] is False
    assert body["refusal"] == "that session is no longer in this guild's queue"
    assert [row["session_id"] for row in body["order"]] == [str(SESSION), str(OTHER_SESSION)]


async def test_a_reorder_that_changed_nothing_says_so_rather_than_claiming_a_write(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """An empty `changed` is the difference between "done" and "nothing to do"."""
    client = await signed_in(
        aiohttp_client, build_test_api(queue=FakeQueue(order=order(changed=())))
    )

    response = await client.post(place_url(), json={"place": "first"})

    assert response.status == 200
    assert (await response.json())["changed"] == []


# ---------------------------------------------------------------------------
# What a drag may say
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="nothing at all"),
        pytest.param({"place": "somewhere"}, id="a placement nobody has"),
        pytest.param({"place": 1}, id="a number where a placement goes"),
        pytest.param({"place": "before"}, id="before nothing in particular"),
        pytest.param({"place": "after"}, id="after nothing in particular"),
        pytest.param(
            {"place": "first", "session": str(OTHER_SESSION)}, id="an anchor that means nothing"
        ),
        pytest.param({"place": "before", "session": 512}, id="an id that is not a string"),
        pytest.param({"place": "before", "session": "later"}, id="an id that is not an id"),
        pytest.param({"place": "before", "session": str(SESSION)}, id="beside itself"),
    ],
)
async def test_a_drag_that_does_not_say_where_is_refused_rather_than_guessed_at(
    aiohttp_client: AiohttpClientFactory, body: dict[str, object]
) -> None:
    """Strict, and never coerced -- the rule `_requested_model` already set.

    Every one of these is a client with a bug in it. A server that picked
    a sensible interpretation would hide that bug until the day the
    sensible interpretation stopped matching what the client meant, and by
    then the queue would be in an order nobody chose.
    """
    queue = FakeQueue(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(place_url(), json=body)

    assert response.status == 400
    assert queue.placed == []


async def test_a_body_that_is_not_json_is_refused_without_being_echoed_back(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queue = FakeQueue(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(
        place_url(), data="not json at all", headers={"Content-Type": "application/json"}
    )

    assert response.status == 400
    assert (await response.json())["error"] == "malformed request body"
    assert queue.placed == []


async def test_a_drag_with_no_body_at_all_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Unlike a re-queue, where an absent body is the button and means "no choice".

    A reorder with no body has not said what to do, and there is no
    sensible default for where something goes.
    """
    queue = FakeQueue(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(place_url())

    assert response.status == 400
    assert queue.placed == []


# ---------------------------------------------------------------------------
# The quick actions
# ---------------------------------------------------------------------------


async def test_an_administrator_can_reorder_a_whole_guilds_queue_by_a_rule(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queues = FakeQueueOverview(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queues=queues))

    response = await client.post(guild_url(), json={"rule": "many-participants-first"})

    assert response.status == 200
    assert queues.reprioritised == [(GUILD, ANNA, "many-participants-first")]
    assert (await response.json())["order"][0]["session_id"] == str(SESSION)


async def test_the_other_quick_action_the_owner_asked_for_is_there_too(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queues = FakeQueueOverview(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queues=queues))

    response = await client.post(guild_url(), json={"rule": "short-recordings-first"})

    assert response.status == 200


async def test_a_rule_nobody_has_is_refused_before_the_queue_is_touched(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """400, and the message names both what was asked for and what there is.

    The registry is closed and its names are literals of this repository,
    so echoing the mistyped one back discloses nothing -- and running some
    other rule instead would reorder a guild's queue in a way nobody could
    tell from the feature working.
    """
    queues = FakeQueueOverview(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queues=queues))

    response = await client.post(guild_url(), json={"rule": "longest-first"})

    assert response.status == 400
    assert "longest-first" in (await response.json())["error"]
    assert queues.reprioritised == []


async def test_a_quick_action_without_a_rule_is_refused_rather_than_defaulted(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queues = FakeQueueOverview(order=order())
    client = await signed_in(aiohttp_client, build_test_api(queues=queues))

    response = await client.post(guild_url(), json={})

    assert response.status == 400
    assert queues.reprioritised == []


async def test_somebody_who_does_not_administer_the_guild_is_told_it_does_not_exist_either(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(queues=FakeQueueOverview()))

    response = await client.post(guild_url(), json={"rule": "many-participants-first"})

    assert response.status == 404
    assert (await response.json())["error"] == "no such guild"


async def test_a_guild_id_that_is_not_a_number_gets_the_same_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(
        aiohttp_client, build_test_api(queues=FakeQueueOverview(order=order()))
    )

    response = await client.post(
        "/api/guilds/nonsense/queue/priority", json={"rule": "many-participants-first"}
    )

    assert response.status == 404


async def test_nothing_in_between_may_cache_a_queue_order(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """It names which meetings a guild has outstanding, behind a session cookie."""
    client = await signed_in(
        aiohttp_client, build_test_api(queues=FakeQueueOverview(order=order()))
    )

    response = await client.post(guild_url(), json={"rule": "short-recordings-first"})

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_every_id_in_an_order_travels_as_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A JSON number loses a snowflake's last digits; one id shape or none."""
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue(order=order())))

    body = await (await client.post(place_url(), json={"place": "first"})).json()

    assert all(isinstance(row["session_id"], str) for row in body["order"])
    assert all(isinstance(session_id, str) for session_id in body["changed"])
