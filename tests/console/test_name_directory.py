"""The two read adapters over the name mirrors, against the real database.

No doubles here, on purpose. Both properties under test are properties of
SQL: the ordering is done in the statement, so a fake that sorted in
Python would only ever prove that the fake sorts; and the authorisation is
one `is_admin` inside the one call, which is not worth proving against a
dictionary.

The rows are written through the real `DirectoryStore` and
`OutlineCollectionStore` -- the writers the bot and the worker actually
use -- so a test here reads what those two really produce rather than what
this file believes they produce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.application.directory_mirror import (
    TEXT,
    VOICE,
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.console.adapters import ConsoleCollectionNames, ConsoleGuildNames
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.directory import DirectoryStore
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.outline_collections import OutlineCollectionStore

T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)

GUILD, OTHER_GUILD = 4711, 8822
ANNA, BEN = 100, 200


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def an_administrator(
    factory: async_sessionmaker[AsyncSession], guild_id: int = GUILD, who: int = ANNA
) -> AdminMemberStore:
    admins = AdminMemberStore(factory)
    await admins.replace(guild_id, [who], T0)
    return admins


def names(factory: async_sessionmaker[AsyncSession], admins: AdminMemberStore) -> ConsoleGuildNames:
    return ConsoleGuildNames(factory, admins)


# ---------------------------------------------------------------------------
# A guild's channels, roles and named people
# ---------------------------------------------------------------------------


async def test_an_administrator_reads_the_names_the_bot_wrote_down(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = await an_administrator(factory)
    store = DirectoryStore(factory)
    await store.replace_channels(GUILD, [MirroredChannel(1, "Standup", VOICE, 3)], T0)
    await store.replace_roles(GUILD, [MirroredRole(77, "Recorded", 7)], T0)
    await store.replace_members(GUILD, [MirroredMember(ANNA, "Anna Example")], T0)

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert [channel.name for channel in directory.channels] == ["Standup"]
    assert [role.name for role in directory.roles] == ["Recorded"]
    assert [member.display_name for member in directory.members] == ["Anna Example"]


async def test_channels_are_grouped_by_kind_and_then_read_as_discord_shows_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Kind first, because somebody looking for a voice channel is not
    reading past the text ones; position second, because that is the
    order in the other window; name last, so two channels sharing a
    position do not swap places between two page loads.
    """
    admins = await an_administrator(factory)
    await DirectoryStore(factory).replace_channels(
        GUILD,
        [
            MirroredChannel(4, "announcements", TEXT, 1),
            MirroredChannel(3, "Standup", VOICE, 2),
            MirroredChannel(2, "Zebra", VOICE, 1),
            MirroredChannel(1, "Alpha", VOICE, 1),
        ],
        T0,
    )

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert [channel.name for channel in directory.channels] == [
        "announcements",
        "Alpha",
        "Zebra",
        "Standup",
    ]


async def test_roles_are_ordered_by_discords_own_sense_of_importance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Descending, which is how Discord lists them: the role at the top
    of the server settings is the one an administrator means first.
    """
    admins = await an_administrator(factory)
    await DirectoryStore(factory).replace_roles(
        GUILD,
        [MirroredRole(1, "Members", 1), MirroredRole(2, "Admins", 9), MirroredRole(3, "Bots", 5)],
        T0,
    )

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert [role.name for role in directory.roles] == ["Admins", "Bots", "Members"]


async def test_members_are_ordered_by_the_name_somebody_is_looking_for(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """By name and not by id: a person scanning the list is reading
    names, and an id order is a shuffle to everybody but the database.
    """
    admins = await an_administrator(factory)
    await DirectoryStore(factory).replace_members(
        GUILD,
        [MirroredMember(300, "Anna"), MirroredMember(100, "Zoe"), MirroredMember(200, "Ben")],
        T0,
    )

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert [member.display_name for member in directory.members] == ["Anna", "Ben", "Zoe"]


async def test_the_freshness_reported_is_that_of_the_stalest_mirror(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One timestamp for three tables, and it is the oldest of them.

    A sweep writes all three on one tick, so normally they agree. When
    they do not -- one write failed and the next sweep has not come round
    -- claiming the freshest of them would tell a reader the whole
    directory is four minutes old when part of it is a day old.
    """
    admins = await an_administrator(factory)
    store = DirectoryStore(factory)
    await store.replace_channels(GUILD, [MirroredChannel(1, "Standup", VOICE, 0)], T0)
    await store.replace_roles(GUILD, [MirroredRole(77, "Recorded", 7)], T1)
    await store.replace_members(GUILD, [MirroredMember(ANNA, "Anna")], T2)

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert directory.synced_at == T0


