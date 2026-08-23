"""A session has documents, plural, once a guild publishes to more than one place.

`session.document_url` stays the primary -- it is what the announcement
posts and what everything already reading a session reads. This table is
what the second, third and fourth destination get, so that one failing
publish does not lose the others.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.crypto import KeyWrapper
from sturnus.infrastructure.db.export_targets import ExportTargetStore
from sturnus.infrastructure.db.models import Base, Session
from sturnus.infrastructure.db.session_documents import SessionDocumentStore

GUILD = 1
T0 = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def minutes(count: int) -> timedelta:
    return timedelta(minutes=count)


@pytest.fixture
async def sessions(clean_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session_id(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as db:
        recording = Session(guild_id=GUILD, channel_id=10, started_at=T0, status="closed")
        db.add(recording)
        await db.commit()
        return recording.id


@pytest.fixture
def targets(sessions: async_sessionmaker[AsyncSession]) -> ExportTargetStore:
    return ExportTargetStore(sessions, KeyWrapper(b"m" * 32, "master-1"))


@pytest.fixture
def store(sessions: async_sessionmaker[AsyncSession]) -> SessionDocumentStore:
    return SessionDocumentStore(sessions)


async def test_a_session_nobody_published_has_no_documents(
    store: SessionDocumentStore, session_id: int
) -> None:
    assert await store.for_session(session_id) == ()


async def test_each_destination_records_its_own_document(
    store: SessionDocumentStore, targets: ExportTargetStore, session_id: int
) -> None:
    """The point of the table. Two destinations, two rows, neither lost."""
    wiki = await targets.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    minutes_target = await targets.save(
        GUILD, format="outline", name="Minutes", target="col-1", config={}, now=T0
    )
    await store.record(
        session_id,
        target_id=wiki,
        provider="confluence",
        document_id="123",
        url="https://wiki.example/123",
        now=T0,
    )
    await store.record(
        session_id,
        target_id=minutes_target,
        provider="outline",
        document_id="abc",
        url="https://outline.example/abc",
        now=T0 + minutes(1),
    )

    stored = await store.for_session(session_id)
    assert [document.provider for document in stored] == ["confluence", "outline"]
    assert [document.target_id for document in stored] == [wiki, minutes_target]


async def test_publishing_again_to_the_same_destination_replaces_the_row(
    store: SessionDocumentStore, targets: ExportTargetStore, session_id: int
) -> None:
    """A re-export overwrites its own destination and nothing else.

    Appending would leave a session pointing at two documents in the same
    place, one of which is stale, with nothing saying which.
    """
    wiki = await targets.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.record(
        session_id, target_id=wiki, provider="confluence", document_id="1", url="u1", now=T0
    )
    await store.record(
        session_id,
        target_id=wiki,
        provider="confluence",
        document_id="2",
        url="u2",
        now=T0 + minutes(5),
    )

    (stored,) = await store.for_session(session_id)
    assert stored.document_id == "2"
    assert stored.url == "u2"


async def test_removing_a_destination_keeps_what_it_published(
    store: SessionDocumentStore, targets: ExportTargetStore, session_id: int
) -> None:
    """The document still exists in the other system; the pointer to it should too.

    Deleting the target row is an administrator saying "stop publishing
    here", not "forget what was published". The link is what somebody
    follows when they go looking for last quarter's minutes.
    """
    wiki = await targets.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.record(
        session_id,
        target_id=wiki,
        provider="confluence",
        document_id="1",
        url="https://wiki.example/1",
        now=T0,
    )
    await targets.delete(GUILD, wiki)

    (stored,) = await store.for_session(session_id)
    assert stored.target_id is None
    assert stored.url == "https://wiki.example/1"
    assert stored.provider == "confluence"


async def test_deleting_a_session_deletes_its_documents(
    store: SessionDocumentStore,
    targets: ExportTargetStore,
    sessions: async_sessionmaker[AsyncSession],
    session_id: int,
) -> None:
    """`session_participant` and `session_tag` cascade; so does this."""
    wiki = await targets.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.record(
        session_id, target_id=wiki, provider="confluence", document_id="1", url="u", now=T0
    )
    async with sessions() as db:
        await db.execute(delete(Session).where(Session.id == session_id))
        await db.commit()

    assert await store.for_session(session_id) == ()


async def test_documents_read_back_oldest_first(
    store: SessionDocumentStore, targets: ExportTargetStore, session_id: int
) -> None:
    """Publication order, so the list reads as the history it is.

    Not by name and not by target id: what a reader wants from a list of
    documents is which one was written first.
    """
    second = await targets.save(GUILD, format="outline", name="Zulu", target="z", config={}, now=T0)
    first = await targets.save(GUILD, format="outline", name="Alfa", target="a", config={}, now=T0)
    await store.record(
        session_id, target_id=second, provider="outline", document_id="z", url="z", now=T0
    )
    await store.record(
        session_id,
        target_id=first,
        provider="outline",
        document_id="a",
        url="a",
        now=T0 + minutes(1),
    )

    assert [document.document_id for document in await store.for_session(session_id)] == ["z", "a"]
