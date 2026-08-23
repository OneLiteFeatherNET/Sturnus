"""Who may withdraw somebody else's consent, and what they are told about it.

The authorisation rule itself lives in `ConsoleConsentDirectory` and is
pinned against the real database in `test_consent_directory.py`. What is
pinned here is the other half, which no adapter test can see: that each
handler passes the *signed-in* person's id as `requested_by` rather than
anything taken from the URL, that a refusal is the same refusal for every
reason there is to refuse, and that the shapes going over the wire are the
ones the console reads.

The one behaviour worth stating twice is the audit line. `consent` records
that a revocation happened and never who performed it, so
`console.consent_revoked` is the only place the pair "who withdrew whose
consent" is ever written down. A test that let it be dropped would let the
feature ship without the thing that makes it defensible.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.adapters import ALREADY_REVOKED, NO_CONSENT_ON_RECORD
from sturnus.console.app import SESSION_COOKIE
from sturnus.console.paging import DEFAULT_PAGE_SIZE, InvalidPage, page_request
from sturnus.console.ports import ConsentHolder, ConsentPage, PersonRevocation, RevocationOutcome
from sturnus.console.routes_consent import MAX_REVOCATIONS_PER_REQUEST
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.observability.events import Event
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeConsents,
    build_test_api,
)

GRANTED = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)
REVOKED_AT = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
#: Two more people than `conftest` names, because a batch of one proves
#: nothing about a batch: the ordering of the outcomes and the per-person
#: audit lines are only visible with several.
CARL, DORA = 300, 400


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def list_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/consents"


def revoke_url(guild_id: int | str = GUILD, discord_user_id: int | str = BEN) -> str:
    return f"/api/guilds/{guild_id}/consents/{discord_user_id}/revoke"


def holder(**over: object) -> ConsentHolder:
    base: dict[str, object] = {
        "discord_user_id": BEN,
        "display_name": "ben",
        "policy_version": "2026-01",
        "granted_at": GRANTED,
        "revoked_at": None,
        "active": True,
        "recordings_with_audio": 3,
        "scope": "audio",
    }
    base.update(over)
    return ConsentHolder(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_an_administrator_sees_who_has_consented_in_their_guild(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(holders=[holder()])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.get(list_url())

    assert response.status == 200
    body = await response.json()
    assert [entry["display_name"] for entry in body["consents"]] == ["ben"]


async def test_the_listing_asks_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The property no response body can show.

    The whole authorisation model is that the id reaching the directory is
    the one out of the signed cookie. A handler that passed anything else
    would look identical from outside, right up until the day the URL
    carried a user id too.
    """
    consents = FakeConsents(holders=[])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents), as_user=BEN)

    await client.get(list_url())

    assert consents.listed == [(GUILD, BEN)]


async def test_somebody_who_does_not_administer_the_guild_is_told_it_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # 404 and not 403: the list names who agreed to be recorded and when,
    # and a 403 would confirm such a list exists here to somebody just
    # established as having no business with it.
    client = await signed_in(aiohttp_client, build_test_api(consents=FakeConsents()))

    response = await client.get(list_url())

    assert response.status == 404
    assert (await response.json())["error"] == "no such guild"


async def test_a_guild_id_that_is_not_a_number_is_the_same_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(
        aiohttp_client, build_test_api(consents=FakeConsents(holders=[holder()]))
    )

    response = await client.get(list_url("not-a-guild"))

    assert response.status == 404


async def test_signing_out_is_the_end_of_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(consents=FakeConsents(holders=[holder()])))

    assert (await client.get(list_url())).status == 401
    assert (await client.post(revoke_url())).status == 401


# ---------------------------------------------------------------------------
# What the listing says
# ---------------------------------------------------------------------------


