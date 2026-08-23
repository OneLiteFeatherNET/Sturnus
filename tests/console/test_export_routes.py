"""The export-target endpoints, and the one thing they must never do.

The assertion this file exists for is `test_no_response_anywhere_carries_the
_credential`. Everything else here is CRUD; that one is the reason export
destinations are a table of their own rather than `guild_config` keys, and
it is written so that a future field on `ExportTarget` cannot slip a token
back into a response without failing it.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    AiohttpClientFactory,
    FakeAdmins,
    FakeExportTargets,
    build_test_api,
    signed_cookie,
)

SESSION_COOKIE = "sturnus_session"

#: A second guild, because one guild cannot demonstrate a per-guild rule.
OTHER_GUILD = 8822

#: A snowflake past 2^53, where a JSON number loses its last digits.
BIG_GUILD = 386950399101370374

TOKEN = "confluence-token-nobody-may-read"


def api(targets: FakeExportTargets | None = None) -> web.Application:
    """The console, with Anna administering `GUILD` and Ben administering nothing."""
    return build_test_api(admins=FakeAdmins({ANNA}), exports=targets or FakeExportTargets())


def outline_body(name: str = "wiki", **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "format": "outline",
        "name": name,
        "target": "c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f",
    }
    body.update(overrides)
    return body


@pytest.fixture
def cookies() -> dict[str, str]:
    return {SESSION_COOKIE: signed_cookie(ANNA)}


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


async def test_a_guild_with_no_destinations_lists_none(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """An empty list rather than a refusal: that is what a guild looks like
    before anybody has configured anything."""
    client = await aiohttp_client(api())
    response = await client.get(f"/api/guilds/{GUILD}/export-targets", cookies=cookies)
    assert response.status == 200
    assert await response.json() == {"guild_id": str(GUILD), "targets": []}


async def test_creating_a_destination_answers_with_it(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.post(
        f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
    )
    assert response.status == 201
    body = await response.json()
    assert body["format"] == "outline"
    assert body["name"] == "wiki"
    assert body["enabled"] is True
    assert body["has_secret"] is False
    assert body["config"] == {}


async def test_a_guild_id_is_a_string_in_every_response(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A snowflake exceeds JavaScript's safe integer range, where a JSON
    number silently loses its last digits and names nobody."""
    client = await aiohttp_client(build_test_api(admins=FakeAdmins(by_guild={BIG_GUILD: {ANNA}})))
    response = await client.post(
        f"/api/guilds/{BIG_GUILD}/export-targets",
        json=outline_body(),
        cookies={SESSION_COOKIE: signed_cookie(ANNA)},
    )
    assert (await response.json())["guild_id"] == str(BIG_GUILD)


