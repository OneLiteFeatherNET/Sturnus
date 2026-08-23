"""Setting a guild up from the console, and the link that gets the bot there.

What is pinned here is the shape of three responses and the rule about who
gets one: the id in the path never authorises anything, the id in the
cookie does, and a guild somebody does not administer is indistinguishable
from a guild that does not exist.

The two things the console cannot render without are pinned hardest.
`bot.has_arrived` is what separates "this server has no voice channels"
from "the bot is not there yet" -- a channel picker that cannot tell them
apart sends somebody hunting for a bug that is not there. And `POST` and
`GET` answer the same shape, because there is nothing to wait on: the
request is a row, and the console polls until its status stops being
`pending`.

What is *not* tested here is the contradiction rule. Which of two requests
a guild is configured from is settled where the guild is configured -- the
bot -- and is tested in `tests/domain/test_onboarding.py` and
`tests/infrastructure/discord/test_setup_apply.py`. This endpoint writes
every request down, deliberately: an administrator who asked twice asked
twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import GuildSetupState
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.domain.onboarding import APPLIED, FAILED, SUPERSEDED, SetupIntent
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeAdmins,
    FakeSetup,
    build_test_api,
)

_INVITE = "/api/invite"

#: A snowflake past 2^53, where a JSON number loses its last digits.
BIG_CHANNEL = 386950399101370374
OTHER_CHANNEL = 386950399101370375

CLIENT_ID = "1289374650912837465"


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def setup_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/setup"


def an_intent(**over: object) -> SetupIntent:
    base: dict[str, object] = {
        "id": 7,
        "guild_id": GUILD,
        "requested_by": ANNA,
        "requested_at": T0,
        "channel_ids": str(BIG_CHANNEL),
        "consent_role_name": "Recorded",
        "applied_at": None,
        "outcome": None,
        "error": None,
    }
    base.update(over)
    return SetupIntent(**base)  # type: ignore[arg-type]


def a_state(*, seen_at: datetime | None = T0, intent: SetupIntent | None = None) -> GuildSetupState:
    return GuildSetupState(seen_at=seen_at, intent=intent)


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_a_signed_out_visitor_may_not_read_a_guilds_setup(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api())
    assert (await client.get(setup_url())).status == 401


async def test_a_signed_out_visitor_may_not_write_one(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api())
    response = await client.post(setup_url(), json={"channel_ids": [str(BIG_CHANNEL)]})
    assert response.status == 401


async def test_a_guild_this_person_does_not_administer_answers_as_a_guild_that_is_not_there(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404, never 403.

    Writing a setup request is an act on somebody else's server, and a 403
    would confirm to somebody just established as having no business with
    that guild that it exists.
    """
    client = await signed_in(aiohttp_client, build_test_api(setup=FakeSetup()), as_user=BEN)
    assert (await client.get(setup_url())).status == 404


async def test_a_guild_id_that_is_not_a_number_answers_the_same_way(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(setup=FakeSetup(a_state())))
    assert (await client.get(setup_url("nonsense"))).status == 404


