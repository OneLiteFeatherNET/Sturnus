"""Mirroring one guild's administrators from the gateway into the database.

The console's API process cannot ask Discord who holds `admin_role_id`: it
has no gateway, and giving it one would mean a second process holding the
Discord token -- one that already holds S3 and the master key, so it can
decrypt every recording ever made (Spec 13.2). The bot, which is that
process legitimately, writes the membership down instead.

This module is the adapter alone. What to *do* -- sync, skip, or clear --
is decided by `sturnus.application.admin_mirror.decide_admin_sync`, which
needs no Discord connection and is tested without one.

The gateway read here is `Guild.get_role(...).members`, a cache lookup
rather than an API call, which is why this can run on the ordinary tick
without a rate-limit budget of its own. It needs the members intent, which
`SturnusClient` already declares for the consent gate.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

import discord

from sturnus.application.admin_mirror import AdminSyncDecision, decide_admin_sync
from sturnus.application.directory_mirror import parse_role_id
from sturnus.domain import settings

log = logging.getLogger(__name__)


class ConfigReader(Protocol):
    async def get(self, guild_id: int, key: str) -> str | None: ...


class AdminMirror(Protocol):
    async def replace(self, guild_id: int, discord_user_ids: list[int], now: datetime) -> None: ...


async def sync_administrators(
    guild: discord.Guild,
    config: ConfigReader,
    mirror: AdminMirror,
    now: datetime,
) -> None:
    """Brings one guild's mirrored administrators in line with its role.

    Three outcomes, and the difference between the last two is the point:

    - The role is configured and exists: its members are written.
    - Nothing is configured: **nothing is written at all.** A guild
      mid-`/setup` has no administrators yet, which is a different fact
      from having none, and an empty write would erase the difference.
    - The role is configured and gone: the mirror is **cleared**. Nobody
      holds a deleted role, so its former members must stop being
      administrators rather than remain ones indefinitely.
    """
    configured = await config.get(guild.id, settings.ADMIN_ROLE_ID)
    role = _resolve_role(guild, configured)
    decision = decide_admin_sync(configured_role_id=configured, role_exists=role is not None)

    if decision is AdminSyncDecision.SKIP:
        return
    if decision is AdminSyncDecision.CLEAR:
        log.info(
            "Guild %d has an admin role configured that no longer resolves; "
            "clearing its mirrored administrators",
            guild.id,
        )
        await mirror.replace(guild.id, [], now)
        return

    assert role is not None  # SYNC is only returned when the role resolved
    await mirror.replace(guild.id, [member.id for member in role.members], now)


def _resolve_role(guild: discord.Guild, configured: str | None) -> discord.Role | None:
    """The configured role, or `None` if it is absent, unparseable or gone.

    Unparseable folds into "gone" deliberately: `guild_config` stores text
    and `admin_role_id` is not among `INTEGER_KEYS`, so a hand-edited row
    can hold anything. A value nobody can interpret must not grant
    anything, and must not stop the sweep either.

    The lenient read itself is `parse_role_id`, shared with the directory
    sweep rather than spelled twice -- two copies of "what counts as a
    role id" is two things to keep in agreement, and the day they disagree
    is the day one sweep grants what the other revokes.
    """
    role_id = parse_role_id(configured)
    return guild.get_role(role_id) if role_id is not None else None
