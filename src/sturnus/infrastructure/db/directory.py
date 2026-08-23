"""A guild's channels, roles and named people, as far as `api` may know.

`voice_channel_id`, `consent_role_id` and `admin_role_id` are Discord
snowflakes an administrator pasted into the console, and the console shows
them back as snowflakes because `api` has no gateway to ask what they are
called -- deliberately: it already holds S3 and the master key, so it can
decrypt every recording in the system, and that is not a process to also
hand the ability to act as the bot (Spec 13.2, and the console design's
Section 2.1).

So `bot`, which does hold the gateway, writes the names here on the sweep
it already runs for `admin_member`, and `api` only ever reads. The cost is
staleness bounded by that sweep; the alternative cost was a second process
holding the Discord token.

**Every write is a replacement scoped to one guild, and never a merge.**
The two halves matter separately. A merge would leave a channel deleted in
Discord in the mirror forever, and the console would go on offering an
administrator a channel nobody can join -- configuring it produces a
recording that never starts, with nothing anywhere saying why. Scoping to
one guild is what keeps that replacement from blanking every other guild's
names until its own sweep next came round; sweeps run per guild, one at a
time.

An empty list is a real instruction rather than a no-op, for the reason
`AdminMemberStore.replace` gives: treating empty as "nothing to do" is how
the last row to become wrong stays right forever.

`guild_member` is deliberately not the guild's membership -- see
`sturnus.application.directory_mirror.members_to_mirror` for what it is
and why it is bounded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.directory_mirror import (
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.infrastructure.db.models import GuildChannel, GuildMember, GuildRole


class DirectoryStore:
    """Reads and replaces one guild's mirrored channels, roles and names."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace_channels(
        self, guild_id: int, channels: Iterable[MirroredChannel], now: datetime
    ) -> None:
        """Makes this guild's mirrored channels exactly `channels`.

        One transaction, for the reason `AdminMemberStore.replace` states:
        a delete followed by a separate insert leaves a window in which
        the console can name nothing, and a sweep on a timer would recur
        into that window forever rather than hit it once.
        """
        # Deduplicated by id: Discord can report the same entity twice
        # across a paginated read, and a primary-key violation would abort
        # the sweep for this whole guild rather than for one row.
        wanted = {channel.channel_id: channel for channel in channels}
        removal = delete(GuildChannel).where(GuildChannel.guild_id == guild_id)
        if wanted:
            removal = removal.where(GuildChannel.channel_id.notin_(wanted))
        rows = [
            {
                "guild_id": guild_id,
                "channel_id": channel.channel_id,
                "name": channel.name,
                "kind": channel.kind,
                "position": channel.position,
                "synced_at": now,
            }
            for channel in wanted.values()
        ]
        async with self._session_factory() as session:
            await session.execute(removal)
            if rows:
                statement = insert(GuildChannel).values(rows)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["guild_id", "channel_id"],
                        set_={
                            "name": statement.excluded.name,
                            "kind": statement.excluded.kind,
                            "position": statement.excluded.position,
                            "synced_at": now,
                        },
                    )
                )
            await session.commit()

    async def replace_roles(
        self, guild_id: int, roles: Iterable[MirroredRole], now: datetime
    ) -> None:
        """Makes this guild's mirrored roles exactly `roles`."""
        wanted = {role.role_id: role for role in roles}
        removal = delete(GuildRole).where(GuildRole.guild_id == guild_id)
        if wanted:
            removal = removal.where(GuildRole.role_id.notin_(wanted))
        rows = [
            {
                "guild_id": guild_id,
                "role_id": role.role_id,
                "name": role.name,
                "position": role.position,
                "synced_at": now,
            }
            for role in wanted.values()
        ]
        async with self._session_factory() as session:
            await session.execute(removal)
            if rows:
                statement = insert(GuildRole).values(rows)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["guild_id", "role_id"],
                        set_={
                            "name": statement.excluded.name,
                            "position": statement.excluded.position,
                            "synced_at": now,
                        },
                    )
                )
            await session.commit()

    async def replace_members(
        self, guild_id: int, members: Iterable[MirroredMember], now: datetime
    ) -> None:
        """Makes this guild's mirrored names exactly `members`.

        Somebody who lost both naming roles falls out of the table rather
        than staying a name in a database about recordings. That is the
        same argument the bound in `members_to_mirror` makes, enforced on
        the way out as well as on the way in.
        """
        wanted = {member.discord_user_id: member for member in members}
        removal = delete(GuildMember).where(GuildMember.guild_id == guild_id)
        if wanted:
            removal = removal.where(GuildMember.discord_user_id.notin_(wanted))
        rows = [
            {
                "guild_id": guild_id,
                "discord_user_id": member.discord_user_id,
                "display_name": member.display_name,
                "synced_at": now,
            }
            for member in wanted.values()
        ]
        async with self._session_factory() as session:
            await session.execute(removal)
            if rows:
                statement = insert(GuildMember).values(rows)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["guild_id", "discord_user_id"],
                        set_={
                            "display_name": statement.excluded.display_name,
                            "synced_at": now,
                        },
                    )
                )
            await session.commit()

    async def channels_for(self, guild_id: int) -> Sequence[MirroredChannel]:
        """This guild's mirrored channels, in the order Discord shows them.

        Ordered by `position` because that is the order an administrator
        is looking at in another window; ties break on id so two reads of
        an unchanged guild render identically.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    GuildChannel.channel_id,
                    GuildChannel.name,
                    GuildChannel.kind,
                    GuildChannel.position,
                )
                .where(GuildChannel.guild_id == guild_id)
                .order_by(GuildChannel.position, GuildChannel.channel_id)
            )
        return [
            MirroredChannel(channel_id=channel_id, name=name, kind=kind, position=position)
            for channel_id, name, kind, position in rows.all()
        ]

    async def roles_for(self, guild_id: int) -> Sequence[MirroredRole]:
        """This guild's mirrored roles, in the order Discord shows them."""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(GuildRole.role_id, GuildRole.name, GuildRole.position)
                .where(GuildRole.guild_id == guild_id)
                .order_by(GuildRole.position, GuildRole.role_id)
            )
        return [
            MirroredRole(role_id=role_id, name=name, position=position)
            for role_id, name, position in rows.all()
        ]

    async def members_for(self, guild_id: int) -> Sequence[MirroredMember]:
        """The people this guild's console may name, ordered by id.

        Ordered by id rather than by name: arbitrary but stable, which is
        the property a list rendered on every page load needs. Sorting by
        display name would reshuffle the list every time somebody changed
        their nickname.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(GuildMember.discord_user_id, GuildMember.display_name)
                .where(GuildMember.guild_id == guild_id)
                .order_by(GuildMember.discord_user_id)
            )
        return [
            MirroredMember(discord_user_id=discord_user_id, display_name=display_name)
            for discord_user_id, display_name in rows.all()
        ]
