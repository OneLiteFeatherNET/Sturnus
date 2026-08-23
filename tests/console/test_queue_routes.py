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
from sturnus.console.ports import (
    GuildQueue,
    QueuedSession,
    QueueSnapshot,
    QueueSpeaker,
    RequeueOutcome,
)
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.domain import transcription_models
from sturnus.domain.transcription_models import FALLBACK
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    SESSION,
    T0,
    AiohttpClientFactory,
    FakeAdmins,
    FakeQueue,
    FakeQueueOverview,
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


def models_url() -> str:
    return "/api/models"


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
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None, FALLBACK))
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
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None, FALLBACK))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue), as_user=BEN)
    await client.post(requeue_url())
    assert queue.requeued == [(SESSION, BEN, transcription_models.FALLBACK)]


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
    queue = FakeQueue(
        snapshot=snapshot(), outcome=RequeueOutcome(True, (ANNA,), (), None, FALLBACK)
    )
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
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (BEN,), None, FALLBACK))
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
    queue = FakeQueue(outcome=RequeueOutcome(False, (), (), reason, None))
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
        outcome=RequeueOutcome(True, (big,), (), None, FALLBACK),
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


# ---------------------------------------------------------------------------
# The guild-wide view: what is outstanding, and where
# ---------------------------------------------------------------------------


