"""The names behind the collection UUID an administrator pasted.

`document_target` is an Outline collection UUID, and `api` has no Outline
token to resolve it. `worker`, which does, sweeps the collection list into
this table. The properties tested are the same two the guild mirrors have:
a full replacement, so a deleted collection stops being offered, and an
empty mirror that reads as "no names known" rather than as an error.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.outline_collections import OutlineCollectionStore

T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


@pytest.fixture
async def store(clean_database: str) -> OutlineCollectionStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return OutlineCollectionStore(async_sessionmaker(engine, expire_on_commit=False))


def _collection(collection_id: str, name: str) -> MirroredCollection:
    return MirroredCollection(collection_id=collection_id, name=name)


async def test_nothing_is_named_until_the_worker_has_swept(
    store: OutlineCollectionStore,
) -> None:
    assert await store.all() == []


async def test_a_swept_collection_can_be_named(store: OutlineCollectionStore) -> None:
    await store.replace([_collection("col-1", "Meetings")], T0)
    assert await store.all() == [_collection("col-1", "Meetings")]


async def test_a_collection_that_was_deleted_is_no_longer_offered(
    store: OutlineCollectionStore,
) -> None:
    """A picker that still offers a deleted collection invites an
    administrator to point `document_target` at somewhere every protocol
    will then fail to be written to.
    """
    await store.replace([_collection("col-1", "Meetings"), _collection("col-2", "Old")], T0)
    await store.replace([_collection("col-1", "Meetings")], T1)
    assert await store.all() == [_collection("col-1", "Meetings")]


async def test_a_renamed_collection_keeps_its_id(store: OutlineCollectionStore) -> None:
    """`document_target` stores the id, so a rename must move the name and
    leave the id the configuration points at alone.
    """
    await store.replace([_collection("col-1", "Meetings")], T0)
    await store.replace([_collection("col-1", "Team meetings")], T1)
    assert await store.all() == [_collection("col-1", "Team meetings")]


async def test_collections_come_back_in_a_stable_order(
    store: OutlineCollectionStore,
) -> None:
    """By name, because that is what a person picking one is reading.
    Stable so the same mirror does not shuffle itself between page loads.
    """
    await store.replace(
        [_collection("col-2", "Meetings"), _collection("col-1", "Archive")],
        T0,
    )
    assert [collection.name for collection in await store.all()] == ["Archive", "Meetings"]


async def test_an_empty_sweep_empties_the_mirror(store: OutlineCollectionStore) -> None:
    """An Outline instance whose last collection was deleted has none, and
    the mirror must be able to say so. The *unreachable* case is a
    different one and never reaches this method -- see
    `sturnus.application.collection_mirror.sweep_outline_collections`.
    """
    await store.replace([_collection("col-1", "Meetings")], T0)
    await store.replace([], T1)
    assert await store.all() == []


async def test_the_same_collection_twice_in_one_sweep_does_not_abort_it(
    store: OutlineCollectionStore,
) -> None:
    """Pagination can repeat an entry across page boundaries, and a
    primary-key violation would lose the whole mirror for that sweep.
    """
    await store.replace([_collection("col-1", "Meetings"), _collection("col-1", "Meetings")], T0)
    assert await store.all() == [_collection("col-1", "Meetings")]