async def test_every_discord_id_travels_as_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # A snowflake exceeds JavaScript's safe integer range, where a JSON
    # number silently loses its last digits and produces an id that looks
    # right and names nobody.
    big = 1234567890123456789
    consents = FakeConsents(holders=[holder(discord_user_id=big)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    body = await (await client.get(list_url())).json()

    assert body["guild_id"] == str(GUILD)
    assert body["consents"][0]["discord_user_id"] == str(big)


async def test_a_consent_ended_by_a_policy_bump_is_reported_as_inactive(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`active` is sent rather than left to the client to work out.

    A grant names the version it was given under, so a guild that moved
    its `policy_version` on has consents with no `revoked_at` and no
    force. A console deriving `active` from the two dates would report
    this person as consenting -- a second implementation of
    `is_consent_active` that agrees with the recorder until one of them
    changes.
    """
    consents = FakeConsents(holders=[holder(active=False)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    entry = (await (await client.get(list_url())).json())["consents"][0]

    assert entry["revoked_at"] is None
    assert entry["active"] is False


async def test_a_person_with_no_recorded_meeting_yet_has_no_name(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(holders=[holder(display_name=None)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    entry = (await (await client.get(list_url())).json())["consents"][0]

    assert entry["display_name"] is None


async def test_the_listing_says_how_many_recordings_survive_a_revocation(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # The number that says what revoking will *not* do. Without it an
    # administrator would reasonably assume withdrawing consent erases
    # what was recorded under it.
    consents = FakeConsents(holders=[holder(recordings_with_audio=7)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    entry = (await (await client.get(list_url())).json())["consents"][0]

    assert entry["recordings_with_audio"] == 7


async def test_a_guild_where_nobody_has_consented_is_an_empty_list(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # Empty and not 404: the difference between "nobody here has consented"
    # and "this is not your guild" is the whole point of the page.
    consents = FakeConsents(holders=[])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.get(list_url())

    assert response.status == 200
    assert (await response.json())["consents"] == []


async def test_the_listing_is_never_cached(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(holders=[holder()])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.get(list_url())

    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# Withdrawing it
# ---------------------------------------------------------------------------


async def test_an_administrator_may_withdraw_somebody_else_s_consent(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(
        outcome=RevocationOutcome(revoked=True, refusal=None, effective_at=REVOKED_AT)
    )
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url())

    assert response.status == 200
    assert (await response.json()) == {
        "revoked": True,
        "refusal": None,
        "effective_at": REVOKED_AT.isoformat(),
        "recordings_from_effective_at": 0,
    }


async def test_the_revocation_names_the_signed_in_administrator_as_the_actor(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(outcome=RevocationOutcome(revoked=True, refusal=None))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents), as_user=ANNA)

    await client.post(revoke_url(discord_user_id=BEN))

    # The subject comes from the URL and the actor comes from the cookie,
    # and mixing the two up is the one mistake this endpoint can make that
    # nothing else would catch.
    assert consents.revoked == [(GUILD, BEN, ANNA)]


async def test_a_second_revocation_is_a_conflict_rather_than_a_success(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """409, not 400 and not 200.

    The request is well formed and the person is real; what is wrong is
    the state they are already in. Answering 200 would tell an
    administrator they had just achieved something that had already
    happened, and 400 would send them looking for a mistake in a request
    that has none.
    """
    consents = FakeConsents(outcome=RevocationOutcome(revoked=False, refusal=ALREADY_REVOKED))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url())

    assert response.status == 409
    assert (await response.json())["refusal"] == ALREADY_REVOKED


async def test_revoking_a_consent_nobody_ever_gave_says_which_of_the_two_it_was(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(outcome=RevocationOutcome(revoked=False, refusal=NO_CONSENT_ON_RECORD))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url())

    assert response.status == 409
    assert (await response.json())["refusal"] == NO_CONSENT_ON_RECORD


async def test_a_revocation_in_a_guild_this_person_does_not_administer_is_a_404(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(consents=FakeConsents()))

    response = await client.post(revoke_url())

    assert response.status == 404


async def test_a_subject_that_is_not_a_number_never_reaches_the_directory(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(outcome=RevocationOutcome(revoked=True, refusal=None))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url(discord_user_id="nobody"))

    assert response.status == 404
    assert consents.revoked == []


# ---------------------------------------------------------------------------
# The audit line
# ---------------------------------------------------------------------------


def _events(caplog: pytest.LogCaptureFixture, event: Event) -> list[logging.LogRecord]:
    """Every record carrying one event name.

    `log_event` puts the name and the fields in `extra` under
    `sturnus_event` and `sturnus_fields` rather than spreading the fields
    across the record, which is what lets `scrub_fields` rebuild them from
    the registry on the way out. Reading them back the same way is how
    these tests stay tests of the sanctioned call shape.
    """
    return [r for r in caplog.records if getattr(r, "sturnus_event", None) == str(event)]


def _fields(record: logging.LogRecord) -> dict[str, object]:
    fields = getattr(record, "sturnus_fields", None)
    assert isinstance(fields, dict)
    return fields


async def test_a_revocation_is_logged_with_who_did_it_to_whom(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The only record there will ever be that this happened.

    `consent.revoked_at` says a revocation occurred and never who
    performed it, so this line is the entire answer to "who withdrew whose
    consent". WARNING because a third party acting on somebody else's
    consent is heavier than anything else the console offers.
    """
    consents = FakeConsents(outcome=RevocationOutcome(revoked=True, refusal=None))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents), as_user=ANNA)

    with caplog.at_level(logging.INFO):
        await client.post(revoke_url(discord_user_id=BEN))

    lines = _events(caplog, Event.CONSOLE_CONSENT_REVOKED)
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    fields = _fields(lines[0])
    assert fields["guild_id"] == GUILD
    assert fields["discord_user_id"] == BEN
    assert fields["requested_by"] == ANNA


async def test_a_refused_revocation_is_logged_as_the_feature_working(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # INFO, not WARNING: two administrators reaching for the same name is
    # not an incident. `reason` is what distinguishes it from a revocation
    # that did something.
    consents = FakeConsents(outcome=RevocationOutcome(revoked=False, refusal=ALREADY_REVOKED))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    with caplog.at_level(logging.INFO):
        await client.post(revoke_url())

    lines = _events(caplog, Event.CONSOLE_CONSENT_REVOKE_REFUSED)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    assert _fields(lines[0])["reason"] == ALREADY_REVOKED


async def test_a_refusal_nobody_was_entitled_to_ask_for_is_not_an_audit_line(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing happened and nothing was authorised, so there is nothing to
    # audit. A line here would let anybody with a session fill the log with
    # names of their choosing.
    client = await signed_in(aiohttp_client, build_test_api(consents=FakeConsents()))

    with caplog.at_level(logging.INFO):
        await client.post(revoke_url())

    assert not _events(caplog, Event.CONSOLE_CONSENT_REVOKED)
    assert not _events(caplog, Event.CONSOLE_CONSENT_REVOKE_REFUSED)


# ---------------------------------------------------------------------------
# The effective instant
# ---------------------------------------------------------------------------


def revoked() -> RevocationOutcome:
    return RevocationOutcome(revoked=True, refusal=None, effective_at=REVOKED_AT)


async def test_a_revocation_that_sends_no_body_still_works(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The compatibility promise, as a test.

    `effective_at` is optional so that a console which has never heard of
    it goes on working unchanged -- which is what "absent means now"
    buys, and the reason it is not a required field with a sentinel.
    """
    consents = FakeConsents(outcome=revoked())
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    assert (await client.post(revoke_url())).status == 200
    assert consents.effective_instants == [None]


async def test_an_administrator_may_schedule_a_withdrawal_for_a_named_instant(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(outcome=revoked())
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url(), json={"effective_at": "2026-09-30T23:59:00+00:00"})

    assert response.status == 200
    assert consents.effective_instants == [datetime(2026, 9, 30, 23, 59, tzinfo=UTC)]


async def test_an_instant_with_an_offset_is_normalised_to_utc(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Every other time in this system is UTC. A `+02:00` that survived to
    the column would be the only one that was not, and the comparison
    against `granted_at` would be the first thing to notice."""
    consents = FakeConsents(outcome=revoked())
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    await client.post(revoke_url(), json={"effective_at": "2026-09-30T12:00:00+02:00"})

    assert consents.effective_instants == [datetime(2026, 9, 30, 10, 0, tzinfo=UTC)]


@pytest.mark.parametrize(
    "value",
    [
        # Naive: a different instant in every zone that reads it, and a
        # revocation is not a thing to be approximately dated.
        "2026-09-30T23:59:00",
        "next tuesday",
        "2026-13-45T00:00:00Z",
        7,
    ],
)
async def test_an_instant_that_names_no_moment_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory, value: object
) -> None:
    """400 rather than 409: a string that names no moment is a malformed
    request, not a state that refuses one."""
    consents = FakeConsents(outcome=revoked())
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url(), json={"effective_at": value})

    assert response.status == 400
    assert consents.revoked == []


async def test_a_backdated_revocation_reports_what_it_did_not_delete(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`/audio purge` and the retention sweep remain the only two things
    that delete audio. This number is what lets the console offer the
    first of them rather than quietly taking it."""
    consents = FakeConsents(
        outcome=RevocationOutcome(
            revoked=True,
            refusal=None,
            effective_at=REVOKED_AT,
            recordings_from_effective_at=4,
        )
    )
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    body = await (await client.post(revoke_url())).json()

    assert body["recordings_from_effective_at"] == 4
    assert body["effective_at"] == REVOKED_AT.isoformat()


async def test_the_audit_line_says_whether_the_instant_was_chosen(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An administrator back-dating a withdrawal to last March is making a
    claim about months of recordings that already exist; one clicking
    "withdraw" is stopping something tomorrow. Both leave a perfectly
    ordinary date in `revoked_at`, and by the time anybody reads the row
    only this field can tell the two acts apart.
    """
    consents = FakeConsents(outcome=revoked())
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    with caplog.at_level(logging.INFO):
        await client.post(revoke_url())
        await client.post(revoke_url(), json={"effective_at": "2026-03-01T00:00:00+00:00"})

    lines = _events(caplog, Event.CONSOLE_CONSENT_REVOKED)
    assert [_fields(line)["effective_at_given"] for line in lines] == [False, True]


# ---------------------------------------------------------------------------
# One page at a time
# ---------------------------------------------------------------------------


async def test_the_roster_arrives_a_page_at_a_time_with_the_count_beside_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The shape `GET /api/sessions` already established, reused unchanged.

    A guild with four hundred participants used to send four hundred
    records to draw the first ten, and the console then sorted them in the
    browser. The window and the total travel together for the reason
    `SessionPage` gives: a count fetched separately can be one grant older
    than the rows.
    """
    consents = FakeConsents(page=ConsentPage(holders=(holder(),), total=412, limit=20, offset=0))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    body = await (await client.get(list_url())).json()

    assert body["total"] == 412
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert [entry["display_name"] for entry in body["consents"]] == ["ben"]


async def test_a_roster_asked_for_without_a_window_gets_the_first_page(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A link to the endpoint means "the first page", exactly as it does
    for the recordings list. The default is `sturnus.console.paging`'s
    rather than a second one invented here."""
    page = ConsentPage(holders=(), total=0, limit=DEFAULT_PAGE_SIZE, offset=0)
    consents = FakeConsents(page=page)
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    await client.get(list_url())

    assert consents.windows == [(DEFAULT_PAGE_SIZE, 0)]


async def test_the_window_the_caller_named_reaches_the_directory(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(page=ConsentPage(holders=(), total=0, limit=5, offset=40))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    await client.get(list_url() + "?limit=5&offset=40")

    assert consents.windows == [(5, 40)]


@pytest.mark.parametrize(
    "query,parameter,value",
    [
        ("?limit=0", "limit", "0"),
        ("?limit=5000", "limit", "5000"),
        ("?limit=ten", "limit", "ten"),
        ("?offset=-1", "offset", "-1"),
    ],
)
async def test_a_window_that_cannot_be_served_is_refused_rather_than_clamped(
    aiohttp_client: AiohttpClientFactory, query: str, parameter: str, value: str
) -> None:
    """`?limit=5000` is a client bug or somebody pulling a whole roster in
    one response, and silently answering with a hundred tells neither of
    them anything.

    The sentence is `sturnus.console.paging`'s own, passed through rather
    than reworded here: one API that refuses a window two different ways
    is two sentences for a console to translate. It names the rule and
    never the value that broke it, because no user input is reflected
    into a response body.
    """
    consents = FakeConsents(page=ConsentPage(holders=(), total=0, limit=20, offset=0))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.get(list_url() + query)

    assert response.status == 400
    assert consents.windows == []
    with pytest.raises(InvalidPage) as refusal:
        page_request(*(value, None) if parameter == "limit" else (None, value))
    assert (await response.json())["error"] == str(refusal.value)


async def test_a_window_past_the_end_of_the_roster_is_an_empty_page_and_not_a_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # What a bookmark to page five looks like once people have left. The
    # total travelling with it is what lets the console say so rather than
    # claim the guild has nobody.
    consents = FakeConsents(page=ConsentPage(holders=(), total=3, limit=20, offset=100))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.get(list_url() + "?offset=100")

    assert response.status == 200
    body = await response.json()
    assert body["consents"] == []
    assert body["total"] == 3


# ---------------------------------------------------------------------------
# Withdrawing several at once
# ---------------------------------------------------------------------------


def bulk_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/consents/revoke"


def revocation(discord_user_id: int = BEN, **over: object) -> PersonRevocation:
    outcome: dict[str, object] = {"revoked": True, "refusal": None, "effective_at": REVOKED_AT}
    outcome.update(over)
    return PersonRevocation(
        discord_user_id=discord_user_id,
        outcome=RevocationOutcome(**outcome),  # type: ignore[arg-type]
    )


async def test_an_administrator_may_withdraw_several_consents_in_one_request(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(revocations=[revocation(BEN), revocation(CARL)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(bulk_url(), json={"discord_user_ids": [str(BEN), str(CARL)]})

    assert response.status == 200
    assert consents.batches == [(GUILD, (BEN, CARL), ANNA)]


async def test_a_mixed_batch_is_answered_person_by_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The whole reason this endpoint has a body worth reading.

    Some of the named people have no consent on record, some were
    withdrawn while the page was open, some are fine. One status code for
    all three would be lying to somebody, so the status describes the
    request and the body describes each person.
    """
    consents = FakeConsents(
        revocations=[
            revocation(BEN, recordings_from_effective_at=4),
            revocation(CARL, revoked=False, refusal=ALREADY_REVOKED, effective_at=None),
            revocation(DORA, revoked=False, refusal=NO_CONSENT_ON_RECORD, effective_at=None),
        ]
    )
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(
        bulk_url(), json={"discord_user_ids": [str(BEN), str(CARL), str(DORA)]}
    )

    assert response.status == 200
    assert (await response.json()) == {
        "guild_id": str(GUILD),
        "requested": 3,
        "revoked": 1,
        "refused": 2,
        "outcomes": [
            {
                "discord_user_id": str(BEN),
                "revoked": True,
                "refusal": None,
                "effective_at": REVOKED_AT.isoformat(),
                "recordings_from_effective_at": 4,
            },
            {
                "discord_user_id": str(CARL),
                "revoked": False,
                "refusal": ALREADY_REVOKED,
                "effective_at": None,
                "recordings_from_effective_at": 0,
            },
            {
                "discord_user_id": str(DORA),
                "revoked": False,
                "refusal": NO_CONSENT_ON_RECORD,
                "effective_at": None,
                "recordings_from_effective_at": 0,
            },
        ],
    }


async def test_a_batch_where_nothing_could_be_withdrawn_is_still_a_complete_answer(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """200, even when every person in it was refused.

    Not 409. The console's `useApi` strips the body off every failed
    request by design -- `ApiError` keeps the status and the path and
    nothing else -- so any status outside 2xx would destroy the per-person
    outcomes this endpoint exists to deliver, and an administrator would
    be told "something was refused" with no way to learn which name.
    """
    consents = FakeConsents(
        revocations=[revocation(BEN, revoked=False, refusal=ALREADY_REVOKED, effective_at=None)]
    )
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(bulk_url(), json={"discord_user_ids": [str(BEN)]})

    assert response.status == 200
    body = await response.json()
    assert body["revoked"] == 0
    assert body["refused"] == 1


async def test_a_bulk_withdrawal_names_the_signed_in_administrator_as_the_actor(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # The subjects come from the body and the actor comes from the cookie.
    consents = FakeConsents(revocations=[revocation(CARL)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents), as_user=BEN)

    await client.post(bulk_url(), json={"discord_user_ids": [str(CARL)]})

    assert consents.batches == [(GUILD, (CARL,), BEN)]


async def test_a_bulk_withdrawal_in_a_guild_this_person_does_not_administer_is_a_404(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # The same refusal as "no such guild", for the same reason: a 403
    # would confirm the roster exists to somebody with no business with it.
    client = await signed_in(aiohttp_client, build_test_api(consents=FakeConsents()))

    response = await client.post(bulk_url(), json={"discord_user_ids": [str(BEN)]})

    assert response.status == 404
    assert (await response.json())["error"] == "no such guild"


async def test_a_bulk_withdrawal_takes_the_same_instant_the_single_one_takes(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(revocations=[revocation(BEN)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    await client.post(
        bulk_url(),
        json={"discord_user_ids": [str(BEN)], "effective_at": "2026-09-30T12:00:00+02:00"},
    )

    assert consents.batch_instants == [datetime(2026, 9, 30, 10, 0, tzinfo=UTC)]


async def test_a_batch_that_names_no_instant_means_now(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(revocations=[revocation(BEN)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    await client.post(bulk_url(), json={"discord_user_ids": [str(BEN)]})

    assert consents.batch_instants == [None]


async def test_an_instant_that_names_no_moment_refuses_the_whole_batch(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(revocations=[revocation(BEN)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(
        bulk_url(), json={"discord_user_ids": [str(BEN)], "effective_at": "next tuesday"}
    )

    assert response.status == 400
    assert consents.batches == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"discord_user_ids": []},
        {"discord_user_ids": "100"},
        {"discord_user_ids": {"0": "100"}},
    ],
)
async def test_a_batch_that_names_nobody_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory, body: object
) -> None:
    """400 rather than an empty success. A request naming nobody is a
    client that built its body wrongly, and answering "nothing happened,
    as you asked" would hide the bug until somebody noticed a roster that
    never changes."""
    consents = FakeConsents(revocations=[])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(bulk_url(), json=body)

    assert response.status == 400
    assert consents.batches == []


@pytest.mark.parametrize("entry", [100, None, "not-a-snowflake", ""])
async def test_an_entry_that_names_no_person_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory, entry: object
) -> None:
    """Every snowflake is a string in JSON, here as everywhere else. A
    number is refused rather than coerced: an id that survived a
    JavaScript `JSON.parse` as a number has already lost its last digits,
    and withdrawing the consent of whoever the rounded id names is worse
    than refusing the request."""
    consents = FakeConsents(revocations=[])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(bulk_url(), json={"discord_user_ids": [entry]})

    assert response.status == 400
    assert consents.batches == []


async def test_a_batch_bigger_than_the_maximum_is_refused_before_anything_is_written(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A request naming ten thousand people is a denial of service with a
    valid session. The bound is `paging.MAX_PAGE_SIZE`, so the largest
    batch is exactly one page of the roster it is withdrawn from."""
    consents = FakeConsents(revocations=[])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    too_many = [str(1000 + n) for n in range(MAX_REVOCATIONS_PER_REQUEST + 1)]
    response = await client.post(bulk_url(), json={"discord_user_ids": too_many})

    assert response.status == 400
    assert str(MAX_REVOCATIONS_PER_REQUEST) in (await response.json())["error"]
    assert consents.batches == []


async def test_a_batch_of_exactly_the_maximum_is_served(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    people = [1000 + n for n in range(MAX_REVOCATIONS_PER_REQUEST)]
    consents = FakeConsents(revocations=[revocation(person) for person in people])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(
        bulk_url(), json={"discord_user_ids": [str(person) for person in people]}
    )

    assert response.status == 200
    assert len((await response.json())["outcomes"]) == MAX_REVOCATIONS_PER_REQUEST


async def test_naming_the_same_person_twice_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Refused rather than de-duplicated. The outcomes are one per name in
    the order they were named, and silently collapsing two entries into
    one would hand the console a shorter list than it sent -- which it
    could only reconcile by matching on id, which is exactly the work this
    shape exists to spare it."""
    consents = FakeConsents(revocations=[])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(
        bulk_url(), json={"discord_user_ids": [str(BEN), str(CARL), str(BEN)]}
    )

    assert response.status == 400
    assert consents.batches == []


async def test_signing_out_ends_the_bulk_endpoint_too(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(consents=FakeConsents(revocations=[revocation()])))

    assert (await client.post(bulk_url(), json={"discord_user_ids": [str(BEN)]})).status == 401


async def test_a_batch_for_a_guild_id_that_is_not_a_number_never_reaches_the_directory(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakeConsents(revocations=[revocation()])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(bulk_url("not-a-guild"), json={"discord_user_ids": [str(BEN)]})

    assert response.status == 404
    assert consents.batches == []


# ---------------------------------------------------------------------------
# The audit line, one per person
# ---------------------------------------------------------------------------


async def test_every_person_in_a_batch_gets_their_own_audit_line(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One line per person, never one line saying "9 people".

    `consent.revoked_at` records no actor, so these lines are the entire
    answer to "was this person's consent withdrawn, and by whom". A
    summary line alone could not answer it without somebody first knowing
    which batch that person was in.
    """
    consents = FakeConsents(revocations=[revocation(BEN), revocation(CARL)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents), as_user=ANNA)

    with caplog.at_level(logging.INFO):
        await client.post(bulk_url(), json={"discord_user_ids": [str(BEN), str(CARL)]})

    lines = _events(caplog, Event.CONSOLE_CONSENT_REVOKED)
    assert [line.levelno for line in lines] == [logging.WARNING, logging.WARNING]
    assert [_fields(line)["discord_user_id"] for line in lines] == [BEN, CARL]
    assert {_fields(line)["requested_by"] for line in lines} == {ANNA}
    assert {_fields(line)["guild_id"] for line in lines} == {GUILD}


async def test_a_person_in_a_batch_who_could_not_be_withdrawn_is_logged_as_refused(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The same two events a single revocation emits, per person, so a
    # query over `console.consent_revoked` answers the same question
    # whether a withdrawal was one of one or one of nine.
    consents = FakeConsents(
        revocations=[
            revocation(BEN),
            revocation(CARL, revoked=False, refusal=NO_CONSENT_ON_RECORD, effective_at=None),
        ]
    )
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    with caplog.at_level(logging.INFO):
        await client.post(bulk_url(), json={"discord_user_ids": [str(BEN), str(CARL)]})

    refused = _events(caplog, Event.CONSOLE_CONSENT_REVOKE_REFUSED)
    assert len(refused) == 1
    assert refused[0].levelno == logging.INFO
    assert _fields(refused[0])["discord_user_id"] == CARL
    assert _fields(refused[0])["reason"] == NO_CONSENT_ON_RECORD


async def test_the_batch_itself_is_logged_beside_the_per_person_lines(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beside, never instead of.

    The per-person lines cannot say whether this was one decision or nine
    separate ones, because nine lines from a batch and nine lines from
    nine clicks are identical. That is the one fact this line adds, and it
    carries no name -- the names are on the lines above it.
    """
    consents = FakeConsents(
        revocations=[
            revocation(BEN),
            revocation(CARL, revoked=False, refusal=ALREADY_REVOKED, effective_at=None),
        ]
    )
    client = await signed_in(aiohttp_client, build_test_api(consents=consents), as_user=ANNA)

    with caplog.at_level(logging.INFO):
        await client.post(bulk_url(), json={"discord_user_ids": [str(BEN), str(CARL)]})

    lines = _events(caplog, Event.CONSOLE_CONSENT_BULK_REVOKED)
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert _fields(lines[0]) == {
        "guild_id": GUILD,
        "requested_by": ANNA,
        "count": 2,
        "revoked": 1,
        "refused": 1,
    }


async def test_the_batch_audit_lines_say_whether_the_instant_was_chosen(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The same field the single endpoint records, for the same reason: a
    # back-dated batch is a claim about recordings that already exist, and
    # `revoked_at` looks like an ordinary date either way.
    consents = FakeConsents(revocations=[revocation(BEN)])
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    with caplog.at_level(logging.INFO):
        await client.post(bulk_url(), json={"discord_user_ids": [str(BEN)]})
        await client.post(
            bulk_url(),
            json={"discord_user_ids": [str(BEN)], "effective_at": "2026-03-01T00:00:00+00:00"},
        )

    lines = _events(caplog, Event.CONSOLE_CONSENT_REVOKED)
    assert [_fields(line)["effective_at_given"] for line in lines] == [False, True]


async def test_a_batch_nobody_was_entitled_to_ask_for_is_not_an_audit_line(
    aiohttp_client: AiohttpClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing happened and nothing was authorised. A line here would let
    # anybody with a session fill the audit log with a hundred names of
    # their choosing per request.
    client = await signed_in(aiohttp_client, build_test_api(consents=FakeConsents()))

    with caplog.at_level(logging.INFO):
        await client.post(bulk_url(), json={"discord_user_ids": [str(BEN)]})

    assert not _events(caplog, Event.CONSOLE_CONSENT_REVOKED)
    assert not _events(caplog, Event.CONSOLE_CONSENT_REVOKE_REFUSED)
    assert not _events(caplog, Event.CONSOLE_CONSENT_BULK_REVOKED)
