"""The names behind the ids the console makes people type.

`api` has no Discord token and must never be given one, so it cannot ask
what channel `1234...` is. `bot` writes the names here and `api` reads
them -- the `admin_member` arrangement, widened. What is tested is the
property that arrangement lives or dies on: a full replacement per guild,
so that something deleted in Discord stops being offered, and one guild's
sweep never touches another's rows.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import InstrumentedAttribute

from sturnus.application.directory_mirror import (
    TEXT,
    VOICE,
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.infrastructure.db.directory import DirectoryStore
from sturnus.infrastructure.db.models import Base, GuildChannel, GuildMember, GuildRole

GUILD, OTHER_GUILD = 1, 2
ANNA, BEN = 100, 200
T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)


@pytest.fixture
async def store(clean_database: str) -> DirectoryStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DirectoryStore(async_sessionmaker(engine, expire_on_commit=False))


def _channel(channel_id: int, name: str, kind: str = VOICE, position: int = 0) -> MirroredChannel:
    return MirroredChannel(channel_id=channel_id, name=name, kind=kind, position=position)


def _role(role_id: int, name: str, position: int = 0) -> MirroredRole:
    return MirroredRole(role_id=role_id, name=name, position=position)


def _member(discord_user_id: int, display_name: str) -> MirroredMember:
    return MirroredMember(discord_user_id=discord_user_id, display_name=display_name)


async def _stamps(url: str, column: InstrumentedAttribute[datetime]) -> list[datetime]:
    """Every `synced_at` in one mirrored table, oldest first.

    The only way to see from outside whether a sweep wrote anything: a
    row rewritten with the values it already had is indistinguishable
    through `channels_for` and yet costs Postgres a new row version.
    """
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(select(column).order_by(column))
            return list(rows.scalars().all())
    finally:
        await engine.dispose()


async def test_a_guild_nothing_has_swept_yet_offers_nothing(store: DirectoryStore) -> None:
    """An empty mirror must read as "no names known", never as an error --
    the console falls back to showing ids, which is what it does today.
    """
    assert await store.channels_for(GUILD) == []
    assert await store.roles_for(GUILD) == []
    assert await store.members_for(GUILD) == []


async def test_a_swept_channel_can_be_named(store: DirectoryStore) -> None:
    await store.replace_channels(GUILD, [_channel(10, "meeting", VOICE, 3)], T0)
    assert await store.channels_for(GUILD) == [_channel(10, "meeting", VOICE, 3)]


async def test_a_channel_that_vanished_is_no_longer_offered(store: DirectoryStore) -> None:
    """The reason this is a replacement and not a merge. A mirror that only
    ever grows would go on offering a channel nobody can join, and the
    administrator picking it would configure a recording that never starts.
    """
    await store.replace_channels(GUILD, [_channel(10, "meeting"), _channel(11, "standup")], T0)
    await store.replace_channels(GUILD, [_channel(10, "meeting")], T1)
    assert [channel.channel_id for channel in await store.channels_for(GUILD)] == [10]


async def test_a_renamed_channel_keeps_its_id_and_takes_the_new_name(
    store: DirectoryStore,
) -> None:
    """A rename is the ordinary case, and it must not orphan the id the
    guild's configuration already points at.
    """
    await store.replace_channels(GUILD, [_channel(10, "meeting")], T0)
    await store.replace_channels(GUILD, [_channel(10, "weekly")], T1)
    assert await store.channels_for(GUILD) == [_channel(10, "weekly")]


async def test_one_guilds_sweep_leaves_another_guilds_names_alone(
    store: DirectoryStore,
) -> None:
    """Sweeps run per guild. A replacement that was not scoped would blank
    every other guild's mirror until its own sweep next came round.
    """
    await store.replace_channels(GUILD, [_channel(10, "meeting")], T0)
    await store.replace_channels(OTHER_GUILD, [_channel(20, "elsewhere")], T0)
    await store.replace_channels(GUILD, [], T1)
    assert await store.channels_for(OTHER_GUILD) == [_channel(20, "elsewhere")]


async def test_a_channel_kind_this_code_has_never_seen_is_still_written(
    store: DirectoryStore,
) -> None:
    """`kind` is a plain string precisely so Discord adding a channel type
    is a row a reader ignores rather than a failed write that takes the
    rest of the guild's channels with it.
    """
    await store.replace_channels(GUILD, [_channel(10, "stage", "stage")], T0)
    assert [channel.kind for channel in await store.channels_for(GUILD)] == ["stage"]


async def test_channels_come_back_in_the_order_discord_shows_them(
    store: DirectoryStore,
) -> None:
    """A picker that reorders a server's channels does not look like the
    server it configures. Ties break on id so two reads always agree.
    """
    await store.replace_channels(
        GUILD,
        [_channel(12, "c", TEXT, 2), _channel(10, "a", VOICE, 1), _channel(11, "b", VOICE, 1)],
        T0,
    )
    assert [channel.channel_id for channel in await store.channels_for(GUILD)] == [10, 11, 12]


async def test_a_swept_role_can_be_named(store: DirectoryStore) -> None:
    await store.replace_roles(GUILD, [_role(50, "recorded", 7)], T0)
    assert await store.roles_for(GUILD) == [_role(50, "recorded", 7)]


async def test_a_role_that_was_deleted_is_no_longer_offered(store: DirectoryStore) -> None:
    """The same argument as a channel, with a sharper edge: a picker that
    still offers a deleted role invites an administrator to configure a
    consent gate nobody can pass.
    """
    await store.replace_roles(GUILD, [_role(50, "recorded"), _role(51, "staff")], T0)
    await store.replace_roles(GUILD, [_role(50, "recorded")], T1)
    assert [role.role_id for role in await store.roles_for(GUILD)] == [50]


async def test_roles_come_back_in_the_order_discord_shows_them(store: DirectoryStore) -> None:
    await store.replace_roles(GUILD, [_role(51, "b", 2), _role(50, "a", 1)], T0)
    assert [role.role_id for role in await store.roles_for(GUILD)] == [50, 51]


async def test_a_swept_member_can_be_named(store: DirectoryStore) -> None:
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T0)
    assert await store.members_for(GUILD) == [_member(ANNA, "Anna")]


async def test_somebody_who_lost_both_roles_stops_being_named(store: DirectoryStore) -> None:
    """The mirror holds only the people the console has reason to name.
    Somebody who revoked their consent and left the roster must fall out
    of it rather than stay a name in a table about recordings.
    """
    await store.replace_members(GUILD, [_member(ANNA, "Anna"), _member(BEN, "Ben")], T0)
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T1)
    assert await store.members_for(GUILD) == [_member(ANNA, "Anna")]


async def test_a_changed_nickname_replaces_the_old_one(store: DirectoryStore) -> None:
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T0)
    await store.replace_members(GUILD, [_member(ANNA, "Anna B.")], T1)
    assert await store.members_for(GUILD) == [_member(ANNA, "Anna B.")]


async def test_an_empty_membership_is_a_real_instruction(store: DirectoryStore) -> None:
    """Treating empty as "nothing to do" is exactly how the last person to
    leave a role keeps being named forever.
    """
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T0)
    await store.replace_members(GUILD, [], T1)
    assert await store.members_for(GUILD) == []


async def test_one_guilds_members_are_not_anothers(store: DirectoryStore) -> None:
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T0)
    await store.replace_members(OTHER_GUILD, [_member(BEN, "Ben")], T0)
    assert await store.members_for(GUILD) == [_member(ANNA, "Anna")]
    assert await store.members_for(OTHER_GUILD) == [_member(BEN, "Ben")]


async def test_the_same_id_twice_in_one_sweep_does_not_abort_it(store: DirectoryStore) -> None:
    """Discord can report the same member twice, and a primary-key
    violation here would lose the whole guild's mirror for that sweep.
    """
    await store.replace_members(GUILD, [_member(ANNA, "Anna"), _member(ANNA, "Anna")], T0)
    assert await store.members_for(GUILD) == [_member(ANNA, "Anna")]


async def test_a_sweep_that_found_nothing_new_writes_nothing(
    store: DirectoryStore, clean_database: str
) -> None:
    """The operational point of the whole store, and the reason it reads
    before it writes.

    This sweep runs every ten seconds, for every guild, forever, against
    tables whose contents change a handful of times a year. An
    unconditional `ON CONFLICT DO UPDATE` that only ever restamps
    `synced_at` still writes a new row version per row per tick -- for
    fifty guilds of forty channels and thirty roles that is millions of
    dead tuples a day, and sustained autovacuum and index bloat, for data
    nobody is reading. `synced_at` standing still is what says no
    statement was issued.
    """
    await store.replace_channels(GUILD, [_channel(10, "meeting", VOICE, 3)], T0)
    await store.replace_roles(GUILD, [_role(50, "recorded", 7)], T0)
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T0)

    await store.replace_channels(GUILD, [_channel(10, "meeting", VOICE, 3)], T1)
    await store.replace_roles(GUILD, [_role(50, "recorded", 7)], T1)
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T1)

    assert await _stamps(clean_database, GuildChannel.synced_at) == [T0]
    assert await _stamps(clean_database, GuildRole.synced_at) == [T0]
    assert await _stamps(clean_database, GuildMember.synced_at) == [T0]


async def test_a_sweep_that_found_a_rename_writes_it(
    store: DirectoryStore, clean_database: str
) -> None:
    """The other half: writing only on change must not become writing
    only sometimes. A rename is the ordinary case this mirror exists for.
    """
    await store.replace_channels(GUILD, [_channel(10, "meeting")], T0)
    await store.replace_channels(GUILD, [_channel(10, "weekly")], T1)
    assert await store.channels_for(GUILD) == [_channel(10, "weekly")]
    assert await _stamps(clean_database, GuildChannel.synced_at) == [T1]


async def test_a_channel_that_only_moved_is_still_written(
    store: DirectoryStore, clean_database: str
) -> None:
    """`position` is mirrored so the picker looks like the server it
    configures, so a channel dragged up the sidebar is a real change even
    though its name and id did not move. Comparing on identity alone
    would freeze the order at whatever it was on the first sweep.
    """
    await store.replace_channels(GUILD, [_channel(10, "meeting", VOICE, 3)], T0)
    await store.replace_channels(GUILD, [_channel(10, "meeting", VOICE, 1)], T1)
    assert await store.channels_for(GUILD) == [_channel(10, "meeting", VOICE, 1)]
    assert await _stamps(clean_database, GuildChannel.synced_at) == [T1]


async def test_a_role_that_only_moved_is_still_written(
    store: DirectoryStore, clean_database: str
) -> None:
    await store.replace_roles(GUILD, [_role(50, "recorded", 7)], T0)
    await store.replace_roles(GUILD, [_role(50, "recorded", 2)], T1)
    assert await store.roles_for(GUILD) == [_role(50, "recorded", 2)]
    assert await _stamps(clean_database, GuildRole.synced_at) == [T1]


async def test_a_departure_is_written_even_though_nobody_was_added(
    store: DirectoryStore, clean_database: str
) -> None:
    """Comparing sets rather than counting them: somebody who revoked
    their consent leaves a strictly smaller membership, and a comparison
    that only looked for new names would keep naming them.
    """
    await store.replace_members(GUILD, [_member(ANNA, "Anna"), _member(BEN, "Ben")], T0)
    await store.replace_members(GUILD, [_member(ANNA, "Anna")], T1)
    assert await store.members_for(GUILD) == [_member(ANNA, "Anna")]
    assert await _stamps(clean_database, GuildMember.synced_at) == [T1]
