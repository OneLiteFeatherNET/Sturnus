"""Admin-only access control for Discord slash commands (Spec 11).

Follows the pattern already used by the organisation's RAG bot: guild
administrators always pass; everyone else needs to hold the configured
admin role.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import discord
from discord import app_commands

T = TypeVar("T")

#: Name of the role that grants config access to non-administrators.
ADMIN_ROLE_NAME = "Sturnus Admin"


def require_admin() -> Callable[[T], T]:
    """App-command check: guild administrators, or holders of the admin role.

    Raises `NoPrivateMessage` outside a guild and `MissingRole` for a guild
    member who is neither an administrator nor in the admin role.
    """

    def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.User):
            raise app_commands.NoPrivateMessage()
        if member.guild_permissions.administrator:
            return True
        if discord.utils.get(member.roles, name=ADMIN_ROLE_NAME) is None:
            raise app_commands.MissingRole(ADMIN_ROLE_NAME)
        return True

    return app_commands.check(predicate)
