"""What the console asked the bot to do to a guild, and what came of it.

`api` must never hold a Discord token, so it cannot create the consent
role, set the Speak overwrites or sync the commands. It writes an intent
instead, and the bot's existing ten-second reconcile tick applies it
through the same `plan_setup` the slash command uses -- the mirror
arrangement run backwards.

**The two properties this store exists to hold.** An intent is settled
exactly once, because the tick runs six times a minute forever and an
intent that stayed pending after being applied would re-create the role
for the life of the guild. And a failure settles it, because one that
stayed pending after failing would retry a permission error against
Discord's rate limiter just as often. `record_outcome` therefore writes
conditionally on the intent still being unapplied and answers whether it
was the caller that settled it, so two ticks racing on one intent produce
one application and one honest `False`.

No business logic: what a setup consists of, whether the bot may perform
it, and what to tell an administrator about the result all belong above
this. `sturnus.domain.onboarding` holds the outcome vocabulary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.domain.onboarding import SetupIntent
from sturnus.infrastructure.db.models import GuildSetupIntent


class SetupIntentStore:
    """Records what the console asked for and what the bot did about it."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def request(
        self,
        guild_id: int,
        *,
        requested_by: int,
        channel_ids: str | None,
        consent_role_name: str | None,
        now: datetime,
    ) -> int:
        """Writes down what should be true, and returns the intent's id.

        Always an insert and never an upsert. An administrator asking
        twice asked twice: the second request is a correction, the first
        is what they asked for before they corrected it, and collapsing
        the two would lose who asked for which and when.
        """
        async with self._session_factory() as session:
            intent_id = await session.scalar(
                insert(GuildSetupIntent)
                .values(
                    guild_id=guild_id,
                    requested_by=requested_by,
                    requested_at=now,
                    channel_ids=channel_ids,
                    consent_role_name=consent_role_name,
                )
                .returning(GuildSetupIntent.id)
            )
            await session.commit()
        # `INSERT ... RETURNING` yields the row it just wrote or raises.
        assert intent_id is not None
        return intent_id

    async def pending_for(self, guild_id: int) -> Sequence[SetupIntent]:
        """This guild's unapplied intents, oldest first.

        Request order, because applying them in any other is what lets a
        mistake overwrite the correction that followed it.
        """
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(GuildSetupIntent)
                .where(
                    GuildSetupIntent.guild_id == guild_id,
                    GuildSetupIntent.applied_at.is_(None),
                )
                .order_by(GuildSetupIntent.requested_at, GuildSetupIntent.id)
            )
            return tuple(_view(row) for row in rows)

    async def latest_for(self, guild_id: int) -> SetupIntent | None:
        """The most recently requested intent, settled or not.

        What the console shows: the state of the last thing that was
        asked, which is the only one an administrator is still waiting on
        an answer about.
        """
        async with self._session_factory() as session:
            row = await session.scalar(
                select(GuildSetupIntent)
                .where(GuildSetupIntent.guild_id == guild_id)
                .order_by(
                    GuildSetupIntent.requested_at.desc(),
                    GuildSetupIntent.id.desc(),
                )
                .limit(1)
            )
        return None if row is None else _view(row)

    async def record_outcome(
        self, intent_id: int, *, outcome: str, error: str | None, now: datetime
    ) -> bool:
        """Settles an intent, and answers whether this caller settled it.

        Conditional on `applied_at` still being null, so the second of
        two ticks racing on one intent is told it had nothing to settle
        rather than silently overwriting the first outcome. `False` is
        also the answer for an intent that does not exist -- from
        outside, an intent somebody else already applied and one that was
        never written look the same, and neither is this caller's to
        report on.
        """
        async with self._session_factory() as session:
            settled = await session.scalar(
                update(GuildSetupIntent)
                .where(
                    GuildSetupIntent.id == intent_id,
                    GuildSetupIntent.applied_at.is_(None),
                )
                .values(applied_at=now, outcome=outcome, error=error)
                .returning(GuildSetupIntent.id)
            )
            await session.commit()
        return settled is not None


def _view(row: GuildSetupIntent) -> SetupIntent:
    return SetupIntent(
        id=row.id,
        guild_id=row.guild_id,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        channel_ids=row.channel_ids,
        consent_role_name=row.consent_role_name,
        applied_at=row.applied_at,
        outcome=row.outcome,
        error=row.error,
    )
