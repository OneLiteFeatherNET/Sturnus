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

**A recording belongs to a room, not to a server.** `self._recordings` is
keyed by `(guild_id, channel_id)`, so a pipeline is findable under the room
it is actually recording and a retarget moves the entry rather than
mutating a field on a server-level object. What is genuinely per guild
stays per guild and says so: the reconfigure lock (a guild's configuration
is one row set), the configuration complaints, and the capture-failure
cooldown (one voice connection failed, so nothing in that server may open a
session for a while).

None of that lets one guild record two rooms at once, and nothing here
could: one bot identity holds one voice connection per guild. That limit
lives in `channel_choice.MAX_CONCURRENT_SESSIONS_PER_GUILD` and is *asked*
-- `_may_open_another`, `_recording_of`, `ChannelSelection.take` -- rather
than assumed, so lifting it is a constant, a second Discord application and
whatever the type checker then points at.

The bot does **not** run Alembic migrations on start -- the worker owns the
schema (Spec 13.1) -- so `main()` (`sturnus.entrypoints.bot`) waits for the
expected tables to exist and fails loudly if they don't, before this class
is ever constructed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.channel_choice import (
    MAX_CONCURRENT_SESSIONS_PER_GUILD,
    choose_channels,
)
from sturnus.application.ports import (
    AudioStore,
    AudioWriterFactory,
    Clock,
    Encryptor,
    VoiceReceiver,
)
from sturnus.application.reconfigure import (
    IDENTITY_KEYS,
    GuildRuntimeConfig,
    ReconfigureAction,
    ReconfigurePlan,
    ReconfigureResult,
    RunningState,
    plan_reconfigure,
)
from sturnus.application.recording import JobQueue, RecordingService
from sturnus.domain import settings
from sturnus.domain.session import EndReason, SessionTimeouts
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.directory import DirectoryStore
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.repositories import (
    AccountLinkRepository,
    ConsentRepository,
    SessionRepository,
)
from sturnus.infrastructure.discord.about_cog import AboutCog
from sturnus.infrastructure.discord.admin_sync import sync_administrators
from sturnus.infrastructure.discord.announcer import DiscordAnnouncer
from sturnus.infrastructure.discord.audio_cog import AudioCog
from sturnus.infrastructure.discord.config_cog import ConfigCog
from sturnus.infrastructure.discord.consent_cog import ConsentCog
from sturnus.infrastructure.discord.directory_sync import sync_directory
from sturnus.infrastructure.discord.link_cog import LinkCog
from sturnus.infrastructure.discord.queue_cog import QueueCog
from sturnus.infrastructure.discord.setup_cog import SetupCog
from sturnus.infrastructure.discord.voice import VoiceReceiveAdapter, voice_close_code
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.health import ReadinessState
from sturnus.infrastructure.telemetry import (
    SESSION_ACTIVE,
    SESSION_CLOSE_DURATION,
    SESSION_DURATION,
    fail_span,
    record,
    span,
)
from sturnus.observability.events import Event, log_event, log_exception

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
#:
#: **Guild-wide, not per channel, and deliberately so.** A guild now allows
#: a list of channels, so the question had to be asked again: is a capture
#: failure in one room a reason to refuse another? It is, because of what
#: these two end reasons actually mean. `CAPTURE_FAILURE` is a join that
#: raised or a reader that died, and `DECODE_FAILURE` is every stream
#: ceasing to decode -- both are properties of *this process's* one voice
#: connection, its libopus and its gateway session, none of which is per
#: channel. The bot has one voice connection per guild; walking it into the
#: next room reuses every part that just failed.
#:
#: The loop the cooldown exists to stop would also be strictly worse per
#: channel. Leaving a channel is itself a voice-state update, so a
#: per-channel guard would send the bot straight into the next allowed
#: channel, fail there, block that one, and work down the list --
#: announcing to each room in turn that it is being recorded, and
#: recording none of them.
#:
#: The decision was put to the question a second time when the runtime
#: became keyed per room, and it survived -- but on narrower grounds than
#: before, which is why the cooldown no longer sits on a room's record at
#: all. It lives in `_capture_cooldowns`, keyed by guild, so that
#: "guild-wide" is something the state says rather than something the
#: shape of the state happens to imply.
#:
#: **What would make it per room.** Not another allowed channel -- that
#: changes nothing about the connection that failed. A *second bot
#: identity* would: a second token, a second gateway session, a second
#: libopus, a second connection. At that point a fault in one identity's
#: room says nothing about the other's, and blocking both would be an
#: outage invented out of caution. Whoever supplies that second identity
#: (see `MAX_CONCURRENT_SESSIONS_PER_GUILD`) has to key this by whatever
#: actually failed -- the connection -- rather than inherit "guild-wide"
#: because that is what it said here.
REJOIN_COOLDOWN = timedelta(minutes=15)

#: The keys `_desired_config` reads as integers, in the order it reads
#: them. Named here so a value that will not parse can be reported by
#: *key*: the value itself must never reach a log line -- it is free text
#: somebody stored, and `str(ValueError)` quotes it back verbatim -- and
#: "an unusable configuration value" with no key at all leaves the
#: operator reading every row of `/config show` by hand.
_RUNTIME_INTEGER_KEYS: tuple[str, ...] = (
    settings.CONSENT_ROLE_ID,
    settings.EMPTY_GRACE_SECONDS,
    settings.IDLE_TIMEOUT_MINUTES,
    settings.MAX_SESSION_HOURS,
    settings.AUDIO_RETENTION_DAYS,
)


def _unparseable_keys(snapshot: Mapping[str, str | None]) -> tuple[str, ...]:
    """Which of a guild's integer keys cannot be read as an integer.

    Asked only after one of them has already raised, so the cost of
    parsing them all a second time is paid on the failing path alone. A
    key that is absent counts too: `int(None)` is a `TypeError`, and a
    missing row and an unreadable one leave the guild equally unusable.
    """
    unusable: list[str] = []
    for key in _RUNTIME_INTEGER_KEYS:
        value = snapshot.get(key)
        try:
            int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            unusable.append(key)
    return tuple(unusable)


