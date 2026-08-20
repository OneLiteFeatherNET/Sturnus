"""The Discord client: wires cogs, voice capture, the tick loop and shutdown.

Every decision already lives elsewhere -- the session state machine and the
encrypt-upload-enqueue sequence in `RecordingService`, consent policy in
`consent_flow`, per-guild configuration in `ConfigStore`. This class only
turns Discord events into calls against those, and turns their results back
into Discord actions: joining or leaving a voice channel, syncing the
command tree, logging a guild that hasn't been configured yet.

The bot does **not** run Alembic migrations on start -- the worker owns the
schema (Spec 13.1) -- so `main()` (`sturnus.entrypoints.bot`) waits for the
expected tables to exist and fails loudly if they don't, before this class
is ever constructed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import discord
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.ports import (
    AudioStore,
    AudioWriterFactory,
    Clock,
    Encryptor,
    VoiceReceiver,
)
from sturnus.application.recording import RecordingService
from sturnus.domain import settings
from sturnus.domain.session import EndReason
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.repositories import (
    AccountLinkRepository,
    ConsentRepository,
    JobRepository,
    SessionRepository,
)
from sturnus.infrastructure.discord.about_cog import AboutCog
from sturnus.infrastructure.discord.audio_cog import AudioCog
from sturnus.infrastructure.discord.config_cog import ConfigCog
from sturnus.infrastructure.discord.consent_cog import ConsentCog
from sturnus.infrastructure.discord.link_cog import LinkCog
from sturnus.infrastructure.discord.setup_cog import SetupCog
from sturnus.infrastructure.discord.voice import VoiceReceiveAdapter
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.health import ReadinessState

log = logging.getLogger(__name__)

#: How often the background loop checks every guild's session for a timeout.
TICK_INTERVAL_SECONDS = 10.0

# A SIGTERM is an external event the state machine itself never observes,
# so this records the honest, dedicated reason for an externally triggered
# close rather than one of the machine's own timeout reasons, none of
# which actually fired here.
SHUTDOWN_END_REASON = EndReason.SHUTDOWN


@dataclass
class _GuildRecording:
    """Everything the client needs to act on one guild's configured channel."""

    channel_id: int
    role_id: int
    service: RecordingService
    #: Typed against the narrow `VoiceReceiver` port rather than the
    #: concrete `VoiceReceiveAdapter` -- the only thing this class ever
    #: does with it is `join`/`leave`, and a test's fake stands in for it
    #: without dragging in `discord-ext-voice-recv` or a real gateway
    #: connection.
    voice: VoiceReceiver


