"""Who administers the bot, as far as the console's API is allowed to know.

`admin_role_id` is a Discord role. The API process has no gateway to ask
about role membership, and giving it one would undo the credential
separation the system rests on: it already holds S3 and the master key, so
it can decrypt every recording ever made -- that is not a process to also
hand the ability to act as the bot (Spec 13.2).

So the bot, which does hold the members intent, mirrors the membership
here on a timer, and the API only reads. The cost of the arrangement is
staleness bounded by the sweep interval; the alternative cost was a second
process holding the Discord token.

`replace` is a replace and never a merge. A revoked role that leaves a
stale row behind is a privilege that outlives its grant, and nothing
downstream would ever surface it -- every caller asks "is this person an
admin", none asks "why", and so nothing would ever notice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.infrastructure.db.models import AdminMember


class AdminMemberStore:
    """Reads and replaces the mirrored administrator membership of a guild."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace(self, guild_id: int, discord_user_ids: Iterable[int], now: datetime) -> None:
        """Makes this guild's mirrored membership exactly `discord_user_ids`.

        One transaction: a delete followed by an insert that is not atomic
        would leave a window in which a real administrator is refused --
        and the sweep runs on a timer, so that window would recur forever
        rather than once.

        An empty list is a real instruction, not a no-op. A guild whose
        admin role has no members has no administrators, and treating
        empty as "nothing to do" is exactly how the last administrator to
        be removed keeps their access indefinitely.
        """
        # Deduplicated because Discord can report the same member twice
        # across paginated role queries, and a primary-key violation here
        # would abort the sweep for the whole guild.
        wanted = sorted(set(discord_user_ids))
        # Scoped to this guild, never the whole table: a sweep runs per
        # guild, and a wholesale clear would de-admin every other guild
        # until its own sweep next ran.
        removal = delete(AdminMember).where(AdminMember.guild_id == guild_id)
        if wanted:
            removal = removal.where(AdminMember.discord_user_id.notin_(wanted))
        async with self._session_factory() as session:
            await session.execute(removal)
            if wanted:
                await session.execute(
                    insert(AdminMember)
                    .values(
                        [
                            {
                                "guild_id": guild_id,
                                "discord_user_id": discord_user_id,
                                "synced_at": now,
                            }
                            for discord_user_id in wanted
                        ]
                    )
                    .on_conflict_do_update(
                        index_elements=["guild_id", "discord_user_id"],
                        set_={"synced_at": now},
                    )
                )
            await session.commit()

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool:
        async with self._session_factory() as session:
            return bool(
                await session.scalar(
                    select(
                        exists().where(
                            AdminMember.guild_id == guild_id,
                            AdminMember.discord_user_id == discord_user_id,
                        )
                    )
                )
            )

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        """Whether this person administers any guild the bot serves.

        The console signs somebody in by Discord id alone -- an OAuth
        identity names no guild -- so this is what decides whether the
        settings section is offered at all. Which guild's settings they may
        then change is a second, narrower question answered by `is_admin`.
        """
        async with self._session_factory() as session:
            return bool(
                await session.scalar(
                    select(exists().where(AdminMember.discord_user_id == discord_user_id))
                )
            )

    async def administrators(self, guild_id: int) -> Sequence[int]:
        """This guild's administrators, ordered so two reads agree.

        Sorted by id is arbitrary but stable, which is the property that
        matters: unordered, the same membership renders differently on
        every page load.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(AdminMember.discord_user_id)
                .where(AdminMember.guild_id == guild_id)
                .order_by(AdminMember.discord_user_id)
            )
            return tuple(rows.scalars())
