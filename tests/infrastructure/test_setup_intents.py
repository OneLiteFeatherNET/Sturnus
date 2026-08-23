"""What the console asked the bot to do, and what happened when it did.

Every step of setting up a guild needs a Discord token and `api` must
never hold one, so the console cannot act. It writes down what should be
true instead, and the bot's ten-second reconcile tick makes it true and
writes back what happened -- the mirror arrangement run backwards.

The two things this table has to get right are that an intent is applied
at most once, and that a failed application says so rather than
disappearing.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.domain.onboarding import APPLIED, FAILED
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.setup_intents import SetupIntentStore

GUILD = 1
OTHER_GUILD = 2
ANNA = 100
T0 = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def minutes(count: int) -> timedelta:
    return timedelta(minutes=count)


@pytest.fixture
async def store(clean_database: str) -> AsyncIterator[SetupIntentStore]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield SetupIntentStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


async def test_a_guild_nobody_asked_about_has_nothing_pending(
    store: SetupIntentStore,
) -> None:
    assert await store.pending_for(GUILD) == ()
    assert await store.latest_for(GUILD) is None


async def test_a_requested_setup_is_pending_until_it_is_applied(
    store: SetupIntentStore,
) -> None:
    intent_id = await store.request(
        GUILD,
        requested_by=ANNA,
        channel_ids="10,11",
        consent_role_name="Recorded",
        now=T0,
    )
    (pending,) = await store.pending_for(GUILD)
    assert pending.id == intent_id
    assert pending.requested_by == ANNA
    assert pending.requested_at == T0
    assert pending.channel_ids == "10,11"
    assert pending.consent_role_name == "Recorded"
    assert pending.applied_at is None
    assert pending.outcome is None


async def test_an_applied_intent_stops_being_pending(store: SetupIntentStore) -> None:
    """Applied at most once. The tick runs every ten seconds forever.

    An intent that stayed pending after being applied would have the bot
    re-create the consent role and re-write the channel overwrites six
    times a minute for the life of the guild.
    """
    intent_id = await store.request(
        GUILD, requested_by=ANNA, channel_ids="10", consent_role_name=None, now=T0
    )
    assert (
        await store.record_outcome(intent_id, outcome=APPLIED, error=None, now=T0 + minutes(1))
        is True
    )
    assert await store.pending_for(GUILD) == ()

    latest = await store.latest_for(GUILD)
    assert latest is not None
    assert latest.applied_at == T0 + minutes(1)
    assert latest.outcome == APPLIED
    assert latest.error is None


async def test_an_intent_that_failed_is_settled_and_says_why(
    store: SetupIntentStore,
) -> None:
    """A failure is an outcome, not a retry.

    Leaving it pending would retry a permission error every ten seconds
    against Discord's rate limiter. The console shows the reason and an
    administrator asks again once they have fixed it.
    """
    intent_id = await store.request(
        GUILD, requested_by=ANNA, channel_ids="10", consent_role_name=None, now=T0
    )
    await store.record_outcome(
        intent_id, outcome=FAILED, error="Missing Permissions", now=T0 + minutes(1)
    )

    assert await store.pending_for(GUILD) == ()
    latest = await store.latest_for(GUILD)
    assert latest is not None
    assert latest.outcome == FAILED
    assert latest.error == "Missing Permissions"


async def test_recording_an_outcome_twice_settles_nothing_further(
    store: SetupIntentStore,
) -> None:
    """Two ticks racing on one intent must not both apply it.

    The write is conditional on the intent still being unapplied, so the
    second caller is told it had nothing to settle rather than silently
    overwriting the first outcome.
    """
    intent_id = await store.request(
        GUILD, requested_by=ANNA, channel_ids="10", consent_role_name=None, now=T0
    )
    assert await store.record_outcome(intent_id, outcome=APPLIED, error=None, now=T0) is True
    assert (
        await store.record_outcome(intent_id, outcome=FAILED, error="late", now=T0 + minutes(1))
        is False
    )
    latest = await store.latest_for(GUILD)
    assert latest is not None
    assert latest.outcome == APPLIED


async def test_pending_intents_are_applied_oldest_first(store: SetupIntentStore) -> None:
    """An administrator who asked twice meant the second one, eventually.

    Applying them in request order is what makes the last word the last
    word; any other order lets a correction be overwritten by the mistake
    it corrected.
    """
    first = await store.request(
        GUILD, requested_by=ANNA, channel_ids="10", consent_role_name=None, now=T0
    )
    second = await store.request(
        GUILD, requested_by=ANNA, channel_ids="11", consent_role_name=None, now=T0 + minutes(1)
    )
    assert [intent.id for intent in await store.pending_for(GUILD)] == [first, second]


async def test_one_guilds_intents_are_not_anothers(store: SetupIntentStore) -> None:
    """The tick runs per guild and must not apply somebody else's request."""
    await store.request(GUILD, requested_by=ANNA, channel_ids="10", consent_role_name=None, now=T0)
    assert await store.pending_for(OTHER_GUILD) == ()


async def test_the_latest_intent_is_the_most_recently_requested(
    store: SetupIntentStore,
) -> None:
    """What the console shows: the state of the last thing that was asked."""
    await store.request(GUILD, requested_by=ANNA, channel_ids="10", consent_role_name=None, now=T0)
    second = await store.request(
        GUILD, requested_by=ANNA, channel_ids="11", consent_role_name=None, now=T0 + minutes(1)
    )
    latest = await store.latest_for(GUILD)
    assert latest is not None
    assert latest.id == second
