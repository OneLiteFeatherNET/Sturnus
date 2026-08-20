"""Thin adapter over `discord-ext-voice-recv` (Spec 6, Spec 3.1).

Everything decidable already lives in `RecordingService`
(`sturnus.application.recording`): the session state machine, the speaker
clock, and consent policy for granting or revoking. This module's only job
is to move packets from the extension into that service, and to drop
packets from users who may not be recorded before they ever reach the
service -- Spec 3.1's two layers. Neither check is redundant on its own:
guild administrators bypass Discord channel permissions and can speak in
the channel without holding the role (caught by the role check below),
and nothing about holding the role guarantees a stored consent record
that is actually still active -- a hand-granted role, or a role granted
under a privacy policy that has since changed (Spec 3.2), both leave the
role in place with no active consent behind it (caught by `ConsentCache`,
consulted after the role check).

The role check runs synchronously, on the extension's own thread, before
a packet is even handed to the event loop -- it reads `discord.Member.
roles`, which is already in memory, so there is no I/O to avoid. The
consent-record check cannot run there: it may need a database read, so it
runs inside the coroutine already being scheduled onto the event loop,
before that coroutine calls into `RecordingService`.

No unit tests for the sink callback itself: it is invoked by the
extension's packet-router thread, outside discord.py's event loop and
outside anything a fake could usefully stand in for. See
`docs/verification/voice-receive-spike.md` for what the installed version
of the library actually hands back on each packet -- this adapter is
written against those findings, not against the extension's
documentation. `ConsentCache`, which the callback delegates the record
check to, is a plain async class and is unit tested directly (see
`tests/infrastructure/discord/test_consent_cache.py`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future

import discord
from discord.ext import voice_recv

from sturnus.application.ports import Clock
from sturnus.application.recording import RecordingService
from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.consent_cache import ConsentCache

log = logging.getLogger(__name__)


class VoiceReceiveAdapter:
    """Satisfies the `VoiceReceiver` port over `discord-ext-voice-recv`."""

    def __init__(
        self,
        client: discord.Client,
        service: RecordingService,
        config_store: ConfigStore,
        clock: Clock,
        consent_repo: ConsentRepository,
    ) -> None:
        self._client = client
        self._service = service
        self._config_store = config_store
        self._clock = clock
        self._consent_cache = ConsentCache(consent_repo, config_store, clock)
        self._voice_client: voice_recv.VoiceRecvClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def join(self, channel_id: int) -> None:
        """Connects to the voice channel and starts listening on a sink."""
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError(f"channel {channel_id} is not a voice channel")

        stored_role_id = await self._config_store.get(channel.guild.id, settings.CONSENT_ROLE_ID)
        if stored_role_id is None:
            raise ValueError(f"guild {channel.guild.id} has no consent role configured")
        consent_role_id = int(stored_role_id)

        self._loop = asyncio.get_running_loop()
        self._voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        self._voice_client.listen(
            voice_recv.BasicSink(self._on_packet(channel.guild.id, consent_role_id))
        )

    async def leave(self) -> None:
        """Stops listening and disconnects."""
        if self._voice_client is None:
            return
        self._voice_client.stop_listening()
        await self._voice_client.disconnect()
        self._voice_client = None
        self._loop = None

    def _on_packet(
        self, guild_id: int, consent_role_id: int
    ) -> Callable[[discord.Member | discord.User | None, voice_recv.VoiceData], None]:
        """Builds the sink callback for one join, closing over its guild and consent role.

        Called by the extension's packet-router thread, never by the
        asyncio event loop -- everything past the (in-memory, synchronous)
        role check is therefore scheduled onto the loop as a coroutine
        rather than awaited directly.
        """

        def callback(
            user: discord.Member | discord.User | None, data: voice_recv.VoiceData
        ) -> None:
            if not isinstance(user, discord.Member):
                return
            has_role = any(role.id == consent_role_id for role in user.roles)
            if not has_role:
                # Fast, synchronous reject: revocation-through-role-removal
                # is caught right here, every packet, with no cache
                # involved and no staleness window.
                return
            assert self._loop is not None

            future = asyncio.run_coroutine_threadsafe(
                self._maybe_record(guild_id, user, data, has_role),
                self._loop,
            )
            future.add_done_callback(_log_packet_error)

        return callback

    async def _maybe_record(
        self,
        guild_id: int,
        user: discord.Member,
        data: voice_recv.VoiceData,
        has_role: bool,
    ) -> None:
        """Consults the cached consent record, then forwards the packet if allowed.

        Runs on the event loop (unlike the role check in `_on_packet`),
        since the cache may need a database read.
        """
        allowed = await self._consent_cache.may_record(guild_id, user.id, has_role)
        if not allowed:
            return
        await self._service.voice_packet(
            user.id,
            user.display_name,
            data.packet.ssrc,
            data.packet.timestamp,
            data.pcm,
            self._clock.now(),
        )


def _log_packet_error(future: Future[None]) -> None:
    """Surfaces a failed `voice_packet` call instead of letting it vanish silently."""
    exc = future.exception()
    if exc is not None:
        log.error("Error handling a voice packet: %s", type(exc).__name__)