@dataclass
class _ChannelRecording:
    """One room's recording pipeline: the session machine and its connection.

    Filed in `SturnusClient._recordings` under `(guild_id, channel_id)`,
    because that is what it is about. It used to be a `_GuildRecording`,
    filed under the guild alone, with the room it served as a field on it
    -- which said that a recording is something a *server* has and the
    room is a detail of it. It is the other way round: a session belongs to
    the room its audio came from, and its `sessions` row names that room
    for as long as the recording exists.

    Renaming it changes what the runtime can express, not what it does
    today. A guild still holds exactly one of these at a time, because a
    bot identity holds one voice connection per guild -- see
    `MAX_CONCURRENT_SESSIONS_PER_GUILD`, which the client asks rather than
    assumes.

    A note on what is *not* per room. `config`, `pending` and
    `pending_teardown` describe the guild's configuration, which is one
    row set read by every room. They live here because the guild's one
    pipeline is where they can be reached, and lifting the connection
    limit means moving them to a guild-keyed record rather than copying
    them into each room's. `_apply_pending` already behaves as if that had
    happened: it lands a deferred change only when every room of the guild
    is idle.
    """

    #: The configuration actually in force for this pipeline right now --
    #: not what the database says, which is what `reconcile_guild`
    #: compares against. `channel_id`/`role_id` are read off it rather
    #: than duplicated beside it, so a retarget cannot update one and
    #: forget the other.
    #:
    #: Per guild, not per room (see the class docstring).
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
    #: flight. Per guild, not per room (see the class docstring).
    pending: GuildRuntimeConfig | None = None
    #: The configuration was cleared mid-session. A separate flag rather
    #: than `pending=None` overloaded: "nothing is waiting" and "stopping
    #: is waiting" are different states. Per guild, not per room.
    pending_teardown: bool = False
    #: Allowed channels that held consenting members and were not being
    #: served, as of the last headcount -- the rooms this room's session is
    #: keeping waiting. Kept so `/config show` can tell a person waiting in
    #: the second room why nothing is happening, and so the log line about
    #: it is emitted once per *decision* rather than once per voice-state
    #: update.
    #:
    #: Per room, deliberately: "which meetings am I not recording" is
    #: answered relative to the one being recorded, and it is this
    #: pipeline that has to answer it.
    waiting_channel_ids: tuple[int, ...] = ()
    #: Monotonic timestamp of the moment this room's session started
    #: recording, or `None` when it is idle. Kept here rather than on
    #: `RecordingService` because it exists purely to feed
    #: `sturnus.session.duration` and `sturnus.session.active`, and
    #: `RecordingService` is `application` code that may not know
    #: OpenTelemetry exists.
    #:
    #: It is also the idempotence key for `_record_session_close`: a
    #: session can now reach its close through the sweep, through
    #: `/config apply force:true` and through `graceful_shutdown`, and
    #: `sturnus.session.active` must be decremented exactly once whichever
    #: of them gets there.
    #:
    #: Per room, and now trivially so: a session belongs to a room, so the
    #: moment it started belongs to the same place.
    started_monotonic: float | None = None

    @property
    def guild_id(self) -> int:
        """The guild this room belongs to. Half of this record's key."""
        return self.service.guild_id

    @property
    def channel_id(self) -> int:
        """The room this pipeline serves. The other half of its key.

        Read off the service rather than the configuration, because that is
        where it is decided: the configuration names every channel that is
        *allowed*, and which of them is being recorded is settled per pass
        from who is sitting in them. During a session it is the channel the
        session's row names, which is the only answer that is ever true of
        audio already on disk.

        Because it is half the key, moving it is not an assignment but a
        move between dictionary entries -- `SturnusClient._point_at` is
        the one place that happens.
        """
        return self.service.channel_id

    @property
    def channel_ids(self) -> tuple[int, ...]:
        """Every channel this guild allows Sturnus to record in."""
        return self.config.channel_ids

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
        admin_mirror: AdminMemberStore | None = None,
        directory_mirror: DirectoryStore | None = None,
        consent_repo: ConsentRepository,
        session_repo: SessionRepository,
        # Typed against the narrow `JobQueue` port rather than the concrete
        # `JobRepository`: the only thing this class does with it is hand it
        # to `RecordingService`, and `sturnus.entrypoints.bot` passes a
        # `TracedJobQueue` wrapper around the real repository. Widening it
        # here is what lets the tracing decorator be applied at the
        # composition root instead of reaching into this class.
        job_repo: JobQueue,
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
        capture_diagnostics: bool = False,
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
        #: Optional: a client built without one does not mirror, which is
        #: what every test that has no interest in administrators gets.
        #: In production `sturnus.entrypoints.bot` always supplies it.
        self._admin_mirror = admin_mirror
        #: Optional for the same reason `admin_mirror` is: a client built
        #: without one does not mirror names, which is what every test
        #: with no interest in the console gets. In production
        #: `sturnus.entrypoints.bot` always supplies it.
        self._directory_mirror = directory_mirror
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
        self._capture_diagnostics = capture_diagnostics

        #: Every room this process is recording, or is watching so that it
        #: can, keyed by the room: `(guild_id, channel_id)`.
        #:
        #: The key is the change. A recording used to be filed under the
        #: guild with the room as a field on it, which made "which room" a
        #: detail of a server-level object; it is the room that a session,
        #: its `sessions` row and its audio all belong to. One guild still
        #: has at most one entry here, because one bot identity holds one
        #: voice connection per guild -- but that is now a *limit the code
        #: asks about* (`_may_open_another`,
        #: `MAX_CONCURRENT_SESSIONS_PER_GUILD`) rather than a shape the
        #: dictionary quietly enforces.
        self._recordings: dict[tuple[int, int], _ChannelRecording] = {}
        self._tick_task: asyncio.Task[None] | None = None
        #: One lock per guild -- **not** per room, and that is a decision
        #: rather than an omission. What it guards is a guild's
        #: *configuration*, which is one row set: a reconcile reads every
        #: key at once and may build, retarget or tear down whatever
        #: pipelines the guild has. Two passes holding different rooms'
        #: locks could each decide "build" from the same configuration and
        #: construct two `VoiceReceiveAdapter`s, one of whose voice
        #: connections would then sit in a channel forever with nothing
        #: holding a reference to it -- which is the exact failure this
        #: lock exists to prevent.
        #:
        #: The tick loop and a slash command both reconcile on this event
        #: loop and both await database I/O, so they do interleave.
        self._reconfigure_locks: dict[int, asyncio.Lock] = {}
        #: Guilds with a voice-state update already queued behind the lock
        #: above. Keyed the same way the lock is, necessarily: it coalesces
        #: waiters *on that lock*, so a per-room key would let one waiter
        #: per room queue behind a single guild-wide lock and undo the
        #: coalescing entirely.
        #:
        #: discord.py dispatches every gateway event as its own task, so a
        #: busy channel can produce a dozen of them while one reconcile
        #: holds the lock through a `leave()`; each would then recount the
        #: very same membership in turn. Since the handler reads every
        #: allowed channel's *current* members rather than replaying a
        #: delta, one waiter answers for all of them and the rest can be
        #: dropped on arrival.
        self._voice_updates_waiting: set[int] = set()
        #: Last configuration complaint logged per guild -- per guild
        #: because the complaint is about the guild's configuration, which
        #: is one row set and is unreadable or missing as a whole. A guild
        #: that is simply unconfigured -- or whose value someone
        #: fat-fingered with a direct UPDATE -- is reported once, not every
        #: ten seconds.
        self._config_notices: dict[int, str] = {}
        #: Rooms last reported as unseeable, as `(guild_id, channel_id)`.
        #:
        #: Per room, and re-keyed from a per-guild tuple deliberately: "id
        #: 5 cannot be seen as a voice channel" is a fact about that
        #: channel, not about the list it sits in. Keyed per guild, one
        #: newly broken id re-announced every id already known to be
        #: broken, because the deduplication compared the whole set. Now
        #: each room is announced once and stays quiet.
        self._unreadable_channels: set[tuple[int, int]] = set()
        #: Guilds that may not open a session until this moment has passed,
        #: because capture failed. **Keyed by guild, and that is the whole
        #: point of it living here** rather than on a room's record: both
        #: end reasons that arm it are properties of this process's one
        #: voice connection, so the guard is guild-wide, and state that is
        #: guild-wide should have to be written down somewhere guild-wide.
        #: The reasoning, and what would make it per room, is on
        #: `REJOIN_COOLDOWN`.
        self._capture_cooldowns: dict[int, datetime] = {}
        #: Set before anything is torn down, so a `/config set` landing
        #: during SIGTERM cannot rebuild a pipeline (and reconnect a voice
        #: client) after `graceful_shutdown` has already left the channel.
        self._shutting_down = False

    # -- The one-connection limit, in one place -----------------------------

    def _recordings_of(self, guild_id: int) -> tuple[_ChannelRecording, ...]:
        """Every room of this guild that has a pipeline, in channel id order.

        The honest reading of `_recordings`, and the one every loop should
        take. Sorted rather than left in insertion order so a tick, a
        shutdown and a test all visit a guild's rooms in the same sequence
        whatever order they were built in.
        """
        return tuple(
            recording
            for (recorded_guild_id, _channel_id), recording in sorted(self._recordings.items())
            if recorded_guild_id == guild_id
        )

    def _recording_of(self, guild_id: int) -> _ChannelRecording | None:
        """This guild's one pipeline, or `None` while it has none.

        **The single-connection assumption, funnelled into one method.**
        Every caller that says "the guild's recording" -- reconcile,
        `/config`'s reply, the running-state reader -- comes through here,
        so lifting `MAX_CONCURRENT_SESSIONS_PER_GUILD` turns "find the
        callers to fix" into "find the callers of this", which the type
        checker can do. Each of them becomes a loop over `_recordings_of`
        and a decision about which room it meant.

        It returns the lowest-numbered room when the limit is above one,
        which is a defensible answer to a question that will have stopped
        making sense; the point is that it is one place to stop asking it.
        """
        recordings = self._recordings_of(guild_id)
        return recordings[0] if recordings else None

    def _recording_channel_ids(self, guild_id: int) -> tuple[int, ...]:
        """The guild's rooms that have a session in progress right now.

        The replacement for a bare "is this guild recording" boolean, and
        the reason `plan_reconfigure` takes a list. Two things read it: the
        plan, which defers an identity change while any of these rooms is
        busy, and `_apply_pending`, which lands the change only once the
        answer is empty.
        """
        return tuple(
            recording.channel_id
            for recording in self._recordings_of(guild_id)
            if recording.service.is_recording
        )

    def _may_open_another(self, guild_id: int) -> bool:
        """Whether this guild has a free slot for another room's pipeline.

        The guard everything that used to *assume* one connection now
        *asks*. A pipeline holds a `VoiceReceiveAdapter`, an adapter holds
        one voice client, and discord.py allows one voice client per guild
        -- so the slot is taken by the pipeline existing, not by a session
        being open in it.

        Lifting the limit is editing `MAX_CONCURRENT_SESSIONS_PER_GUILD`
        and supplying the second bot identity its docstring describes.
        Nothing else in this class needs to learn to count.
        """
        return len(self._recordings_of(guild_id)) < MAX_CONCURRENT_SESSIONS_PER_GUILD

    def _point_at(self, recording: _ChannelRecording, channel_id: int) -> None:
        """Moves an idle pipeline to another room, its key included.

        The one place a recording changes room, and the reason
        `_recordings` is keyed by `(guild_id, channel_id)` at all: a
        session is a property of the room it happens in, so a pipeline
        that moves has to be findable under the room it moved to and
        nowhere else. Re-pointing without re-keying would file a guild's
        recording under a room nothing is being recorded from, which is
        exactly the bookkeeping this change exists to remove.

        Also the one place the cached channel *name* is refreshed, because
        `RecordingService.retarget` refreshes it and nothing else in the
        process ever does -- so this is called even when the room is
        unchanged. See `_retarget`.

        `RecordingService.retarget` refuses mid-session (a `sessions` row
        must never name one room while its audio came from another), so
        every caller has already established that this pipeline is idle.
        """
        guild_id = recording.guild_id
        self._recordings.pop((guild_id, recording.channel_id), None)
        recording.service.retarget(channel_id, self._channel_name(guild_id, channel_id))
        self._recordings[(guild_id, channel_id)] = recording

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
        # Reads and re-queues transcription jobs. It takes the session
        # factory rather than `JobQueue`/`SessionRepository` on purpose:
        # its selections are specific to these three admin commands and
        # used nowhere else, so neither of those grows a method only a
        # slash command calls -- the same shape `AudioCog` above already
        # has, and for the same reason.
        await self.add_cog(QueueCog(self._session_factory, self._clock))
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
        one-shot `_configure_guild` unconditionally overwrote the guild's
        entry in `self._recordings`, so that blip dropped a recording guild's
        `RecordingService` (unflushed writers, an orphaned plaintext WAV)
        and its `VoiceReceiveAdapter` (a live voice connection nothing
        would ever disconnect). `reconcile_guild` is idempotent, so this
        path is now a no-op for a guild whose configuration is unchanged.
        """
        for guild in self.guilds:
            await self.reconcile_guild(guild.id)
        self._readiness.discord_connected = True
        log_event(
            log,
            logging.INFO,
            Event.BOT_CONNECTED,
            "Connected to Discord",
            count=len(self._recordings),
        )

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

        Returns `None` when the guild cannot record at all -- no recording
        channel named, or `consent_role_id` unset -- which is a genuine
        answer and leads to a teardown, not an error.

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
        # Reads the list key and falls back to the singular one it replaced
        # -- the one place the deprecation is resolved for the bot, so a
        # guild configured before the rename keeps recording without a
        # migration. Raises `InvalidChannelList` for a value that will not
        # parse, which is caught below with every other unusable value.
        try:
            channel_ids = settings.recording_channel_ids(snapshot)
        except settings.InvalidChannelList as exc:
            # The type and the key travel; the value does not.
            # `InvalidChannelList`'s own message embeds the text it
            # refused, and `_notice` renders its arguments *into* the
            # message before logging it -- so passing `exc` here would put
            # a stored configuration value into `LogRecord`'s output, which
            # is precisely what `routes_settings._write` refuses to do for
            # this same exception class. `/config show` is where the value
            # may be read, by somebody who is allowed to.
            self._notice(
                guild_id,
                "Guild %d has an unusable list of recording channels (%s); keeping the "
                "configuration already in force. Read it with /config show and fix it "
                "with /config set %s.",
                guild_id,
                type(exc).__name__,
                settings.VOICE_CHANNEL_IDS,
            )
            return current
        role = snapshot.get(settings.CONSENT_ROLE_ID)
        if not channel_ids or role is None:
            # `missing` is the addition that matters: the prose line says a
            # guild is unconfigured without saying which key is absent, so
            # the operator's next step was always "run /config show and read
            # it yourself". Emitted behind `_notice`'s return value rather
            # than beside it, so the structured line inherits the same
            # per-guild deduplication -- a guild nobody has configured is
            # reconciled every ten seconds forever.
            if self._notice(
                guild_id,
                "Guild %d is missing voice_channel_ids and/or consent_role_id; "
                "an administrator must run /config show to see what's missing.",
                guild_id,
            ):
                log_event(
                    log,
                    logging.WARNING,
                    Event.GUILD_UNCONFIGURED,
                    "Guild cannot record: required configuration is missing. "
                    "An administrator must run /config show.",
                    guild_id=guild_id,
                    missing=sorted(settings.missing_required(snapshot) & set(IDENTITY_KEYS)),
                )
            return None
        try:
            desired = GuildRuntimeConfig(
                channel_ids=channel_ids,
                role_id=int(role),
                timeouts=SessionTimeouts(
                    empty_grace_seconds=int(snapshot[settings.EMPTY_GRACE_SECONDS]),
                    idle_timeout_minutes=int(snapshot[settings.IDLE_TIMEOUT_MINUTES]),
                    max_session_hours=int(snapshot[settings.MAX_SESSION_HOURS]),
                ),
                retention_days=int(snapshot[settings.AUDIO_RETENTION_DAYS]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            # Same rule as the channel list above, and the same reason:
            # `int("half an hour")` raises a `ValueError` that quotes the
            # whole string back. Naming the *keys* that will not parse is
            # more use to the operator than the value would have been
            # anyway -- "an unusable configuration value" with no key left
            # them reading five rows of `/config show` by hand.
            unusable = _unparseable_keys(snapshot)
            self._notice(
                guild_id,
                "Guild %d has an unusable configuration value (%s) for %s; keeping the "
                "configuration already in force. Read it with /config show and fix it "
                "with /config set.",
                guild_id,
                type(exc).__name__,
                ", ".join(unusable) if unusable else "one of its settings",
            )
            return current
        self._clear_notice(guild_id)
        return desired

    def _notice(self, guild_id: int, message: str, *args: object) -> bool:
        """Logs a per-guild configuration complaint once, not once per tick.

        Deduplicated on the *rendered* text, not the template, so a value
        that changes from one bad state to another is reported again while
        the same complaint repeating every ten seconds is not.

        Returns whether this call was the one that logged, so a caller with
        a structured `log_event` to emit alongside the prose can hang it off
        the same deduplication instead of building a second one that drifts.

        The rendered text goes through `%s` rather than being the format
        string: `LogRecord.msg` stays a literal, which is the one thing
        `sturnus.infrastructure.observability.scrub_event` forwards to
        Sentry.

        Which makes every `*args` a caller passes part of what is logged,
        one frame away from where it looks like it is. Ids, key names and
        exception *types* are fine here; a stored configuration value or
        an exception carrying one is not, and `tests/test_logging_
        discipline.py`'s rule R7 fails the build for the latter rather
        than letting the indirection hide it.
        """
        rendered = message % args
        if self._config_notices.get(guild_id) == rendered:
            return False
        self._config_notices[guild_id] = rendered
        log.warning("%s", rendered)
        return True

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
        recording = self._recording_of(guild_id)
        if self._shutting_down:
            return self._result(
                ReconfigurePlan(ReconfigureAction.NOTHING, False, (), ()), recording, False
            )

        current = recording.config if recording is not None else None
        desired = await self._desired_config(guild_id, current)
        # Collected across the guild's rooms rather than read off one of
        # them: an identity key belongs to the guild, so a session
        # anywhere in it is a reason to wait, and the plan gets to name
        # which rooms those are.
        plan = plan_reconfigure(
            current=current,
            desired=desired,
            recording_channel_ids=self._recording_channel_ids(guild_id),
        )

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
            # The configuration says "build"; whether there is a
            # connection left to build with is a different question, and
            # it is asked in exactly one place. Today the two answers can
            # only disagree if a pipeline already exists, which
            # `plan_reconfigure` has just ruled out -- the guard is here
            # so that raising the limit is the whole change rather than
            # the beginning of a hunt.
            if self._may_open_another(guild_id):
                await self._build(guild_id, desired)
                recording = self._recording_of(guild_id)
                became_live = True
        elif plan.action is ReconfigureAction.RETARGET:
            assert recording is not None and desired is not None
            await self._retarget(guild_id, recording, desired)
        elif plan.action is ReconfigureAction.DEFER_RETARGET:
            assert recording is not None and desired is not None
            if force:
                await self._end_sessions_now(guild_id)
                await self._retarget(guild_id, recording, desired)
                plan = replace(
                    plan,
                    action=ReconfigureAction.RETARGET,
                    applied_keys=plan.applied_keys + plan.deferred_keys,
                    deferred_keys=(),
                    deferred_for_channel_ids=(),
                )
            else:
                self._defer_retarget(guild_id, recording, desired, plan.deferred_for_channel_ids)
        elif plan.action is ReconfigureAction.TEARDOWN:
            assert recording is not None
            await self._teardown_guild(guild_id)
            log.info("Guild %d is no longer configured; stopped watching it.", guild_id)
        elif plan.action is ReconfigureAction.DEFER_TEARDOWN:
            assert recording is not None
            if force:
                await self._end_sessions_now(guild_id)
                await self._teardown_guild(guild_id)
                plan = replace(
                    plan,
                    action=ReconfigureAction.TEARDOWN,
                    applied_keys=plan.deferred_keys,
                    deferred_keys=(),
                    deferred_for_channel_ids=(),
                )
            else:
                self._defer_teardown(guild_id, recording, plan.deferred_for_channel_ids)

        live = self._recording_of(guild_id)
        if live is not None:
            self._forget_stale_deferrals(guild_id, live, plan.action)
        return self._result(plan, live, became_live)

    def _defer_retarget(
        self,
        guild_id: int,
        recording: _ChannelRecording,
        desired: GuildRuntimeConfig,
        waiting_on: tuple[int, ...],
    ) -> None:
        """Parks an identity change until the sessions end, announcing it once.

        This runs on every reconcile pass for as long as the session lasts
        -- every ten seconds from the tick loop alone -- so the log line
        belongs to the *transition* into waiting, not to the waiting. A
        deferral standing for a four hour session would otherwise emit the
        same sentence some 1400 times.

        The stored snapshot is refreshed on every pass regardless: only its
        identity is deferred (`_apply_pending` takes nothing else from it),
        but keeping it current means it never describes a configuration
        that has since been replaced.

        `waiting_on` comes from the plan, which collected it across the
        guild's rooms. It is what the line names, rather than "the room
        this pipeline happens to serve" -- with one connection the two are
        the same room, and only one of them is still the right answer
        afterwards.
        """
        if recording.pending is None or recording.pending.identity != desired.identity:
            log.info(
                "Guild %d: channel/role change is stored but a session is "
                "recording in channel %s; it takes effect when that session "
                "ends (at the latest after max_session_hours=%d).",
                guild_id,
                ", ".join(str(channel_id) for channel_id in waiting_on),
                recording.config.timeouts.max_session_hours,
            )
        recording.pending = desired

    def _defer_teardown(
        self, guild_id: int, recording: _ChannelRecording, waiting_on: tuple[int, ...]
    ) -> None:
        """Parks a teardown until the sessions end, announcing it once.

        Same reasoning as `_defer_retarget`: the transition is news, the
        state persisting is not.
        """
        if not recording.pending_teardown:
            log.info(
                "Guild %d: configuration was cleared while a session is "
                "recording in channel %s; the recording finishes and uploads "
                "normally, then Sturnus stops watching.",
                guild_id,
                ", ".join(str(channel_id) for channel_id in waiting_on),
            )
        recording.pending_teardown = True

    def _forget_stale_deferrals(
        self, guild_id: int, recording: _ChannelRecording, action: ReconfigureAction
    ) -> None:
        """Retracts a deferral this pass no longer asks for.

        `pending` and `pending_teardown` are decisions taken by an earlier
        pass, and an administrator is free to undo what prompted them:
        clear `voice_channel_id` mid-session and set it again, or move the
        channel and move it back. Nothing else ever retracts them --
        `_apply_pending` only ever *acts* on them -- so without this the
        pipeline is torn down (or retargeted) the instant the recording
        ends, for a change the database has not asked for since, and
        `/config show` announces that teardown for the whole session
        meanwhile.

        Keyed on the plan rather than on comparing configurations again:
        `plan_reconfigure` has just made exactly that comparison, and any
        action other than the matching deferral means the reason to wait
        is gone.
        """
        if action is not ReconfigureAction.DEFER_TEARDOWN and recording.pending_teardown:
            recording.pending_teardown = False
            log.info(
                "Guild %d: the cleared configuration was restored before the recording "
                "ended; the teardown is cancelled and Sturnus keeps watching channel %d.",
                guild_id,
                recording.channel_id,
            )
        if action is not ReconfigureAction.DEFER_RETARGET and recording.pending is not None:
            recording.pending = None
            log.info(
                "Guild %d: the deferred channel/role change no longer applies -- the "
                "stored configuration changed again before it could land.",
                guild_id,
            )

    def _result(
        self,
        plan: ReconfigurePlan,
        recording: _ChannelRecording | None,
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

    async def _build(self, guild_id: int, desired: GuildRuntimeConfig) -> None:
        """Constructs a guild's recording pipeline and counts who is already there.

        The only path on which a `RecordingService` or a
        `VoiceReceiveAdapter` is ever created, and it runs only for a
        guild that holds neither and has a free connection slot
        (`_may_open_another`, checked by the caller). Every other change
        retargets the existing objects in place, so there is no window in
        which an adapter -- and with it a live voice connection -- can be
        dropped without anyone left to disconnect it.

        The `_sync_participants` at the end is not an optimisation: a
        pipeline built from an empty headcount only ever learns who is in
        the channel from the *next* voice-state update, so an
        administrator who fixed the configuration while three people sat
        waiting in the channel got a bot that reported itself configured
        and recorded nothing until somebody left and rejoined. It is also
        what settles *which* of the allowed channels this pipeline serves:
        the id below is only a starting point, and the headcount replaces
        it with whichever allowed channel actually holds a meeting.
        """
        # The lowest allowed id, purely so the service has a channel before
        # anyone has been counted. `_sync_participants` below retargets it
        # to the real answer within the same call; nothing reads it in
        # between, because a session can only open from that headcount.
        initial_channel_id = desired.channel_ids[0]
        service = RecordingService(
            guild_id=guild_id,
            channel_id=initial_channel_id,
            channel_name=self._channel_name(guild_id, initial_channel_id),
            timeouts=desired.timeouts,
            sessions=self._session_repo,
            jobs=self._job_repo,
            store=self._audio_store,
            writers=self._writer_factory,
            encryptor=self._encryptor,
            # The real adapter, not a seam: unlike `_make_voice` below,
            # nothing here needs a gateway connection to *construct*, so a
            # test can drive this build path and still see what the
            # pipeline said by standing in for the channel it resolves.
            announcer=DiscordAnnouncer(self),
            retention_days=desired.retention_days,
        )
        recording = _ChannelRecording(
            config=desired, service=service, voice=self._make_voice(service)
        )
        self._recordings[(guild_id, initial_channel_id)] = recording
        log_event(
            log,
            logging.INFO,
            Event.GUILD_CONFIGURED,
            "Guild is armed to record; watching its voice channel",
            guild_id=guild_id,
            channel_id=initial_channel_id,
            count=len(desired.channel_ids),
        )
        await self._sync_participants(self.get_guild(guild_id), recording)

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
        instead of either reaching into `_recordings` after the fact or
        needing `discord-ext-voice-recv` and a live gateway connection.
        """
        return VoiceReceiveAdapter(
            self,
            service,
            self._config_store,
            self._clock,
            self._consent_repo,
            capture_diagnostics=self._capture_diagnostics,
        )

    async def _retarget(
        self, guild_id: int, recording: _ChannelRecording, desired: GuildRuntimeConfig
    ) -> None:
        """Points an idle guild at a new list of channels and/or consent role, in place.

        Nothing is replaced: the same `RecordingService` and the same
        adapter carry on, so the adapter's reference to the service can
        never go stale and no voice connection is left behind. `leave()`
        only when the channel being served has just stopped being allowed
        -- it is idempotent, but a role change, or a channel merely being
        *added* to the list, has no connection to drop, and the adapter
        re-reads the consent role on its next `join()` anyway.

        Which of the newly allowed channels is served is not decided here.
        `_sync_participants` decides it, from the headcounts, on the same
        call -- so a list change and an ordinary voice-state update reach
        exactly the same rule, and there is no second place where a
        channel gets chosen. What *is* decided here is the cached channel
        name, which `retarget` re-reads unconditionally: a reconcile is
        the only moment this process asks Discord what the room is called.

        Then the same headcount `_build` takes, for the same reason and
        with the same consequence if it is skipped: after a retarget the
        people who matter are the ones sitting in the *newly allowed*
        channels (or the ones in the old one who have just been given the
        new consent role), and none of them will emit a voice-state update
        merely because the configuration changed underneath them.
        """
        target = recording.channel_id
        if target not in desired.channel_ids:
            await recording.voice.leave()
            # The service must not be left naming a channel the guild no
            # longer allows: if nobody is in any of them, nothing else
            # would move it before the next session opened against it.
            target = desired.channel_ids[0]
        # Unconditional, including when the served channel is unchanged.
        # `_point_at` refreshes the cached channel *name*, and nothing
        # else in the process ever does: `RecordingService` holds the name
        # for the worker, which has no Discord connection to resolve one,
        # and a rename in Discord emits no event this bot acts on. A
        # reconcile is the moment it is re-read -- so a guild that renamed
        # its room and then changed only `consent_role_id` would otherwise
        # keep the old name for the life of the process, and head every
        # protocol from then on with a room that no longer exists under it.
        self._point_at(recording, target)
        recording.config = desired
        recording.pending = None
        await self._sync_participants(self.get_guild(guild_id), recording)

    def _consenting_counts(
        self, guild: discord.Guild, recording: _ChannelRecording
    ) -> dict[int, int]:
        """How many consenting members sit in each allowed channel right now.

        Counts members carrying the consent role, never everyone present:
        an administrator can be in a channel without the role and must not,
        by their presence alone, start a recording nobody consented to
        (Spec 3.1).

        A channel that cannot be read at all -- deleted, or not a voice
        channel this process can see -- is **absent from the result**,
        never present as a zero. The distinction is the same one the
        single-channel version made with `None`: zero means everybody left,
        which is what starts the empty-grace countdown on a live session,
        while "I could not look" must never be spelled that way. Absence
        also keeps one broken entry in the list from stopping the other
        channels from working, which is the whole reason the list is worth
        having.
        """
        counts: dict[int, int] = {}
        for channel_id in recording.channel_ids:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                self._note_unreadable_channel(guild.id, channel_id)
                continue
            self._forget_unreadable_channel(guild.id, channel_id)
            counts[channel_id] = sum(
                1
                for participant in channel.members
                if any(role.id == recording.role_id for role in participant.roles)
            )
        return counts

    def _note_unreadable_channel(self, guild_id: int, channel_id: int) -> None:
        """Reports one room this process cannot see, once.

        Every headcount runs this, and a headcount runs on every voice-state
        update, so the line has to belong to the *transition* -- a channel
        deleted out from under a stored list would otherwise repeat the same
        sentence for as long as the id stays in the configuration.

        Per room rather than per guild, which is the honest key for it: a
        second stale id appearing in the list is news about that id, and
        under the old per-guild tuple it re-announced every id already
        known to be stale alongside it.
        """
        if (guild_id, channel_id) in self._unreadable_channels:
            return
        self._unreadable_channels.add((guild_id, channel_id))
        log.warning(
            "Guild %d allows channel %d, which cannot be seen as a voice channel; "
            "skipping it. Every other allowed channel keeps working. Remove it with "
            "/config set %s.",
            guild_id,
            channel_id,
            settings.VOICE_CHANNEL_IDS,
        )

    def _forget_unreadable_channel(self, guild_id: int, channel_id: int) -> None:
        """Forgets a room that can be read again, so a relapse is reported.

        A channel can come back -- the gateway cache fills in after a
        reconnect, or somebody recreates the room and puts the new id in
        the list. Without this the complaint would be suppressed for the
        life of the process the next time the same id went missing.
        """
        self._unreadable_channels.discard((guild_id, channel_id))

    async def _sync_participants(
        self, guild: discord.Guild | None, recording: _ChannelRecording
    ) -> None:
        """Counts every allowed channel, picks one, and joins it if it starts a session.

        The single place that turns "who is in the channels" into a
        session, used by the voice-state handler and by every path that
        builds or retargets a pipeline alike. Reading each channel's
        membership rather than accumulating deltas is what makes it safe to
        call from all of them: the answer is the truth at the moment it
        runs, so a caller that has just changed which channels count gets
        the right numbers without anyone having to replay the events it
        missed.

        It is also the single place a channel is *chosen*. A guild allows a
        list, `sturnus.application.channel_choice.choose_channels` orders
        it -- most consenting members first, lowest channel id to break a
        tie -- and how many of that order are actually served is asked of
        `MAX_CONCURRENT_SESSIONS_PER_GUILD` rather than assumed to be one.
        The rule lives there rather than inline here so every clause of it
        can be pinned down without a gateway.

        The single place a session can start is also the single place the
        capture-failure guard is enforced. The bot's own `leave()` emits a
        voice-state update, so a guild that has just stopped being able to
        hear would otherwise be handed straight back its own departure and
        rejoin into the same fault; and a deferred channel change landing
        in the meantime must not smuggle a session past the guard either.
        """
        if guild is None:
            return
        blocked_until = self._capture_cooldowns.get(guild.id)
        if blocked_until is not None:
            log.info(
                "Not counting guild %d: capture failed in channel %d and no session "
                "will start anywhere in this server before %s.",
                guild.id,
                recording.channel_id,
                blocked_until.isoformat(),
            )
            return

        counts = self._consenting_counts(guild, recording)

        # A session in progress is never moved. Its `sessions` row already
        # names the channel it opened against, and the audio on disk came
        # from there -- so while one is open (or still closing) the only
        # headcount that matters is that channel's, whatever the others are
        # doing. The bigger meeting in the next room waits for this one to
        # end, which is also the only honest thing to do with one voice
        # connection.
        if recording.service.is_recording or recording.service.needs_reset:
            consented_count = counts.get(recording.channel_id)
            if consented_count is None:
                return
            # The rooms this session is keeping waiting, ranked by the same
            # rule that would pick one of them the moment it ends -- so the
            # first name in the line is the room that goes next, not merely
            # the room with the lowest id.
            others = {
                channel_id: count
                for channel_id, count in counts.items()
                if channel_id != recording.channel_id
            }
            self._note_waiting(
                guild.id,
                recording,
                tuple(ranking.channel_id for ranking in choose_channels(others).ranked),
            )
            await recording.service.participants_changed(consented_count, self._clock.now())
            return

        if not counts:
            # Not one allowed channel could be read. Same answer the single
            # channel version gave: say nothing to the machine rather than
            # tell it everybody left.
            return

        served = choose_channels(counts).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
        self._note_waiting(guild.id, recording, served.waiting)
        if not served.serving:
            await recording.service.participants_changed(0, self._clock.now())
            return
        # This guild holds one pipeline because it holds one voice
        # connection, so it serves the head of the selection and nothing
        # else. `serving` is already no longer than the limit allows, which
        # is the half that will not need rewriting: lifting the limit turns
        # these three lines into a loop that pairs each served room with
        # its own pipeline.
        chosen = served.serving[0]
        if chosen.channel_id != recording.channel_id:
            # Idle, so this is safe: `retarget` only refuses mid-session,
            # and the branch above has already taken every such case.
            self._point_at(recording, chosen.channel_id)
        await recording.service.participants_changed(chosen.consenting, self._clock.now())
        if recording.service.is_recording:
            # The session's bookkeeping opens here, not inside
            # `_start_capture`. The session row exists from this moment
            # whether or not `join()` then works, and `_start_capture`
            # deliberately swallows a failed join into
            # `EndReason.CAPTURE_FAILURE` -- so the close path decrements
            # `sturnus.session.active` for a capture failure too. Pairing
            # the increment with the *session* rather than with the join is
            # what keeps that counter from drifting negative.
            recording.started_monotonic = time.monotonic()
            # An up/down counter, so "is anything recording right now" and
            # "did a session leak" are both one query. A gauge would need a
            # callback on the reader's own thread, which has no event loop.
            record(SESSION_ACTIVE, 1, guild_id=recording.service.guild_id)
            await self._start_capture(recording)

    def _note_waiting(
        self, guild_id: int, recording: _ChannelRecording, waiting: tuple[int, ...]
    ) -> None:
        """Records, and announces once, which allowed channels are not being served.

        A person sitting in the second room deserves an explanation. They
        will not read the pod's logs, so the stored tuple is the half that
        reaches them -- `/config show` renders it -- and the log line is
        for the operator they will ask.

        Emitted on the *transition*, not on the state: a headcount runs on
        every voice-state update and every tick, so a second meeting
        running alongside the first for an hour would otherwise repeat the
        same sentence hundreds of times.

        The rooms arrive ranked, so the line names the one that goes next
        first. It also names the *number* Sturnus can serve rather than
        asserting "one" in prose: the reason those people are waiting is a
        count of voice connections, and printing the count is what makes
        the sentence stay true if the count ever changes.
        """
        if waiting == recording.waiting_channel_ids:
            return
        recording.waiting_channel_ids = waiting
        if not waiting:
            return
        log.info(
            "Guild %d: recording channel %d; %s also %s consenting members and "
            "%s waiting. Sturnus records %d of a server's allowed channels at a "
            "time -- one bot identity holds one voice connection per server -- so "
            "it takes whichever has the most consenting members and follows that "
            "one until its session ends.",
            guild_id,
            recording.channel_id,
            ", ".join(str(channel_id) for channel_id in waiting),
            "have" if len(waiting) > 1 else "has",
            "are" if len(waiting) > 1 else "is",
            MAX_CONCURRENT_SESSIONS_PER_GUILD,
        )

    async def _start_capture(self, recording: _ChannelRecording) -> None:
        """Joins the voice channel, or ends the session it could not capture.

        A `join` that raises used to leave the session row open with no
        capture behind it at all: nothing would ever arrive, so it closed
        at the idle timeout looking exactly like a meeting where nobody
        spoke. Arming `CAPTURE_FAILURE` instead means the next tick closes
        it, leaves the channel and resets, and the row says we could not
        hear rather than that there was nothing to hear.
        """
        # `join()` is a gateway round trip, a libopus probe and a
        # `listen()` call, and it is the only step between "a session row
        # exists" and "audio is arriving". A span over exactly it is what
        # separates a slow join from a slow meeting in a trace.
        with span(
            "session.open",
            guild_id=recording.service.guild_id,
            channel_id=recording.channel_id,
            session_id=recording.service.session_id,
        ) as active:
            try:
                await recording.voice.join(recording.channel_id)
            except Exception as exc:
                # The exception is swallowed here on purpose (see the
                # docstring), so the span has to be marked by hand: `span`
                # only marks the ones that propagate out of it.
                fail_span(active, exc)
                log_exception(
                    log,
                    logging.ERROR,
                    Event.VOICE_JOIN_FAILED,
                    "Could not start voice capture; ending the session rather than "
                    "leaving it open with nothing arriving.",
                    exc,
                    guild_id=recording.service.guild_id,
                    channel_id=recording.channel_id,
                    session_id=recording.service.session_id,
                    end_reason=EndReason.CAPTURE_FAILURE.value,
                    # The one join failure whose type name says nothing
                    # useful: `discord.ConnectionClosed` is what Discord
                    # raises for "session no longer valid", "you were
                    # moved", "rate limited" and "voice server crashed"
                    # alike, and its message is withheld by
                    # `redaction.SAFE_MESSAGE_TYPES`. The code separates
                    # them; see `voice.voice_close_code`.
                    close_code=voice_close_code(exc),
                )
                recording.service.request_close(EndReason.CAPTURE_FAILURE)

    async def _return_to_idle(self, guild_id: int, recording: _ChannelRecording) -> None:
        """Leaves the channel and puts the machine back where a session can start.

        Deliberately independent of whether the `close()` that preceded it
        succeeded. `close()` encrypts, uploads to the object store and
        enqueues jobs, so it can fail for reasons that have nothing to do
        with this guild's ability to record -- and it flips the service to
        closed *before* any of that, and only ever after the machine has
        already moved to CLOSING. A close that raised therefore used to
        leave the machine parked in CLOSING with nothing left to call
        `reset()`: `is_recording` false forever, `voice_packet` dropping
        every packet, `participants_changed` unable to open a row. A guild
        that quietly stops recording is exactly the failure this branch
        exists to prevent, so the way back must not hang off the upload
        having worked.

        `leave()` is guarded for the same reason -- it is a gateway call,
        and a failing one must not take `reset()` down with it.

        A no-op unless the machine is actually parked in CLOSING, so it is
        safe on a path that merely might have closed something: nothing
        here may disconnect a session that is still recording.
        """
        if not recording.service.needs_reset:
            return
        try:
            await recording.voice.leave()
        except Exception as exc:
            log_exception(
                log,
                logging.ERROR,
                Event.VOICE_LEFT_FAILED,
                "Leaving the voice channel failed; continuing anyway so the guild is "
                "able to record again.",
                exc,
                guild_id=guild_id,
                channel_id=recording.channel_id,
                session_id=recording.service.session_id,
            )
        recording.service.reset()

    async def _end_session_now(self, guild_id: int, recording: _ChannelRecording) -> None:
        """Ends the session in progress deliberately, keeping every recording.

        The same sequence a timeout takes -- encrypt, upload, enqueue,
        close the row, leave the channel, `reset()` -- so `force` ends a
        recording early but never discards one, and leaves the pipeline as
        ready for the next session as a timed-out one would.

        `end_now()` rather than `close()`: closing alone leaves the
        `SessionMachine` in RECORDING, so the `reset()` below used to
        raise. The command that reaches this is `/config apply
        force:true`, which the bot's own reply recommends -- an
        administrator following that advice mid-session was left with a
        guild that recorded nothing at all until some later timeout
        fired.

        The `finally` is the second half of that same lesson: a `close()`
        that raises mid-upload leaves the machine in CLOSING just as
        surely as a successful one, and wedges the guild in exactly the
        same way if the recovery is skipped. The error still propagates --
        `/config apply` renders a failed reconcile as its own third answer
        rather than claiming the change is in effect -- but it propagates
        out of a guild that can record again.
        """
        if not recording.service.is_recording:
            return
        session_id = recording.service.session_id
        try:
            await recording.service.end_now(SHUTDOWN_END_REASON, self._clock.now())
        finally:
            # In the `finally` for the same reason `_return_to_idle` is: a
            # close that raised mid-upload still ended the session, and a
            # `sturnus.session.active` that only comes down on the happy
            # path is a counter that climbs forever.
            self._record_session_close(recording, SHUTDOWN_END_REASON, session_id)
            await self._return_to_idle(guild_id, recording)

    async def _end_sessions_now(self, guild_id: int) -> None:
        """Ends every session this guild has in progress. `force`'s first half.

        A loop rather than a call, because a session belongs to a room and
        a guild may in principle have one per free connection. With the
        limit at one it runs at most once -- but "end the recording in
        progress" was never a promise about a single room, and `/config
        apply force:true` says "any recording in progress" for a reason.
        """
        for recording in self._recordings_of(guild_id):
            await self._end_session_now(guild_id, recording)

    async def _teardown(self, guild_id: int, recording: _ChannelRecording) -> None:
        """Stops watching one room. The lock is kept, not popped: a waiter
        blocked on it must not be handed a fresh one and run concurrently."""
        await recording.voice.leave()
        self._recordings.pop((guild_id, recording.channel_id), None)

    async def _teardown_guild(self, guild_id: int) -> None:
        """Stops watching a guild: every room of it, and its guild-wide notes.

        The bookkeeping cleared here is the bookkeeping that is *about the
        guild* rather than about one of its rooms, which is why it is
        cleared here rather than in `_teardown`: an unreadable channel and
        a capture cooldown outlive any single pipeline, and a guild that is
        no longer configured has no use for either.

        The reconfigure lock is deliberately not among them -- see
        `_teardown`.
        """
        for recording in self._recordings_of(guild_id):
            await self._teardown(guild_id, recording)
        # Forgotten with the pipeline, so a guild reconfigured later is
        # told again about a channel it still cannot see.
        self._unreadable_channels = {
            room for room in self._unreadable_channels if room[0] != guild_id
        }
        # Forgotten for the same reason, and with the same consequence: a
        # guild reconfigured after its configuration was cleared starts
        # from a clean slate rather than serving out a guard whose fault
        # nobody can any longer investigate.
        self._capture_cooldowns.pop(guild_id, None)

    async def _apply_pending(self, guild_id: int, recording: _ChannelRecording) -> None:
        """Lands a deferred identity change, at the one safe moment there is.

        Called from `_tick_guild` immediately after `reset()`, which is
        immediately after `close()` has awaited the whole encrypt ->
        upload -> enqueue -> `close_session` -> unlink sequence to
        completion. Nothing is in flight at that instant: no writer is
        open, no file is unencrypted, no job is unenqueued, and the voice
        connection has already been left. It is the only point in the
        process where a channel or role may move without lying about
        where audio came from or stranding a connection.

        **Every room of the guild, not merely this one.** What was
        deferred is a change to `consent_role_id` or `voice_channel_ids`,
        and both belong to the guild: one decides whose voice is recorded
        in every room at once, the other decides which rooms may be
        recorded at all. So the moment is safe only when the guild has no
        session left anywhere -- landing it while a second room still
        records would move that room's consent role or its allowed list
        out from under a session in progress, which is the exact thing
        the deferral exists to prevent.

        With one voice connection per guild the room that just closed is
        always the only one there was, so this guard never fires today. It
        is the difference between a rule and a coincidence.
        """
        if not recording.pending_teardown and recording.pending is None:
            return
        still_recording = self._recording_channel_ids(guild_id)
        if still_recording:
            log.info(
                "Guild %d: a room finished recording, but channel %s is still "
                "recording and the deferred change belongs to the whole server; "
                "it waits for that one too.",
                guild_id,
                ", ".join(str(channel_id) for channel_id in still_recording),
            )
            return
        if recording.pending_teardown:
            await self._teardown_guild(guild_id)
            log.info(
                "Guild %d: the recording finished and uploaded; its configuration "
                "was cleared meanwhile, so Sturnus has stopped watching it.",
                guild_id,
            )
            return
        if recording.pending is not None:
            log.info(
                "Guild %d: the deferred channel/role change is now in force "
                "(allowed channels %s -> %s).",
                guild_id,
                ",".join(str(channel_id) for channel_id in recording.channel_ids),
                ",".join(str(channel_id) for channel_id in recording.pending.channel_ids),
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
                    channel_ids=recording.pending.channel_ids,
                    role_id=recording.pending.role_id,
                ),
            )

    def running_state(self, guild_id: int) -> RunningState:
        """What this process is actually doing for a guild, right now.

        Synchronous and in-memory on purpose: `/config show` reads the
        database for the stored values already, and this line exists
        precisely to say whether those values are the ones in use --
        answering that from the database again would defeat the point.

        `channel_ids` is a collection and `session_limit` travels beside
        it, so the reply can say *serving one of three, because one
        connection is the limit* rather than naming one room and leaving a
        reader to assume the other two are idle by choice.
        """
        recordings = self._recordings_of(guild_id)
        if not recordings:
            return RunningState(
                is_live=False,
                is_recording=False,
                channel_ids=(),
                session_limit=MAX_CONCURRENT_SESSIONS_PER_GUILD,
                allowed_channel_ids=(),
                waiting_channel_ids=(),
                pending_keys=(),
                pending_teardown=False,
            )
        # The guild's configuration, its deferrals and its waiting rooms
        # are the same on every one of its pipelines -- they belong to the
        # guild (see `_ChannelRecording`) -- so any of them answers for all
        # of them, while the served rooms are collected across the lot.
        first = recordings[0]
        pending_keys: tuple[str, ...] = ()
        if first.pending is not None:
            pending_keys = first.pending.identity_changes_from(first.config)
        return RunningState(
            is_live=True,
            is_recording=any(recording.service.is_recording for recording in recordings),
            channel_ids=tuple(recording.channel_id for recording in recordings),
            session_limit=MAX_CONCURRENT_SESSIONS_PER_GUILD,
            allowed_channel_ids=first.channel_ids,
            waiting_channel_ids=first.waiting_channel_ids,
            pending_keys=pending_keys,
            pending_teardown=first.pending_teardown,
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Recomputes the consenting headcount and drives the session machine.

        Runs under the guild's reconfigure lock, because reconfiguring and
        counting participants are not independent. `_teardown` and
        `_retarget` both `await voice.leave()`, and a join landing inside
        that await used to interleave with it: the handler still saw the
        pipeline in `_recordings`, opened a session row against the channel
        being abandoned, generated its data key and reconnected the voice
        client that had just been disconnected -- after which `_teardown`
        popped the guild, stranding both (a session row that nothing is
        left to close, and a voice connection nothing holds a reference to
        and so nothing will ever disconnect). On the `_retarget` path the
        same interleaving instead trips `RecordingService.retarget`'s
        mid-session assertion and leaves the reconfigure half-applied.

        The lock costs nothing on the event loop -- awaiting it suspends
        this handler's task, not the loop -- and a burst of updates cannot
        pile up behind it, because `_voice_updates_waiting` keeps at most
        one waiter per guild: the headcount is read from the channel's
        current membership when the waiter runs, which is at least as
        fresh as anything a dropped update could have contributed.

        Everything the handler decides on is therefore re-read *after* the
        lock is held. The pre-lock reads are a filter and nothing more, so
        that an update about an unrelated channel never takes the lock at
        all.

        An update arriving while this guild is waiting out a capture
        failure ends up doing nothing: `_sync_participants` holds that
        guard, because the bot's own `leave()` produces one of these
        events and acting on it is precisely how a persistent fault would
        keep restarting itself.
        """
        guild_id = member.guild.id
        recording = self._recording_of(guild_id)
        if recording is None:
            return

        # Every *allowed* channel is interesting, not only the one being
        # served: a meeting starting in the second allowed room is exactly
        # the update that has to wake this handler, and filtering on the
        # served channel alone would mean the bot never noticed it.
        touched_channel_ids = {
            channel.id for channel in (before.channel, after.channel) if channel is not None
        }
        if touched_channel_ids.isdisjoint(recording.channel_ids):
            return

        if guild_id in self._voice_updates_waiting:
            return
        lock = self._reconfigure_locks.setdefault(guild_id, asyncio.Lock())
        self._voice_updates_waiting.add(guild_id)
        try:
            async with lock:
                self._voice_updates_waiting.discard(guild_id)
                # `graceful_shutdown` leaves every channel without taking
                # the lock, so a join arriving during it must not rejoin
                # one behind its back.
                if self._shutting_down:
                    return
                for recording in self._recordings_of(guild_id):
                    await self._sync_participants(member.guild, recording)
        finally:
            self._voice_updates_waiting.discard(guild_id)

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
        guild_ids = {guild.id for guild in self.guilds} | {
            guild_id for guild_id, _channel_id in self._recordings
        }
        for guild_id in sorted(guild_ids):
            try:
                await self._tick_guild(guild_id, now)
            except Exception as exc:
                log_exception(
                    log,
                    logging.ERROR,
                    Event.GUILD_TICK_FAILED,
                    "The periodic tick failed for this guild; every other guild is unaffected.",
                    exc,
                    guild_id=guild_id,
                )

    async def _tick_guild(self, guild_id: int, now: datetime) -> None:
        """Closes a due session, lands anything deferred, then reconciles.

        `RecordingService.reset()` is the fix for a bot that used to go
        deaf after its first session: `tick()` on its own only closes a
        session, and closing left `is_recording` false forever, since
        nothing ever put the machine's `SessionMachine` back in `IDLE`.
        `_sweep_due_session` owns that half now -- the `reset()` it calls
        is what lets the very next consenting participant open a fresh
        session, its own row, its own data key, its own writers, on the
        same `RecordingService` instance, so the voice adapter's reference
        to it never goes stale. `_apply_pending` follows immediately,
        because that same instant is the only one at which a deferred
        channel or role change may land.

        Skips the guild entirely while a command is mid-reconcile rather
        than queueing behind it: the loop comes back in ten seconds
        anyway, and queueing would let ticks pile up behind one slow
        database call.

        Lifting a capture-failure cooldown belongs here, last, for the
        same reason the cooldown exists at all: capture failing is not
        something the people in the channel did, so recovery must not
        wait on one of them leaving and coming back. Last, because the
        recount it performs should use the configuration the reconcile
        above has just settled on rather than the one before it.
        """
        lock = self._reconfigure_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            # Per room, because a due session is one room's session: each
            # is swept, closed and put back to idle on its own terms.
            for recording in self._recordings_of(guild_id):
                await self._sweep_due_session(guild_id, recording, now)
            await self._reconcile(guild_id)
            await self._mirror_administrators(guild_id, now)
            await self._mirror_directory(guild_id, now)
            # Per guild, because the cooldown is: one voice connection
            # failed, so nothing in this server may open a session until
            # it has been left alone for a while.
            blocked_until = self._capture_cooldowns.get(guild_id)
            if blocked_until is not None and now >= blocked_until:
                await self._end_capture_cooldown(guild_id)

    async def _mirror_administrators(self, guild_id: int, now: datetime) -> None:
        """Writes this guild's administrators where the console's API can read them.

        On the ordinary tick rather than a sweep of its own: the gateway
        read behind it is a cache lookup, not an API call, so it costs
        nothing to run every ten seconds and needs no rate-limit budget.
        The mirror is therefore never more than one tick stale, which is
        the whole reason it is acceptable for the API to trust it.

        Failures are logged and swallowed. A guild whose administrator
        list could not be refreshed keeps the membership it had, and the
        alternative -- letting this raise -- would take the session
        timeout enforcement in the same tick down with it.
        """
        if self._admin_mirror is None:
            return
        guild = self.get_guild(guild_id)
        if guild is None:
            return
        try:
            await sync_administrators(guild, self._config_store, self._admin_mirror, now)
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.GUILD_TICK_FAILED,
                "Could not mirror this guild's administrators; the console keeps "
                "the membership it already had",
                exc,
                guild_id=guild_id,
            )

    async def _mirror_directory(self, guild_id: int, now: datetime) -> None:
        """Writes this guild's channel, role and member names for `api` to read.

        On the same tick as `_mirror_administrators`, and for the same
        reasons: every gateway read behind it is a cache lookup rather
        than an API call, so it needs no rate-limit budget of its own.

        That accounts for the Discord side only, and the Discord side was
        never the expensive one. A ten-second sweep is affordable here
        because `DirectoryStore` compares before it writes: three indexed
        reads of this guild's own rows per tick, and a statement only when
        something actually moved. Written unconditionally, the same
        cadence would restamp every channel and every role of every guild
        every ten seconds, in tables that change a handful of times a
        year. See `sturnus.infrastructure.db.directory` for what that
        would have cost and what the read costs instead.

        A separate method rather than a second write inside that one,
        because the two mirrors carry different weight. `admin_member`
        decides who may change a guild's settings; this decides whether a
        picker shows a word or a snowflake. Keeping them apart means a
        failure to refresh the cosmetic one never costs the privilege one
        its own refresh in the same tick.

        A guild the bot cannot currently see is skipped entirely --
        `get_guild` returning `None` is "we could not look", which must
        not be written down as "there is nothing there". The mirror keeps
        what it had, and the console goes on naming what it named before.
        """
        if self._directory_mirror is None:
            return
        guild = self.get_guild(guild_id)
        if guild is None:
            return
        try:
            await sync_directory(guild, self._config_store, self._directory_mirror, now)
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.GUILD_TICK_FAILED,
                "Could not mirror this guild's channel and role names; the console "
                "keeps the names it already had",
                exc,
                guild_id=guild_id,
            )

    async def _sweep_due_session(
        self, guild_id: int, recording: _ChannelRecording, now: datetime
    ) -> None:
        """Closes a session the clock says is over, then makes the guild recordable again.

        The close is allowed to fail and the guild still recovers. What
        must never happen is the machine being left in CLOSING with nobody
        to reset it, because from that moment the guild records nothing at
        all -- not until the next restart, since nothing else in the
        process ever leaves that state. So the return to IDLE is keyed on
        `needs_reset`, which is true whether `close()` finished or raised
        halfway through, rather than on the reason `tick()` did or did not
        get to return.

        The failure is logged loudly rather than swallowed: a close that
        raised may well have left a speaker's audio unuploaded, which is
        worth an operator's attention even though `recover_orphans` picks
        it up on the next start. It is not re-raised -- `_tick_all` would
        only log it a second time, and the reconcile that follows in
        `_tick_guild` is exactly what a guild in this state needs.

        The reason is kept rather than reduced to a boolean, because two
        of them -- a capture that died and a set of streams that all
        stopped decoding -- say that this process cannot hear this
        channel, which is a reason not to walk straight back into it.
        Armed before `_apply_pending`, so a deferred channel change
        landing in the same breath cannot start a session behind the
        guard's back.
        """
        reason: EndReason | None = None
        closed = False
        # Read before `tick()`, because a successful close is followed by
        # `reset()` and the id is gone by the time there is anything to say
        # about it.
        session_id = recording.service.session_id
        try:
            reason = await recording.service.tick(now)
            closed = reason is not None
        except Exception as exc:
            # Not necessarily a failed close: `tick()` could equally have
            # raised before deciding anything, in which case nothing ever
            # moved to CLOSING and there is nothing to recover from.
            closed = recording.service.needs_reset
            if closed:
                log_exception(
                    log,
                    logging.ERROR,
                    Event.SESSION_CLOSE_FAILED,
                    "Closing the due session failed; its audio may not have been "
                    "uploaded (recover_orphans picks that up on the next start). "
                    "Returning the guild to a recordable state so it does not stop "
                    "recording silently.",
                    exc,
                    guild_id=guild_id,
                    channel_id=recording.channel_id,
                    session_id=session_id,
                    reason="timeout_sweep",
                )
            else:
                log_exception(
                    log,
                    logging.ERROR,
                    Event.GUILD_TICK_FAILED,
                    "The timeout sweep failed before closing anything; the session in "
                    "progress is untouched.",
                    exc,
                    guild_id=guild_id,
                    session_id=session_id,
                )
        if not closed:
            return
        # Before `_return_to_idle`, which resets the service: after it,
        # there is no session left to attribute the measurement to.
        self._record_session_close(recording, reason, session_id)
        await self._return_to_idle(guild_id, recording)
        if reason is not None and reason in CAPTURE_FAILURE_REASONS:
            self._begin_capture_cooldown(recording, reason, now)
        await self._apply_pending(guild_id, recording)

    def _begin_capture_cooldown(
        self, recording: _ChannelRecording, reason: EndReason, now: datetime
    ) -> None:
        """Stops this guild rejoining straight back into the same fault.

        A session that ended because nothing could be heard is not a
        session that ended. Leaving the channel is itself a voice-state
        update, so without this the very next event reopens a session
        row, rejoins with fresh decoders, meets the same fault and closes
        again -- an endless run of empty sessions, every one of them
        announcing to the channel that it is being recorded.

        Armed by a *room* -- one room's session failed -- and recorded
        against the *guild*, because what failed is the connection the
        whole guild shares. `REJOIN_COOLDOWN` carries the argument, and
        what a second bot identity would change about it.
        """
        self._capture_cooldowns[recording.guild_id] = now + REJOIN_COOLDOWN
        # `duration_seconds` rather than the absolute `blocked_until`: the
        # line carries its own `ts`, so the two together give the moment
        # the guard lifts, and the registry has no field for a timestamp
        # precisely because every line already has one.
        log_event(
            log,
            logging.ERROR,
            Event.VOICE_REJOIN_BLOCKED,
            "The session in this channel ended because we could not hear it; not "
            "recording there again until the cooldown passes. Investigate before then: "
            "a rejoin would meet the same fault.",
            guild_id=recording.service.guild_id,
            channel_id=recording.channel_id,
            end_reason=reason.value,
            duration_seconds=REJOIN_COOLDOWN.total_seconds(),
        )

    async def _end_capture_cooldown(self, guild_id: int) -> None:
        """Lifts the guard and picks the rooms back up, if anyone is still in them.

        Deliberately driven from the tick rather than from the next
        voice-state update: the people who were in the channel when
        capture died are still in it, and nothing about waiting out a
        cooldown makes one of them leave and come back. A guard that can
        only lapse on somebody else's action is an outage wearing a
        timeout's clothes.

        Lifted for the guild, because that is what it was taken out
        against -- and the recount that follows is the ordinary one, so
        the room picked up afterwards is whichever room the headcount now
        says, not necessarily the room capture failed in.
        """
        log.info(
            "The capture-failure cooldown for guild %d has passed; recording may resume.",
            guild_id,
        )
        del self._capture_cooldowns[guild_id]
        for recording in self._recordings_of(guild_id):
            await self._sync_participants(self.get_guild(guild_id), recording)

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
        for (guild_id, _channel_id), recording in sorted(self._recordings.items()):
            # Each room is isolated: SIGTERM gives us one pass at this,
            # and one room whose upload fails must not cost every room
            # after it in the dict the session it is still holding open.
            was_recording = recording.service.is_recording
            session_id = recording.service.session_id
            started = time.monotonic()
            outcome = "ok"
            try:
                # No `reset()` afterwards, deliberately: the process is
                # going away, and a machine left in CLOSING cannot offer a
                # session that would never be recorded.
                #
                # The highest-consequence span in the system, and the reason
                # `sturnus.session.close.duration` exists. `end_now()`
                # encrypts, uploads and enqueues **serially, per speaker**,
                # and it runs during SIGTERM. If six speakers take longer
                # than `terminationGracePeriodSeconds`, Kubernetes kills the
                # pod mid-loop and Spec 15's "the entire session is lost,
                # not just a portion" is what happens. Per-guild isolation
                # changes the blast radius of that, not the question:
                # comparing this histogram's p99 to the grace period is what
                # turns "we lost an evening during a deploy" into a number
                # somebody can act on beforehand.
                with span(
                    "session.close",
                    guild_id=guild_id,
                    session_id=session_id,
                    end_reason=SHUTDOWN_END_REASON.value,
                ):
                    await recording.service.end_now(SHUTDOWN_END_REASON, self._clock.now())
                await recording.voice.leave()
            except Exception as exc:
                # `outcome` is the label main's rewrite made worth having:
                # shutdown is per-guild now, so "the close ran" and "the
                # close ran and worked" are genuinely different questions
                # and the histogram can answer both.
                outcome = "error"
                log_exception(
                    log,
                    logging.ERROR,
                    Event.SESSION_CLOSE_FAILED,
                    "Closing this guild's session during shutdown failed; its audio may "
                    "be left for recover_orphans. Every other guild is unaffected.",
                    exc,
                    guild_id=guild_id,
                    session_id=session_id,
                    reason="shutdown",
                )
            finally:
                if was_recording:
                    record(
                        SESSION_CLOSE_DURATION,
                        time.monotonic() - started,
                        end_reason=SHUTDOWN_END_REASON.value,
                        outcome=outcome,
                    )
                self._record_session_close(recording, SHUTDOWN_END_REASON, session_id)

    def _record_session_close(
        self,
        recording: _ChannelRecording,
        reason: EndReason | None,
        session_id: int | None,
    ) -> None:
        """Closes out one session's metrics. Idempotent per session.

        `end_reason` is an `EndReason` member -- a fixed source literal, so
        bounded as a metric label -- and it answers a question nothing else
        does: are sessions ending because people left, because the idle
        timeout fired, because a deploy cut them short, or because this
        process could not hear the channel? Those are four different
        operational stories that all look like "session closed" today, and
        the last two are the ones that cost a meeting.

        Called from every path a session can now end on -- the timeout
        sweep, `/config apply force:true`, and `graceful_shutdown` -- which
        is why it has to be idempotent rather than merely careful:
        `started_monotonic` is both the measurement's start and the "this
        session has already been accounted for" flag.

        `reason=None` means `tick()` raised after moving the machine to
        CLOSING, so the session did end and nothing can say why. That is
        recorded as `unknown` rather than skipped: skipping it would leave
        `sturnus.session.active` counting a session that no longer exists,
        which is worse than a label admitting ignorance.
        """
        if recording.started_monotonic is None:
            return
        end_reason = reason.value if reason is not None else "unknown"
        record(
            SESSION_DURATION,
            time.monotonic() - recording.started_monotonic,
            end_reason=end_reason,
            guild_id=recording.service.guild_id,
        )
        record(SESSION_ACTIVE, -1, guild_id=recording.service.guild_id)
        recording.started_monotonic = None
        log_event(
            log,
            logging.DEBUG,
            Event.SESSION_CLOSING,
            "Session bookkeeping closed out",
            session_id=session_id,
            guild_id=recording.service.guild_id,
            reason=end_reason,
        )