async def test_a_second_destination_of_the_same_name_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The store would upsert on `(guild_id, name)`, which is right for an
    update and wrong for a create: a typo would redirect a guild's
    protocols with nothing said."""
    client = await aiohttp_client(api())
    await client.post(f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies)
    again = await client.post(
        f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
    )
    assert again.status == 409


async def test_updating_a_destination_keeps_its_name(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """A name is how an administrator refers to a destination. Changing one
    silently under an id is how "publish to Wiki" stops meaning what the
    person who set it up thought it meant."""
    client = await aiohttp_client(api())
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    updated = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}",
        json=outline_body(name="renamed", target="col-2", enabled=False),
        cookies=cookies,
    )
    body = await updated.json()
    assert updated.status == 200
    assert body["name"] == "wiki"
    assert body["target"] == "col-2"
    assert body["enabled"] is False


async def test_updating_a_destination_that_does_not_exist_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.put(
        f"/api/guilds/{GUILD}/export-targets/99", json=outline_body(), cookies=cookies
    )
    assert response.status == 404


async def test_deleting_a_destination_removes_it(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    removed = await client.delete(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}", cookies=cookies
    )
    assert removed.status == 204
    listed = await (await client.get(f"/api/guilds/{GUILD}/export-targets", cookies=cookies)).json()
    assert listed["targets"] == []


async def test_deleting_a_destination_that_does_not_exist_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.delete(f"/api/guilds/{GUILD}/export-targets/99", cookies=cookies)
    assert response.status == 404


async def test_a_disabled_destination_is_still_listed(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """Switching a destination off is not the same as forgetting how it was
    configured, and a page that hid it would leave no way to switch it back
    on."""
    client = await aiohttp_client(api())
    await client.post(
        f"/api/guilds/{GUILD}/export-targets",
        json=outline_body(enabled=False),
        cookies=cookies,
    )
    listed = await (await client.get(f"/api/guilds/{GUILD}/export-targets", cookies=cookies)).json()
    assert [t["enabled"] for t in listed["targets"]] == [False]


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


async def test_a_credential_can_be_written(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    targets = FakeExportTargets()
    client = await aiohttp_client(api(targets))
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    response = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}/secret",
        json={"secret": TOKEN},
        cookies=cookies,
    )
    assert response.status == 200
    assert (await response.json())["has_secret"] is True
    assert targets.secrets[created["id"]] == TOKEN


async def test_a_credential_can_be_cleared(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    targets = FakeExportTargets()
    client = await aiohttp_client(api(targets))
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}/secret",
        json={"secret": TOKEN},
        cookies=cookies,
    )
    cleared = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}/secret",
        json={"secret": None},
        cookies=cookies,
    )
    assert (await cleared.json())["has_secret"] is False
    assert targets.secrets[created["id"]] is None


async def test_no_response_anywhere_carries_the_credential(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The whole reason export destinations are not `guild_config` keys.

    Every response this API can produce for a destination that has a
    credential, checked against the credential itself -- not against a
    field name, so a future field that happened to carry it would fail
    here rather than ship.
    """
    targets = FakeExportTargets()
    client = await aiohttp_client(api(targets))
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    stored = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}/secret",
        json={"secret": TOKEN},
        cookies=cookies,
    )
    listed = await client.get(f"/api/guilds/{GUILD}/export-targets", cookies=cookies)
    updated = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}",
        json=outline_body(target="col-3"),
        cookies=cookies,
    )

    for response in (stored, listed, updated):
        text = await response.text()
        assert TOKEN not in text
        # Not even a prefix of it: "masked but recoverable" is recoverable.
        assert TOKEN[:8] not in text


