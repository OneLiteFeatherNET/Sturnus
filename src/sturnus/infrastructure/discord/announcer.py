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

#: Which mentions in a posted message Discord may turn into an actual
#: notification. Both messages that go out through this adapter name
#: people on purpose -- the document link mentions everyone the session
#: recorded, the silent-audio warning names the one speaker it is about --
#: and a mention nobody is notified by defeats the reason either message
#: names anyone at all.
#:
#: Everything except `users` is off. `sturnus.application.publishing`
#: renders no role or `@everyone` mention in either template, so allowing
#: them could only ever grant reach to something that reached the text by
#: accident; denying them here is the layer that makes that harmless
#: rather than merely improbable. `replied_user` is irrelevant -- nothing
#: posted here is a reply -- and off for the same reason.
#:
#: Set explicitly rather than left to the library default: discord.py
#: falls back to the client's own `allowed_mentions`, which is a
#: process-wide setting anything else could change without this file's
#: knowledge.
_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, users=True, roles=False, replied_user=False
)


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
        await channel.send(text, allowed_mentions=_ALLOWED_MENTIONS)