def queued(**over: object) -> QueuedSession:
    base: dict[str, object] = {
        "id": SESSION,
        "channel_id": 555,
        "channel_name": "meeting",
        "started_at": T0,
        "ended_at": None,
        "status": "closed",
        "document_url": None,
        "pending": 2,
        "running": 1,
        "done": 0,
        "dead": 0,
        "priority": 0,
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


def guild_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/queue"


async def test_an_administrator_sees_what_their_guild_still_owes(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    overview = FakeQueueOverview(queue=guild_queue())
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    response = await client.get(guild_url())

    assert response.status == 200
    body = await response.json()
    assert body["counts"] == {"pending": 2, "running": 1, "done": 40, "dead": 1}
    assert body["sessions"][0]["counts"]["pending"] == 2


async def test_the_overview_says_where_each_session_sits_in_the_queue(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The number an administrator just wrote has to come back, or the page
    cannot render the order it set."""
    overview = FakeQueueOverview(queue=guild_queue(sessions=(queued(priority=2),)))
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    body = await (await client.get(guild_url())).json()

    assert body["sessions"][0]["priority"] == 2


async def test_a_session_with_nothing_queued_has_no_place_rather_than_the_ordinary_one(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Null, not zero, and the difference is what a page acts on.

    Zero is the ordinary priority and a real place in the queue. A meeting
    that is still recording has no jobs at all, so it has no place -- and
    a row reported as `0` would be a row offering a drag handle that
    nothing can be reordered about.
    """
    overview = FakeQueueOverview(queue=guild_queue(sessions=(queued(priority=None),)))
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    body = await (await client.get(guild_url())).json()

    assert body["sessions"][0]["priority"] is None


async def test_the_overview_asks_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # The whole authorisation model is that the id reaching the overview
    # comes out of the signed cookie. A handler passing anything else would
    # look identical from outside.
    overview = FakeQueueOverview(queue=guild_queue())
    client = await signed_in(aiohttp_client, build_test_api(queues=overview), as_user=BEN)

    await client.get(guild_url())

    assert overview.asked == [(GUILD, BEN)]


async def test_a_guild_this_person_does_not_administer_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # 404 and not 403, for the reason the session endpoints answer 404: the
    # list names when a guild met and in which channel, and a 403 confirms
    # such a list exists to somebody just established as having no business
    # with it.
    client = await signed_in(aiohttp_client, build_test_api(queues=FakeQueueOverview()))

    response = await client.get(guild_url())

    assert response.status == 404
    assert (await response.json())["error"] == "no such guild"


async def test_a_guild_id_that_is_not_a_number_gets_the_same_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    overview = FakeQueueOverview(queue=guild_queue())
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    assert (await client.get(guild_url("nope"))).status == 404
    # And never reached the overview, so a malformed path cannot be used to
    # find out which guild ids are well formed.
    assert overview.asked == []


async def test_the_overview_needs_a_session_like_every_other_endpoint(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(queues=FakeQueueOverview(queue=guild_queue())))
    assert (await client.get(guild_url())).status == 401


async def test_the_lease_travels_with_the_count_it_produced(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`running_past_lease` is derived from an assumed lease.

    The lease that actually applies is the worker's `job_lease_seconds`,
    which this process cannot see. Sending the number it used is what lets
    the console name it rather than presenting a derived count as a fact --
    the same caveat `/queue status` prints in Discord.
    """
    overview = FakeQueueOverview(queue=guild_queue(running_past_lease=3, lease_seconds=600.0))
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    body = await (await client.get(guild_url())).json()

    assert body["running_past_lease"] == 3
    assert body["lease_seconds"] == 600.0


async def test_a_cut_list_says_that_it_was_cut(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # Otherwise a page showing twenty sessions reads as "there are twenty",
    # which for a guild that has been broken for a month is the opposite of
    # the truth.
    overview = FakeQueueOverview(queue=guild_queue(truncated=True))
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    assert (await (await client.get(guild_url())).json())["truncated"] is True


async def test_a_guild_with_nothing_outstanding_is_an_empty_list(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # Empty and not 404: "everything here is finished" and "this is not
    # your guild" are different answers and must look different.
    overview = FakeQueueOverview(
        queue=guild_queue(pending=0, running=1, sessions=(), oldest_pending_session_ended_at=None)
    )
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    body = await (await client.get(guild_url())).json()

    assert body["sessions"] == []
    assert body["oldest_pending_session_ended_at"] is None


async def test_every_id_in_the_overview_travels_as_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    big = 308_000_000_000_000_001
    overview = FakeQueueOverview(queue=guild_queue(sessions=(queued(channel_id=big),)))
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    body = await (await client.get(guild_url())).json()

    assert body["guild_id"] == str(GUILD)
    assert body["sessions"][0]["channel_id"] == str(big)
    assert body["sessions"][0]["id"] == str(SESSION)


async def test_nothing_in_between_may_cache_the_overview(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    overview = FakeQueueOverview(queue=guild_queue())
    client = await signed_in(aiohttp_client, build_test_api(queues=overview))

    response = await client.get(guild_url())

    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# Which model a re-queue runs
# ---------------------------------------------------------------------------


async def test_a_re_queue_that_names_nothing_asks_for_the_fallback(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The console's existing button sends no body at all, and must not break.

    `None` never reaches the write: it is resolved to a concrete
    registered name at this boundary, so that
    `transcription_job.requested_model` records what was asked for rather
    than that nothing was.
    """
    queue = FakeQueue(
        outcome=RequeueOutcome(True, (ANNA,), (), None, transcription_models.FALLBACK)
    )
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(requeue_url())

    assert response.status == 200
    assert queue.requeued == [(SESSION, ANNA, transcription_models.FALLBACK)]
    assert (await response.json())["model"] == transcription_models.FALLBACK


async def test_an_administrator_may_name_any_registered_model(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The dropdown's whole point: a redo against a different model.

    The name travels into the write and back out in the response, because
    "which model did this redo actually ask for" is the one thing an
    administrator cannot see from anywhere else until a worker picks the
    job up.
    """
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None, "small"))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(requeue_url(), json={"model": "small"})

    assert response.status == 200
    assert queue.requeued == [(SESSION, ANNA, "small")]
    assert (await response.json())["model"] == "small"


async def test_a_model_nobody_has_is_refused_before_a_job_is_ever_touched(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """400 here, not a job that dies four attempts later.

    Before the registry, a typo travelled all the way into
    `WhisperModel(...)`, which raises rather than falling back: the job
    failed, `attempts` climbed, and at `max_attempts` the speaker was
    `dead` with no transcript and no way back except another re-queue. One
    typo cost one recording. The refusal names what is available, because
    there is no second request that would show it.
    """
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None, "small"))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(requeue_url(), json={"model": "large-v4"})

    assert response.status == 400
    assert queue.requeued == [], "the write was reached with a model nobody has"
    error = (await response.json())["error"]
    assert "large-v4" in error
    assert transcription_models.FALLBACK in error


@pytest.mark.parametrize("body", [{"model": 3}, {"model": ["small"]}, {"model": None}])
async def test_a_model_that_is_not_a_string_is_refused_rather_than_coerced(
    body: dict[str, object], aiohttp_client: AiohttpClientFactory
) -> None:
    """No coercion, and `null` is not "I did not choose".

    Omitting the key is how a caller says they made no choice. Sending
    `null` is a client that built a body out of an unset form field, and
    quietly reading it as the fallback would hide that bug for as long as
    the fallback happened to be what they wanted.
    """
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None, "small"))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(requeue_url(), json=body)

    assert response.status == 400
    assert queue.requeued == []


async def test_a_body_that_is_not_json_is_refused_rather_than_ignored(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A caller who meant to name a model must not be told their redo succeeded."""
    queue = FakeQueue(outcome=RequeueOutcome(True, (ANNA,), (), None, "small"))
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(
        requeue_url(), data="model=small", headers={"Content-Type": "application/json"}
    )

    assert response.status == 400
    assert queue.requeued == []


async def test_somebody_who_administers_nothing_cannot_name_a_model_either(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Naming a model is not a way past the administrator rule.

    The endpoint's authorisation *is* the model policy: an administrator
    of the session's guild may choose, and nobody else may -- because
    nobody else may re-queue at all. The request is refused whole rather
    than obeyed with the model discarded, which is what "anybody else gets
    the fallback and may not name one" comes to here. It is still 404 and
    not 403, so the refusal never confirms the session exists.
    """
    client = await signed_in(aiohttp_client, build_test_api(queue=FakeQueue()))

    response = await client.post(requeue_url(), json={"model": "small"})

    assert response.status == 404


async def test_a_refusal_reports_no_model_because_nothing_was_written(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The field says what was stored, and a refused re-queue stored nothing."""
    queue = FakeQueue(
        outcome=RequeueOutcome(False, (), (), "A worker is still holding jobs.", None)
    )
    client = await signed_in(aiohttp_client, build_test_api(queue=queue))

    response = await client.post(requeue_url(), json={"model": "small"})

    assert response.status == 409
    assert (await response.json())["model"] is None


# ---------------------------------------------------------------------------
# The registry, for the dropdown
# ---------------------------------------------------------------------------


async def test_an_administrator_is_told_what_may_be_asked_for(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The list the dropdown is built from, descriptions and all.

    A dropdown that offers seven names and explains none of them is a
    guess: the difference between `tiny` and `large-v3` is hours of worker
    time and whether the transcript is worth reading, and neither is
    legible from the name. The order is the registry's own, which reads
    from "fast and rough" to "slow and right".
    """
    client = await signed_in(aiohttp_client, build_test_api(admins=FakeAdmins({ANNA})))

    response = await client.get(models_url())

    assert response.status == 200
    body = await response.json()
    assert body["fallback"] == transcription_models.FALLBACK
    assert [model["name"] for model in body["models"]] == [
        model.name for model in transcription_models.KNOWN_MODELS
    ]
    for entry in body["models"]:
        assert entry["approximate_size"]
        assert entry["summary"]


async def test_somebody_who_administers_nothing_has_no_use_for_the_registry(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """403 here, where every other refusal in this file is a 404.

    Those 404s exist so that a refusal does not confirm a fact about
    *somebody else* -- that a session ran, that a guild exists. This route
    holds no such fact: it is seven literals from this repository's own
    source. The only thing its refusal discloses is whether the caller
    administers anything, which `/api/me` already tells them about
    themselves.
    """
    client = await signed_in(aiohttp_client, build_test_api(admins=FakeAdmins(by_guild={})))

    assert (await client.get(models_url())).status == 403


async def test_the_registry_cannot_be_read_without_signing_in(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(admins=FakeAdmins({ANNA})))
    assert (await client.get(models_url())).status == 401
