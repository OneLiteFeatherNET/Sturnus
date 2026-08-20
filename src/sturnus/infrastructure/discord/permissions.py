"""Admin-only access control for Discord slash commands (Spec 11).

Follows the pattern already used by the organisation's RAG bot: guild
administrators always pass; everyone else needs to hold the role configured
under `admin_role_id` (Spec 11) for that guild -- a per-guild id rather than
a hardcoded role name, since names change and are never guaranteed unique
across guilds.

Discord's own administrator permission still passes unconditionally,
independent of anything in configuration: otherwise a guild that has not
set `admin_role_id` yet would be locked out of the very commands
(`/config`, `/setup`) needed to set it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import discord
from discord import app_commands

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore

T = TypeVar("T")


def _config_store(interaction: discord.Interaction) -> ConfigStore | None:
    """Reads the bot's `ConfigStore` off the client that received `interaction`.

    Every cog that uses `require_admin()` is registered on the same
    `SturnusClient`, which holds exactly one `ConfigStore` for the whole
    process; reaching for it here, once, means no individual cog needs to
    thread it through its own check wiring.
    """
    store = getattr(interaction.client, "_config_store", None)
    return store if isinstance(store, ConfigStore) else None


def require_admin() -> Callable[[T], T]:
    """App-command check: guild administrators, or holders of the configured admin role.

    Raises `NoPrivateMessage` outside a guild and `MissingRole` for a guild
    member who is neither an administrator nor a holder of the role stored
    under `admin_role_id`. A guild that has not configured `admin_role_id`
    yet rejects every non-administrator, rather than granting access to
    nobody-in-particular.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.User):
            raise app_commands.NoPrivateMessage()
        if member.guild_permissions.administrator:
            return True

        role_id: int | None = None
        store = _config_store(interaction)
        if store is not None:
            stored = await store.get(member.guild.id, settings.ADMIN_ROLE_ID)
            if stored is not None:
                role_id = int(stored)

        if role_id is None or discord.utils.get(member.roles, id=role_id) is None:
            raise app_commands.MissingRole(
                role_id if role_id is not None else settings.ADMIN_ROLE_ID
            )
        return True

    return app_commands.check(predicate)
