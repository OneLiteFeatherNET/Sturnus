from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.models import Base

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
ANNA = 100


@pytest.fixture
async def store(clean_database: str) -> LinkStateStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return LinkStateStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_an_issued_state_resolves_to_its_user(store: LinkStateStore) -> None:
    state = await store.issue(ANNA, "outline", T0, TTL)
    pending = await store.consume(state, T0 + timedelta(minutes=1))
    assert pending is not None
    assert pending.discord_user_id == ANNA
    assert pending.provider == "outline"


async def test_a_state_can_only_be_used_once(store: LinkStateStore) -> None:
    """Replaying a callback must not link a second time."""
    state = await store.issue(ANNA, "outline", T0, TTL)
    assert await store.consume(state, T0) is not None
    assert await store.consume(state, T0) is None


async def test_an_expired_state_is_refused(store: LinkStateStore) -> None:
    state = await store.issue(ANNA, "outline", T0, TTL)
    assert await store.consume(state, T0 + TTL + timedelta(seconds=1)) is None


async def test_an_unknown_state_is_refused(store: LinkStateStore) -> None:
    """A forged callback must not resolve to anyone."""
    assert await store.consume("not-a-real-state", T0) is None


async def test_purging_removes_only_expired_states(store: LinkStateStore) -> None:
    old = await store.issue(ANNA, "outline", T0 - timedelta(hours=1), TTL)
    fresh = await store.issue(ANNA, "outline", T0, TTL)
    removed = await store.purge_expired(T0)
    assert removed == 1
    assert await store.consume(old, T0) is None
    assert await store.consume(fresh, T0) is not None
