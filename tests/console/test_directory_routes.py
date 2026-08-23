"""The names behind the ids, as two endpoints.

What is pinned here is the shape of the two responses and the rule about
who gets one: the id in the path never authorises anything, the id in the
cookie does, and a guild somebody does not administer is indistinguishable
from a guild that does not exist.

The *ordering* is not tested here. It is done in the statement, and it is
tested against the real database in `tests/console/test_name_directory.py`
-- a double that sorted in Python would only ever prove that the double
sorts.
"""

from __future__ import annotations

from datetime import timedelta

from aiohttp import web
from aiohttp.test_utils import TestClient

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.application.directory_mirror import (
    VOICE,
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.console.app import SESSION_COOKIE
from sturnus.console.ports import CollectionListing, GuildDirectory
from sturnus.console.session import SessionCookie, SignedSession
from tests.console.conftest import (
    ANNA,
    BEN,
    GUILD,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeCollections,
    FakeNames,
    build_test_api,
)

_COLLECTIONS = "/api/outline/collections"

#: A snowflake past 2^53, where a JSON number loses its last digits.
BIG_CHANNEL = 386950399101370374


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


def directory_url(guild_id: int | str = GUILD) -> str:
    return f"/api/guilds/{guild_id}/directory"


def a_directory(**over: object) -> GuildDirectory:
    base: dict[str, object] = {
        "name": "Acme Corp",
        "channels": (
            MirroredChannel(channel_id=BIG_CHANNEL, name="Standup", kind=VOICE, position=3),
        ),
        "roles": (MirroredRole(role_id=77, name="Recorded", position=7),),
        "members": (MirroredMember(discord_user_id=ANNA, display_name="Anna Example"),),
        "synced_at": T0,
    }
    base.update(over)
    return GuildDirectory(**base)  # type: ignore[arg-type]


def a_listing(**over: object) -> CollectionListing:
    base: dict[str, object] = {
        "collections": (MirroredCollection(collection_id="c-1", name="Meetings"),),
        "synced_at": T0,
    }
    base.update(over)
    return CollectionListing(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A guild's channels, roles and named people
# ---------------------------------------------------------------------------


async def test_an_administrator_sees_the_names_their_guild_was_mirrored_with(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames(a_directory())))

    response = await client.get(directory_url())

    assert response.status == 200
    body = await response.json()
    assert body["channels"] == [
        {"id": str(BIG_CHANNEL), "name": "Standup", "kind": "voice", "position": 3}
    ]
    assert body["roles"] == [{"id": "77", "name": "Recorded", "position": 7}]
    assert body["members"] == [{"discord_user_id": str(ANNA), "display_name": "Anna Example"}]


async def test_every_id_in_the_directory_is_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A snowflake past 2^53 loses its last digits as a JSON number, and
    an id that looks right and names nothing is worse than no picker.
    """
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames(a_directory())))

    body = await (await client.get(directory_url())).json()

    assert body["guild_id"] == str(GUILD)
    assert body["channels"][0]["id"] == "386950399101370374"


async def test_the_directory_says_when_the_bot_last_saw_the_guild(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A mirror presented without its age reads as live truth."""
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames(a_directory())))

    body = await (await client.get(directory_url())).json()

    assert body["synced_at"] == T0.isoformat()


async def test_the_directory_says_what_the_guild_itself_is_called(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Carried here as well as in the guild list: a client that already
    holds the directory can title the page from it rather than making a
    second request for one string.
    """
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames(a_directory())))

    body = await (await client.get(directory_url())).json()

    assert body["name"] == "Acme Corp"


async def test_a_guild_nothing_was_ever_mirrored_for_is_empty_and_undated(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Present and null rather than absent, so a client never has to tell
    "never swept" from "an API that does not send this".
    """
    empty = a_directory(name=None, channels=(), roles=(), members=(), synced_at=None)
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames(empty)))

    body = await (await client.get(directory_url())).json()

    assert body["synced_at"] is None
    assert body["name"] is None
    assert body["channels"] == [] and body["roles"] == [] and body["members"] == []


async def test_the_directory_is_asked_for_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    names = FakeNames(a_directory())
    client = await signed_in(aiohttp_client, build_test_api(names=names), as_user=BEN)

    await client.get(directory_url())

    assert names.asked == [(GUILD, BEN)]


async def test_a_guild_this_person_does_not_administer_does_not_exist(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    # It names people who consented to being recorded. A 403 would
    # confirm that such a list exists here, to somebody just established
    # as having no business with it.
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames()))

    response = await client.get(directory_url())

    assert response.status == 404
    assert (await response.json())["error"] == "no such guild"


async def test_a_guild_id_that_is_not_a_number_never_reaches_the_directory(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    names = FakeNames(a_directory())
    client = await signed_in(aiohttp_client, build_test_api(names=names))

    assert (await client.get(directory_url("nope"))).status == 404
    assert names.asked == []


async def test_the_directory_is_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(names=FakeNames(a_directory())))

    response = await client.get(directory_url())

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_the_directory_needs_a_session_like_every_other_endpoint(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(names=FakeNames(a_directory())))

    assert (await client.get(directory_url())).status == 401


# ---------------------------------------------------------------------------
# The Outline collections
# ---------------------------------------------------------------------------


async def test_an_administrator_sees_the_collections_the_worker_mirrored(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(
        aiohttp_client, build_test_api(collections=FakeCollections(a_listing()))
    )

    response = await client.get(_COLLECTIONS)

    assert response.status == 200
    body = await response.json()
    assert body["collections"] == [{"id": "c-1", "name": "Meetings"}]


async def test_the_collection_list_says_how_fresh_it_is(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A picker that silently offers a stale list is how somebody
    configures a collection that was deleted last week.
    """
    client = await signed_in(
        aiohttp_client, build_test_api(collections=FakeCollections(a_listing()))
    )

    body = await (await client.get(_COLLECTIONS)).json()

    assert body["synced_at"] == T0.isoformat()


async def test_a_collection_list_nothing_ever_swept_is_empty_and_undated(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    empty = a_listing(collections=(), synced_at=None)
    client = await signed_in(aiohttp_client, build_test_api(collections=FakeCollections(empty)))

    body = await (await client.get(_COLLECTIONS)).json()

    assert body["collections"] == []
    assert body["synced_at"] is None


async def test_the_collections_are_asked_for_on_behalf_of_the_signed_in_person(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    collections = FakeCollections(a_listing())
    client = await signed_in(aiohttp_client, build_test_api(collections=collections), as_user=BEN)

    await client.get(_COLLECTIONS)

    assert collections.asked == [BEN]


async def test_somebody_who_administers_nothing_is_told_there_is_nothing(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """404 rather than 403, consistent with every other administrative
    endpoint: a person who administers no guild has no use for the list
    and no business learning that it exists.
    """
    client = await signed_in(aiohttp_client, build_test_api(collections=FakeCollections()))

    response = await client.get(_COLLECTIONS)

    assert response.status == 404
    assert (await response.json())["error"] == "no such collection list"


async def test_the_collections_are_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(
        aiohttp_client, build_test_api(collections=FakeCollections(a_listing()))
    )

    response = await client.get(_COLLECTIONS)

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_the_collections_need_a_session_like_every_other_endpoint(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api(collections=FakeCollections(a_listing())))

    assert (await client.get(_COLLECTIONS)).status == 401
