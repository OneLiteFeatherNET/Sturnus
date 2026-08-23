"""The settings endpoints, through the real routes and the real stores.

Two choices here are deliberate and carry the weight of the file.

**No fake `ConfigStore`.** The value validation these endpoints enforce is
the store's, not the API's -- `ConfigStore.set` is what refuses `"soon"`
for an integer key and refuses a key nobody reads. A double that
reimplemented those rules would be a second copy of them, maintained by
hand, and the first thing to drift the day a rule changes. So these tests
run against the real store on the real database, and a 400 here is a real
`ValueError` from the real write path.

**No fake `AdminMemberStore` either.** The authorisation rule is per
guild: `is_admin_anywhere` decides whether the console offers the section
at all, and `is_admin(guild_id, ...)` decides whether *this* guild's
settings may be read or written. An administrator of one guild reaching
another guild's settings is the failure this file exists to make
impossible, and it is not a failure worth proving against a dictionary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.session import SessionCookie, SignedSession
from sturnus.domain import settings
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    build_test_api,
)

SESSION_COOKIE = "sturnus_session"

#: A second guild, because one guild cannot demonstrate a per-guild rule.
OTHER_GUILD = 8822

#: A snowflake past 2^53, where a JSON number loses its last digits.
BIG_GUILD = 386950399101370374


@dataclass(frozen=True)
class Stores:
    config: ConfigStore
    admins: AdminMemberStore


@pytest.fixture
async def stores(clean_database: str) -> Stores:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    return Stores(config=ConfigStore(factory), admins=AdminMemberStore(factory))


def api(stores: Stores) -> web.Application:
    # Through the shared factory, which defaults every collaborator these
    # tests are not about. The two that matter here are the real ones: the
    # value validation under test is `ConfigStore`'s, and a per-guild
    # authorisation rule is not worth proving against a dictionary.
    return build_test_api(admins=stores.admins, config=stores.config)


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in_client(
    aiohttp_client: AiohttpClientFactory, stores: Stores, as_user: int = ANNA
) -> Any:
    client = await aiohttp_client(api(stores))
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def setting(body: dict[str, Any], key: str) -> dict[str, Any]:
    """The one key's entry out of a settings listing."""
    found: list[dict[str, Any]] = [entry for entry in body["settings"] if entry["key"] == key]
    assert found, f"{key} was not listed at all"
    return found[0]


# ---------------------------------------------------------------------------
# Which guilds the caller may touch at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/guilds"),
        ("get", f"/api/guilds/{GUILD}/settings"),
        ("put", f"/api/guilds/{GUILD}/settings/timezone"),
        ("delete", f"/api/guilds/{GUILD}/settings/timezone"),
    ],
)
async def test_every_settings_endpoint_refuses_an_anonymous_caller(
    aiohttp_client: AiohttpClientFactory, stores: Stores, method: str, path: str
) -> None:
    """Enumerated rather than trusted, because `require_session` is a
    decorator: forgetting it on one route leaves that route silently
    public, and nothing else in the system would notice.
    """
    client = await aiohttp_client(api(stores))
    assert (await getattr(client, method)(path, json={"value": "x"})).status == 401


