"""Admin slash commands for per-guild runtime configuration (Spec 11).

`REQUIRED_KEYS` has no defaults and must be set explicitly before a guild's
capture pipeline can go live; `missing_required` is what finally checks that.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.discord.permissions import require_admin

#: Every key `ConfigStore.set` accepts — the same union it validates against.
_KNOWN_KEYS: frozenset[str] = frozenset(settings.DEFAULTS) | settings.REQUIRED_KEYS


async def missing_required(store: ConfigStore, guild_id: int) -> list[str]:
    """Lists required keys that have neither a stored value nor a default.

    Sorted so repeated calls and command output stay stable — `REQUIRED_KEYS`
    is a frozenset and offers no ordering guarantee of its own.
    """
    missing = [key for key in settings.REQUIRED_KEYS if await store.get(guild_id, key) is None]
    return sorted(missing)


async def _effective(store: ConfigStore, guild_id: int, key: str) -> tuple[str | None, str]:
    """Returns the effective value of `key` and its source.

    Source is one of "stored", "default", or "unset".
    """
    stored = await store.get_stored(guild_id, key)
    if stored is not None:
        return stored, "stored"
    default = settings.DEFAULTS.get(key)
    if default is not None:
        return default, "default"
    return None, "unset"


@app_commands.guild_only()
class ConfigCog(
    commands.GroupCog, name="config", description="Manage Sturnus's runtime configuration."
):
    """Admin-only `/config` command group.

    None of today's keys hold a secret, so `/config show` is safe to print
    in full — that must be re-checked before any key that does gets added.
    """

    def __init__(self, store: ConfigStore) -> None:
        self._store = store
        super().__init__()

    @app_commands.command(
        name="get", description="Show the effective value of a configuration key."
    )
    @app_commands.describe(key="Configuration key, e.g. voice_channel_id")
    @require_admin()
    async def get(self, interaction: discord.Interaction, key: str) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if key not in _KNOWN_KEYS:
            await interaction.response.send_message(
                f"Unknown configuration key: `{key}`.", ephemeral=True
            )
            return
        value, source = await _effective(self._store, guild_id, key)
        rendered = value if value is not None else "*(unset)*"
        await interaction.response.send_message(f"`{key}` = {rendered} ({source})", ephemeral=True)

    @app_commands.command(name="set", description="Set a configuration key for this server.")
    @app_commands.describe(key="Configuration key", value="New value")
    @require_admin()
    async def set(self, interaction: discord.Interaction, key: str, value: str) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        try:
            await self._store.set(guild_id, key, value, discord.utils.utcnow())
        except ValueError as exc:
            await interaction.response.send_message(f"Rejected: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"`{key}` set to `{value}`.", ephemeral=True)

    @app_commands.command(
        name="clear", description="Clear a configuration key, restoring its default."
    )
    @app_commands.describe(key="Configuration key")
    @require_admin()
    async def clear(self, interaction: discord.Interaction, key: str) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if key not in _KNOWN_KEYS:
            await interaction.response.send_message(
                f"Unknown configuration key: `{key}`.", ephemeral=True
            )
            return
        await self._store.set(guild_id, key, None, discord.utils.utcnow())
        await interaction.response.send_message(f"`{key}` cleared.", ephemeral=True)

    @app_commands.command(
        name="show", description="List every configuration key and what is still missing."
    )
    @require_admin()
    async def show(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        lines: list[str] = []
        for key in sorted(_KNOWN_KEYS):
            value, source = await _effective(self._store, guild_id, key)
            rendered = value if value is not None else "*(unset)*"
            lines.append(f"`{key}` = {rendered} ({source})")
        missing = await missing_required(self._store, guild_id)
        lines.append("")
        if missing:
            lines.append("**Missing required keys:** " + ", ".join(f"`{k}`" for k in missing))
        else:
            lines.append("All required keys are set.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
