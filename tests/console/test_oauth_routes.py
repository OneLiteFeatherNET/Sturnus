"""Configuring a guild's own sign-in client, through the real routes.

Three properties carry this file, and each of them is a decision from
§2.2 rather than an implementation detail:

- **The secret never comes back.** Not from the endpoint that stored it,
  not masked, not truncated. That is why this configuration is not a
  `guild_config` key: the settings API renders every value it holds
  straight back to whoever asks.
- **Every refusal is the same 404.** Not administering the guild, no such
  guild and no client configured answer identically, because whether a
  given guild has its own sign-in is the fact the guild-specific-link
  design exists to keep undiscoverable.
- **Changing a guild's sign-in credential is audited.** It is the
  credential that decides who gets a session at all, and the log line is
  the only record that anybody changed it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.console.session import SessionCookie, SignedSession
from sturnus.observability.events import Event
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeAdmins,
    FakeGuildOAuthClients,
    build_test_api,
    now_at,
)

SESSION_COOKIE = "sturnus_session"

#: A second guild, administered by nobody in these tests unless a test
#: says so. Real-shaped, like `GUILD`.
OTHER_GUILD = 9911

REGISTRATION = {
    "slug": "acme",
    "provider": "outline",
    "base_url": "https://outline.acme.example",
    "client_id": "acme-client",
    "redirect_uri": None,
}


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


def build(clients: FakeGuildOAuthClients | None = None) -> web.Application:
    admins = FakeAdmins({ANNA})
    return build_test_api(
        admins=admins,
        oauth_clients=clients or FakeGuildOAuthClients(admins=admins),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
    )


async def signed_in(
    aiohttp_client: AiohttpClientFactory,
    app: web.Application,
    as_user: int = ANNA,
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/oauth-client"


def secret_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/oauth-client/secret"


def _events(caplog: pytest.LogCaptureFixture, event: Event) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "sturnus_event", None) == str(event)]


def _fields(record: logging.LogRecord) -> dict[str, object]:
    fields = getattr(record, "sturnus_fields", None)
    assert isinstance(fields, dict)
    return fields


# ---------------------------------------------------------------------------
# Nothing here is reachable without a session
# ---------------------------------------------------------------------------


async def test_every_route_refuses_a_request_with_no_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build())
    assert (await client.get(url())).status == 401
    assert (await client.put(url(), json=REGISTRATION)).status == 401
    assert (await client.delete(url())).status == 401
    assert (await client.put(secret_url(), json={"client_secret": "x"})).status == 401
    assert (await client.delete(secret_url())).status == 401


# ---------------------------------------------------------------------------
# Registering, and reading back what was registered
# ---------------------------------------------------------------------------


async def test_an_administrator_registers_their_guilds_sign_in(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())

    response = await client.put(url(), json=REGISTRATION)

    assert response.status == 200
    body = await response.json()
    assert body["guild_id"] == str(GUILD)
    assert body["oauth_client"] == {
        "slug": "acme",
        "provider": "outline",
        "base_url": "https://outline.acme.example",
        "client_id": "acme-client",
        "redirect_uri": None,
        "has_secret": False,
        "created_at": T0.isoformat(),
        "updated_at": T0.isoformat(),
    }


async def test_the_guild_id_is_a_string_because_a_snowflake_is(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A JSON number silently loses its last digits in JavaScript."""
    client = await signed_in(aiohttp_client, build())
    body = await (await client.put(url(), json=REGISTRATION)).json()
    assert isinstance(body["guild_id"], str)


async def test_a_registration_reads_back_as_it_was_written(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)

    response = await client.get(url())

    assert response.status == 200
    assert (await response.json())["oauth_client"]["slug"] == "acme"


