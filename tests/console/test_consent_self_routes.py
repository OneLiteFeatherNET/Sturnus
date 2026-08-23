"""What a person may do to their own consent, over the wire.

The rules themselves live in `ConsolePersonalConsents` and are pinned
against the real database in `test_consent_directory.py`. What is pinned
here is the half no adapter test can see: that the id reaching the
directory is the one out of the signed cookie and that there is nowhere
for another one to come from, that a refusal is a 409 and a malformed
request is a 400, and that the shapes going over the wire are the ones
the console will code against.

The one property worth stating in prose: **no path in this module names a
user.** `routes_consent` carries a subject in its URL because an
administrator acts on somebody else; here there is no parameter to
substitute, so there is no version of these endpoints that acts on a third
party. A test cannot demonstrate the absence of a path, but it can
demonstrate that the id used is the session's -- which is what
`test_the_listing_is_of_the_signed_in_person_and_nobody_else` does.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import OwnConsent, RevocationOutcome, ScopeOutcome
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.observability.events import Event
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakePersonalConsents,
    build_test_api,
)

GRANTED = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)
REVOKED_AT = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def own(**over: object) -> OwnConsent:
    base: dict[str, object] = {
        "guild_id": GUILD,
        "state": "active",
        "active": True,
        "scope": "audio",
        "policy_version": "2026-01",
        "guild_policy_version": "2026-01",
        "granted_at": GRANTED,
        "revoked_at": None,
        "video_consent_offered": False,
    }
    base.update(over)
    return OwnConsent(**base)  # type: ignore[arg-type]


def _events(caplog: pytest.LogCaptureFixture, event: Event) -> list[logging.LogRecord]:
    """Every record carrying one event name.

    `log_event` puts the name and the fields in `extra` under
    `sturnus_event` and `sturnus_fields` rather than spreading them across
    the record, which is what lets `scrub_fields` rebuild them from the
    registry on the way out. Reading them back the same way is how these
    tests stay tests of the sanctioned call shape.
    """
    return [r for r in caplog.records if getattr(r, "sturnus_event", None) == str(event)]


def _fields(record: logging.LogRecord) -> dict[str, object]:
    fields = getattr(record, "sturnus_fields", None)
    assert isinstance(fields, dict)
    return fields


def scope_url(guild_id: int | str = GUILD) -> str:
    return f"/api/me/consents/{guild_id}/scope"


def revoke_url(guild_id: int | str = GUILD) -> str:
    return f"/api/me/consents/{guild_id}/revoke"


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


async def test_a_person_sees_the_guilds_they_are_recorded_in(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakePersonalConsents(consents=[own()])
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    response = await client.get("/api/me/consents")

    assert response.status == 200
    assert (await response.json()) == {
        "consents": [
            {
                "guild_id": str(GUILD),
                "state": "active",
                "active": True,
                "scope": "audio",
                "policy_version": "2026-01",
                "guild_policy_version": "2026-01",
                "granted_at": GRANTED.isoformat(),
                "revoked_at": None,
                "video_consent_offered": False,
            }
        ]
    }


async def test_the_listing_is_of_the_signed_in_person_and_nobody_else(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The property no response body can show, and the reason this is a
    module of its own: the id reaching the directory comes out of the
    signed cookie, and the URL has nowhere to put a different one."""
    consents = FakePersonalConsents()
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents), as_user=BEN)

    await client.get("/api/me/consents")

    assert consents.listed == [BEN]


async def test_signing_out_is_the_end_of_it(aiohttp_client: AiohttpClientFactory) -> None:
    client = await aiohttp_client(build_test_api())

    assert (await client.get("/api/me/consents")).status == 401


