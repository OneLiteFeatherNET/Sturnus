from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD = 4711


@pytest.fixture
async def store(clean_database: str) -> ConfigStore:
    engine: AsyncEngine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return ConfigStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_unset_key_without_default_is_none(store: ConfigStore) -> None:
    assert await store.get(GUILD, settings.VOICE_CHANNEL_ID) is None


async def test_unset_key_falls_back_to_default(store: ConfigStore) -> None:
    assert await store.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "15"


async def test_stored_value_wins_over_default(store: ConfigStore) -> None:
    await store.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    assert await store.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "45"


async def test_set_is_idempotent_and_updates_in_place(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "8", T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "8"


async def test_clearing_a_value_restores_the_default(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    await store.set(GUILD, settings.MAX_SESSION_HOURS, None, T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "4"


async def test_guilds_are_isolated(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    assert await store.get(9999, settings.MAX_SESSION_HOURS) == "4"


async def test_timeouts_are_assembled_from_config(store: ConfigStore) -> None:
    await store.set(GUILD, settings.EMPTY_GRACE_SECONDS, "90", T0)
    timeouts = await store.timeouts(GUILD)
    assert timeouts.empty_grace_seconds == 90
    assert timeouts.idle_timeout_minutes == 15  # default value
    assert timeouts.max_session_hours == 4


async def test_set_rejects_a_non_numeric_value_for_an_integer_key(store: ConfigStore) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        await store.set(GUILD, settings.MAX_SESSION_HOURS, "not-a-number", T0)


async def test_set_rejects_a_non_positive_value_for_an_integer_key(store: ConfigStore) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        await store.set(GUILD, settings.MAX_SESSION_HOURS, "0", T0)
    with pytest.raises(ValueError, match="must be positive"):
        await store.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "-5", T0)


async def test_rejected_set_does_not_change_the_stored_value(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    with pytest.raises(ValueError):
        await store.set(GUILD, settings.MAX_SESSION_HOURS, "bogus", T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "6"
