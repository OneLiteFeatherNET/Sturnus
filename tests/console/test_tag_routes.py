"""The tag endpoints, over real HTTP.

What is tested here is the half that only exists at the HTTP boundary:
the status codes, the shapes a body may and may not have, and that every
handler writes for the *signed-in* person rather than for anybody the
request could name.

Whether the statement scopes is a property of SQL and lives in
`tests/console/test_adapters.py`; when two labels are one label is pure
and lives in `tests/console/test_tags.py`.

The one thing invisible in either: a session id in a path is a number a
caller chose, and no handler may turn it into a write that is not scoped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.session import SessionCookie, SignedSession
from sturnus.console.statistics import TagUse
from sturnus.console.tags import MAX_TAG_CHARS, MAX_TAGS_PER_SESSION
from tests.console.conftest import (
    ANNA,
    BEN,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeReads,
    FakeTags,
    build_test_api,
    now_at,
)

SESSION_COOKIE = "sturnus_session"
SESSION = 4711


def app(reads: FakeReads | None = None, tags: FakeTags | None = None) -> web.Application:
    return build_test_api(
        reads=reads or FakeReads(),
        tags=tags or FakeTags(participants={SESSION: {ANNA}}),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
    )


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory,
    reads: FakeReads | None = None,
    tags: FakeTags | None = None,
    as_user: int = ANNA,
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app(reads, tags))
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


# ---------------------------------------------------------------------------
# Nothing here is readable or writable without a session
# ---------------------------------------------------------------------------


async def test_a_signed_out_caller_may_not_read_the_labels_anybody_uses(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app())
    assert (await client.get("/api/tags")).status == 401


async def test_a_signed_out_caller_may_not_label_a_recording(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Refused before the body is even looked at: an unauthenticated
    write that got as far as validation would report which sessions
    exist through the difference between a 400 and a 404."""
    client = await aiohttp_client(app())
    response = await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": ["retro"]})
    assert response.status == 401


# ---------------------------------------------------------------------------
# Reading the labels one person uses
# ---------------------------------------------------------------------------


async def test_the_labels_offered_are_the_signed_in_persons_own(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The property that cannot be seen in a response body: the handler
    asked about the person in the cookie and about nobody else."""
    reads = FakeReads(tags=(TagUse("retro", 2),))
    client = await signed_in(aiohttp_client, reads=reads, as_user=BEN)

    response = await client.get("/api/tags")

    assert response.status == 200
    assert await response.json() == {"tags": [{"tag": "retro", "sessions": 2}]}
    assert reads.asked_for == [BEN]


async def test_somebody_who_has_never_tagged_anything_gets_an_empty_list(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    assert await (await client.get("/api/tags")).json() == {"tags": []}


async def test_the_labels_someone_uses_are_never_cached(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """They say how somebody thinks about their meetings, and they change
    the moment a chip is edited."""
    client = await signed_in(aiohttp_client, reads=FakeReads(tags=(TagUse("retro", 1),)))
    response = await client.get("/api/tags")
    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# Writing them
# ---------------------------------------------------------------------------


async def test_a_participant_may_label_their_own_recording(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    tags = FakeTags(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, tags=tags)

    response = await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": ["Retro"]})

    assert response.status == 200
    assert await response.json() == {"tags": ["retro"]}


async def test_the_write_names_the_signed_in_person_and_not_the_request(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A handler that took the owner from anywhere but the cookie would
    let one person write labels onto another's account."""
    tags = FakeTags(participants={SESSION: {BEN}})
    client = await signed_in(aiohttp_client, tags=tags, as_user=BEN)

    await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": ["retro"]})

    assert tags.written == [(SESSION, BEN, ("retro",))]


async def test_labelling_a_meeting_you_were_not_in_is_a_404(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The same answer as "no such session", deliberately: a 403 would
    confirm that a meeting exists to somebody just established as having
    had no part in it."""
    client = await signed_in(aiohttp_client, tags=FakeTags(participants={SESSION: {BEN}}))

    response = await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": ["retro"]})

    assert response.status == 404


async def test_a_session_id_that_is_not_a_number_is_a_404_rather_than_a_400(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """It names no session either, and a distinct answer would only tell
    the caller which ids are well formed."""
    client = await signed_in(aiohttp_client)
    response = await client.put("/api/sessions/not-a-number/tags", json={"tags": []})
    assert response.status == 404


async def test_what_comes_back_is_what_was_stored_rather_than_what_was_sent(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Two chips somebody typed may be one label. A client shown its own
    input back would keep displaying a tag the database does not have."""
    client = await signed_in(aiohttp_client)

    response = await client.put(
        f"/api/sessions/{SESSION}/tags", json={"tags": ["Retro", "  retro"]}
    )

    assert await response.json() == {"tags": ["retro"]}


async def test_clearing_every_label_is_an_ordinary_write(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": []})
    assert response.status == 200
    assert await response.json() == {"tags": []}


# ---------------------------------------------------------------------------
# Bodies that are not a set of labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tags": "retro"},
        {"tags": {"retro": True}},
        ["retro"],
        "retro",
    ],
)
async def test_a_body_that_is_not_a_set_of_labels_is_refused(
    aiohttp_client: AiohttpClientFactory, body: object
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(f"/api/sessions/{SESSION}/tags", json=body)
    assert response.status == 400


async def test_a_body_that_is_not_json_at_all_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(
        f"/api/sessions/{SESSION}/tags",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 400


async def test_a_label_longer_than_the_limit_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(
        f"/api/sessions/{SESSION}/tags", json={"tags": ["x" * (MAX_TAG_CHARS + 1)]}
    )
    assert response.status == 400


async def test_more_labels_than_a_recording_may_carry_are_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client)
    response = await client.put(
        f"/api/sessions/{SESSION}/tags",
        json={"tags": [f"tag-{index}" for index in range(MAX_TAGS_PER_SESSION + 1)]},
    )
    assert response.status == 400


async def test_a_refusal_never_echoes_the_label_that_caused_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """No user input is reflected into a response body. An endpoint that
    echoes what it was handed is an XSS sink for whatever renders its
    errors, however careful today's client happens to be."""
    client = await signed_in(aiohttp_client)
    attack = "<script>alert(1)</script>" + "x" * MAX_TAG_CHARS

    response = await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": [attack]})

    assert response.status == 400
    assert "script" not in (await response.text())


async def test_a_refused_body_writes_nothing(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The refusal happens before the write, so a bad chip in a set does
    not leave the set half applied."""
    tags = FakeTags(participants={SESSION: {ANNA}})
    client = await signed_in(aiohttp_client, tags=tags)

    await client.put(f"/api/sessions/{SESSION}/tags", json={"tags": ["retro", "   "]})

    assert tags.written == []
