"""The one adapter that turns `Announcer` into an actual Discord message.

Two callers post through it, and both post into the session's own
`channel_id` -- the recording voice channel, which discord.py's
`VoiceChannel` supports directly via `.send()`, the same way
`sturnus.infrastructure.discord.voice.VoiceReceiveAdapter` already resolves
that id with `get_channel`. `sturnus.entrypoints.bot._publish_loop` posts a
finished session's document link (Spec 8.5); `sturnus.application.recording.
RecordingService` posts the one in-meeting warning it has to give, that a
speaker's audio is arriving with no audible level.

It lives here rather than in `sturnus.entrypoints.bot`, where it began, for
exactly that second caller: `SturnusClient` builds the recording pipeline
and would otherwise have to import from an entrypoint, which is the
dependency direction backwards. One adapter also means one place where a
message can go out, so a change to how this system addresses a channel --
permissions, threading, failure handling -- cannot land in one path and
miss the other.
"""

from __future__ import annotations

import discord


class DiscordAnnouncer:
    """Satisfies `sturnus.application.publishing.Announcer` over the gateway."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def post(self, channel_id: int, text: str) -> None:
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(
            channel_id
        )
        if not isinstance(channel, discord.abc.Messageable):
            raise ValueError(f"channel {channel_id} cannot receive messages")
        await channel.send(text)