async def test_the_guild_list_names_only_the_guilds_the_caller_administers(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.admins.replace(OTHER_GUILD, [BEN], T0)
    client = await signed_in_client(aiohttp_client, stores)
    body = await (await client.get("/api/guilds")).json()
    assert [entry["guild_id"] for entry in body["guilds"]] == [str(GUILD)]


async def test_the_guild_list_is_empty_for_somebody_who_administers_nothing(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """Empty, not 403. Having no guilds to configure is an ordinary state
    for a participant who signed in to look at their own recordings.
    """
    await stores.admins.replace(GUILD, [BEN], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.get("/api/guilds")
    assert response.status == 200
    assert (await response.json())["guilds"] == []


async def test_a_guild_id_is_serialised_as_a_string(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """A snowflake exceeds JavaScript's safe integer range, where a JSON
    number silently loses its last digits -- producing an id that looks
    right and names nothing.
    """
    await stores.admins.replace(BIG_GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    body = await (await client.get("/api/guilds")).json()
    assert body["guilds"][0]["guild_id"] == str(BIG_GUILD)


# ---------------------------------------------------------------------------
# The rule that carries everything: authorisation is per guild
# ---------------------------------------------------------------------------


async def test_an_administrator_of_one_guild_may_not_read_another_guilds_settings(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """`is_admin_anywhere` is true for this caller and still not enough.

    This is the whole reason the check is `is_admin(guild_id, ...)` and
    not a boolean about the person: a console that asked the cheaper
    question would hand every administrator every guild's configuration.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.admins.replace(OTHER_GUILD, [BEN], T0)
    client = await signed_in_client(aiohttp_client, stores)
    assert (await client.get(f"/api/guilds/{OTHER_GUILD}/settings")).status == 403


async def test_an_administrator_of_one_guild_may_not_write_another_guilds_settings(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.admins.replace(OTHER_GUILD, [BEN], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{OTHER_GUILD}/settings/timezone", json={"value": "Europe/Lisbon"}
    )
    assert response.status == 403
    assert await stores.config.get_stored(OTHER_GUILD, settings.TIMEZONE) is None


async def test_an_administrator_of_one_guild_may_not_clear_another_guilds_settings(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.admins.replace(OTHER_GUILD, [BEN], T0)
    await stores.config.set(OTHER_GUILD, settings.TIMEZONE, "Europe/Lisbon", T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.delete(f"/api/guilds/{OTHER_GUILD}/settings/timezone")
    assert response.status == 403
    assert await stores.config.get_stored(OTHER_GUILD, settings.TIMEZONE) == "Europe/Lisbon"


async def test_a_signed_in_participant_who_administers_nothing_is_refused(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """The console hides the settings section from them. That is a
    courtesy to the person looking at the page and never a control -- a
    hidden section is one `curl` away from being visible.
    """
    await stores.admins.replace(GUILD, [BEN], T0)
    client = await signed_in_client(aiohttp_client, stores)
    assert (await client.get(f"/api/guilds/{GUILD}/settings")).status == 403


async def test_a_guild_id_that_is_not_a_number_is_not_found(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    client = await signed_in_client(aiohttp_client, stores)
    assert (await client.get("/api/guilds/not-a-guild/settings")).status == 404


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_the_listing_carries_every_key_with_its_metadata(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    body = await (await client.get(f"/api/guilds/{GUILD}/settings")).json()

    assert body["guild_id"] == str(GUILD)
    assert {entry["key"] for entry in body["settings"]} == set(settings.KNOWN_KEYS)

    grace = setting(body, "empty_grace_seconds")
    assert grace["required"] is False
    assert grace["integer"] is True
    assert grace["value"] == grace["default"]

    channel = setting(body, "voice_channel_ids")
    assert channel["required"] is True
    assert channel["value"] is None
    assert channel["default"] is None


async def test_a_stored_value_is_what_the_listing_reports(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.config.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    client = await signed_in_client(aiohttp_client, stores)
    body = await (await client.get(f"/api/guilds/{GUILD}/settings")).json()
    entry = setting(body, "idle_timeout_minutes")
    assert entry["value"] == "45"
    assert entry["default"] != "45"


async def test_one_guilds_stored_value_does_not_leak_into_another(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.admins.replace(OTHER_GUILD, [ANNA], T0)
    await stores.config.set(OTHER_GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    client = await signed_in_client(aiohttp_client, stores)
    body = await (await client.get(f"/api/guilds/{GUILD}/settings")).json()
    assert setting(body, "idle_timeout_minutes")["value"] != "45"


# ---------------------------------------------------------------------------
# Writing, and the validation that is not the API's own
# ---------------------------------------------------------------------------


async def test_a_write_stores_the_value(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/idle_timeout_minutes", json={"value": "45"}
    )
    assert response.status == 200
    assert (await response.json())["setting"]["value"] == "45"
    assert await stores.config.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "45"


async def test_a_value_the_store_cannot_parse_is_refused_and_nothing_is_written(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """The 400 comes from `ConfigStore.set` raising, not from a check here.

    Reimplementing "this key must be a positive integer" in the handler
    would give the system two copies of the rule and one day two answers.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/idle_timeout_minutes", json={"value": "soon"}
    )
    assert response.status == 400
    assert await stores.config.get_stored(GUILD, settings.IDLE_TIMEOUT_MINUTES) is None


async def test_a_negative_number_for_an_integer_key_is_refused(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """Also the store's rule, and the one a naive `int()` check would miss."""
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/idle_timeout_minutes", json={"value": "-5"}
    )
    assert response.status == 400
    assert await stores.config.get_stored(GUILD, settings.IDLE_TIMEOUT_MINUTES) is None


async def test_a_rejected_value_is_not_echoed_back(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """The rule the whole module is built on: no user input reaches a
    response body. `ConfigStore`'s own message embeds the value it
    refused, which is exactly the thing that must not travel.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/idle_timeout_minutes",
        json={"value": "<script>alert(1)</script>"},
    )
    assert response.status == 400
    assert "script" not in await response.text()


async def test_an_unknown_key_is_not_found_and_writes_nothing(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """Without this, `guild_config` is a table anybody with a session and
    one guild can write arbitrary rows into.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/drop_all_recordings", json={"value": "yes"}
    )
    assert response.status == 404
    assert await stores.config.get_stored(GUILD, "drop_all_recordings") is None


async def test_an_unknown_key_is_refused_before_the_guild_is_even_considered(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """A caller with no rights over the guild still gets 403, not 404.

    Answering "no such key" first would turn the settings endpoint into an
    oracle for which keys exist, readable by anyone with a session.
    """
    await stores.admins.replace(GUILD, [BEN], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/drop_all_recordings", json={"value": "yes"}
    )
    assert response.status == 403


@pytest.mark.parametrize("body", [{}, {"value": 45}, {"value": None}])
async def test_a_body_that_does_not_carry_a_string_value_is_refused(
    aiohttp_client: AiohttpClientFactory, stores: Stores, body: dict[str, Any]
) -> None:
    """No coercion. `45` becoming `"45"` here would be the API quietly
    holding a second opinion about what a valid value looks like, and
    `None` is a clear, which is what `DELETE` is for.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(f"/api/guilds/{GUILD}/settings/timezone", json=body)
    assert response.status == 400


async def test_a_body_that_is_not_json_is_refused_rather_than_a_server_error(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/timezone",
        data="not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert response.status == 400


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------


async def test_clearing_restores_the_default(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.config.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.delete(f"/api/guilds/{GUILD}/settings/idle_timeout_minutes")
    assert response.status == 200
    entry = (await response.json())["setting"]
    assert entry["value"] == entry["default"]
    assert await stores.config.get_stored(GUILD, settings.IDLE_TIMEOUT_MINUTES) is None


async def test_clearing_a_key_that_was_never_set_is_not_an_error(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """Clearing what is already clear is the outcome the caller wanted."""
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    assert (await client.delete(f"/api/guilds/{GUILD}/settings/timezone")).status == 200


@pytest.mark.parametrize("key", ["policy_version", "voice_channel_ids", "voice_channel_id"])
async def test_a_key_with_no_default_may_not_be_cleared(
    aiohttp_client: AiohttpClientFactory, stores: Stores, key: str
) -> None:
    """There is no default to fall back to, so clearing it would stop the
    guild recording rather than restore anything. That holds for the
    deprecated `voice_channel_id` too: a guild that has not moved to the
    list key yet is still being served by it.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    await stores.config.set(GUILD, key, "42", T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.delete(f"/api/guilds/{GUILD}/settings/{key}")
    assert response.status == 409
    assert await stores.config.get_stored(GUILD, key) == "42"


async def test_clearing_an_unknown_key_is_not_found(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    assert (await client.delete(f"/api/guilds/{GUILD}/settings/nonsense")).status == 404


# ---------------------------------------------------------------------------
# What the operator is told about the consequences
# ---------------------------------------------------------------------------


async def test_changing_the_policy_version_answers_with_the_consent_flag(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """The change goes through -- the operator is allowed to make it. What
    must not happen is that they learn afterwards that every consent
    naming the old version just became inactive.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/policy_version", json={"value": "2026-08-22"}
    )
    assert response.status == 200
    assert (await response.json())["setting"]["invalidates_consent"] is True
    assert await stores.config.get(GUILD, settings.POLICY_VERSION) == "2026-08-22"


async def test_a_write_does_not_claim_the_bot_is_already_using_the_value(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """The API process has no gateway and cannot reconcile anything.

    `/config set` writes *and* reconciles before it replies. This
    endpoint can only do the first half, so it says which of the three
    things has to happen next rather than implying the value is live.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/empty_grace_seconds", json={"value": "90"}
    )
    assert (await response.json())["setting"]["takes_effect"] == "next_reconcile"


async def test_a_write_to_a_key_read_once_at_start_says_a_restart_is_needed(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/publish_poll_seconds", json={"value": "45"}
    )
    assert (await response.json())["setting"]["takes_effect"] == "process_restart"


async def test_a_write_to_the_voice_channel_says_it_may_wait_for_the_recording(
    aiohttp_client: AiohttpClientFactory, stores: Stores
) -> None:
    """A recording is never discarded to make a setting land sooner, so an
    identity key can sit stored-but-not-applied for as long as a session
    lasts. The console cannot see whether one is in progress; it can at
    least say that the wait is possible.
    """
    await stores.admins.replace(GUILD, [ANNA], T0)
    client = await signed_in_client(aiohttp_client, stores)
    response = await client.put(
        f"/api/guilds/{GUILD}/settings/voice_channel_ids", json={"value": "12345"}
    )
    assert (await response.json())["setting"]["deferred_while_recording"] is True
