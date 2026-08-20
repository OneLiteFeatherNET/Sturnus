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
    await store.set(GUILD, settings.VOICE_CHANNEL_ID, "12345", T0)
    assert settings.VOICE_CHANNEL_ID not in await missing_required(store, GUILD)


async def test_a_fully_configured_guild_is_missing_nothing(store: ConfigStore) -> None:
    for key in settings.REQUIRED_KEYS:
        await store.set(GUILD, key, "1", T0)
    assert await missing_required(store, GUILD) == []


async def test_an_unknown_key_is_rejected(store: ConfigStore) -> None:
    """A typo must not silently store a setting nobody reads."""
    with pytest.raises(ValueError, match="unknown"):
        await store.set(GUILD, "voice_chanel_id", "1", T0)
