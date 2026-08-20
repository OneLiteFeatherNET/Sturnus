"""Tests for the one adapter that turns `Announcer` into a Discord message.

Both things this system says out loud go through it -- a finished session's
document link and the in-meeting warning that a speaker's audio carries no
level -- so the two branches it owns are worth pinning here rather than
only where they happen to be exercised: the cache miss, and the channel
that cannot be sent to at all.
"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sturnus.infrastructure.discord.announcer import DiscordAnnouncer

CHANNEL_ID = 4242


def _messageable() -> MagicMock:
    """A `VoiceChannel` stand-in that really does satisfy `isinstance`.

    `MagicMock(spec=...)` reports the spec class as its own, so the
    adapter's `isinstance(channel, discord.abc.Messageable)` check runs for
    real against it -- and `send`, being a coroutine function on the spec,
    comes back as an `AsyncMock`.
    """
    return MagicMock(spec=discord.VoiceChannel)


async def test_it_sends_into_the_channel_it_was_given() -> None:
    client = MagicMock(spec=discord.Client)
    channel = _messageable()
    client.get_channel = MagicMock(return_value=channel)

    await DiscordAnnouncer(client).post(CHANNEL_ID, "hello")

    client.get_channel.assert_called_once_with(CHANNEL_ID)
    channel.send.assert_awaited_once_with("hello")


async def test_a_channel_missing_from_the_cache_is_fetched() -> None:
    """`get_channel` reads a cache the gateway fills, and it can be cold.

    A bot restarted mid-meeting, or one whose cache never covered this
    guild, would otherwise silently drop the message into a `None` -- the
    two things posted through here are a finished session's only link and
    the one warning that can still save a recording, so neither may depend
    on a cache being warm.
    """
    channel = _messageable()
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(return_value=channel)

    await DiscordAnnouncer(client).post(CHANNEL_ID, "hello")

    client.fetch_channel.assert_awaited_once_with(CHANNEL_ID)
    channel.send.assert_awaited_once_with("hello")


async def test_a_channel_that_cannot_receive_messages_is_refused_loudly() -> None:
    """Raising beats calling `send` on something that does not have one.

    Both callers already survive a failed post -- `announce_ready_sessions`
    retries on its next sweep and `RecordingService` logs and carries on --
    so an error here costs one message and reaches somebody's log, whereas
    an `AttributeError` from inside a `send` that was never there would say
    nothing about which channel was misconfigured.
    """
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=MagicMock(spec=discord.CategoryChannel))

    with pytest.raises(ValueError, match=str(CHANNEL_ID)):
        await DiscordAnnouncer(client).post(CHANNEL_ID, "hello")