async def test_updating_a_destination_does_not_clear_its_credential(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The edit form cannot render the token, so it cannot re-submit it
    either -- and a `PUT` that also wrote the credential would therefore
    clear it every time somebody changed a collection id."""
    targets = FakeExportTargets()
    client = await aiohttp_client(api(targets))
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}/secret",
        json={"secret": TOKEN},
        cookies=cookies,
    )
    updated = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}",
        json=outline_body(target="col-3"),
        cookies=cookies,
    )
    assert (await updated.json())["has_secret"] is True
    assert targets.secrets[created["id"]] == TOKEN


async def test_an_empty_credential_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """`""` is not a credential and is not "clear it" either -- `null` is.
    Storing an empty string would leave `has_secret` true for a
    destination that cannot authenticate."""
    client = await aiohttp_client(api())
    created = await (
        await client.post(
            f"/api/guilds/{GUILD}/export-targets", json=outline_body(), cookies=cookies
        )
    ).json()
    response = await client.put(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}/secret",
        json={"secret": ""},
        cookies=cookies,
    )
    assert response.status == 400


async def test_a_credential_for_a_destination_that_does_not_exist_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.put(
        f"/api/guilds/{GUILD}/export-targets/99/secret",
        json={"secret": TOKEN},
        cookies=cookies,
    )
    assert response.status == 404


# ---------------------------------------------------------------------------
# Authorisation: 404, and the same 404 every time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "", None),
        ("post", "", {"format": "outline", "name": "n", "target": "t"}),
        ("put", "/1", {"format": "outline", "name": "n", "target": "t"}),
        ("delete", "/1", None),
        ("put", "/1/secret", {"secret": "x"}),
    ],
)
async def test_somebody_who_does_not_administer_the_guild_gets_a_404(
    aiohttp_client: AiohttpClientFactory,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    """404 and not 403. A 403 confirms that the guild exists and that it
    has a destination with that id, to somebody the system has just
    decided has no business knowing either."""
    client = await aiohttp_client(api())
    call = getattr(client, method)
    response = await call(
        f"/api/guilds/{GUILD}/export-targets{path}",
        json=body,
        cookies={SESSION_COOKIE: signed_cookie(BEN)},
    )
    assert response.status == 404
    assert await response.json() == {"error": "no such export target"}


async def test_an_administrator_of_one_guild_is_nobody_in_another(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.get(f"/api/guilds/{OTHER_GUILD}/export-targets", cookies=cookies)
    assert response.status == 404


async def test_a_destination_of_another_guild_is_not_reachable_by_its_id(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The guild is part of every lookup, so a target id belonging to
    somebody else reads exactly like a target id belonging to nobody."""
    targets = FakeExportTargets()
    client = await aiohttp_client(
        build_test_api(
            admins=FakeAdmins(by_guild={GUILD: {ANNA}, OTHER_GUILD: {BEN}}), exports=targets
        )
    )
    created = await (
        await client.post(
            f"/api/guilds/{OTHER_GUILD}/export-targets",
            json=outline_body(),
            cookies={SESSION_COOKIE: signed_cookie(BEN)},
        )
    ).json()
    response = await client.get(
        f"/api/guilds/{GUILD}/export-targets", cookies={SESSION_COOKIE: signed_cookie(ANNA)}
    )
    assert (await response.json())["targets"] == []
    stolen = await client.delete(
        f"/api/guilds/{GUILD}/export-targets/{created['id']}",
        cookies={SESSION_COOKIE: signed_cookie(ANNA)},
    )
    assert stolen.status == 404


async def test_a_guild_id_that_is_not_a_number_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.get("/api/guilds/not-a-guild/export-targets", cookies=cookies)
    assert response.status == 404


async def test_a_target_id_that_is_not_a_number_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.delete(f"/api/guilds/{GUILD}/export-targets/abc", cookies=cookies)
    assert response.status == 404


async def test_every_route_refuses_a_request_with_no_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(api())
    assert (await client.get(f"/api/guilds/{GUILD}/export-targets")).status == 401


# ---------------------------------------------------------------------------
# What may be configured
# ---------------------------------------------------------------------------


async def test_a_format_this_deployment_cannot_publish_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """`pdf` is specified and deliberately not built. Accepting a target
    for it would produce a destination that is silently skipped after
    every meeting, with nothing anywhere saying why."""
    client = await aiohttp_client(api())
    response = await client.post(
        f"/api/guilds/{GUILD}/export-targets",
        json=outline_body(format="pdf"),
        cookies=cookies,
    )
    assert response.status == 400
    body = await response.json()
    assert set(body["supported"]) == {"outline", "markdown", "html"}


async def test_an_unknown_format_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.post(
        f"/api/guilds/{GUILD}/export-targets",
        json=outline_body(format="smoke-signal"),
        cookies=cookies,
    )
    assert response.status == 400


async def test_an_object_store_target_that_climbs_out_of_its_prefix_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The target becomes part of an object key, and the format is what
    decides what a key may say -- so this refusal comes from the registry
    entry rather than from a branch in the API."""
    client = await aiohttp_client(api())
    response = await client.post(
        f"/api/guilds/{GUILD}/export-targets",
        json={"format": "markdown", "name": "archive", "target": "../secrets"},
        cookies=cookies,
    )
    assert response.status == 400


@pytest.mark.parametrize(
    "body",
    [
        {"format": "outline", "name": "", "target": "col-1"},
        {"format": "outline", "name": "wiki", "target": ""},
        {"format": "outline", "name": "wiki", "target": "col-1", "config": []},
        {"format": "outline", "name": "wiki", "target": "col-1", "enabled": "yes"},
        {"name": "wiki", "target": "col-1"},
    ],
)
async def test_a_malformed_destination_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str], body: dict[str, object]
) -> None:
    client = await aiohttp_client(api())
    response = await client.post(f"/api/guilds/{GUILD}/export-targets", json=body, cookies=cookies)
    assert response.status == 400


async def test_a_body_that_is_not_an_object_is_refused(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api())
    response = await client.post(
        f"/api/guilds/{GUILD}/export-targets", json=["nope"], cookies=cookies
    )
    assert response.status == 400


async def test_a_destinations_own_configuration_survives_a_round_trip(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """`config` is whatever the format needs -- a base URL, a space key --
    and it is a mapping rather than five columns four formats would leave
    null."""
    client = await aiohttp_client(api())
    response = await client.post(
        f"/api/guilds/{GUILD}/export-targets",
        json=outline_body(config={"base_url": "https://wiki.example", "space": "TEAM"}),
        cookies=cookies,
    )
    assert (await response.json())["config"] == {
        "base_url": "https://wiki.example",
        "space": "TEAM",
    }
