"""Who may re-run a transcription, and what they are told about it.

The authorisation here is a different rule from every other console route,
and the difference is the thing most worth pinning: elsewhere the question
is "was this your meeting", because playing your own recording back is a
use of your own data. A re-queue spends worker time, clears transcripts,
replaces a shared document and re-announces it — so it takes an
administrator of the guild, and `ConsoleQueueControl` makes that check
part of the call rather than something a handler remembers.

The other half is honesty about what a re-queue did. A skipped speaker
must never be folded into the count of re-queued ones: an administrator
told "3 speakers re-queued" and not told a fourth was left alone would
reasonably assume the whole document had been regenerated.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import QueueSnapshot, QueueSpeaker, RequeueOutcome
from sturnus.console.session import SessionCookie, SignedSession
from tests.console.conftest import (
    ANNA,
    BEN,
    SECRET,
    SESSION,
    T0,
    AiohttpClientFactory,
    FakeQueue,
    build_test_api,
)


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def status_url(session_id: int = SESSION) -> str:
    return f"/api/sessions/{session_id}/queue"


def requeue_url(session_id: int = SESSION) -> str:
    return f"/api/sessions/{session_id}/queue/requeue"


def snapshot(**over: object) -> QueueSnapshot:
    base: dict[str, object] = {
        "session_status": "documented",
        "document_url": "https://outline.example/doc/1",
        "speakers": (
            QueueSpeaker(ANNA, "anna", "done", 1, None),
            QueueSpeaker(BEN, "ben", "dead", 3, "AccessDenied on GetObject"),
        ),
        "can_requeue": True,
        "refusal": None,
    }
    base.update(over)
    return QueueSnapshot(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_an_administrator_sees_where_the_transcription_has_got_to(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queue = FakeQueue(snapshot=snapshot())
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))
    response = await client.get(status_url())
    assert response.status == 200

    body = await response.json()
    assert body["session_status"] == "documented"
    assert body["can_requeue"] is True
    assert [speaker["status"] for speaker in body["speakers"]] == ["done", "dead"]
    assert body["speakers"][1]["error"] == "AccessDenied on GetObject"


async def test_somebody_who_does_not_administer_the_guild_is_told_it_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404, not 403, and for the same reason the audio endpoint gives.

    403 would confirm the session exists and roughly when it ran, to
    somebody the system has just decided has no business knowing.
    """
    # The control answers `None` for "no such session" *and* for "not
    # yours", which is the whole point of folding them together there.
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue()))
    assert (await client.get(status_url())).status == 404
    assert (await client.post(requeue_url())).status == 404


async def test_a_request_without_a_session_cannot_reach_the_queue_at_all(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None))
    client = await aiohttp_client(build_test_api(queue=queue))
    assert (await client.post(requeue_url())).status == 401
    # And nothing reached the write.
    assert queue.requeued == []


async def test_the_write_is_told_who_asked_not_what_the_url_said(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The id that authorises is the signed-in one, always.

    There is no way to name a different user in this request, and this
    pins that there never accidentally is one.
    """
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue), as_user=BEN)
    await client.post(requeue_url())
    assert queue.requeued == [(SESSION, BEN)]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/sessions/abc/queue"),
        ("POST", "/api/sessions/abc/queue/requeue"),
    ],
)
async def test_an_id_that_is_not_a_number_names_nothing(
    method: str, path: str, aiohttp_client: AiohttpClientFactory
) -> None:
    queue = FakeQueue(snapshot=snapshot(), outcome=RequeueOutcome(True, (ANNA,), (), None))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))
    response = await client.request(method, path)
    assert response.status == 404
    assert queue.requeued == []


# ---------------------------------------------------------------------------
# What a re-queue says it did
# ---------------------------------------------------------------------------


async def test_a_skipped_speaker_is_never_folded_into_the_re_queued_ones(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The honesty this endpoint is most able to get wrong.

    A speaker whose audio is erased keeps their old transcript, and the
    new document is the redone speakers plus that untouched old text.
    Somebody told "1 speaker re-queued" without being told about the
    second would reasonably assume the whole document had been
    regenerated.
    """
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (BEN,), None))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))
    response = await client.post(requeue_url())
    assert response.status == 200

    body = await response.json()
    assert body["accepted"] is True
    assert body["requeued"] == [str(ANNA)]
    assert body["skipped_erased"] == [str(BEN)]


async def test_a_refusal_is_a_conflict_and_says_what_to_do_about_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """409, not 400: the request is well formed and the session is real.

    What is wrong is the state it is in, which is the distinction a client
    needs to tell "fix your request" from "try again when the queue is
    idle" -- and the reason travels with it, because a button that greys
    out without saying why is a bug report waiting to be filed.
    """
    reason = "A worker is still holding jobs from this session."
    queue = FakeQueue(outcome=RequeueOutcome(False, (), (), reason))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))
    response = await client.post(requeue_url())

    assert response.status == 409
    body = await response.json()
    assert body["accepted"] is False
    assert body["refusal"] == reason


async def test_a_snapshot_that_cannot_be_re_queued_says_so_before_the_button_is_pressed(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The progress view and the write must refuse for the same reasons.

    Both derive from the same `plan_requeue`, so a button disabled here is
    a button whose press would have been refused there -- rather than a
    second, drifting set of conditions.
    """
    queue = FakeQueue(snapshot=snapshot(can_requeue=False, refusal="Not finished yet."))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))
    body = await (await client.get(status_url())).json()
    assert body["can_requeue"] is False
    assert body["refusal"] == "Not finished yet."


async def test_ids_travel_as_strings_so_no_snowflake_loses_its_last_digits(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A Discord snowflake exceeds JavaScript's safe integer range, where a
    JSON number produces an id that looks right and names nobody."""
    big = 308_000_000_000_000_001
    queue = FakeQueue(
        snapshot=snapshot(speakers=(QueueSpeaker(big, "anna", "done", 1, None),)),
        outcome=RequeueOutcome(True, (big,), (), None),
    )
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    status = await (await client.get(status_url())).json()
    assert status["speakers"][0]["discord_user_id"] == str(big)
    applied = await (await client.post(requeue_url())).json()
    assert applied["requeued"] == [str(big)]


async def test_nothing_in_between_may_cache_a_queue_snapshot(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """It names who spoke in a meeting, and it goes stale the moment a
    worker picks a job up."""
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue(snapshot=snapshot())))
    response = await client.get(status_url())
    assert response.headers["Cache-Control"] == "private, no-store"
