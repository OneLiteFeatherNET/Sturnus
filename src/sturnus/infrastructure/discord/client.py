"""The Discord client: wires cogs, voice capture, the tick loop and shutdown.

Every decision already lives elsewhere -- the session state machine and the
encrypt-upload-enqueue sequence in `RecordingService`, consent policy in
`consent_flow`, per-guild configuration in `ConfigStore`. This class only
turns Discord events into calls against those, and turns their results back
into Discord actions: joining or leaving a voice channel, syncing the
command tree, logging a guild that hasn't been configured yet.

Configuration is *reconciled*, not read once. `reconcile_guild` compares
what this process holds against what the database says and is safe to run
at any moment; it is driven by the tick loop (the authority, every ten
seconds), by the `/config` and `/setup` cogs (for latency, and so the
reply can state what actually took effect), and by `on_guild_join`. What
it may do to a guild that is recording right now is decided by
`sturnus.application.reconfigure.plan_reconfigure` -- read its module
docstring before changing anything here.

The bot does **not** run Alembic migrations on start -- the worker owns the
schema (Spec 13.1) -- so `main()` (`sturnus.entrypoints.bot`) waits for the
expected tables to exist and fails loudly if they don't, before this class
is ever constructed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
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
from sturnus.application.reconfigure import (
    GuildRuntimeConfig,
    ReconfigureAction,
    ReconfigurePlan,
    ReconfigureResult,
    RunningState,
    plan_reconfigure,
)
from sturnus.application.recording import RecordingService
from sturnus.domain import settings
from sturnus.domain.session import EndReason, SessionTimeouts
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

    #: The configuration actually in force for this pipeline right now --
    #: not what the database says, which is what `reconcile_guild`
    #: compares against. `channel_id`/`role_id` are read off it rather
    #: than duplicated beside it, so a retarget cannot update one and
    #: forget the other.
    config: GuildRuntimeConfig
    service: RecordingService
    #: Typed against the narrow `VoiceReceiver` port rather than the
    #: concrete `VoiceReceiveAdapter` -- the only thing this class ever
    #: does with it is `join`/`leave`, and a test's fake stands in for it
    #: without dragging in `discord-ext-voice-recv` or a real gateway
    #: connection.
    voice: VoiceReceiver
    #: An identity change that arrived mid-session and must wait for it to
    #: end. Applied by `_apply_pending`, at the one moment nothing is in
    #: flight.
    pending: GuildRuntimeConfig | None = None
    #: The configuration was cleared mid-session. A separate flag rather
    #: than `pending=None` overloaded: "nothing is waiting" and "stopping
    #: is waiting" are different states.
    pending_teardown: bool = False

    @property
    def channel_id(self) -> int:
        return self.config.channel_id

    @property
    def role_id(self) -> int:
        return self.config.role_id


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
        #: One lock per guild. The tick loop and a slash command both
        #: reconcile on this event loop and both await database I/O, so
        #: they interleave; without this, two passes could each decide
        #: "build" and construct two `VoiceReceiveAdapter`s, one of whose
        #: voice connections would then sit in the channel forever with
        #: nothing holding a reference to it.
        self._reconfigure_locks: dict[int, asyncio.Lock] = {}
        #: Last configuration complaint logged per guild, so a guild that
        #: is simply unconfigured -- or whose value someone fat-fingered
        #: with a direct UPDATE -- is reported once, not every ten seconds.
        self._config_notices: dict[int, str] = {}
        #: Set before anything is torn down, so a `/config set` landing
        #: during SIGTERM cannot rebuild a pipeline (and reconnect a voice
        #: client) after `graceful_shutdown` has already left the channel.
        self._shutting_down = False

    async def setup_hook(self) -> None:
        """Loads the cogs and syncs the command tree; runs once before login completes.

        Every cog Sturnus ships is registered here -- a cog that exists but
        is missing from this list is unreachable at runtime even though it
        compiles and its own tests pass (see
        `tests/infrastructure/discord/test_client_cogs.py`, added
        specifically to catch that failure mode).
        """
        await self.add_cog(ConsentCog(self._consent_repo, self._config_store, self._clock))
        await self.add_cog(ConfigCog(self._config_store, self.reconcile_guild, self.running_state))
        await self.add_cog(SetupCog(self._config_store, self._clock, self.reconcile_guild))
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
        """Reconciles every guild. Re-entrant, because Discord re-fires this.

        discord.py raises `on_ready` again whenever a RESUME fails and the
        client has to re-IDENTIFY -- a routine gateway blip. The old
        one-shot `_configure_guild` unconditionally overwrote
        `self._guilds[guild.id]`, so that blip dropped a recording guild's
        `RecordingService` (unflushed writers, an orphaned plaintext WAV)
        and its `VoiceReceiveAdapter` (a live voice connection nothing
        would ever disconnect). `reconcile_guild` is idempotent, so this
        path is now a no-op for a guild whose configuration is unchanged.
        """
        for guild in self.guilds:
            await self.reconcile_guild(guild.id)
        self._readiness.discord_connected = True
        log.info("Connected to Discord; configured for %d guild(s)", len(self._guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Configures a guild that invited the bot while the process was running.

        Without this, such a guild was only ever picked up by the next
        `on_ready` -- in practice, the next restart. The periodic reconcile
        would catch it too, within ten seconds; this makes it immediate.
        """
        await self.reconcile_guild(guild.id)

    async def _desired_config(
        self, guild_id: int, current: GuildRuntimeConfig | None
    ) -> GuildRuntimeConfig | None:
        """Reads what the database says this guild's runtime state should be.

        Returns `None` when the guild cannot record at all -- `voice_channel_id`
        or `consent_role_id` unset -- which is a genuine answer and leads to
        a teardown, not an error.

        A value that will not parse is a different matter and must not be
        treated as either. `ConfigStore.set` validates integers, but a
        direct `UPDATE` against the database does not, and this now runs on
        the same task as the timeout sweep and the readiness heartbeat: an
        unparseable value raising out of here would kill the tick loop for
        *every* guild. So it is caught, logged once, and the currently live
        configuration is kept -- deliberately not the defaults, because
        falling back would silently un-configure a working guild over
        somebody's typo in a shell.
        """
        snapshot = await self._config_store.snapshot(guild_id)
        channel = snapshot.get(settings.VOICE_CHANNEL_ID)
        role = snapshot.get(settings.CONSENT_ROLE_ID)
        if channel is None or role is None:
            self._notice(
                guild_id,
                "Guild %d is missing voice_channel_id and/or consent_role_id; "
                "an administrator must run /config show to see what's missing.",
                guild_id,
            )
            return None
        try:
            desired = GuildRuntimeConfig(
                channel_id=int(channel),
                role_id=int(role),
                timeouts=SessionTimeouts(
                    empty_grace_seconds=int(snapshot[settings.EMPTY_GRACE_SECONDS]),
                    idle_timeout_minutes=int(snapshot[settings.IDLE_TIMEOUT_MINUTES]),
                    max_session_hours=int(snapshot[settings.MAX_SESSION_HOURS]),
                ),
                retention_days=int(snapshot[settings.AUDIO_RETENTION_DAYS]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            self._notice(
                guild_id,
                "Guild %d has an unusable configuration value (%s: %s); keeping the "
                "configuration already in force. Fix it with /config set.",
                guild_id,
                type(exc).__name__,
                exc,
            )
            return current
        self._clear_notice(guild_id)
        return desired

    def _notice(self, guild_id: int, message: str, *args: object) -> None:
        """Logs a per-guild configuration complaint once, not once per tick.

        Deduplicated on the *rendered* text, not the template, so a value
        that changes from one bad state to another is reported again while
        the same complaint repeating every ten seconds is not.
        """
        rendered = message % args
        if self._config_notices.get(guild_id) == rendered:
            return
        self._config_notices[guild_id] = rendered
        log.warning("%s", rendered)

    def _clear_notice(self, guild_id: int) -> None:
        """Forgets a guild's last complaint, so a fixed value is reported again."""
        if self._config_notices.pop(guild_id, None) is not None:
            log.info("Guild %d has a usable configuration again.", guild_id)

    async def reconcile_guild(self, guild_id: int, *, force: bool = False) -> ReconfigureResult:
        """Brings this process in line with the stored configuration for one guild.

        Safe to call at any moment and from anywhere: the slash commands
        await it so their reply can state what took effect, the tick loop
        runs it every ten seconds so a direct database edit or a failed
        command hook cannot leave the process stale, and `on_ready` and
        `on_guild_join` run it too.

        `force=True` additionally ends a session that is in the way --
        through the ordinary close path, so it is still encrypted,
        uploaded and enqueued -- rather than deferring the change behind
        it. Only `/config apply force:true` passes it, and only after
        saying so.
        """
        lock = self._reconfigure_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            return await self._reconcile(guild_id, force=force)

    async def _reconcile(self, guild_id: int, *, force: bool = False) -> ReconfigureResult:
        """The reconcile body. Assumes the guild's lock is already held."""
        recording = self._guilds.get(guild_id)
        if self._shutting_down:
            return self._result(
                ReconfigurePlan(ReconfigureAction.NOTHING, False, (), ()), recording, False
            )

        current = recording.config if recording is not None else None
        desired = await self._desired_config(guild_id, current)
        is_recording = recording is not None and recording.service.is_recording
        plan = plan_reconfigure(current=current, desired=desired, is_recording=is_recording)

        if plan.retune and recording is not None and desired is not None:
            # Timeouts and retention move now, mid-session included, even
            # when the identity keys in the same write have to wait.
            recording.service.apply_tunables(desired.timeouts, desired.retention_days)
            recording.config = replace(
                recording.config,
                timeouts=desired.timeouts,
                retention_days=desired.retention_days,
            )

        became_live = False
        if plan.action is ReconfigureAction.BUILD:
            assert desired is not None
            self._build(guild_id, desired)
            recording = self._guilds[guild_id]
            became_live = True
        elif plan.action is ReconfigureAction.RETARGET:
            assert recording is not None and desired is not None
            await self._retarget(guild_id, recording, desired)
        elif plan.action is ReconfigureAction.DEFER_RETARGET:
            assert recording is not None and desired is not None
            if force:
                await self._end_session_now(recording)
                await self._retarget(guild_id, recording, desired)
                plan = replace(
                    plan,
                    action=ReconfigureAction.RETARGET,
                    applied_keys=plan.applied_keys + plan.deferred_keys,
                    deferred_keys=(),
                )
            else:
                recording.pending = desired
                log.info(
                    "Guild %d: channel/role change is stored but a session is "
                    "recording in channel %d; it takes effect when that session "
                    "ends (at the latest after max_session_hours=%d).",
                    guild_id,
                    recording.channel_id,
                    recording.config.timeouts.max_session_hours,
                )
        elif plan.action is ReconfigureAction.TEARDOWN:
            assert recording is not None
            await self._teardown(guild_id, recording)
            log.info("Guild %d is no longer configured; stopped watching it.", guild_id)
        elif plan.action is ReconfigureAction.DEFER_TEARDOWN:
            assert recording is not None
            if force:
                await self._end_session_now(recording)
                await self._teardown(guild_id, recording)
                plan = replace(
                    plan,
                    action=ReconfigureAction.TEARDOWN,
                    applied_keys=plan.deferred_keys,
                    deferred_keys=(),
                )
            else:
                recording.pending_teardown = True
                log.info(
                    "Guild %d: configuration was cleared while a session is "
                    "recording; the recording finishes and uploads normally, "
                    "then Sturnus stops watching.",
                    guild_id,
                )

        live = self._guilds.get(guild_id)
        return self._result(plan, live, became_live)

    def _result(
        self,
        plan: ReconfigurePlan,
        recording: _GuildRecording | None,
        became_live: bool,
    ) -> ReconfigureResult:
        """Turns the plan and the resulting state into the cogs' answer."""
        exceeded = False
        if recording is not None and recording.service.is_recording:
            exceeded = recording.service.due_reason(self._clock.now()) is not None
        return ReconfigureResult(
            action=plan.action,
            applied_keys=plan.applied_keys,
            deferred_keys=plan.deferred_keys,
            is_live=recording is not None,
            is_recording=recording is not None and recording.service.is_recording,
            became_live=became_live,
            session_exceeds_timeouts=exceeded,
        )

    def _build(self, guild_id: int, desired: GuildRuntimeConfig) -> None:
        """Constructs a guild's recording pipeline.

        The only path on which a `RecordingService` or a
        `VoiceReceiveAdapter` is ever created, and it runs only for a
        guild that holds neither. Every other change retargets the
        existing objects in place, so there is no window in which an
        adapter -- and with it a live voice connection -- can be dropped
        without anyone left to disconnect it.
        """
        service = RecordingService(
            guild_id=guild_id,
            channel_id=desired.channel_id,
            channel_name=self._channel_name(guild_id, desired.channel_id),
            timeouts=desired.timeouts,
            sessions=self._session_repo,
            jobs=self._job_repo,
            store=self._audio_store,
            writers=self._writer_factory,
            encryptor=self._encryptor,
            retention_days=desired.retention_days,
        )
        self._guilds[guild_id] = _GuildRecording(
            config=desired, service=service, voice=self._make_voice(service)
        )
        log.info(
            "Guild %d is now configured; watching voice channel %d.",
            guild_id,
            desired.channel_id,
        )

    def _channel_name(self, guild_id: int, channel_id: int) -> str | None:
        """The voice channel's name, resolved here because only we can.

        The worker writes the protocol header but holds no Discord
        connection and would see nothing but the id. `None` when the guild
        or the channel is not in cache -- the header then falls back to a
        bare link, which is worse than a name and far better than refusing
        to record.
        """
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        return channel.name if channel is not None else None

    def _make_voice(self, service: RecordingService) -> VoiceReceiver:
        """The one place a voice connection is created.

        A named seam rather than an inline constructor call: it is what
        lets the client-level tests exercise the real build path -- the
        one the reported defect broke -- against a fake `VoiceReceiver`,
        instead of either reaching into `_guilds` after the fact or
        needing `discord-ext-voice-recv` and a live gateway connection.
        """
        return VoiceReceiveAdapter(
            self, service, self._config_store, self._clock, self._consent_repo
        )

    async def _retarget(
        self, guild_id: int, recording: _GuildRecording, desired: GuildRuntimeConfig
    ) -> None:
        """Points an idle guild at a new channel and/or consent role, in place.

        Nothing is replaced: the same `RecordingService` and the same
        adapter carry on, so the adapter's reference to the service can
        never go stale and no voice connection is left behind. `leave()`
        only when the channel itself moved -- it is idempotent, but a role
        change alone has no connection to drop, and the adapter re-reads
        the consent role on its next `join()` anyway.
        """
        if desired.channel_id != recording.channel_id:
            await recording.voice.leave()
        recording.service.retarget(
            desired.channel_id, self._channel_name(guild_id, desired.channel_id)
        )
        recording.config = desired
        recording.pending = None

    async def _end_session_now(self, recording: _GuildRecording) -> None:
        """Ends the session in progress deliberately, keeping every recording.

        The same sequence a timeout takes -- encrypt, upload, enqueue,
        close the row -- so `force` ends a recording early but never
        discards one.
        """
        if recording.service.is_recording:
            await recording.service.close(SHUTDOWN_END_REASON, self._clock.now())
            await recording.voice.leave()
            recording.service.reset()

    async def _teardown(self, guild_id: int, recording: _GuildRecording) -> None:
        """Stops watching a guild. The lock is kept, not popped: a waiter
        blocked on it must not be handed a fresh one and run concurrently."""
        await recording.voice.leave()
        self._guilds.pop(guild_id, None)

    async def _apply_pending(self, guild_id: int, recording: _GuildRecording) -> None:
        """Lands a deferred identity change, at the one safe moment there is.

        Called from `_tick_guild` immediately after `reset()`, which is
        immediately after `close()` has awaited the whole encrypt ->
        upload -> enqueue -> `close_session` -> unlink sequence to
        completion. Nothing is in flight at that instant: no writer is
        open, no file is unencrypted, no job is unenqueued, and the voice
        connection has already been left. It is the only point in the
        process where a channel or role may move without lying about
        where audio came from or stranding a connection.
        """
        if recording.pending_teardown:
            await self._teardown(guild_id, recording)
            log.info(
                "Guild %d: the recording finished and uploaded; its configuration "
                "was cleared meanwhile, so Sturnus has stopped watching it.",
                guild_id,
            )
            return
        if recording.pending is not None:
            log.info(
                "Guild %d: the deferred channel/role change is now in force (channel %d -> %d).",
                guild_id,
                recording.channel_id,
                recording.pending.channel_id,
            )
            # Only the *identity* comes from what was deferred. The
            # tunables on `config` are the live ones -- a retune that
            # landed while the identity waited has already been applied to
            # the running service, and adopting the deferred snapshot
            # wholesale would silently roll it back for one tick.
            await self._retarget(
                guild_id,
                recording,
                replace(
                    recording.config,
                    channel_id=recording.pending.channel_id,
                    role_id=recording.pending.role_id,
                ),
            )

    def running_state(self, guild_id: int) -> RunningState:
        """What this process is actually doing for a guild, right now.

        Synchronous and in-memory on purpose: `/config show` reads the
        database for the stored values already, and this line exists
        precisely to say whether those values are the ones in use --
        answering that from the database again would defeat the point.
        """
        recording = self._guilds.get(guild_id)
        if recording is None:
            return RunningState(
                is_live=False,
                is_recording=False,
                channel_id=None,
                pending_keys=(),
                pending_teardown=False,
            )
        pending_keys: tuple[str, ...] = ()
        if recording.pending is not None:
            pending_keys = recording.pending.identity_changes_from(recording.config)
        return RunningState(
            is_live=True,
            is_recording=recording.service.is_recording,
            channel_id=recording.channel_id,
            pending_keys=pending_keys,
            pending_teardown=recording.pending_teardown,
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
            await recording.voice.join(recording.channel_id)

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
        """Ticks and reconciles every guild, isolating each from the others.

        Split out of `_tick_loop` so this -- the actual per-guild decision,
        not the sleep around it -- can be driven directly by a test without
        sleeping through real time.

        Iterates over every guild the process is *in*, not only those it
        already holds a pipeline for: a guild configured after startup has
        no entry yet, and skipping it is precisely the defect this loop
        exists to close.

        Every guild's body is wrapped: this one task also carries the
        readiness heartbeat and every other guild's timeout enforcement,
        and one guild raising here must not take those with it.
        """
        guild_ids = {guild.id for guild in self.guilds} | set(self._guilds)
        for guild_id in sorted(guild_ids):
            try:
                await self._tick_guild(guild_id, now)
            except Exception:
                log.exception("Tick failed for guild %d; other guilds are unaffected.", guild_id)

    async def _tick_guild(self, guild_id: int, now: datetime) -> None:
        """Closes a due session, lands anything deferred, then reconciles.

        `RecordingService.reset()` is the fix for a bot that used to go
        deaf after its first session: `tick()` on its own only closes a
        session, and closing left `is_recording` false forever, since
        nothing ever put the machine's `SessionMachine` back in `IDLE`.
        Calling `reset()` here, right after `close()` has finished
        encrypting, uploading and enqueuing, is what lets the very next
        consenting participant open a fresh session -- its own row, its
        own data key, its own writers -- on the same `RecordingService`
        instance, so the voice adapter's reference to it never goes stale.
        `_apply_pending` follows immediately, because that same instant is
        the only one at which a deferred channel or role change may land.

        Skips the guild entirely while a command is mid-reconcile rather
        than queueing behind it: the loop comes back in ten seconds
        anyway, and queueing would let ticks pile up behind one slow
        database call.
        """
        lock = self._reconfigure_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            recording = self._guilds.get(guild_id)
            if recording is not None:
                reason = await recording.service.tick(now)
                if reason is not None:
                    await recording.voice.leave()
                    recording.service.reset()
                    await self._apply_pending(guild_id, recording)
            await self._reconcile(guild_id)

    async def graceful_shutdown(self) -> None:
        """Closes every active session before the connection is torn down.

        Stops receiving, closes the writers, encrypts, uploads, enqueues,
        then disconnects -- in that order, because a routine deploy sends
        SIGTERM and this is the only thing standing between it and losing
        every session still in progress (Spec 6.4).
        """
        # First, before anything is torn down: a `/config set` that lands
        # while this runs must not rebuild a pipeline and reconnect a
        # voice client behind the `leave()` calls below.
        self._shutting_down = True
        if self._tick_task is not None:
            self._tick_task.cancel()
        for recording in list(self._guilds.values()):
            if recording.service.is_recording:
                await recording.service.close(SHUTDOWN_END_REASON, self._clock.now())
            await recording.voice.leave()
