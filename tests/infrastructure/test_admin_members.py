"""The bridge that lets `api` know who administers the bot.

`admin_role_id` is a Discord role and `api` has no gateway to ask about
role membership -- by design: a process that can decrypt every recording
in the system is not one to also hand the ability to act as the bot
(Spec 13.2, and the console design's Section 2.1). So `bot`, which does
hold the members intent, mirrors the membership into a table `api` can
read.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.models import Base

GUILD = 1
OTHER_GUILD = 2
ANNA, BEN, CARA = 100, 200, 300
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def store(clean_database: str) -> AdminMemberStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return AdminMemberStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_nobody_is_an_administrator_until_the_bot_says_so(
    store: AdminMemberStore,
) -> None:
    """The safe default. An empty table must never read as "everyone"."""
    assert await store.is_admin(GUILD, ANNA) is False


async def test_a_synced_member_is_an_administrator(store: AdminMemberStore) -> None:
    await store.replace(GUILD, [ANNA, BEN], T0)
    assert await store.is_admin(GUILD, ANNA) is True
    assert await store.is_admin(GUILD, BEN) is True


async def test_someone_who_lost_the_role_stops_being_an_administrator(
    store: AdminMemberStore,
) -> None:
    """The whole reason this is a replace and not an insert.

    A revoked role that leaves a stale row behind is a privilege that
    outlives its grant -- and nothing in the console would ever show it,
    because the console only ever asks "is this person an admin", never
    "why".
    """
    await store.replace(GUILD, [ANNA, BEN], T0)
    await store.replace(GUILD, [ANNA], T0 + timedelta(minutes=5))
    assert await store.is_admin(GUILD, ANNA) is True
    assert await store.is_admin(GUILD, BEN) is False


async def test_one_guilds_administrators_are_not_anothers(store: AdminMemberStore) -> None:
    """Sturnus serves more than one guild, and an admin in one is an
    ordinary participant in another.
    """
    await store.replace(GUILD, [ANNA], T0)
    await store.replace(OTHER_GUILD, [BEN], T0)
    assert await store.is_admin(GUILD, BEN) is False
    assert await store.is_admin(OTHER_GUILD, ANNA) is False


async def test_replacing_one_guild_leaves_another_untouched(store: AdminMemberStore) -> None:
    """A sync sweep runs per guild. A bug that cleared the table wholesale
    would silently de-admin everybody in every other guild until their own
    sweep next ran.
    """
    await store.replace(GUILD, [ANNA], T0)
    await store.replace(OTHER_GUILD, [BEN], T0)
    await store.replace(GUILD, [CARA], T0 + timedelta(minutes=1))
    assert await store.is_admin(OTHER_GUILD, BEN) is True


async def test_syncing_an_empty_list_removes_everyone(store: AdminMemberStore) -> None:
    """A guild whose admin role has no members has no administrators.

    Not a no-op: treating "empty" as "nothing to do" is how the last
    administrator to be removed keeps their access forever.
    """
    await store.replace(GUILD, [ANNA, BEN], T0)
    await store.replace(GUILD, [], T0 + timedelta(minutes=1))
    assert await store.is_admin(GUILD, ANNA) is False


async def test_syncing_the_same_membership_twice_is_stable(store: AdminMemberStore) -> None:
    """The sweep runs on a timer, so the unchanged case is the common one."""
    await store.replace(GUILD, [ANNA, BEN], T0)
    await store.replace(GUILD, [ANNA, BEN], T0 + timedelta(minutes=1))
    assert await store.is_admin(GUILD, ANNA) is True
    assert await store.is_admin(GUILD, BEN) is True


async def test_a_duplicate_in_the_incoming_list_does_not_break_the_sync(
    store: AdminMemberStore,
) -> None:
    """Discord can report a member twice across paginated role queries, and
    a primary-key violation here would abort the whole sweep.
    """
    await store.replace(GUILD, [ANNA, ANNA, BEN], T0)
    assert await store.is_admin(GUILD, ANNA) is True
    assert await store.is_admin(GUILD, BEN) is True


async def test_the_administrators_of_a_guild_can_be_listed(store: AdminMemberStore) -> None:
    await store.replace(GUILD, [BEN, ANNA], T0)
    assert await store.administrators(GUILD) == (ANNA, BEN)


async def test_listing_is_ordered_so_two_reads_agree(store: AdminMemberStore) -> None:
    """Unordered, the same membership renders in a different order on every
    page load. Sorted by id is arbitrary but stable, which is the property
    that matters.
    """
    await store.replace(GUILD, [CARA, ANNA, BEN], T0)
    assert await store.administrators(GUILD) == (ANNA, BEN, CARA)


async def test_is_admin_anywhere_answers_without_naming_a_guild(
    store: AdminMemberStore,
) -> None:
    """The console signs a person in by Discord id alone -- the OAuth
    identity carries no guild. Settings are per guild, so the question
    "may this person see the settings section at all" is answered across
    every guild the bot serves.
    """
    await store.replace(OTHER_GUILD, [BEN], T0)
    assert await store.is_admin_anywhere(BEN) is True
    assert await store.is_admin_anywhere(ANNA) is False


async def test_the_guilds_one_person_administers_can_be_listed(
    store: AdminMemberStore,
) -> None:
    """The reverse of `administrators`, and what the console's guild picker
    is: a person signs in knowing no guild, and this is the only thing that
    tells them which ones are theirs to configure.
    """
    await store.replace(GUILD, [ANNA, BEN], T0)
    await store.replace(OTHER_GUILD, [BEN], T0)
    assert await store.administered_guilds(ANNA) == (GUILD,)


async def test_somebody_who_administers_nothing_gets_an_empty_list(
    store: AdminMemberStore,
) -> None:
    """An ordinary state, not an error: a participant who signed in to look
    at their own recordings administers nothing at all.
    """
    await store.replace(GUILD, [BEN], T0)
    assert await store.administered_guilds(ANNA) == ()


async def test_the_guild_listing_is_ordered_so_two_reads_agree(
    store: AdminMemberStore,
) -> None:
    await store.replace(OTHER_GUILD, [ANNA], T0)
    await store.replace(GUILD, [ANNA], T0)
    assert await store.administered_guilds(ANNA) == tuple(sorted((GUILD, OTHER_GUILD)))