async def test_a_guild_snowflake_travels_as_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A Discord snowflake exceeds JavaScript's safe integer range, where a
    JSON number loses its last digits and names a guild that does not
    exist."""
    consents = FakePersonalConsents(consents=[own(guild_id=1361473094564769793)])
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    body = await (await client.get("/api/me/consents")).json()

    assert body["consents"][0]["guild_id"] == "1361473094564769793"


async def test_the_listing_is_never_cached(aiohttp_client: AiohttpClientFactory) -> None:
    consents = FakePersonalConsents(consents=[own()])
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    response = await client.get("/api/me/consents")

    assert response.headers["Cache-Control"] == "private, no-store"


# ---------------------------------------------------------------------------
# Changing the scope
# ---------------------------------------------------------------------------


async def test_a_person_may_widen_what_their_consent_covers(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakePersonalConsents(
        scope_outcome=ScopeOutcome(
            scope="audio_video", changed=True, refusal=None, policy_version="2026-06"
        )
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    response = await client.put(scope_url(), json={"scope": "audio_video"})

    assert response.status == 200
    assert (await response.json()) == {
        "scope": "audio_video",
        "changed": True,
        "refusal": None,
        "policy_version": "2026-06",
    }
    assert consents.scopes == [(ANNA, GUILD, "audio_video")]


async def test_a_guild_that_does_not_offer_video_refuses_rather_than_downgrades(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """409 and a named refusal, not a 200 reporting `audio`. A success
    answering a different question has told somebody something false about
    their own consent."""
    consents = FakePersonalConsents(
        scope_outcome=ScopeOutcome(
            scope="audio", changed=False, refusal="video_consent_not_offered"
        )
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    response = await client.put(scope_url(), json={"scope": "audio_video"})

    assert response.status == 409
    assert (await response.json())["refusal"] == "video_consent_not_offered"


async def test_a_scope_this_system_cannot_name_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """400 rather than 409, and the distinction is the one a client needs:
    "fix your request" is not the same instruction as "this is not
    available here"."""
    consents = FakePersonalConsents()
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    response = await client.put(scope_url(), json={"scope": "audio_video_and_screen"})

    assert response.status == 400
    assert consents.scopes == []


@pytest.mark.parametrize("body", [{}, {"scope": 7}, [], "audio"])
async def test_a_body_that_is_not_a_scope_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory, body: object
) -> None:
    client = await signed_in(aiohttp_client, build_test_api())

    assert (await client.put(scope_url(), json=body)).status == 400


async def test_a_guild_id_that_is_not_a_number_is_a_bad_request(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Not a 404. Unlike the administrator's routes there is nothing here
    whose existence is worth concealing: the caller is asking about their
    own records."""
    client = await signed_in(aiohttp_client, build_test_api())

    assert (await client.put(scope_url("not-a-guild"), json={"scope": "audio"})).status == 400


async def test_a_scope_change_is_logged_with_what_it_now_covers(
    aiohttp_client: AiohttpClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    consents = FakePersonalConsents(
        scope_outcome=ScopeOutcome(scope="audio", changed=True, refusal=None)
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    with caplog.at_level(logging.INFO):
        await client.put(scope_url(), json={"scope": "audio"})

    lines = _events(caplog, Event.CONSOLE_CONSENT_SCOPE_CHANGED)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    assert _fields(lines[0])["scope"] == "audio"


# ---------------------------------------------------------------------------
# Withdrawing it
# ---------------------------------------------------------------------------


async def test_a_person_may_withdraw_their_own_consent(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakePersonalConsents(
        revocation=RevocationOutcome(revoked=True, refusal=None, effective_at=REVOKED_AT)
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents), as_user=BEN)

    response = await client.post(revoke_url())

    assert response.status == 200
    assert consents.revoked == [(BEN, GUILD)]


async def test_the_answer_says_the_discord_role_stays(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`api` holds no Discord token and never will, so recording stops
    within five seconds while Discord goes on showing a role that means
    nothing. A person left to discover that from `/consent status` has
    been told half of what happened."""
    consents = FakePersonalConsents(
        revocation=RevocationOutcome(revoked=True, refusal=None, effective_at=REVOKED_AT)
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    body = await (await client.post(revoke_url())).json()

    assert body == {
        "revoked": True,
        "refusal": None,
        "effective_at": REVOKED_AT.isoformat(),
        "recordings_from_effective_at": 0,
        "role_stays": True,
    }


async def test_withdrawing_twice_says_so_rather_than_pretending(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    consents = FakePersonalConsents(
        revocation=RevocationOutcome(revoked=False, refusal="already_revoked")
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents))

    response = await client.post(revoke_url())

    assert response.status == 409
    assert (await response.json())["refusal"] == "already_revoked"


async def test_a_self_withdrawal_names_the_person_as_both_subject_and_actor(
    aiohttp_client: AiohttpClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """`requested_by` equals `discord_user_id` here and is written anyway:
    the audit query for "who withdrew whose consent" runs over this event
    and `console.consent_revoked` together, and a line missing the field
    would drop out of it rather than answering "themselves"."""
    consents = FakePersonalConsents(
        revocation=RevocationOutcome(revoked=True, refusal=None, effective_at=REVOKED_AT)
    )
    client = await signed_in(aiohttp_client, build_test_api(own_consents=consents), as_user=BEN)

    with caplog.at_level(logging.INFO):
        await client.post(revoke_url())

    lines = _events(caplog, Event.CONSOLE_CONSENT_SELF_REVOKED)
    assert len(lines) == 1
    fields = _fields(lines[0])
    assert fields["discord_user_id"] == BEN
    assert fields["requested_by"] == BEN