class SturnusClient(commands.Bot):
    """The bot process's single Discord connection."""

    def __init__(
        self,
        *,
        clock: Clock,
        config_store: ConfigStore,
        consent_repo: ConsentRepository,
        session_repo: SessionRepository,
        job_repo: JobRepository,
        audio_store: AudioStore,
        writer_factory: AudioWriterFactory,
        encryptor: Encryptor,
        readiness: ReadinessState,
        database_ping: Callable[[], Awaitable[bool]],
        session_factory: async_sessionmaker[AsyncSession],
        outline_oauth: OutlineOAuth,
        link_states: LinkStateStore,
        account_links: AccountLinkRepository,
        tick_interval_seconds: float = TICK_INTERVAL_SECONDS,
    ) -> None:
        intents = discord.Intents.default()
        # `members` is a privileged intent and must also be turned on for
        # this application in the Discord developer portal ("Server Members
        # Intent"), or the gateway rejects the connection outright. It is
        # the only one: discord.py's `Intents.default()` is everything
        # except `presences`, `members` and `message_content`, which are
        # exactly Discord's three privileged intents.
        intents.members = True
        # Already on via `default()`. Set explicitly because the whole bot
        # depends on it -- a future switch to a narrower intent set should
        # have to delete this line deliberately rather than lose voice
        # events by omission. There is no portal switch for this one.
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self._clock = clock
        self._config_store = config_store
        self._consent_repo = consent_repo
        self._session_repo = session_repo
        self._job_repo = job_repo
        self._audio_store = audio_store
        self._writer_factory = writer_factory
        self._encryptor = encryptor
        self._readiness = readiness
        self._database_ping = database_ping
        self._session_factory = session_factory
        self._outline_oauth = outline_oauth
        self._link_states = link_states
        self._account_links = account_links
        self._tick_interval_seconds = tick_interval_seconds

        self._guilds: dict[int, _GuildRecording] = {}
        self._tick_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        """Loads the cogs and syncs the command tree; runs once before login completes.

        Every cog Sturnus ships is registered here -- a cog that exists but
        is missing from this list is unreachable at runtime even though it
        compiles and its own tests pass (see
        `tests/infrastructure/discord/test_client_cogs.py`, added
        specifically to catch that failure mode).
        """
        await self.add_cog(ConsentCog(self._consent_repo, self._config_store, self._clock))
        await self.add_cog(ConfigCog(self._config_store))
        await self.add_cog(SetupCog(self._config_store, self._clock))
        await self.add_cog(AudioCog(self._session_factory, self._audio_store, self._clock))
        await self.add_cog(
            LinkCog(self._outline_oauth, self._link_states, self._account_links, self._clock)
        )
        # The AGPL's section 13 obliges us to offer the source to people who
        # interact with this over a network. They never receive a binary, so a
        # LICENSE file in the repository does not reach them — /about does.
        await self.add_cog(AboutCog())
        await self.tree.sync()
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self._configure_guild(guild)
        self._readiness.discord_connected = True
        log.info("Connected to Discord; configured for %d guild(s)", len(self._guilds))

    async def _configure_guild(self, guild: discord.Guild) -> None:
        """Builds this guild's recording pipeline, or skips it if unconfigured.

        A guild missing either key cannot record at all -- there is no
        channel to join or no way to tell who has consented -- so it is
        skipped with a log line naming the command an administrator needs
        to run, rather than the bot guessing at a default.
        """
        voice_channel_id = await self._config_store.get(guild.id, settings.VOICE_CHANNEL_ID)
        consent_role_id = await self._config_store.get(guild.id, settings.CONSENT_ROLE_ID)
        if voice_channel_id is None or consent_role_id is None:
            log.warning(
                "Guild %d is missing voice_channel_id and/or consent_role_id; "
                "an administrator must run /config show to see what's missing.",
                guild.id,
            )
            return

        timeouts = await self._config_store.timeouts(guild.id)
        retention = await self._config_store.get(guild.id, settings.AUDIO_RETENTION_DAYS)
        assert retention is not None, "audio_retention_days has a default and is never unset"

        service = RecordingService(
            guild_id=guild.id,
            channel_id=int(voice_channel_id),
            timeouts=timeouts,
            sessions=self._session_repo,
            jobs=self._job_repo,
            store=self._audio_store,
            writers=self._writer_factory,
            encryptor=self._encryptor,
            retention_days=int(retention),
        )
        voice = VoiceReceiveAdapter(
            self, service, self._config_store, self._clock, self._consent_repo
        )
        self._guilds[guild.id] = _GuildRecording(
            channel_id=int(voice_channel_id),
            role_id=int(consent_role_id),
            service=service,
            voice=voice,
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Recomputes the consenting headcount and drives the session machine.

        Counts members carrying the consent role, never everyone present:
        an administrator can be in the channel without the role and must
        not, by their presence alone, start a recording nobody consented
        to (Spec 3.1).
        """
        recording = self._guilds.get(member.guild.id)
        if recording is None:
            return

        touched_channel_ids = {
            channel.id for channel in (before.channel, after.channel) if channel is not None
        }
        if recording.channel_id not in touched_channel_ids:
            return

        channel = member.guild.get_channel(recording.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return

        consented_count = sum(
            1
            for participant in channel.members
            if any(role.id == recording.role_id for role in participant.roles)
        )

        was_recording = recording.service.is_recording
        await recording.service.participants_changed(consented_count, self._clock.now())
        if recording.service.is_recording and not was_recording:
            await self._start_capture(recording)

    async def _start_capture(self, recording: _GuildRecording) -> None:
        """Joins the voice channel, or ends the session it could not capture.

        A `join` that raises used to leave the session row open with no
        capture behind it at all: nothing would ever arrive, so it closed
        at the idle timeout looking exactly like a meeting where nobody
        spoke. Arming `CAPTURE_FAILURE` instead means the next tick closes
        it, leaves the channel and resets, and the row says we could not
        hear rather than that there was nothing to hear.
        """
        try:
            await recording.voice.join(recording.channel_id)
        except Exception:
            log.exception(
                "Could not start voice capture in channel %d; ending the session as %s "
                "instead of leaving it open with nothing arriving.",
                recording.channel_id,
                EndReason.CAPTURE_FAILURE.value,
            )
            recording.service.request_close(EndReason.CAPTURE_FAILURE)

    async def _tick_loop(self) -> None:
        """Checks every guild's session for a timeout roughly every 10 seconds.

        Also the readiness heartbeat: the database is polled here, once per
        tick, rather than on every request to `/readyz`.
        """
        try:
            while True:
                await asyncio.sleep(self._tick_interval_seconds)
                self._readiness.database_reachable = await self._database_ping()
                await self._tick_all(self._clock.now())
        except asyncio.CancelledError:
            pass

    async def _tick_all(self, now: datetime) -> None:
        """Checks every guild's session for a timeout and re-arms any that closed.

        Split out of `_tick_loop` so this -- the actual per-guild decision,
        not the sleep around it -- can be driven directly by a test without
        sleeping through real time.

        `RecordingService.reset()` is the fix for a bot that used to go
        deaf after its first session: `tick()` on its own only closes a
        session, and closing left `is_recording` false forever, since
        nothing ever put the machine's `SessionMachine` back in `IDLE`.
        Calling `reset()` here, right after `close()` has finished
        encrypting, uploading and enqueuing, is what lets the very next
        consenting participant open a fresh session -- its own row, its
        own data key, its own writers -- on the same `RecordingService`
        instance, so the voice adapter's reference to it never goes stale.
        """
        for recording in list(self._guilds.values()):
            reason = await recording.service.tick(now)
            if reason is not None:
                await recording.voice.leave()
                recording.service.reset()

    async def graceful_shutdown(self) -> None:
        """Closes every active session before the connection is torn down.

        Stops receiving, closes the writers, encrypts, uploads, enqueues,
        then disconnects -- in that order, because a routine deploy sends
        SIGTERM and this is the only thing standing between it and losing
        every session still in progress (Spec 6.4).
        """
        if self._tick_task is not None:
            self._tick_task.cancel()
        for recording in list(self._guilds.values()):
            if recording.service.is_recording:
                await recording.service.close(SHUTDOWN_END_REASON, self._clock.now())
            await recording.voice.leave()
