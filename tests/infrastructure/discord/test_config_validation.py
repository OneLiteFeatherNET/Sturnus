from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.discord.config_cog import missing_required

T0 = datetime(2026, 8, 19, tzinfo=UTC)
GUILD = 4711


@pytest.fixture
async def store(clean_database: str) -> ConfigStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return ConfigStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_a_fresh_guild_is_missing_every_required_key(store: ConfigStore) -> None:
    assert set(await missing_required(store, GUILD)) == set(settings.REQUIRED_KEYS)


async def test_setting_a_key_removes_it_from_the_missing_list(store: ConfigStore) -> None:
    await store.set(GUILD, settings.VOICE_CHANNEL_IDS, "12345", T0)
    assert settings.VOICE_CHANNEL_IDS not in await missing_required(store, GUILD)


async def test_a_guild_still_on_the_deprecated_key_is_not_told_to_configure_anything(
    store: ConfigStore,
) -> None:
    """The pair is one requirement: either spelling satisfies it.

    A guild configured before the rename must not be reported as missing a
    key it has never heard of -- that reads as an outage where there is
    none, and the fix it suggests is a migration nobody has to run.
    """
    await store.set(GUILD, settings.VOICE_CHANNEL_ID, "12345", T0)
    assert settings.VOICE_CHANNEL_IDS not in await missing_required(store, GUILD)


async def test_a_list_that_cannot_be_parsed_is_refused_at_the_write(store: ConfigStore) -> None:
    """Refused where somebody is looking at the reply, not at the join.

    Discovering it at the join means a guild that reports itself configured
    and records nothing until the next person happens to read the pod's
    logs.
    """
    with pytest.raises(ValueError, match="not one"):
        await store.set(GUILD, settings.VOICE_CHANNEL_IDS, "12345,general", T0)
    assert await store.get_stored(GUILD, settings.VOICE_CHANNEL_IDS) is None


async def test_a_list_naming_one_channel_twice_is_refused_at_the_write(
    store: ConfigStore,
) -> None:
    with pytest.raises(ValueError, match="more than once"):
        await store.set(GUILD, settings.VOICE_CHANNEL_IDS, "12345,12345", T0)


async def test_a_list_of_several_channels_is_accepted(store: ConfigStore) -> None:
    await store.set(GUILD, settings.VOICE_CHANNEL_IDS, "12345, 67890", T0)
    assert await store.get_stored(GUILD, settings.VOICE_CHANNEL_IDS) == "12345, 67890"


async def test_a_fully_configured_guild_is_missing_nothing(store: ConfigStore) -> None:
    for key in settings.REQUIRED_KEYS:
        await store.set(GUILD, key, "1", T0)
    assert await missing_required(store, GUILD) == []


async def test_an_unknown_key_is_rejected(store: ConfigStore) -> None:
    """A typo must not silently store a setting nobody reads."""
    with pytest.raises(ValueError, match="unknown"):
        await store.set(GUILD, "voice_chanel_id", "1", T0)
