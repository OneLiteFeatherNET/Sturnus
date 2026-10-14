"""Who Discord offers each command to (openspec change `single-guild-instance`).

The administrative commands declare Manage Server as their default member
permission and Discord enforces it; the bot applies no second check. These
tests read the declaration off the command tree the client actually builds,
because a declaration that never reaches the tree protects nobody.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from sturnus.infrastructure.discord.about_cog import AboutCog
from sturnus.infrastructure.discord.audio_cog import AudioCog, AudioPurgeCog
from sturnus.infrastructure.discord.config_cog import ConfigCog
from sturnus.infrastructure.discord.consent_cog import ConsentCog
from sturnus.infrastructure.discord.link_cog import LinkCog
from sturnus.infrastructure.discord.queue_cog import QueueCog
from sturnus.infrastructure.discord.setup_cog import SetupCog
from tests.infrastructure.discord.test_client import T0, FakeClock, _client

MANAGE_SERVER = discord.Permissions(manage_guild=True)


async def _tree() -> app_commands.CommandTree:
    clock = FakeClock(T0)
    client = _client(clock)
    await client.add_cog(ConfigCog(MagicMock(), MagicMock(), MagicMock()))
    await client.add_cog(QueueCog(MagicMock(), clock))
    await client.add_cog(SetupCog(MagicMock(), clock, MagicMock()))
    await client.add_cog(AudioCog(MagicMock(), MagicMock(), clock))
    await client.add_cog(AudioPurgeCog(MagicMock(), MagicMock(), clock))
    await client.add_cog(ConsentCog(MagicMock(), MagicMock(), clock, "transcribe.example"))
    await client.add_cog(LinkCog(MagicMock(), MagicMock(), MagicMock(), clock))
    await client.add_cog(AboutCog("transcribe.example"))
    return client.tree


@pytest.mark.parametrize("name", ["config", "queue", "setup", "audio-purge"])
async def test_an_administrative_command_requires_manage_server(name: str) -> None:
    command = (await _tree()).get_command(name)
    assert command is not None, f"/{name} is not on the tree"
    assert command.default_permissions == MANAGE_SERVER


@pytest.mark.parametrize("name", ["consent", "link", "about", "audio"])
async def test_a_member_command_stays_open_to_everyone(name: str) -> None:
    command = (await _tree()).get_command(name)
    assert command is not None, f"/{name} is not on the tree"
    assert command.default_permissions is None


async def test_audio_delete_stays_in_the_open_group_and_purge_is_not_in_it() -> None:
    """Discord applies a default permission per top-level command, so `purge` could not
    stay a subcommand of `/audio` without closing `/audio delete` to members too."""
    group = (await _tree()).get_command("audio")
    assert isinstance(group, app_commands.Group)
    assert [command.name for command in group.commands] == ["delete"]
