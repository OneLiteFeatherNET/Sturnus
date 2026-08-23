"""The console's write into `guild_setup_intent`, against the real database.

No doubles, on purpose: the two properties under test are properties of
the rows. Authorisation is one `is_admin` inside the one call, and
`seen_at` is a column of the guild mirror rather than of the intent --
which is the whole point of reading it, because it answers a different
question from anything an intent can.

The rows are written through the real `SetupIntentStore` and
`DirectoryStore`, the writers the bot actually uses, so a test here reads
what those really produce rather than what this file believes they
produce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.directory_mirror import MirroredGuild
from sturnus.console.adapters import ConsoleGuildSetup
from sturnus.domain.onboarding import APPLIED
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.directory import DirectoryStore
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.setup_intents import SetupIntentStore

T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)

GUILD, OTHER_GUILD = 4711, 8822
ANNA, BEN = 100, 200
STANDUP, RETRO = 10, 11


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def a_setup(
    factory: async_sessionmaker[AsyncSession], *, administers: int = ANNA
) -> ConsoleGuildSetup:
    admins = AdminMemberStore(factory)
    await admins.replace(GUILD, [administers], T0)
    return ConsoleGuildSetup(factory, admins, SetupIntentStore(factory))


async def test_somebody_who_administers_nothing_is_told_what_a_missing_guild_would_tell_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """404 from the route, and `None` from here that makes it one."""
    setup = await a_setup(factory)
    assert await setup.state(GUILD, requested_by=BEN) is None


async def test_somebody_who_administers_nothing_cannot_ask_for_anything_either(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The authorisation rule lives inside the call, not in the handler.

    A write is the half that matters: reading somebody else's onboarding
    state is a leak, writing to it is an act on their server.
    """
    setup = await a_setup(factory)

    written = await setup.request(
        GUILD, requested_by=BEN, channel_ids=str(STANDUP), consent_role_name=None, now=T0
    )

    assert written is None
    assert await SetupIntentStore(factory).latest_for(GUILD) is None


async def test_an_administrator_of_one_guild_is_nobody_in_another(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    setup = await a_setup(factory)
    assert await setup.state(OTHER_GUILD, requested_by=ANNA) is None


async def test_a_guild_the_bot_has_never_swept_has_no_moment_to_show(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The state the console renders as "waiting for the bot to arrive".

    Empty mirrors here mean nobody has looked, not that the server has no
    channels -- and the two are indistinguishable without this field.
    """
    setup = await a_setup(factory)

    state = await setup.state(GUILD, requested_by=ANNA)

    assert state is not None
    assert state.seen_at is None
    assert state.intent is None


async def test_a_guild_the_bot_has_swept_carries_when_it_last_looked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    setup = await a_setup(factory)
    await DirectoryStore(factory).replace_guild(MirroredGuild(GUILD, "Acme", None), T1)

    state = await setup.state(GUILD, requested_by=ANNA)

    assert state is not None
    assert state.seen_at == T1


async def test_a_request_comes_back_as_the_guilds_current_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    setup = await a_setup(factory)

    state = await setup.request(
        GUILD,
        requested_by=ANNA,
        channel_ids=f"{STANDUP},{RETRO}",
        consent_role_name="Recorded",
        now=T0,
    )

    assert state is not None
    assert state.intent is not None
    assert state.intent.requested_by == ANNA
    assert state.intent.channel_ids == f"{STANDUP},{RETRO}"
    assert state.intent.consent_role_name == "Recorded"
    assert state.intent.is_pending


async def test_asking_twice_keeps_both_rows_and_answers_with_the_newer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An administrator who asked twice asked twice.

    Collapsing them would lose who asked for which and when. Which one the
    guild is configured from is settled by the bot, which applies the
    newest and settles the rest as superseded.
    """
    setup = await a_setup(factory)
    intents = SetupIntentStore(factory)

    await setup.request(
        GUILD, requested_by=ANNA, channel_ids=str(STANDUP), consent_role_name=None, now=T0
    )
    state = await setup.request(
        GUILD, requested_by=ANNA, channel_ids=str(RETRO), consent_role_name=None, now=T1
    )

    assert len(await intents.pending_for(GUILD)) == 2
    assert state is not None
    assert state.intent is not None
    assert state.intent.channel_ids == str(RETRO)


async def test_the_state_shows_what_the_bot_wrote_back(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole reason the console polls: the answer arrives on a tick."""
    setup = await a_setup(factory)
    intents = SetupIntentStore(factory)
    intent_id = await intents.request(
        GUILD, requested_by=ANNA, channel_ids=str(STANDUP), consent_role_name=None, now=T0
    )
    await intents.record_outcome(intent_id, outcome=APPLIED, error=None, now=T1)

    state = await setup.state(GUILD, requested_by=ANNA)

    assert state is not None
    assert state.intent is not None
    assert state.intent.outcome == APPLIED
    assert state.intent.applied_at == T1


async def test_one_guilds_request_is_not_anothers(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    setup = await a_setup(factory)
    await setup.request(
        GUILD, requested_by=ANNA, channel_ids=str(STANDUP), consent_role_name=None, now=T0
    )

    assert await SetupIntentStore(factory).latest_for(OTHER_GUILD) is None