async def test_a_guild_the_bot_has_not_swept_yet_is_empty_and_undated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = await an_administrator(factory)

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert directory.channels == () and directory.roles == () and directory.members == ()
    assert directory.synced_at is None


async def test_somebody_who_does_not_administer_the_guild_is_told_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = await an_administrator(factory)
    await DirectoryStore(factory).replace_members(GUILD, [MirroredMember(ANNA, "Anna")], T0)

    assert await names(factory, admins).for_guild(GUILD, requested_by=BEN) is None


async def test_administering_one_guild_does_not_name_anything_in_another(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An administrator of one guild is nobody in another, and the
    statement is scoped as well -- so even the guild they do administer
    never shows a channel from somewhere else.
    """
    admins = await an_administrator(factory)
    store = DirectoryStore(factory)
    await store.replace_channels(GUILD, [MirroredChannel(1, "Ours", VOICE, 0)], T0)
    await store.replace_channels(OTHER_GUILD, [MirroredChannel(2, "Theirs", VOICE, 0)], T0)

    directory = await names(factory, admins).for_guild(GUILD, requested_by=ANNA)

    assert directory is not None
    assert [channel.name for channel in directory.channels] == ["Ours"]
    assert await names(factory, admins).for_guild(OTHER_GUILD, requested_by=ANNA) is None


# ---------------------------------------------------------------------------
# The Outline collections
# ---------------------------------------------------------------------------


async def test_an_administrator_of_any_guild_may_read_the_collection_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The list is not guild-scoped -- one deployment, one Outline -- so
    the only question this can ask is whether the caller administers
    anything at all.
    """
    admins = await an_administrator(factory, OTHER_GUILD, ANNA)
    await OutlineCollectionStore(factory).replace([MirroredCollection("c-1", "Meetings")], T0)

    listing = await ConsoleCollectionNames(factory, admins).mirrored(requested_by=ANNA)

    assert listing is not None
    assert [collection.name for collection in listing.collections] == ["Meetings"]


async def test_somebody_who_administers_nothing_is_told_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = await an_administrator(factory)
    await OutlineCollectionStore(factory).replace([MirroredCollection("c-1", "Meetings")], T0)

    assert await ConsoleCollectionNames(factory, admins).mirrored(requested_by=BEN) is None


async def test_collections_are_ordered_the_way_outlines_own_sidebar_is(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = await an_administrator(factory)
    await OutlineCollectionStore(factory).replace(
        [
            MirroredCollection("c-2", "Retrospectives"),
            MirroredCollection("c-1", "Meetings"),
            MirroredCollection("c-3", "Architecture"),
        ],
        T0,
    )

    listing = await ConsoleCollectionNames(factory, admins).mirrored(requested_by=ANNA)

    assert listing is not None
    assert [collection.name for collection in listing.collections] == [
        "Architecture",
        "Meetings",
        "Retrospectives",
    ]


async def test_the_collection_list_carries_when_the_worker_last_swept_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admins = await an_administrator(factory)
    await OutlineCollectionStore(factory).replace([MirroredCollection("c-1", "Meetings")], T1)

    listing = await ConsoleCollectionNames(factory, admins).mirrored(requested_by=ANNA)

    assert listing is not None
    assert listing.synced_at == T1


async def test_a_collection_list_nothing_ever_swept_is_empty_and_undated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty rather than absent: an administrator who administers a guild
    is entitled to the answer "the worker has not looked yet", which is
    what an empty list with no date says.
    """
    admins = await an_administrator(factory)

    listing = await ConsoleCollectionNames(factory, admins).mirrored(requested_by=ANNA)

    assert listing is not None
    assert listing.collections == ()
    assert listing.synced_at is None