async def test_the_signed_in_id_authorises_and_never_one_from_the_url(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The id in the cookie is the only one that decides anything."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup), as_user=BEN)

    await client.get(setup_url())

    assert setup.asked == [(GUILD, BEN)]


# ---------------------------------------------------------------------------
# Whether the bot is there at all
# ---------------------------------------------------------------------------


async def test_a_guild_the_bot_has_never_swept_says_so_outright(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The field the channel picker depends on.

    An empty picker for a guild the bot has not joined reads exactly like
    a server with no voice channels. One means "wait"; the other means
    "look for the bug". The API must not make the console guess.
    """
    setup = FakeSetup(a_state(seen_at=None))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    body = await (await client.get(setup_url())).json()

    assert body["bot"] == {"has_arrived": False, "seen_at": None}


async def test_a_guild_the_bot_has_swept_carries_when_it_last_looked(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    setup = FakeSetup(a_state(seen_at=T0))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    body = await (await client.get(setup_url())).json()

    assert body["bot"] == {"has_arrived": True, "seen_at": T0.isoformat()}


# ---------------------------------------------------------------------------
# Reading a request back
# ---------------------------------------------------------------------------


async def test_a_guild_nobody_has_ever_asked_about_carries_no_request(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Present and null, never absent: "nobody has asked" is an answer."""
    setup = FakeSetup(a_state(intent=None))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    body = await (await client.get(setup_url())).json()

    assert body["guild_id"] == str(GUILD)
    assert body["request"] is None


async def test_a_request_the_bot_has_not_reached_yet_is_pending(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    setup = FakeSetup(a_state(intent=an_intent()))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    request = (await (await client.get(setup_url())).json())["request"]

    assert request == {
        "id": "7",
        "status": "pending",
        "requested_by": str(ANNA),
        "requested_at": T0.isoformat(),
        "channel_ids": [str(BIG_CHANNEL)],
        "consent_role_name": "Recorded",
        "settled_at": None,
        "error": None,
    }


async def test_a_snowflake_never_reaches_the_client_as_a_number(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Past 2^53 a JSON number silently loses its last digits, and produces
    an id that looks right and names nothing."""
    setup = FakeSetup(a_state(intent=an_intent(channel_ids=str(BIG_CHANNEL))))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    raw = await (await client.get(setup_url())).text()

    assert f'"{BIG_CHANNEL}"' in raw
    assert str(BIG_CHANNEL) + "," not in raw.replace(f'"{BIG_CHANNEL}"', "")


async def test_a_request_the_bot_applied_says_when(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    settled = T0 + timedelta(seconds=10)
    setup = FakeSetup(a_state(intent=an_intent(applied_at=settled, outcome=APPLIED)))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    request = (await (await client.get(setup_url())).json())["request"]

    assert request["status"] == APPLIED
    assert request["settled_at"] == settled.isoformat()
    assert request["error"] is None


async def test_a_request_that_failed_carries_something_a_person_can_act_on(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """There is no retry to wait for: an attempt settles the intent either
    way, so this text is the whole answer an administrator gets."""
    setup = FakeSetup(
        a_state(
            intent=an_intent(
                applied_at=T0,
                outcome=FAILED,
                error="I am missing the Manage Roles permission.",
            )
        )
    )
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    request = (await (await client.get(setup_url())).json())["request"]

    assert request["status"] == FAILED
    assert "Manage Roles" in request["error"]


async def test_a_request_a_newer_one_replaced_says_superseded(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """So the console can say "somebody else asked after you" rather than
    leaving a request that never applied and never failed."""
    setup = FakeSetup(a_state(intent=an_intent(applied_at=T0, outcome=SUPERSEDED)))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    request = (await (await client.get(setup_url())).json())["request"]

    assert request["status"] == SUPERSEDED


async def test_an_outcome_this_build_has_never_seen_is_rendered_rather_than_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`outcome` is text and not a database enum precisely so a value this
    code does not know is a row a reader can ignore. An endpoint that
    refused to render one would give that property back."""
    setup = FakeSetup(a_state(intent=an_intent(applied_at=T0, outcome="from_the_future")))
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    request = (await (await client.get(setup_url())).json())["request"]

    assert request["status"] == "from_the_future"


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


async def test_a_request_is_written_in_the_spelling_the_bot_reads(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Stored exactly as `guild_config` holds it, so applying an intent is
    a write of the value rather than a second serialisation."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup, admins=FakeAdmins({ANNA})))

    response = await client.post(
        setup_url(),
        json={"channel_ids": [str(BIG_CHANNEL), str(OTHER_CHANNEL)], "consent_role_name": "Rec"},
    )

    assert response.status == 202
    guild_id, asked_by, channel_ids, role_name, _now = setup.written[0]
    assert (guild_id, asked_by) == (GUILD, ANNA)
    assert channel_ids == f"{BIG_CHANNEL},{OTHER_CHANNEL}"
    assert role_name == "Rec"


async def test_asking_answers_with_the_same_shape_reading_does(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """There is nothing to wait for, so the answer is the guild's state.

    Under the rule that the newest ask wins, "what did I just ask for" and
    "what will this guild be configured from" are the same question.
    """
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    body = await (await client.post(setup_url(), json={"channel_ids": [str(BIG_CHANNEL)]})).json()

    assert body["guild_id"] == str(GUILD)
    assert body["bot"]["has_arrived"] is True
    assert body["request"]["status"] == "pending"
    assert body["request"]["channel_ids"] == [str(BIG_CHANNEL)]


async def test_naming_no_role_is_allowed_and_means_keep_whatever_is_there(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Omitting it must never be the destructive path (Spec 10.1)."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    response = await client.post(setup_url(), json={"channel_ids": [str(BIG_CHANNEL)]})

    assert response.status == 202
    assert setup.written[0][3] is None


async def test_a_guild_this_person_does_not_administer_cannot_be_asked_about(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    setup = FakeSetup()
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    response = await client.post(setup_url(), json={"channel_ids": [str(BIG_CHANNEL)]})

    assert response.status == 404
    assert setup.written == []


async def test_asking_twice_writes_twice(aiohttp_client: AiohttpClientFactory) -> None:
    """An administrator who asked twice asked twice.

    Collapsing the two would lose who asked for which and when. Which one
    the guild is configured from is the bot's decision, not this one's.
    """
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    await client.post(setup_url(), json={"channel_ids": [str(BIG_CHANNEL)]})
    await client.post(setup_url(), json={"channel_ids": [str(OTHER_CHANNEL)]})

    assert [written[2] for written in setup.written] == [
        str(BIG_CHANNEL),
        str(OTHER_CHANNEL),
    ]


# ---------------------------------------------------------------------------
# What a request may say
# ---------------------------------------------------------------------------


async def test_a_body_that_is_not_an_object_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(setup=FakeSetup(a_state())))
    assert (await client.post(setup_url(), json=["10"])).status == 400


async def test_a_channel_list_of_numbers_is_refused_rather_than_coerced(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A client that sent numbers has already lost the last digits of every
    snowflake past 2^53. Accepting them would store a room nobody has."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    response = await client.post(setup_url(), json={"channel_ids": [BIG_CHANNEL]})

    assert response.status == 400
    assert setup.written == []


async def test_naming_no_channel_at_all_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """ "Allowed to record nowhere" is what `/config clear` is for, and it
    is not what somebody pressing a setup button meant."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    assert (await client.post(setup_url(), json={"channel_ids": []})).status == 400
    assert setup.written == []


async def test_a_channel_named_twice_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Always a mistake, and silently collapsing it hides the mistake from
    whoever made it -- `settings.parse_channel_ids`' rule, not a second
    copy of it."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    response = await client.post(
        setup_url(), json={"channel_ids": [str(BIG_CHANNEL), str(BIG_CHANNEL)]}
    )

    assert response.status == 400
    assert setup.written == []


async def test_a_channel_id_that_is_not_a_snowflake_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    assert (await client.post(setup_url(), json={"channel_ids": ["general"]})).status == 400
    assert setup.written == []


async def test_a_blank_role_name_is_refused_rather_than_treated_as_absent(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A role called nothing is not what anybody meant, and Discord would
    refuse it a tick later anyway -- by which point the person has left
    the page."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    response = await client.post(
        setup_url(), json={"channel_ids": [str(BIG_CHANNEL)], "consent_role_name": "   "}
    )

    assert response.status == 400
    assert setup.written == []


async def test_a_role_name_longer_than_discord_allows_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    response = await client.post(
        setup_url(), json={"channel_ids": [str(BIG_CHANNEL)], "consent_role_name": "x" * 101}
    )

    assert response.status == 400
    assert setup.written == []


async def test_a_role_name_is_trimmed_before_it_is_stored(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A trailing space is a different role name in Discord, and nobody
    typed one on purpose."""
    setup = FakeSetup(a_state())
    client = await signed_in(aiohttp_client, build_test_api(setup=setup))

    await client.post(
        setup_url(), json={"channel_ids": [str(BIG_CHANNEL)], "consent_role_name": " Rec "}
    )

    assert setup.written[0][3] == "Rec"


async def test_nothing_a_caller_typed_is_reflected_back_in_a_refusal(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Fixed strings, the same rule the rest of `sturnus.console` follows.

    Echoing the value would make this an echo endpoint for anything an
    administrator can type into a form.
    """
    client = await signed_in(aiohttp_client, build_test_api(setup=FakeSetup(a_state())))

    response = await client.post(
        setup_url(),
        json={"channel_ids": ["<script>alert(1)"], "consent_role_name": "x" * 101},
    )

    assert response.status == 400
    body = await response.text()
    assert "script" not in body
    assert "xxx" not in body


# ---------------------------------------------------------------------------
# The invite link
# ---------------------------------------------------------------------------


async def test_a_signed_out_visitor_is_offered_no_invite_link(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Public is not the same as unauthenticated: an endpoint of this API
    that answered without a session would be the only one."""
    client = await aiohttp_client(build_test_api())
    assert (await client.get(_INVITE)).status == 401


async def test_the_invite_link_names_this_deployments_application(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(discord_client_id=CLIENT_ID))

    body = await (await client.get(_INVITE)).json()

    query = parse_qs(urlparse(body["url"]).query)
    assert body["client_id"] == CLIENT_ID
    assert query["client_id"] == [CLIENT_ID]
    assert query["permissions"] == [body["permissions"]]
    assert query["scope"] == [" ".join(body["scopes"])]


async def test_a_deployment_with_no_application_id_says_so_rather_than_erroring(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Present and null, so a console never has to tell "not configured"
    from "an API that does not serve this"."""
    client = await signed_in(aiohttp_client, build_test_api(discord_client_id=None))

    body = await (await client.get(_INVITE)).json()

    assert body["url"] is None
    assert body["client_id"] is None
    # Still sent: they are what the page tells somebody to tick if they
    # build the link by hand in Discord's own URL generator instead.
    assert body["permissions"] == "269487104"
    assert body["scopes"] == ["bot", "applications.commands"]


async def test_the_setup_payload_is_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """It names who asked for what on somebody's server."""
    client = await signed_in(aiohttp_client, build_test_api(setup=FakeSetup(a_state())))

    response = await client.get(setup_url())

    assert response.headers["Cache-Control"] == "private, no-store"


def test_the_pending_status_is_not_one_of_the_stored_outcomes() -> None:
    """It is the absence of one: the row is not settled.

    Colliding with a real outcome would make a client unable to tell "the
    bot has not reached this yet" from something the bot decided.
    """
    from sturnus.console.routes_setup import PENDING
    from sturnus.domain.onboarding import OUTCOMES

    assert PENDING not in OUTCOMES


def test_the_moments_this_endpoint_sends_are_timezone_aware() -> None:
    """A naive timestamp renders as local time in whatever browser gets it."""
    assert T0.tzinfo is UTC