async def test_a_guild_may_name_its_own_callback(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    body = await (
        await client.put(url(), json=REGISTRATION | {"redirect_uri": "https://acme.example/back"})
    ).json()
    assert body["oauth_client"]["redirect_uri"] == "https://acme.example/back"


async def test_registering_again_does_not_disturb_the_secret(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Registering the client and supplying its secret are two steps.

    An administrator correcting a base URL should not have to re-type a
    secret they may no longer have a copy of.
    """
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)
    await client.put(secret_url(), json={"client_secret": "hunter2"})

    body = await (
        await client.put(url(), json=REGISTRATION | {"base_url": "https://moved.example"})
    ).json()

    assert body["oauth_client"]["has_secret"] is True


# ---------------------------------------------------------------------------
# The secret, which goes in and does not come out
# ---------------------------------------------------------------------------


async def test_the_stored_secret_is_never_in_any_response(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Never send a secret back -- not even masked-but-recoverable (2.2).

    Checked against the raw text of every response the four endpoints
    that could possibly hold one produce, rather than against a field
    name, because a field nobody thought of is exactly how one would
    escape.
    """
    clients = FakeGuildOAuthClients(admins=FakeAdmins({ANNA}))
    client = await signed_in(aiohttp_client, build(clients))
    await client.put(url(), json=REGISTRATION)

    stored = await client.put(secret_url(), json={"client_secret": "hunter2"})
    read = await client.get(url())
    written = await client.put(url(), json=REGISTRATION)

    assert clients.secrets[GUILD] == "hunter2"
    for response in (stored, read, written):
        assert "hunter2" not in await response.text()


async def test_storing_a_secret_says_only_that_there_is_one(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)

    response = await client.put(secret_url(), json={"client_secret": "hunter2"})

    assert response.status == 200
    assert (await response.json())["oauth_client"]["has_secret"] is True


async def test_the_secret_can_be_cleared_without_freeing_the_slug(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """An administrator whose secret leaked wants it gone now.

    Deleting the whole registration to achieve that would release the
    slug, and somebody else could claim it in between.
    """
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)
    await client.put(secret_url(), json={"client_secret": "hunter2"})

    response = await client.delete(secret_url())

    assert response.status == 200
    body = await response.json()
    assert body["oauth_client"]["has_secret"] is False
    assert body["oauth_client"]["slug"] == "acme"


@pytest.mark.parametrize("body", [{}, {"client_secret": ""}, {"client_secret": 7}, {"secret": "x"}])
async def test_a_secret_that_is_not_a_secret_is_refused(
    aiohttp_client: AiohttpClientFactory, body: dict[str, object]
) -> None:
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)
    assert (await client.put(secret_url(), json=body)).status == 400


async def test_a_secret_for_a_guild_with_no_registration_is_the_same_404(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    response = await client.put(secret_url(), json={"client_secret": "hunter2"})
    assert response.status == 404
    assert await response.json() == {"error": "no sign-in configuration"}


# ---------------------------------------------------------------------------
# Who may see and change one, which is one answer for three questions
# ---------------------------------------------------------------------------


async def test_a_guild_you_do_not_administer_answers_as_one_that_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The enumeration property, from the authenticated side.

    An administrator of one guild is nobody in another, and the refusal
    must not say which of "not yours" and "not configured" it was --
    those two answers together are a map of which guilds run their own
    sign-in.
    """
    clients = FakeGuildOAuthClients(admins=FakeAdmins({ANNA}))
    client = await signed_in(aiohttp_client, build(clients))
    await client.put(url(), json=REGISTRATION)

    configured_but_not_theirs = await signed_in(aiohttp_client, build(clients), as_user=BEN)
    theirs_but_unconfigured = await client.get(url(OTHER_GUILD))
    refused = await configured_but_not_theirs.get(url())

    assert refused.status == theirs_but_unconfigured.status == 404
    assert await refused.json() == await theirs_but_unconfigured.json()


async def test_somebody_else_cannot_write_a_guilds_sign_in(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build(), as_user=BEN)
    assert (await client.put(url(), json=REGISTRATION)).status == 404
    assert (await client.delete(url())).status == 404
    assert (await client.put(secret_url(), json={"client_secret": "x"})).status == 404


async def test_a_guild_id_that_is_not_a_number_is_the_same_404(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    response = await client.get(url("not-a-guild"))
    assert response.status == 404
    assert await response.json() == {"error": "no sign-in configuration"}


# ---------------------------------------------------------------------------
# What may be written, decided in the domain and refused here
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["Acme", "acme industries", "acme/x", "-acme", "acme--x", "1289374650912837465", "ab"],
)
async def test_a_slug_that_is_not_a_slug_is_refused(
    aiohttp_client: AiohttpClientFactory, slug: str
) -> None:
    """The shape is the domain's decision; this endpoint reports it.

    A slug selects a credential from a public URL, so its shape is the
    one rule the sign-in path and the write path must agree about.
    """
    client = await signed_in(aiohttp_client, build())
    assert (await client.put(url(), json=REGISTRATION | {"slug": slug})).status == 400


async def test_a_name_this_deployment_serves_itself_is_not_available(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """And it answers exactly as a name another guild already holds.

    Registration needs a uniqueness answer or an administrator cannot be
    told why their choice failed -- so the two refusals are made one, and
    the reply says nothing about which it was.
    """
    clients = FakeGuildOAuthClients(admins=FakeAdmins({ANNA}, {OTHER_GUILD: {ANNA}}))
    client = await signed_in(aiohttp_client, build(clients))
    await client.put(url(OTHER_GUILD), json=REGISTRATION | {"slug": "taken"})

    reserved = await client.put(url(), json=REGISTRATION | {"slug": "api"})
    claimed = await client.put(url(), json=REGISTRATION | {"slug": "taken"})

    assert reserved.status == claimed.status == 409
    assert await reserved.json() == await claimed.json()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://outline.acme.example",
        "https://outline.acme.example@evil.example/",
        "javascript:alert(1)",
        "outline.acme.example",
        "",
        7,
    ],
)
async def test_a_base_url_other_browsers_follow_must_be_an_https_address(
    aiohttp_client: AiohttpClientFactory, base_url: object
) -> None:
    client = await signed_in(aiohttp_client, build())
    assert (await client.put(url(), json=REGISTRATION | {"base_url": base_url})).status == 400


async def test_a_redirect_uri_is_held_to_the_same_rule(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    refused = await client.put(url(), json=REGISTRATION | {"redirect_uri": "http://acme.example"})
    assert refused.status == 400


async def test_a_provider_this_deployment_cannot_exchange_with_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Storing one would produce a guild whose link is silently broken.

    A registration nothing here can complete resolves to nothing at
    sign-in time, which is indistinguishable from an unknown slug -- so
    the administrator would never learn what went wrong.
    """
    client = await signed_in(aiohttp_client, build())
    assert (await client.put(url(), json=REGISTRATION | {"provider": "confluence"})).status == 400


@pytest.mark.parametrize("body", [{"client_id": ""}, {"client_id": 7}])
async def test_a_client_id_that_is_not_one_is_refused(
    aiohttp_client: AiohttpClientFactory, body: dict[str, object]
) -> None:
    client = await signed_in(aiohttp_client, build())
    assert (await client.put(url(), json=REGISTRATION | body)).status == 400


async def test_a_body_that_is_not_an_object_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    assert (await client.put(url(), data="not json")).status == 400
    assert (await client.put(url(), json=["acme"])).status == 400


async def test_no_refusal_reflects_what_it_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """One of the values these handlers parse is a client secret.

    An endpoint that echoed what it refused would have echoed one the
    first time somebody sent a malformed body.
    """
    client = await signed_in(aiohttp_client, build())
    response = await client.put(url(), json=REGISTRATION | {"slug": "<script>alert(1)</script>"})
    assert response.status == 400
    assert "script" not in await response.text()


# ---------------------------------------------------------------------------
# Removing one
# ---------------------------------------------------------------------------


async def test_removing_a_registration_frees_it(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)

    assert (await client.delete(url())).status == 204
    assert (await client.get(url())).status == 404


async def test_removing_one_twice_is_not_the_same_reply(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """An administrator who clicked twice is told which click did it.

    "Already gone" and "removed" are different answers to the same
    gesture, and collapsing them would hide a registration that somebody
    else removed in between.
    """
    client = await signed_in(aiohttp_client, build())
    await client.put(url(), json=REGISTRATION)
    await client.delete(url())
    assert (await client.delete(url())).status == 404


# ---------------------------------------------------------------------------
# The audit line
# ---------------------------------------------------------------------------


async def test_every_change_to_a_guilds_sign_in_is_audited(
    aiohttp_client: AiohttpClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The credential that decides who gets a session at all.

    WARNING, a level above the settings writes next door: whoever
    controls the identity provider a slug points at controls who this
    console believes is signing in, and this line is the only record that
    anybody changed it.
    """
    client = await signed_in(aiohttp_client, build())

    with caplog.at_level(logging.INFO):
        await client.put(url(), json=REGISTRATION)
        await client.put(secret_url(), json={"client_secret": "hunter2"})
        await client.delete(secret_url())
        await client.delete(url())

    lines = _events(caplog, Event.CONSOLE_OAUTH_CLIENT_CHANGED)
    assert [_fields(line)["outcome"] for line in lines] == [
        "registered",
        "secret_set",
        "secret_cleared",
        "removed",
    ]
    assert {line.levelno for line in lines} == {logging.WARNING}
    for line in lines:
        assert _fields(line)["guild_id"] == GUILD
        assert _fields(line)["requested_by"] == ANNA


async def test_the_audit_line_carries_neither_half_of_the_credential(
    aiohttp_client: AiohttpClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The secret is obvious. The client id is left off deliberately.

    It is one half of a pair, and a retained, Grafana-readable log is not
    the place to narrow the other half's blast radius by one guess.
    """
    client = await signed_in(aiohttp_client, build())

    with caplog.at_level(logging.INFO):
        await client.put(url(), json=REGISTRATION)
        await client.put(secret_url(), json={"client_secret": "hunter2"})

    for line in _events(caplog, Event.CONSOLE_OAUTH_CLIENT_CHANGED):
        rendered = str(_fields(line)) + line.getMessage()
        assert "hunter2" not in rendered
        assert "acme-client" not in rendered


async def test_a_refused_change_is_not_an_audit_line(
    aiohttp_client: AiohttpClientFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing changed, so there is nothing to record.

    An audit line for every refused attempt would also be a way for
    anybody with a session to write into an operator's log by guessing
    guild ids.
    """
    client = await signed_in(aiohttp_client, build(), as_user=BEN)

    with caplog.at_level(logging.INFO):
        await client.put(url(), json=REGISTRATION)
        await client.delete(url())

    assert _events(caplog, Event.CONSOLE_OAUTH_CLIENT_CHANGED) == []
