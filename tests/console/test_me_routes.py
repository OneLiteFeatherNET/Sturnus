"""Who the console thinks you are, and what you told it about yourself.

Two halves, tested two different ways for a reason.

**Identity** is a fake `ProfileDirectory`. What is worth pinning there is
that the name comes out of the signed cookie rather than out of anything
the request could name, and that the field is present-and-null rather than
missing when there is no name to give -- neither of which needs a database.

**Preferences run against the real `PreferenceStore` on the real
database.** The refusals these endpoints enforce are the store's:
`PreferenceStore.set` is what refuses a key nobody reads and a value
outside `ALLOWED_VALUES`. A double reimplementing those rules would be a
second copy of `sturnus.domain.preferences`, maintained by hand, and the
first thing to drift the day a value is added. So a 400 here is a real
`ValueError` from the real write path -- the same choice
`test_settings_routes` makes about `ConfigStore`, for the same reason.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.app import SESSION_COOKIE
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.domain import preferences
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.preferences import PreferenceStore
from tests.console.conftest import (
    ANNA,
    BEN,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeProfile,
    build_test_api,
)

_PREFERENCES = "/api/me/preferences"


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


async def signed_in(
    aiohttp_client: AiohttpClientFactory, app: web.Application, as_user: int = ANNA
) -> TestClient[web.Request, web.Application]:
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(as_user)})
    return client


@pytest.fixture
async def store(clean_database: str) -> PreferenceStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    return PreferenceStore(factory)


async def preferences_of(client: TestClient[web.Request, web.Application]) -> dict[str, Any]:
    response = await client.get(_PREFERENCES)
    assert response.status == 200
    body: dict[str, Any] = await response.json()
    return dict(body["preferences"])


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


async def test_a_signed_in_person_is_named_by_the_link_they_made(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api())

    body = await (await client.get("/api/me")).json()

    assert body["display_name"] == "Anna Example"
    assert body["discord_user_id"] == str(ANNA)


async def test_a_person_with_no_link_row_is_named_null_rather_than_not_named(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The field is present and null, never absent.

    A client that had to tell "this person has no name" from "this API is
    older than the name" would have to guess, and would guess wrong on
    exactly one of them.
    """
    client = await signed_in(aiohttp_client, build_test_api(profile=FakeProfile({})))

    body = await (await client.get("/api/me")).json()

    assert "display_name" in body
    assert body["display_name"] is None


