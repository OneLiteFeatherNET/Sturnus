"""Mirroring a guild's own name, channels, roles and named people into the database.

The sibling of `sturnus.infrastructure.discord.admin_sync`, on the same
sweep and for the same reason. `api` has no gateway and must not be given
one (Spec 13.2, and the console design's Section 2.1), so it cannot turn
the snowflake in `voice_channel_id` back into "meeting", nor the one in
`consent_role_id` back into "recorded". The bot can, so the bot writes
them down.

This module is the adapter alone. What is *decided* -- which people may be
named at all, and when a guild's mirror is left alone rather than emptied
-- lives in `sturnus.application.directory_mirror`, which needs no Discord
connection and is tested without one.

Every gateway read here is a cache lookup rather than an API call:
`Guild.name`, `Guild.icon`, `Guild.voice_channels`, `Guild.text_channels`,
`Guild.roles` and `Role.members` all answer from what the gateway already
pushed. That is
why this can ride the ordinary ten-second tick without a rate-limit budget
of its own, exactly as `sync_administrators` does. `Role.members` needs the
members intent, which `SturnusClient` already declares for the consent
gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import discord

from sturnus.application.directory_mirror import (
    TEXT,
    VOICE,
    DirectorySyncDecision,
    MirroredChannel,
    MirroredGuild,
    MirroredMember,
    MirroredRole,
    decide_member_mirror,
    members_to_mirror,
    parse_role_id,
)
from sturnus.domain import settings


class ConfigReader(Protocol):
    async def get(self, guild_id: int, key: str) -> str | None: ...


class DirectoryMirror(Protocol):
    async def replace_guild(self, guild: MirroredGuild, now: datetime) -> None: ...

    async def replace_channels(
        self, guild_id: int, channels: list[MirroredChannel], now: datetime
    ) -> None: ...

    async def replace_roles(
        self, guild_id: int, roles: list[MirroredRole], now: datetime
    ) -> None: ...

    async def replace_members(
        self, guild_id: int, members: list[MirroredMember], now: datetime
    ) -> None: ...


async def sync_directory(
    guild: discord.Guild,
    config: ConfigReader,
    mirror: DirectoryMirror,
    now: datetime,
) -> None:
    """Brings one guild's mirrored names in line with the gateway.

    Channels and roles are written unconditionally, empty included: they
    are what `/setup` is about to ask an administrator to choose *from*,
    so a guild that has configured nothing is exactly the guild that most
    needs them. A channel or role deleted in Discord disappears here on
    the same sweep, which is what stops the console offering something
    nobody can join.

    The guild's own name is written on the same terms as the channels:
    ungated, because a guild that has configured nothing is exactly the
    guild an administrator is looking at while running `/setup`, and it
    is the name every admin page in the console puts at the top. Clearing
    it is not a case this reaches -- a guild the bot cannot see has no
    `discord.Guild` to be called with, so the caller skips it and the
    stored name stands.

    Member names are gated, and the gate is the skip-versus-clear
    distinction `admin_mirror` draws. A guild that has configured neither
    naming role is left alone: it has no roster *yet*, which is not the
    same fact as a roster nobody is on. Once either role is configured the
    union of their holders is written, empty included -- a role that was
    deleted or emptied must stop naming people rather than go on naming
    them out of a mirror nothing refreshes.
    """
    await mirror.replace_guild(_guild(guild), now)
    await mirror.replace_channels(guild.id, _channels(guild), now)
    await mirror.replace_roles(guild.id, _roles(guild), now)

    consent = await config.get(guild.id, settings.CONSENT_ROLE_ID)
    admin = await config.get(guild.id, settings.ADMIN_ROLE_ID)
    if decide_member_mirror([consent, admin]) is DirectorySyncDecision.SKIP:
        return

    # Exactly two role memberships, and nothing else. See
    # `members_to_mirror`: mirroring the guild's whole member list would
    # copy a Discord user directory into a database that exists to hold
    # recordings, covering people who never joined a recorded channel and
    # consented to nothing. These two are the bounded set every page that
    # names a person draws from -- a consent roster, the speakers in a
    # queue, an administrator list.
    named = members_to_mirror(_holders(guild, consent), _holders(guild, admin))
    await mirror.replace_members(guild.id, list(named), now)


def _guild(guild: discord.Guild) -> MirroredGuild:
    """What this server is called, and where its icon is.

    Both come off the same cached `discord.Guild` the channels and roles
    are read from, so this costs the sweep nothing beyond the row it
    writes -- and reading the icon here rather than later is why there is
    no second sweep for one string.

    `Guild.icon` is an asset or nothing; a guild without one is ordinary
    and mirrors a null. The name is never logged: it is an
    organisation's name, and nothing in a log line needs it.
    """
    icon = guild.icon
    return MirroredGuild(
        guild_id=guild.id,
        name=guild.name,
        icon_url=None if icon is None else icon.url,
    )


def _channels(guild: discord.Guild) -> list[MirroredChannel]:
    """Every voice and text channel, tagged with which kind it is.

    Only these two kinds are read. A category, a stage or a forum is not
    something any Sturnus setting can point at, so mirroring it would be
    filling a picker with entries that cannot be chosen. `kind` is still a
    free string in the database rather than an enum, so widening this
    later is a change here and nowhere else.
    """
    return [
        MirroredChannel(
            channel_id=channel.id, name=channel.name, kind=kind, position=channel.position
        )
        for kind, channels in ((VOICE, guild.voice_channels), (TEXT, guild.text_channels))
        for channel in channels
    ]


def _roles(guild: discord.Guild) -> list[MirroredRole]:
    """Every role, `@everyone` included.

    Not filtered: `@everyone` is a real role with a real id that a
    hand-edited `guild_config` can name, and a mirror that silently
    omitted it would make that configuration unexplainable rather than
    merely wrong.
    """
    return [
        MirroredRole(role_id=role.id, name=role.name, position=role.position)
        for role in guild.roles
    ]


def _holders(guild: discord.Guild, configured: str | None) -> list[MirroredMember]:
    """The members of a configured role, or nobody.

    "Nobody" covers unset, unparseable and deleted alike. All three mean
    the same thing for the purpose of naming people -- there is no role
    here whose members could be named -- and the difference between them
    was already settled by `decide_member_mirror` before this is reached.
    """
    role_id = parse_role_id(configured)
    role = guild.get_role(role_id) if role_id is not None else None
    if role is None:
        return []
    return [
        MirroredMember(discord_user_id=member.id, display_name=member.display_name)
        for member in role.members
    ]
