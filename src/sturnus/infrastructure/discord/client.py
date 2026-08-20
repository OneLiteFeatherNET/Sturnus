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
from datetime import datetime, timedelta

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

#: The end reasons that mean "we could not hear", as opposed to "there was
#: nothing to hear". Both are properties of this process's connection to
#: this channel rather than of the meeting, and both survive a rejoin.
CAPTURE_FAILURE_REASONS = frozenset({EndReason.CAPTURE_FAILURE, EndReason.DECODE_FAILURE})

#: How long a guild waits before another session may start after capture
#: failed. Long enough that a persistent fault is visibly a fault rather
#: than a flutter, short enough that a genuinely transient one costs a
#: meeting's opening minutes rather than the rest of the day.
REJOIN_COOLDOWN = timedelta(minutes=15)


@dataclass
class _GuildRecording:
    """Everything the client needs to act on one guild's configured channel."""

    #: Kept so the tick loop can recount the channel on its own. Recovery
    #: from a capture failure must not wait for a voice-state update: the
    #: people who were there when capture died are still there, and none
    #: of them has any reason to leave and come back.
    guild: discord.Guild
    channel_id: int
    role_id: int
    service: RecordingService
    #: Typed against the narrow `VoiceReceiver` port rather than the
    #: concrete `VoiceReceiveAdapter` -- the only thing this class ever
    #: does with it is `join`/`leave`, and a test's fake stands in for it
    #: without dragging in `discord-ext-voice-recv` or a real gateway
    #: connection.
    voice: VoiceReceiver
    #: Set when a session ended because capture failed; while it is in the
    #: future, no new session opens for this guild. Cleared by the tick
    #: loop once it has passed -- a guard that only an operator can clear
    #: turns a transient fault into an outage.
    blocked_until: datetime | None = None


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
            guild=guild,
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

        Ignored entirely while this guild is waiting out a capture
        failure: the bot's own `leave()` emits one of these events, so
        acting on it is precisely how a persistent fault would keep
        restarting itself.
        """
        recording = self._guilds.get(member.guild.id)
        if recording is None:
            return

        touched_channel_ids = {
            channel.id for channel in (before.channel, after.channel) if channel is not None
        }
        if recording.channel_id not in touched_channel_ids:
            return

        if recording.blocked_until is not None:
            # The bot's own departure from the channel produces one of
            # these, so acting on it is how a persistent capture fault
            # feeds itself: leave, rejoin, fail, leave again.
            log.info(
                "Ignoring a voice-state update in channel %d: capture failed and no session "
                "will start there before %s.",
                recording.channel_id,
                recording.blocked_until.isoformat(),
            )
            return

        await self._sync_participants(recording)

    async def _sync_participants(self, recording: _GuildRecording) -> None:
        """Recounts the consenting members present and drives the session machine.

        Reached both from a voice-state update and from the tick loop when
        a capture-failure cooldown lapses, which is why it recounts the
        channel rather than trusting anything the event carried.
        """
        channel = recording.guild.get_channel(recording.channel_id)
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

        It is also where a capture-failure cooldown is armed and, once it
        has passed, lifted. The lifting belongs here rather than in
        `on_voice_state_update` for the same reason the guard exists at
        all: capture failing is not something the people in the channel
        did, so recovery must not wait on one of them doing something.
        """
        for recording in list(self._guilds.values()):
            reason = await recording.service.tick(now)
            if reason is not None:
                await recording.voice.leave()
                recording.service.reset()
                if reason in CAPTURE_FAILURE_REASONS:
                    self._begin_capture_cooldown(recording, reason, now)
                continue
            if recording.blocked_until is not None and now >= recording.blocked_until:
                await self._end_capture_cooldown(recording)

    def _begin_capture_cooldown(
        self, recording: _GuildRecording, reason: EndReason, now: datetime
    ) -> None:
        """Stops this guild rejoining straight back into the same fault.

        A session that ended because nothing could be heard is not a
        session that ended. Leaving the channel is itself a voice-state
        update, so without this the very next event reopens a session
        row, rejoins with fresh decoders, meets the same fault and closes
        again -- an endless run of empty sessions, every one of them
        announcing to the channel that it is being recorded.
        """
        recording.blocked_until = now + REJOIN_COOLDOWN
        log.error(
            "The session in channel %d ended with %s; not recording there again before %s. "
            "Investigate before then: a rejoin would meet the same fault.",
            recording.channel_id,
            reason.value,
            recording.blocked_until.isoformat(),
        )

    async def _end_capture_cooldown(self, recording: _GuildRecording) -> None:
        """Lifts the guard and picks the channel back up, if anyone is still in it.

        Deliberately driven from the tick rather than from the next
        voice-state update: the people who were in the channel when
        capture died are still in it, and nothing about waiting out a
        cooldown makes one of them leave and come back. A guard that can
        only lapse on somebody else's action is an outage wearing a
        timeout's clothes.
        """
        log.info(
            "The capture-failure cooldown for channel %d has passed; recording may resume.",
            recording.channel_id,
        )
        recording.blocked_until = None
        await self._sync_participants(recording)

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
