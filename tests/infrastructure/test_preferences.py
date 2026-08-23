"""What a person set about their own view of the console.

`user_preference` is `guild_config` keyed by person, and this store is
`ConfigStore` keyed by person, so what is tested here is the same set of
boundaries: a default nobody stored, a stored value winning over it,
clearing a value back to the default, and the two ways a bad write is
refused before it reaches a read path.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.domain import preferences
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.preferences import PreferenceStore

ANNA, BEN = 100, 200
T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def store(clean_database: str) -> PreferenceStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return PreferenceStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_somebody_who_never_opened_the_settings_gets_the_defaults(
    store: PreferenceStore,
) -> None:
    assert await store.snapshot(ANNA) == preferences.DEFAULTS


async def test_a_stored_value_wins_over_its_default(store: PreferenceStore) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    assert (await store.snapshot(ANNA))[preferences.THEME] == "dark"


async def test_a_key_that_was_never_set_still_answers_alongside_one_that_was(
    store: PreferenceStore,
) -> None:
    """The snapshot is layered, not replaced: setting a theme must not
    leave the locale unanswered, or every caller would have to fall back
    to `DEFAULTS` itself and one of them would forget.
    """
    await store.set(ANNA, preferences.THEME, "dark", T0)
    snapshot = await store.snapshot(ANNA)
    assert snapshot[preferences.LOCALE] == preferences.DEFAULTS[preferences.LOCALE]


async def test_setting_a_value_twice_keeps_one_row_and_the_later_value(
    store: PreferenceStore,
) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    await store.set(ANNA, preferences.THEME, "light", T0 + timedelta(minutes=1))
    assert (await store.snapshot(ANNA))[preferences.THEME] == "light"


async def test_clearing_a_value_restores_the_default(store: PreferenceStore) -> None:
    """`None` is how a person says "follow my system again". Without it,
    choosing `light` once would be irreversible.
    """
    await store.set(ANNA, preferences.THEME, "dark", T0)
    await store.set(ANNA, preferences.THEME, None, T0 + timedelta(minutes=1))
    assert (await store.snapshot(ANNA))[preferences.THEME] == "system"


async def test_clearing_a_value_that_was_never_set_is_not_an_error(
    store: PreferenceStore,
) -> None:
    await store.set(ANNA, preferences.THEME, None, T0)
    assert (await store.snapshot(ANNA))[preferences.THEME] == "system"


async def test_one_persons_preference_is_not_another_persons(store: PreferenceStore) -> None:
    """The whole table is keyed by person, and a snapshot that leaked
    across would hand somebody else's console settings to whoever asked
    first.
    """
    await store.set(ANNA, preferences.THEME, "dark", T0)
    assert (await store.snapshot(BEN))[preferences.THEME] == "system"


async def test_a_key_nobody_registered_is_refused(store: PreferenceStore) -> None:
    """A typo would otherwise be stored forever as a setting nothing reads."""
    with pytest.raises(ValueError):
        await store.set(ANNA, "colour_scheme", "dark", T0)


async def test_a_value_outside_the_allowed_set_is_refused(store: PreferenceStore) -> None:
    """Both of these keys select a code path -- a stylesheet, a message
    catalogue -- so a value naming neither is a broken page rather than a
    preference. Refusing at write time keeps the read path reachable only
    with data already known to be renderable.
    """
    with pytest.raises(ValueError):
        await store.set(ANNA, preferences.THEME, "midnight", T0)


async def test_a_refused_write_leaves_the_previous_value_alone(store: PreferenceStore) -> None:
    await store.set(ANNA, preferences.THEME, "dark", T0)
    with pytest.raises(ValueError):
        await store.set(ANNA, preferences.THEME, "midnight", T0 + timedelta(minutes=1))
    assert (await store.snapshot(ANNA))[preferences.THEME] == "dark"


async def test_clearing_an_unknown_key_is_refused_too(store: PreferenceStore) -> None:
    """`None` skips the value check, not the key check -- otherwise a
    misspelled key would be a silent no-op instead of a mistake.
    """
    with pytest.raises(ValueError):
        await store.set(ANNA, "colour_scheme", None, T0)
