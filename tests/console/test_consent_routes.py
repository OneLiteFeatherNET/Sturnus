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
from sturnus.console.ports import ConsentHolder, RevocationOutcome
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
    consents = FakeConsents(outcome=RevocationOutcome(revoked=True, refusal=None))
    client = await signed_in(aiohttp_client, build_test_api(consents=consents))

    response = await client.post(revoke_url())

    assert response.status == 200
    assert (await response.json()) == {"revoked": True, "refusal": None}


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