async def test_the_name_is_looked_up_for_the_person_in_the_cookie(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    profile = FakeProfile({ANNA: "Anna Example", BEN: "Ben Example"})
    client = await signed_in(aiohttp_client, build_test_api(profile=profile), as_user=BEN)

    body = await (await client.get("/api/me")).json()

    assert profile.asked == [BEN]
    assert body["display_name"] == "Ben Example"


async def test_who_you_are_is_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await signed_in(aiohttp_client, build_test_api())

    response = await client.get("/api/me")

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_who_you_are_still_needs_a_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(build_test_api())

    assert (await client.get("/api/me")).status == 401


# ---------------------------------------------------------------------------
# Reading preferences
# ---------------------------------------------------------------------------


async def test_a_preference_nobody_set_answers_the_default(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """The defaults are served, not left for the client to know.

    A console that fell back itself would be a second copy of `DEFAULTS`
    in a language that cannot import the first one.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    assert await preferences_of(client) == dict(preferences.DEFAULTS)


async def test_a_stored_preference_wins_over_its_default(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    assert (await preferences_of(client))[preferences.THEME] == "dark"


async def test_every_known_key_is_answered_even_when_one_of_them_was_set(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    assert set(await preferences_of(client)) == set(preferences.KNOWN_KEYS)


async def test_preferences_are_never_cached_by_anything_in_between(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    response = await client.get(_PREFERENCES)

    assert response.headers["Cache-Control"] == "private, no-store"


async def test_preferences_need_a_session_like_every_other_endpoint(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    client = await aiohttp_client(build_test_api(prefs=store))

    assert (await client.get(_PREFERENCES)).status == 401
    assert (await client.put(f"{_PREFERENCES}/theme", json={"value": "dark"})).status == 401
    assert (await client.delete(f"{_PREFERENCES}/theme")).status == 401


# ---------------------------------------------------------------------------
# Writing one
# ---------------------------------------------------------------------------


async def test_setting_a_preference_answers_the_whole_effective_set(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """A write reads back everything, so the console never has to merge.

    Answering only the key that was written would leave the client
    holding two half-answers and reconciling them itself.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    response = await client.put(f"{_PREFERENCES}/{preferences.THEME}", json={"value": "dark"})

    assert response.status == 200
    body = await response.json()
    assert body["preferences"] == {**preferences.DEFAULTS, preferences.THEME: "dark"}


async def test_a_preference_that_was_set_is_the_one_that_is_read_back(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    await client.put(f"{_PREFERENCES}/{preferences.LOCALE}", json={"value": "de"})

    assert (await preferences_of(client))[preferences.LOCALE] == "de"


async def test_a_key_that_names_no_preference_is_refused(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """400 rather than 404, unlike the settings endpoints.

    There the key check is the API's own, because "there is no such
    setting" has to be answered before the guild is even looked at. Here
    both refusals come from one place -- `PreferenceStore.set` raises for
    an unknown key and for an unacceptable value alike -- and restating
    the registry in the handler to distinguish them would be the second
    copy this arrangement exists to avoid.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    response = await client.put(f"{_PREFERENCES}/wallpaper", json={"value": "dark"})

    assert response.status == 400


async def test_a_value_the_key_does_not_accept_is_refused(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    response = await client.put(f"{_PREFERENCES}/{preferences.THEME}", json={"value": "neon"})

    assert response.status == 400
    assert (await preferences_of(client))[preferences.THEME] == "system"


async def test_a_refusal_never_echoes_what_was_sent(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """`PreferenceStore`'s own message embeds the value it refused.

    Passing it through would make this an echo endpoint for anything
    somebody can type, which is the one way a JSON API becomes an XSS
    sink one careless renderer later.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    response = await client.put(
        f"{_PREFERENCES}/{preferences.THEME}", json={"value": "<script>alert(1)</script>"}
    )

    assert "script" not in await response.text()


async def test_a_body_that_is_not_an_object_is_refused(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    assert (await client.put(f"{_PREFERENCES}/{preferences.THEME}", data="not json")).status == 400
    assert (await client.put(f"{_PREFERENCES}/{preferences.THEME}", json=["dark"])).status == 400


async def test_a_value_that_is_not_a_string_is_refused_rather_than_coerced(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """`{"value": 3}` is not a preference. Coercing it to `"3"` here would
    be this module holding an opinion about what a legal value looks
    like, which is exactly what `sturnus.domain.preferences` is for.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    theme = f"{_PREFERENCES}/{preferences.THEME}"

    assert (await client.put(theme, json={"value": 3})).status == 400
    assert (await client.put(theme, json={"value": None})).status == 400


# ---------------------------------------------------------------------------
# Clearing one
# ---------------------------------------------------------------------------


async def test_clearing_a_preference_restores_its_default(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    response = await client.delete(f"{_PREFERENCES}/{preferences.THEME}")

    assert response.status == 200
    body = await response.json()
    assert body["preferences"][preferences.THEME] == preferences.DEFAULTS[preferences.THEME]


async def test_clearing_a_preference_nobody_set_is_not_an_error(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """Resetting something already at its default is what a "reset"
    button does on a fresh account, and it succeeded.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    assert (await client.delete(f"{_PREFERENCES}/{preferences.LOCALE}")).status == 200


async def test_clearing_a_key_that_names_no_preference_is_refused(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """A misspelled key must not report success. A silent no-op is a
    reset button that does nothing with nothing anywhere saying why.
    """
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    assert (await client.delete(f"{_PREFERENCES}/wallpaper")).status == 400


async def test_clearing_one_preference_leaves_the_others_alone(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    await store.set(ANNA, preferences.LOCALE, "de", T0)
    client = await signed_in(aiohttp_client, build_test_api(prefs=store))

    await client.delete(f"{_PREFERENCES}/{preferences.THEME}")

    assert (await preferences_of(client))[preferences.LOCALE] == "de"


# ---------------------------------------------------------------------------
# Whose preferences these are
# ---------------------------------------------------------------------------


async def test_a_preference_belongs_to_the_person_in_the_cookie(
    aiohttp_client: AiohttpClientFactory, store: PreferenceStore
) -> None:
    """No endpoint here takes a user id, so this is the whole rule: the
    session decides whose preferences are read and written, and there is
    no request a second person could make that reaches the first one's.
    """
    app = build_test_api(prefs=store)
    anna = await signed_in(aiohttp_client, app)
    ben = await signed_in(aiohttp_client, app, as_user=BEN)

    await anna.put(f"{_PREFERENCES}/{preferences.THEME}", json={"value": "dark"})

    assert (await preferences_of(ben))[preferences.THEME] == "system"
    assert await store.snapshot(BEN) == dict(preferences.DEFAULTS)
