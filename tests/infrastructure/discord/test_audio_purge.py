"""`/audio-purge`: the administrator's erasure, now a top-level command of its own."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sturnus.infrastructure.discord import audio_cog
from sturnus.infrastructure.discord.audio_cog import AudioPurgeCog
from tests.infrastructure.discord.test_client import T0, FakeClock


async def test_purge_erases_the_named_users_audio_and_replies_ephemerally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    erase = AsyncMock(return_value=3)
    monkeypatch.setattr(audio_cog, "_erase_audio", erase)
    cog = AudioPurgeCog(MagicMock(), MagicMock(), FakeClock(T0))
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    target = MagicMock(spec=discord.User)
    target.id = 42
    target.mention = "<@42>"

    callback: Any = cog.purge.callback  # unbound: the command takes `self` explicitly
    await callback(cog, interaction, target)

    assert erase.await_args is not None
    assert erase.await_args.args[2] == 42, "the named user's audio is erased, not the caller's"
    (message,), kwargs = interaction.response.send_message.await_args
    assert "3 recording(s)" in message and "<@42>" in message
    assert kwargs == {"ephemeral": True}


def test_purge_applies_no_in_process_check() -> None:
    """Discord withholds the command; a second check could disagree with Integrations."""
    assert AudioPurgeCog.purge.checks == []
