"""Client-level lifecycle tests (Spec 6, Spec 11).

The session state machine and the encrypt-upload-enqueue sequence are
already covered against fakes in `tests/application/test_recording.py`.
What was missing -- and let the bot regress to recording exactly one
session per process lifetime and then going silently deaf -- was whether
the *client* can carry a guild through more than one session at all.

`on_ready` builds one `RecordingService` per guild and never rebuilds it;
before the fix, nothing ever put a closed session's `SessionMachine` back
into `IDLE`, so `is_recording` stayed `False` forever after the first
session closed. `voice_packet` then returned early and
`participants_changed` could never open a second session row.

Real `discord.py` objects (`Member`, `Guild`, `VoiceChannel`, `VoiceState`)
read live gateway state through their properties, so they are heavy to
construct directly. `unittest.mock.MagicMock(spec=...)` stands in for them
here -- it still satisfies every `isinstance()` check
`on_voice_state_update` makes, since `spec=` sets the mock's `__class__`.
The voice connection itself (`VoiceReceiveAdapter`, `discord-ext-voice-recv`,
a live gateway connection) is replaced with a minimal fake against the
`VoiceReceiver` port -- this suite has none of those and does not need
them to exercise the lifecycle logic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.ports import AudioStore, AudioWriter, Clock, SessionKey, VoiceReceiver
from sturnus.application.reconfigure import GuildRuntimeConfig, ReconfigureAction
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
from sturnus.infrastructure.discord.client import (
    REJOIN_COOLDOWN,
    SturnusClient,
    _GuildRecording,
)
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.health import ReadinessState

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD_ID, CHANNEL_ID, ROLE_ID = 1, 2, 3
OTHER_GUILD_ID, NEW_CHANNEL_ID, NEW_ROLE_ID = 11, 22, 33
ANNA, BEN = 100, 200
RTP = 48_000


def _runtime_config(
    *,
    channel_id: int = CHANNEL_ID,
    role_id: int = ROLE_ID,
    empty_grace_seconds: int = 60,
    idle_timeout_minutes: int = 15,
    max_session_hours: int = 4,
    retention_days: int = 30,
) -> GuildRuntimeConfig:
    return GuildRuntimeConfig(
        channel_id=channel_id,
        role_id=role_id,
        timeouts=SessionTimeouts(
            empty_grace_seconds=empty_grace_seconds,
            idle_timeout_minutes=idle_timeout_minutes,
            max_session_hours=max_session_hours,
        ),
        retention_days=retention_days,
    )


def pcm(frames: int) -> bytes:
    """`frames` of 48 kHz stereo 16-bit input, as Discord delivers it."""
    return b"\x10\x27" * 2 * frames


class FakeClock:
    """Satisfies the `Clock` port with a value the test controls."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeSessions:
    """Stands in for `SessionRepository`, tracking just enough to assert isolation."""

    def __init__(self) -> None:
        self.opened: list[int] = []
        self.keys: dict[int, tuple[str, bytes]] = {}
        self.closed: list[tuple[int, str]] = []
        self.silent_audio: list[tuple[int, int, datetime]] = []
        self._participants: dict[int, set[int]] = {}
        self._next = 1

    async def open_session(
        self, _guild_id: int, _channel_id: int, _channel_name: str | None, _now: datetime
    ) -> int:
        sid = self._next
        self._next += 1
        self.opened.append(sid)
        self._participants[sid] = set()
        return sid

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None:
        self.keys[session_id] = (encryption_key_id, wrapped_data_key)

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:
        return self.keys.get(session_id)

    async def add_participant(
        self, session_id: int, discord_user_id: int, _display_name: str, _now: datetime
    ) -> None:
        self._participants[session_id].add(discord_user_id)

    async def set_audio_epoch(self, _session_id: int, _discord_user_id: int, _at: datetime) -> None:
        pass

    async def record_silent_audio(
        self, session_id: int, discord_user_id: int, at: datetime
    ) -> None:
        self.silent_audio.append((session_id, discord_user_id, at))

    async def close_session(self, session_id: int, _ended_at: datetime, reason: str) -> None:
        self.closed.append((session_id, reason))

    async def session_status(self, _session_id: int) -> str | None:
        return None

    def participants_of(self, session_id: int) -> set[int]:
        return self._participants.get(session_id, set())


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    async def enqueue(self, **kwargs: object) -> int:
        self.enqueued.append(kwargs)
        return len(self.enqueued)


class FakeStore:
    async def put(self, _key: str, source: Path) -> None:
        assert source.exists(), "uploading a file that is not there"

    async def delete(self, key: str) -> None:
        pass


class FakeAudioWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")

    def write(self, _at: datetime, pcm: bytes) -> None:
        self._file.write(pcm)

    def close(self) -> None:
        self._file.close()


class FakeAudioWriterFactory:
    def __init__(self, root: Path) -> None:
        self._root = root

    def open(self, session_id: int, discord_user_id: int, _epoch: datetime) -> AudioWriter:
        path = self._root / f"session-{session_id}" / f"{discord_user_id}.wav"
        return FakeAudioWriter(path)


class FakeEncryptor:
    key_id = "k1"

    def new_session_key(self) -> SessionKey:
        return SessionKey(plaintext=b"0" * 32, wrapped=b"wrapped-key")

    def encrypt(self, source: Path, target: Path, _key: bytes) -> None:
        target.write_bytes(source.read_bytes())


class FakeConfigStore:
    """Stands in for `ConfigStore` on the reconcile path.

    Only `snapshot` matters here -- it is the single query the reconcile
    pass makes -- and it merges over `DEFAULTS` exactly as the real store
    does, so a guild with only the two required keys stored still yields
    usable timeouts and retention.
    """

    def __init__(self) -> None:
        self.values: dict[int, dict[str, str]] = {}

    def write(self, guild_id: int, key: str, value: str | None) -> None:
        """What `/config set` (or a direct `UPDATE`) does to the database."""
        stored = self.values.setdefault(guild_id, {})
        if value is None:
            stored.pop(key, None)
        else:
            stored[key] = value

    async def snapshot(self, guild_id: int) -> dict[str, str]:
        return {**settings.DEFAULTS, **self.values.get(guild_id, {})}


class FakeAnnouncer:
    """Satisfies the `Announcer` port for the pipelines these tests build by hand.

    The pipelines the *client* builds get the real `DiscordAnnouncer`
    instead -- see `test_a_pipeline_the_client_builds_can_warn_its_own_
    channel`, which stands in for the channel rather than for the adapter.
    """

    async def post(self, channel_id: int, text: str) -> None:  # noqa: ARG002
        return None


class FakeVoiceReceiver:
    """Satisfies the `VoiceReceiver` port without a real gateway connection."""

    def __init__(self, *, join_fails: bool = False) -> None:
        self.joined: list[int] = []
        self.left = 0
        self.join_fails = join_fails

    async def join(self, channel_id: int) -> None:
        if self.join_fails:
            raise RuntimeError("the gateway said no")
        self.joined.append(channel_id)

    async def leave(self) -> None:
        self.left += 1


def _role(role_id: int) -> discord.Role:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    return role


def _voice_channel(channel_id: int, members: list[discord.Member]) -> discord.VoiceChannel:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.members = members
    return channel


def _guild(guild_id: int, channel: discord.VoiceChannel) -> MagicMock:
    """Returns the raw `MagicMock`, not narrowed to `discord.Guild`.

    Callers reassign `guild.get_channel.return_value` as the fake channel's
    membership changes between events -- an attribute a real, narrowly
    typed `discord.Guild` does not expose.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel = MagicMock(return_value=channel)
    return guild


def _member(member_id: int, guild: discord.Guild, role_ids: list[int]) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.guild = guild
    member.roles = [_role(role_id) for role_id in role_ids]
    return member


def _voice_state(channel: discord.VoiceChannel | None) -> discord.VoiceState:
    state = MagicMock(spec=discord.VoiceState)
    state.channel = channel
    return state


async def _never_pinged() -> bool:
    return True


class _TestClient(SturnusClient):
    """A client whose built pipelines get a fake voice connection.

    Overriding the one seam (`_make_voice`) rather than patching
    `_guilds` afterwards is what lets these tests drive the *real* build
    path -- `reconcile_guild` -> `_build` -> `RecordingService` -- which is
    exactly the path the reported defect never reached, without needing
    `discord-ext-voice-recv` or a gateway connection.
    """

    voices: list[FakeVoiceReceiver] = []

    def _make_voice(self, _service: RecordingService) -> VoiceReceiver:
        voice = FakeVoiceReceiver()
        self.voices.append(voice)
        return voice


def _client(
    clock: Clock,
    *,
    config_store: FakeConfigStore | None = None,
    sessions: FakeSessions | None = None,
    jobs: FakeJobs | None = None,
    recording_dir: Path | None = None,
    audio_store: AudioStore | None = None,
) -> _TestClient:
    """A `SturnusClient` with every dependency these tests do not touch stubbed out.

    `config_store`, `sessions` and `jobs` are injectable because the
    reconcile tests drive the whole build path through the client rather
    than constructing a `RecordingService` themselves, and then need to
    assert on what that pipeline actually persisted. `audio_store` joins
    them for the tests that need the upload inside `close()` to fail.
    """
    client = _TestClient(
        clock=clock,
        config_store=cast(ConfigStore, config_store)
        if config_store is not None
        else MagicMock(spec=ConfigStore),
        consent_repo=MagicMock(spec=ConsentRepository),
        session_repo=cast(SessionRepository, sessions)
        if sessions is not None
        else MagicMock(spec=SessionRepository),
        job_repo=cast(JobRepository, jobs) if jobs is not None else MagicMock(spec=JobRepository),
        audio_store=audio_store if audio_store is not None else FakeStore(),
        writer_factory=FakeAudioWriterFactory(recording_dir or Path("/tmp")),
        encryptor=FakeEncryptor(),
        readiness=ReadinessState(),
        database_ping=_never_pinged,
        session_factory=MagicMock(spec=async_sessionmaker[AsyncSession]),
        outline_oauth=MagicMock(spec=OutlineOAuth),
        link_states=MagicMock(spec=LinkStateStore),
        account_links=MagicMock(spec=AccountLinkRepository),
    )
    client.voices = []
    return client


def _in_guild(client: SturnusClient, guild: MagicMock) -> None:
    """Makes `client.guilds` report this guild, as the gateway would.

    The reconcile pass visits every guild the bot is *in*, not only those
    it already holds a pipeline for -- a guild configured after startup
    has no pipeline yet, and that is the whole defect.
    """
    client._connection._guilds[guild.id] = guild


def _voice_of(client: SturnusClient, guild_id: int) -> FakeVoiceReceiver:
    voice = client._guilds[guild_id].voice
    assert isinstance(voice, FakeVoiceReceiver)
    return voice


async def test_two_consecutive_sessions_through_the_client(tmp_path: Path) -> None:
    """A guild must be able to record a second session after its first one closes.

    Before the fix: `on_ready` builds exactly one `RecordingService` per
    guild, `tick()` closing a session never put its `SessionMachine` back
    in `IDLE`, and nothing else ever did either -- so after Anna's session
    closed, `is_recording` stayed `False` forever, `voice_packet` for Ben
    would have been silently dropped, and `on_voice_state_update` would
    never have opened a second session row. This test fails against that
    code: `sessions.opened` stops at `[1]` and `client._tick_all` never
    calls `voice.leave()`/`reset()` a second time because there is no
    second session to close.
    """
    clock = FakeClock(T0)
    sessions = FakeSessions()
    jobs = FakeJobs()
    service = RecordingService(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        timeouts=SessionTimeouts(
            empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
        ),
        sessions=sessions,
        jobs=jobs,
        store=FakeStore(),
        writers=FakeAudioWriterFactory(tmp_path),
        encryptor=FakeEncryptor(),
        announcer=FakeAnnouncer(),
        retention_days=30,
    )
    voice = FakeVoiceReceiver()
    # A store that already agrees with the pipeline below, so the reconcile
    # every tick now performs is a no-op and this test keeps measuring only
    # what it was written for: two consecutive sessions on one pipeline.
    store = _configured_store()
    client = _client(clock, config_store=store)
    client._guilds[GUILD_ID] = _GuildRecording(
        config=_runtime_config(), service=service, voice=voice
    )

    empty_channel = _voice_channel(CHANNEL_ID, members=[])
    guild = _guild(GUILD_ID, empty_channel)
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    ben = _member(BEN, guild, role_ids=[ROLE_ID])

    # --- Session 1: Anna joins, speaks, then leaves and the session times out. ---
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[anna])
    await client.on_voice_state_update(anna, _voice_state(None), _voice_state(empty_channel))

    assert sessions.opened == [1]
    assert voice.joined == [CHANNEL_ID]
    assert service.is_recording is True

    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(empty_channel), _voice_state(None))

    clock.advance(timedelta(seconds=61))  # past empty_grace_seconds
    await client._tick_all(clock.now())

    assert voice.left == 1
    assert service.is_recording is False
    assert sessions.closed == [(1, "empty")]
    assert len(jobs.enqueued) == 1
    assert jobs.enqueued[0]["session_id"] == 1
    assert jobs.enqueued[0]["discord_user_id"] == ANNA

    # --- Session 2: Ben joins on the very same client/service/voice objects. ---
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[ben])
    await client.on_voice_state_update(ben, _voice_state(None), _voice_state(empty_channel))

    assert sessions.opened == [1, 2]  # a second, distinct session row
    assert voice.joined == [CHANNEL_ID, CHANNEL_ID]
    assert service.is_recording is True
    assert service.session_id == 2

    await service.voice_packet(BEN, "ben", ssrc=2, rtp_timestamp=RTP, pcm=pcm(960), now=clock.now())

    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(ben, _voice_state(empty_channel), _voice_state(None))

    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert voice.left == 2
    assert sessions.closed == [(1, "empty"), (2, "empty")]
    assert len(jobs.enqueued) == 2
    assert jobs.enqueued[1]["session_id"] == 2
    assert jobs.enqueued[1]["discord_user_id"] == BEN

    # Nothing from the first session leaked into the second: each session
    # has its own participant, its own data key, and its own uploaded file.
    assert sessions.participants_of(1) == {ANNA}
    assert sessions.participants_of(2) == {BEN}
    assert sessions.keys.keys() == {1, 2}
    assert list(tmp_path.rglob("*")) == []  # both sessions cleaned up after themselves


def _configured_store(
    guild_id: int = GUILD_ID, channel_id: int = CHANNEL_ID, role_id: int = ROLE_ID
) -> FakeConfigStore:
    """A store holding exactly the two keys a guild needs to be recordable."""
    store = FakeConfigStore()
    store.write(guild_id, settings.VOICE_CHANNEL_ID, str(channel_id))
    store.write(guild_id, settings.CONSENT_ROLE_ID, str(role_id))
    return store


async def _start_session(client: SturnusClient, guild: MagicMock, member: discord.Member) -> None:
    """Puts one consenting member in the channel, which opens a session."""
    channel_id = client._guilds[guild.id].channel_id
    guild.get_channel.return_value = _voice_channel(channel_id, members=[member])
    await client.on_voice_state_update(
        member, _voice_state(None), _voice_state(_voice_channel(channel_id, members=[member]))
    )
    assert client._guilds[guild.id].service.is_recording is True


async def test_a_guild_configured_after_startup_records_without_a_restart(tmp_path: Path) -> None:
    """The reported defect: `/config set` on a live bot did nothing until a restart.

    `_configure_guild` ran exactly once, at `on_ready`. A guild that was
    unconfigured at that moment -- which is every guild at first install --
    got a log line and no pipeline, and nothing ever revisited that
    decision. The values were in the database and `/config show` reported
    them, but the process was not watching anything.

    This test fails against that code: `_tick_all` there iterates only
    `self._guilds`, which stays empty forever, so the second tick builds
    nothing and `on_voice_state_update` returns early on a guild it has no
    entry for. No slash command is used here on purpose -- the periodic
    reconcile alone must be enough, because a direct database edit or a
    command whose hook raised has no command to lean on.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = FakeConfigStore()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )

    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)

    await client._tick_all(clock.now())
    assert client._guilds == {}, "an unconfigured guild must not be built, and must not raise"

    # An administrator configures the guild while the process keeps running.
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(CHANNEL_ID))
    store.write(GUILD_ID, settings.CONSENT_ROLE_ID, str(ROLE_ID))

    await client._tick_all(clock.now())
    assert GUILD_ID in client._guilds, "the periodic reconcile must pick up the new config"

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)

    assert sessions.opened == [1]
    assert _voice_of(client, GUILD_ID).joined == [CHANNEL_ID]


async def test_a_channel_change_mid_session_never_discards_the_recording(tmp_path: Path) -> None:
    """The load-bearing guarantee: a config change must not touch a live session.

    Replacing the `_GuildRecording` here would drop a `RecordingService`
    holding open `AudioWriter`s (unflushed buffers, a plaintext WAV on the
    PVC that no job points at) and a `VoiceReceiveAdapter` holding a live
    voice connection nothing would ever disconnect. So the change is
    *deferred*: the session finishes, uploads and enqueues first, and only
    then does the new channel take effect.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    voice = _voice_of(client, GUILD_ID)
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    # The channel and the consent role both move, mid-session.
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    store.write(GUILD_ID, settings.CONSENT_ROLE_ID, str(NEW_ROLE_ID))
    result = await client.reconcile_guild(GUILD_ID)

    assert result.action is ReconfigureAction.DEFER_RETARGET
    assert set(result.deferred_keys) == {settings.VOICE_CHANNEL_ID, settings.CONSENT_ROLE_ID}
    # Nothing was swapped, nothing was disconnected, nothing stopped recording.
    assert client._guilds[GUILD_ID].service is service
    assert _voice_of(client, GUILD_ID) is voice
    assert voice.left == 0
    assert service.is_recording is True
    assert client._guilds[GUILD_ID].channel_id == CHANNEL_ID
    assert client._guilds[GUILD_ID].role_id == ROLE_ID

    # A packet arriving after the reconcile still lands in the same session.
    await service.voice_packet(
        ANNA, "anna", ssrc=1, rtp_timestamp=RTP * 2, pcm=pcm(960), now=clock.now()
    )
    assert service.session_id == 1

    # Now let the session time out normally.
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    # The close sequence ran in full *before* the new channel took effect.
    assert sessions.closed == [(1, "empty")]
    assert [job["discord_user_id"] for job in jobs.enqueued] == [ANNA]
    assert sessions.participants_of(1) == {ANNA}
    # ... and only then did the deferred change land.
    assert client._guilds[GUILD_ID].channel_id == NEW_CHANNEL_ID
    assert client._guilds[GUILD_ID].role_id == NEW_ROLE_ID
    assert client._guilds[GUILD_ID].pending is None
    assert client._guilds[GUILD_ID].service is service, "still the same pipeline"
    assert list(tmp_path.rglob("*")) == [], "no orphaned audio left behind"


async def test_clearing_the_channel_mid_session_still_uploads_the_recording(
    tmp_path: Path,
) -> None:
    """A cleared key must end the watch, not the recording in progress."""
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, None)
    result = await client.reconcile_guild(GUILD_ID)

    assert result.action is ReconfigureAction.DEFER_TEARDOWN
    assert client._guilds[GUILD_ID].service is service
    assert service.is_recording is True

    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "empty")]
    assert len(jobs.enqueued) == 1
    assert GUILD_ID not in client._guilds
    assert list(tmp_path.rglob("*")) == []


async def test_a_shortened_idle_timeout_applies_to_the_session_in_progress(
    tmp_path: Path,
) -> None:
    """Tunables are safe to swap mid-session, so they do not wait for anything."""
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.IDLE_TIMEOUT_MINUTES, "1")
    result = await client.reconcile_guild(GUILD_ID)

    assert result.applied_keys == (settings.IDLE_TIMEOUT_MINUTES,)
    assert result.deferred_keys == ()
    assert service.is_recording is True, "changing a tunable must not close the session itself"

    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "idle_timeout")]
    assert len(jobs.enqueued) == 1
    assert client._guilds[GUILD_ID].service is service


async def test_reconciling_twice_changes_nothing(tmp_path: Path) -> None:
    """Idempotence, which is also what makes a re-fired `on_ready` safe.

    discord.py raises `on_ready` again after a failed RESUME. The old
    `_configure_guild` overwrote `self._guilds[guild.id]` unconditionally,
    so a gateway blip during a recording destroyed the live session by
    exactly the mechanism the deferral above exists to prevent.
    """
    clock = FakeClock(T0)
    store = _configured_store()
    client = _client(clock, config_store=store, recording_dir=tmp_path)
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)

    first = await client.reconcile_guild(GUILD_ID)
    service = client._guilds[GUILD_ID].service
    voice = _voice_of(client, GUILD_ID)

    second = await client.reconcile_guild(GUILD_ID)

    assert first.action is ReconfigureAction.BUILD
    assert second.action is ReconfigureAction.NOTHING
    assert client._guilds[GUILD_ID].service is service
    assert _voice_of(client, GUILD_ID) is voice
    assert voice.left == 0
    assert len(client.voices) == 1, "a second pipeline would leak a voice connection"


async def test_an_unparseable_value_neither_raises_nor_un_configures_a_guild(
    tmp_path: Path,
) -> None:
    """`ConfigStore.set` validates integers; a direct `UPDATE` does not.

    This read now runs on the task that also carries the readiness
    heartbeat and every guild's timeout enforcement, so a bad value
    raising here would take all of that down. It must instead keep the
    last known-good configuration -- not fall back to the defaults, which
    would silently un-configure a working guild over someone's typo.
    """
    clock = FakeClock(T0)
    store = _configured_store()
    store.write(OTHER_GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    store.write(OTHER_GUILD_ID, settings.CONSENT_ROLE_ID, str(NEW_ROLE_ID))
    client = _client(clock, config_store=store, recording_dir=tmp_path)
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    other = _guild(OTHER_GUILD_ID, _voice_channel(NEW_CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    _in_guild(client, other)

    await client._tick_all(clock.now())
    before = client._guilds[GUILD_ID].config

    store.write(GUILD_ID, settings.IDLE_TIMEOUT_MINUTES, "abc")
    await client._tick_all(clock.now())

    assert client._guilds[GUILD_ID].config == before, "the last known-good config is kept"
    assert OTHER_GUILD_ID in client._guilds, "one broken guild must not stop the others"

    # A fixed value is picked up again without a restart.
    store.write(GUILD_ID, settings.IDLE_TIMEOUT_MINUTES, "5")
    await client._tick_all(clock.now())
    assert client._guilds[GUILD_ID].config.timeouts.idle_timeout_minutes == 5


async def test_a_reconcile_during_shutdown_cannot_resurrect_a_guild(tmp_path: Path) -> None:
    """`graceful_shutdown` leaves the channel; nothing may rejoin behind it."""
    clock = FakeClock(T0)
    store = FakeConfigStore()
    client = _client(clock, config_store=store, recording_dir=tmp_path)
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)

    await client.graceful_shutdown()
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(CHANNEL_ID))
    store.write(GUILD_ID, settings.CONSENT_ROLE_ID, str(ROLE_ID))
    result = await client.reconcile_guild(GUILD_ID)

    assert result.action is ReconfigureAction.NOTHING
    assert client._guilds == {}


async def test_retarget_refuses_to_run_mid_session(tmp_path: Path) -> None:
    """The assertion that keeps a session row from disagreeing with its audio."""
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)

    with pytest.raises(AssertionError, match="mid-session"):
        client._guilds[GUILD_ID].service.retarget(NEW_CHANNEL_ID, None)


async def test_a_tunable_changed_while_an_identity_change_waits_is_not_rolled_back(
    tmp_path: Path,
) -> None:
    """A deferred channel move must not carry a stale snapshot of the tunables.

    The identity waits for the session; the tunables did not. When the
    deferred change finally lands, taking the whole snapshot it was
    captured with would revert a retune that has already been applied to
    the running service.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    await client.reconcile_guild(GUILD_ID)
    store.write(GUILD_ID, settings.AUDIO_RETENTION_DAYS, "7")
    await client.reconcile_guild(GUILD_ID)

    assert client._guilds[GUILD_ID].config.retention_days == 7
    assert client._guilds[GUILD_ID].channel_id == CHANNEL_ID, "the channel still waits"

    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert client._guilds[GUILD_ID].channel_id == NEW_CHANNEL_ID
    assert client._guilds[GUILD_ID].config.retention_days == 7, "the retune must survive"
    assert jobs.enqueued[0]["retention_until"] == clock.now() + timedelta(days=7)


async def test_forcing_the_apply_ends_the_session_and_still_uploads_it(tmp_path: Path) -> None:
    """`/config apply force:true` is the command the bot itself recommends.

    `render_apply_result` tells an administrator to run it whenever a
    channel change is waiting, and promises the recording "is still
    uploaded and transcribed". Before the fix the force branch raised
    `AssertionError` out of `_end_session_now`: `RecordingService.close()`
    leaves the `SessionMachine` in `RECORDING` (only `tick()` ever moves it
    to `CLOSING`), so the `reset()` that follows tripped its own guard. The
    guild was then left with a service that was closed but never reset --
    `is_recording` false, `voice_packet` dropping every packet,
    `participants_changed` unable to open a row -- so it recorded nothing
    until some later timeout happened to fire, or forever.

    Both halves of the promise are asserted here: the forced end succeeds
    *and* everything captured so far reaches the job queue.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    assert (await client.reconcile_guild(GUILD_ID)).action is ReconfigureAction.DEFER_RETARGET

    result = await client.reconcile_guild(GUILD_ID, force=True)

    # The forced end worked ...
    assert result.action is ReconfigureAction.RETARGET
    assert result.deferred_keys == ()
    assert settings.VOICE_CHANNEL_ID in result.applied_keys
    assert client._guilds[GUILD_ID].channel_id == NEW_CHANNEL_ID
    assert client._guilds[GUILD_ID].pending is None
    # ... and the audio captured so far went the ordinary way out.
    assert sessions.closed == [(1, "shutdown")]
    assert [job["discord_user_id"] for job in jobs.enqueued] == [ANNA]
    assert jobs.enqueued[0]["session_id"] == 1
    assert list(tmp_path.rglob("*")) == [], "no plaintext or orphaned audio left behind"

    # And the guild is not wedged: the very next participant records again.
    ben = _member(BEN, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, ben)
    assert sessions.opened == [1, 2]
    assert client._guilds[GUILD_ID].service is service
    assert _voice_of(client, GUILD_ID).joined[-1] == NEW_CHANNEL_ID


async def test_forcing_the_apply_after_a_cleared_key_ends_and_uploads_the_session(
    tmp_path: Path,
) -> None:
    """The same forced end, on the teardown branch rather than the retarget one."""
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, None)
    result = await client.reconcile_guild(GUILD_ID, force=True)

    assert result.action is ReconfigureAction.TEARDOWN
    assert result.deferred_keys == ()
    assert GUILD_ID not in client._guilds
    assert sessions.closed == [(1, "shutdown")]
    assert [job["discord_user_id"] for job in jobs.enqueued] == [ANNA]
    assert list(tmp_path.rglob("*")) == []


async def test_restoring_a_cleared_key_mid_session_cancels_the_pending_teardown(
    tmp_path: Path,
) -> None:
    """A teardown that is no longer wanted must stop being announced -- and stop happening.

    Before the fix nothing ever cleared `pending_teardown`: `/config show`
    kept promising a teardown that the restored key had already cancelled,
    and `_apply_pending` then destroyed the pipeline anyway the moment the
    session ended, only for the next reconcile to build it again.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    voice = _voice_of(client, GUILD_ID)
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, None)
    assert (await client.reconcile_guild(GUILD_ID)).action is ReconfigureAction.DEFER_TEARDOWN
    assert client.running_state(GUILD_ID).pending_teardown is True

    # The administrator changes their mind during the same session.
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(CHANNEL_ID))
    result = await client.reconcile_guild(GUILD_ID)

    assert result.action is ReconfigureAction.NOTHING
    assert client._guilds[GUILD_ID].pending_teardown is False
    assert client.running_state(GUILD_ID).pending_teardown is False

    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "empty")]
    assert len(jobs.enqueued) == 1
    assert GUILD_ID in client._guilds, "the teardown was cancelled, so nothing may be torn down"
    assert client._guilds[GUILD_ID].service is service, "the pipeline must not be rebuilt"
    assert _voice_of(client, GUILD_ID) is voice
    assert len(client.voices) == 1, "a rebuild would leak a voice connection"


async def test_reverting_a_deferred_identity_change_clears_the_pending_key(
    tmp_path: Path,
) -> None:
    """A deferred change that is undone in the database must stop being reported."""
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    await client.reconcile_guild(GUILD_ID)
    assert client.running_state(GUILD_ID).pending_keys == (settings.VOICE_CHANNEL_ID,)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(CHANNEL_ID))
    result = await client.reconcile_guild(GUILD_ID)

    assert result.action is ReconfigureAction.NOTHING
    assert client._guilds[GUILD_ID].pending is None
    assert client.running_state(GUILD_ID).pending_keys == ()

    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert client._guilds[GUILD_ID].channel_id == CHANNEL_ID, "the reverted channel must not land"
    assert client._guilds[GUILD_ID].service is service


async def test_a_standing_deferral_is_logged_once_not_every_reconcile(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A guild reconciles every ten seconds; a deferral must log the transition only.

    Before the fix both deferral branches logged at INFO on every pass, so
    one deferred channel change emitted the same line 360 times an hour for
    as long as the session lasted.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    await client._guilds[GUILD_ID].service.voice_packet(
        ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0
    )

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    with caplog.at_level(logging.INFO, logger="sturnus.infrastructure.discord.client"):
        for _ in range(4):
            await client.reconcile_guild(GUILD_ID)
        deferred = [
            r for r in caplog.records if "takes effect when that session ends" in r.getMessage()
        ]
        assert len(deferred) == 1, "the transition is news; the state repeating is not"

        caplog.clear()
        store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, None)
        for _ in range(4):
            await client.reconcile_guild(GUILD_ID)
        teardown = [r for r in caplog.records if "then Sturnus stops watching" in r.getMessage()]
        assert len(teardown) == 1


class ExplodingStore:
    """A `FakeStore` whose upload can be made to fail, as S3 does.

    `close()` reaches the object store in the middle of its
    encrypt-upload-enqueue sequence, so this is the realistic way to make
    a close raise *after* the `SessionMachine` has already moved to
    CLOSING -- the state nothing but `reset()` ever leaves.
    """

    def __init__(self) -> None:
        self.fail = False
        self.put_calls = 0

    async def put(self, _key: str, _source: Path) -> None:
        self.put_calls += 1
        if self.fail:
            raise RuntimeError("the object store is unreachable")

    async def delete(self, key: str) -> None:
        pass


class BlockingVoiceReceiver(FakeVoiceReceiver):
    """A `FakeVoiceReceiver` whose `leave()` can be suspended mid-await.

    `_teardown` and `_retarget` both `await voice.leave()`, and that await
    is the window a voice-state update used to slip into. Suspending it on
    demand is what lets a test hold the reconcile open at exactly that
    point and deliver the join that used to interleave with it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.block_next_leave = False
        self.entered_leave = asyncio.Event()
        self.release_leave = asyncio.Event()

    async def leave(self) -> None:
        if self.block_next_leave:
            self.block_next_leave = False
            self.entered_leave.set()
            await self.release_leave.wait()
        await super().leave()


def _channel_map(guild: MagicMock, channels: dict[int, discord.VoiceChannel]) -> None:
    """Makes the mock guild answer `get_channel` per id, as a real one does.

    The default `_guild` mock returns the same channel whatever it is
    asked for, which is enough for a test that only ever has one. A test
    that retargets needs the two channels told apart -- otherwise the
    people waiting in the old one are counted as waiting in the new one.
    """
    guild.get_channel.side_effect = lambda channel_id: channels.get(channel_id)


async def _settle() -> None:
    """Lets every other runnable task advance as far as it can.

    The race tests start a handler and then need it to reach the point
    where it either blocks (fixed) or does its damage (broken) before
    they assert. A few passes through the loop is what separates those
    two outcomes; nothing here sleeps for real time.
    """
    for _ in range(4):
        await asyncio.sleep(0)


async def test_a_failed_upload_must_not_leave_a_guild_recording_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The wedge the first fix round closed, reached through a failing `close()`.

    `tick()` moves the `SessionMachine` to CLOSING and only then runs the
    encrypt-upload-enqueue sequence. That sequence talks to the object
    store, so it can fail for reasons that have nothing to do with this
    guild -- and when it did, the `reset()` that follows was never
    reached: the machine stayed in CLOSING, `is_recording` stayed false,
    `participants_changed` could not open another row, and the guild
    recorded nothing at all until the process was restarted. Nobody would
    notice, which is what makes it the worst failure this bot has.

    So the return to a recordable state must not hang off the upload
    having worked. It must also be *audible*: the assertion on the log is
    load-bearing, because a recovery that recorded nothing about the lost
    upload would trade a silent wedge for a silent data loss.

    Against the unfixed code this fails at `_start_session`: the service
    is closed-but-never-reset, so Ben's arrival opens nothing.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    audio = ExplodingStore()
    store = _configured_store()
    client = _client(
        clock,
        config_store=store,
        sessions=sessions,
        jobs=jobs,
        recording_dir=tmp_path,
        audio_store=cast(AudioStore, audio),
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    # Anna leaves, and the object store goes down before the grace period
    # expires -- so the close this tick performs raises mid-sequence.
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    audio.fail = True
    clock.advance(timedelta(seconds=61))

    with caplog.at_level(logging.ERROR, logger="sturnus.infrastructure.discord.client"):
        await client._tick_all(clock.now())

    assert audio.put_calls == 1, "the close really did get as far as the upload"
    assert sessions.closed == [], "and really did fail before closing the row"
    assert [record for record in caplog.records if record.levelno >= logging.ERROR], (
        "a close that lost a recording must be visible, not swallowed"
    )

    # The guild is not wedged: the very next consenting participant records.
    audio.fail = False
    ben = _member(BEN, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, ben)

    assert sessions.opened == [1, 2], "a second session must still be reachable"
    assert client._guilds[GUILD_ID].service is service, "on the same pipeline"
    assert _voice_of(client, GUILD_ID).joined == [CHANNEL_ID, CHANNEL_ID]

    await service.voice_packet(BEN, "ben", ssrc=2, rtp_timestamp=RTP, pcm=pcm(960), now=clock.now())
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(ben, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert sessions.closed == [(2, "empty")], "and closes normally once the store is back"
    assert [job["discord_user_id"] for job in jobs.enqueued] == [BEN]
    # Anna's encrypted recording is deliberately still on disk: the upload
    # never confirmed, so `recover_orphans` owns it from here.
    assert [path.name for path in tmp_path.rglob("*.enc")] == [f"{ANNA}.enc"]


async def test_a_forced_apply_whose_upload_fails_still_leaves_the_guild_recordable(
    tmp_path: Path,
) -> None:
    """The same wedge on the `end_now()` path, which `/config apply force:true` takes.

    `_end_session_now` used to run `leave()` and `reset()` as plain
    statements after the close, so a close that raised skipped both --
    leaving a machine in CLOSING that nothing would ever return to IDLE,
    for an administrator who had just been told by the bot to run this
    very command.

    The error still propagates (`/config apply` renders a failed reconcile
    as its own answer rather than claiming success), but it propagates out
    of a guild that can record again. Against the unfixed code the
    `_start_session` below fails.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    audio = ExplodingStore()
    store = _configured_store()
    client = _client(
        clock,
        config_store=store,
        sessions=sessions,
        jobs=jobs,
        recording_dir=tmp_path,
        audio_store=cast(AudioStore, audio),
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)
    service = client._guilds[GUILD_ID].service
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    assert (await client.reconcile_guild(GUILD_ID)).action is ReconfigureAction.DEFER_RETARGET

    audio.fail = True
    with pytest.raises(RuntimeError, match="object store"):
        await client.reconcile_guild(GUILD_ID, force=True)

    assert service.needs_reset is False, "the machine must not be left parked in CLOSING"
    assert service.is_recording is False

    audio.fail = False
    ben = _member(BEN, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, ben)

    assert sessions.opened == [1, 2]
    assert client._guilds[GUILD_ID].service is service


async def test_members_already_in_the_channel_start_recording_without_rejoining(
    tmp_path: Path,
) -> None:
    """The reported complaint: configured, three people waiting, and nothing happens.

    A freshly built pipeline used to learn its headcount only from
    *subsequent* voice-state updates, so an administrator who fixed the
    configuration while people sat in the channel got a bot that reported
    itself live and recorded nothing until somebody left and rejoined.

    Not a single voice-state update is delivered anywhere in this test, on
    purpose -- and what is asserted is that a session actually opens, that
    the bot actually joins, and that audio actually lands in that session,
    rather than that some counting function was called.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = FakeConfigStore()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )

    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    ben = _member(BEN, guild, role_ids=[ROLE_ID])
    # An administrator sitting in on the call without the consent role,
    # who must not be counted (Spec 3.1).
    admin = _member(999, guild, role_ids=[NEW_ROLE_ID])
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[anna, ben, admin])

    await client._tick_all(clock.now())
    assert client._guilds == {}, "nothing to build yet"

    # The configuration is fixed while all three are already in the channel.
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(CHANNEL_ID))
    store.write(GUILD_ID, settings.CONSENT_ROLE_ID, str(ROLE_ID))
    await client._tick_all(clock.now())

    service = client._guilds[GUILD_ID].service
    assert sessions.opened == [1], "the people already waiting must start a session"
    assert service.is_recording is True
    assert _voice_of(client, GUILD_ID).joined == [CHANNEL_ID], "and the bot must join them"

    # And it is a real recording, not just an open row.
    await service.voice_packet(ANNA, "anna", ssrc=1, rtp_timestamp=RTP, pcm=pcm(960), now=T0)
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[])
    await client.on_voice_state_update(anna, _voice_state(guild.get_channel()), _voice_state(None))
    clock.advance(timedelta(seconds=61))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "empty")]
    assert sessions.participants_of(1) == {ANNA}
    assert [job["discord_user_id"] for job in jobs.enqueued] == [ANNA]
    assert list(tmp_path.rglob("*")) == []


async def test_a_retarget_counts_the_people_already_in_the_new_channel(
    tmp_path: Path,
) -> None:
    """The same omission on the retarget path: the channel moves, nobody moves.

    Moving the configured channel emits no voice-state update for the
    people already sitting in the new one, so a retargeted pipeline that
    does not count them waits for a rejoin exactly as a freshly built one
    did.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )

    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    _channel_map(
        guild,
        {
            CHANNEL_ID: _voice_channel(CHANNEL_ID, members=[]),
            NEW_CHANNEL_ID: _voice_channel(NEW_CHANNEL_ID, members=[anna]),
        },
    )

    await client.reconcile_guild(GUILD_ID)
    assert sessions.opened == [], "the configured channel is empty; nothing starts"

    # The administrator points the bot at the channel Anna is already in.
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    result = await client.reconcile_guild(GUILD_ID)

    assert result.action is ReconfigureAction.RETARGET
    assert sessions.opened == [1]
    assert client._guilds[GUILD_ID].service.is_recording is True
    assert _voice_of(client, GUILD_ID).joined == [NEW_CHANNEL_ID]


async def test_a_join_during_a_teardown_cannot_strand_a_session_or_a_connection(
    tmp_path: Path,
) -> None:
    """`on_voice_state_update` ran outside the per-guild reconfigure lock.

    `_teardown` awaits `voice.leave()` and only afterwards drops the guild
    from `_guilds`. A member joining inside that await used to interleave:
    the handler still found the pipeline, opened a session row against the
    channel being abandoned, generated its data key, and reconnected the
    voice client the teardown had just disconnected. `_teardown` then
    popped the guild -- stranding both. Nothing was left to tick that
    session to a close, so its row stayed `open` forever and its audio was
    never uploaded, and nothing held the voice connection, so nothing
    would ever disconnect it.

    Against the unfixed code this fails on the first two assertions.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client.reconcile_guild(GUILD_ID)

    voice = BlockingVoiceReceiver()
    client._guilds[GUILD_ID].voice = voice

    # The configuration is cleared, and the teardown stalls inside `leave()`.
    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, None)
    voice.block_next_leave = True
    teardown = asyncio.create_task(client.reconcile_guild(GUILD_ID))
    await voice.entered_leave.wait()

    # Anna joins at exactly that moment.
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    channel = _voice_channel(CHANNEL_ID, members=[anna])
    guild.get_channel.return_value = channel
    joining = asyncio.create_task(
        client.on_voice_state_update(anna, _voice_state(None), _voice_state(channel))
    )
    await _settle()

    voice.release_leave.set()
    await teardown
    await joining

    assert sessions.opened == [], "a join during the teardown must not open a session row"
    assert voice.joined == [], "and must not reconnect the connection just disconnected"
    assert GUILD_ID not in client._guilds
    assert voice.left == 1


async def test_a_join_during_a_retarget_cannot_corrupt_the_reconfigure(
    tmp_path: Path,
) -> None:
    """The same interleaving on `_retarget`, where it corrupts differently.

    Here the join lands while the *old* channel is still what
    `recording.channel_id` reports, so the handler opened a session
    against the channel being left behind -- and `_retarget` then reached
    `RecordingService.retarget`, whose mid-session assertion fired and
    tore the reconcile in half: the voice client disconnected, the config
    not updated, a session row open on a channel nobody is watching.

    Under the lock the handler waits and then recounts against the channel
    that is configured *now*, which is the only headcount that was ever
    meaningful. Against the unfixed code this raises `AssertionError`.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    # Kept as a dict so the old channel's membership can change between
    # events, the way a real one does, without reassigning a read-only
    # `VoiceChannel.members`.
    channels: dict[int, discord.VoiceChannel] = {
        CHANNEL_ID: _voice_channel(CHANNEL_ID, members=[]),
        NEW_CHANNEL_ID: _voice_channel(NEW_CHANNEL_ID, members=[]),
    }
    _channel_map(guild, channels)
    await client.reconcile_guild(GUILD_ID)

    voice = BlockingVoiceReceiver()
    client._guilds[GUILD_ID].voice = voice

    store.write(GUILD_ID, settings.VOICE_CHANNEL_ID, str(NEW_CHANNEL_ID))
    voice.block_next_leave = True
    retarget = asyncio.create_task(client.reconcile_guild(GUILD_ID))
    await voice.entered_leave.wait()

    # Anna joins the channel the bot is in the middle of leaving.
    channels[CHANNEL_ID] = _voice_channel(CHANNEL_ID, members=[anna])
    joining = asyncio.create_task(
        client.on_voice_state_update(anna, _voice_state(None), _voice_state(channels[CHANNEL_ID]))
    )
    await _settle()

    voice.release_leave.set()
    result = await retarget
    await joining

    assert result.action is ReconfigureAction.RETARGET
    assert client._guilds[GUILD_ID].channel_id == NEW_CHANNEL_ID
    assert sessions.opened == [], "the abandoned channel must not open a session"
    assert voice.joined == [], "and the new channel is empty, so nothing is joined"


def _service(sessions: FakeSessions, jobs: FakeJobs, root: Path) -> RecordingService:
    """The guild's recording pipeline, with everything below it faked."""
    return RecordingService(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        timeouts=SessionTimeouts(
            empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
        ),
        sessions=sessions,
        jobs=jobs,
        store=FakeStore(),
        writers=FakeAudioWriterFactory(root),
        encryptor=FakeEncryptor(),
        announcer=FakeAnnouncer(),
        retention_days=30,
    )


def _capture_guild(
    client: SturnusClient,
    sessions: FakeSessions,
    voice: FakeVoiceReceiver,
    tmp_path: Path,
) -> tuple[discord.Member, discord.VoiceChannel]:
    """Registers one configured guild whose channel holds a single consenting member.

    The pipeline is handed in ready-made rather than built through
    `reconcile_guild`, because these tests need to hold the voice
    connection themselves -- one of them needs a `join` that fails. The
    stored configuration matches it exactly, so the reconcile every tick
    performs is a no-op and the guard is the only thing under test.

    Returns that member and the channel they are in. Nobody leaves it in
    either test below: staying is the point -- the fault is the bot's,
    not theirs.
    """
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    client._guilds[GUILD_ID] = _GuildRecording(
        config=_runtime_config(),
        service=_service(sessions, FakeJobs(), tmp_path),
        voice=voice,
    )

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    occupied = _voice_channel(CHANNEL_ID, members=[anna])
    guild.get_channel.return_value = occupied
    return anna, occupied


async def test_a_join_that_fails_ends_the_session_rather_than_leaving_it_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A session with no capture behind it must not look like a quiet meeting.

    `join` raising used to propagate into discord.py's event dispatcher
    and leave the session row open with nothing listening: no frame and no
    speaking event would ever arrive, so it closed at the idle timeout,
    indistinguishable in the database from a channel where nobody spoke.
    That row is the whole reason the production incident went unnoticed for
    hours, so a capture that never started gets its own end reason.
    """
    clock = FakeClock(T0)
    sessions = FakeSessions()
    voice = FakeVoiceReceiver(join_fails=True)
    client = _client(clock, config_store=_configured_store())
    anna, occupied = _capture_guild(client, sessions, voice, tmp_path)

    with caplog.at_level(logging.ERROR, logger="sturnus.infrastructure.discord.client"):
        await client.on_voice_state_update(anna, _voice_state(None), _voice_state(occupied))

    assert len(caplog.records) == 1, "a capture that never started is never silent"
    assert sessions.opened == [1], "the session row was already open by then"

    clock.advance(timedelta(seconds=1))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "capture_failure")], "not idle_timeout, fifteen minutes later"
    assert voice.left == 1
    assert client._guilds[GUILD_ID].service.is_recording is False


async def test_a_capture_side_end_does_not_open_another_session_straight_after(
    tmp_path: Path,
) -> None:
    """The bot's own leave() must not be what starts the next session.

    Closing a session makes the bot leave the channel, and leaving is
    itself a voice-state update. Without the guard the very next event
    opens a fresh session row, rejoins with fresh decoders, meets the
    same fault and closes again -- a run of empty sessions, each one
    announcing to everyone present that they are being recorded.

    Fails without the guard: `sessions.opened` reaches `[1, 2]` on the
    voice-state update the bot's own departure produced.
    """
    clock = FakeClock(T0)
    sessions = FakeSessions()
    voice = FakeVoiceReceiver()
    client = _client(clock, config_store=_configured_store())
    anna, occupied = _capture_guild(client, sessions, voice, tmp_path)

    await client.on_voice_state_update(anna, _voice_state(None), _voice_state(occupied))
    assert sessions.opened == [1]
    assert voice.joined == [CHANNEL_ID]

    # Every stream stops decoding, so the adapter arms a close that is
    # nothing like a timeout.
    client._guilds[GUILD_ID].service.request_close(EndReason.DECODE_FAILURE)
    clock.advance(timedelta(seconds=1))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "decode_failure")]
    assert voice.left == 1

    # The bot leaving is itself a voice-state update, and Anna is still there.
    clock.advance(timedelta(seconds=1))
    await client.on_voice_state_update(anna, _voice_state(occupied), _voice_state(occupied))

    assert sessions.opened == [1], "a rejoin would meet the same fault"
    assert voice.joined == [CHANNEL_ID]

    # Nor do the ticks in between quietly do it instead.
    clock.advance(timedelta(seconds=30))
    await client._tick_all(clock.now())

    assert sessions.opened == [1]
    assert voice.joined == [CHANNEL_ID]


async def test_the_guard_lapses_on_the_tick_with_no_voice_state_update_at_all(
    tmp_path: Path,
) -> None:
    """Recovery must not wait for somebody to happen to join or leave.

    Capture failing is not something the people in the channel did, and
    none of them has any reason to leave and come back afterwards. A
    guard that can only lapse inside `on_voice_state_update` therefore
    leaves a guild whose membership does not change again blocked
    forever -- a transient fault turned into an outage.

    Fails without the tick-driven lapse: no voice-state update is
    dispatched after the failure anywhere in this test, so
    `sessions.opened` stays `[1]`.
    """
    clock = FakeClock(T0)
    sessions = FakeSessions()
    voice = FakeVoiceReceiver()
    client = _client(clock, config_store=_configured_store())
    anna, occupied = _capture_guild(client, sessions, voice, tmp_path)

    await client.on_voice_state_update(anna, _voice_state(None), _voice_state(occupied))
    client._guilds[GUILD_ID].service.request_close(EndReason.CAPTURE_FAILURE)
    clock.advance(timedelta(seconds=1))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "capture_failure")]
    assert sessions.opened == [1]

    clock.advance(REJOIN_COOLDOWN + timedelta(seconds=1))
    await client._tick_all(clock.now())

    assert sessions.opened == [1, 2], "a pause, not a giving up"
    assert voice.joined == [CHANNEL_ID, CHANNEL_ID]
    assert client._guilds[GUILD_ID].service.is_recording is True
    assert client._guilds[GUILD_ID].blocked_until is None


async def test_a_pipeline_the_client_builds_can_warn_its_own_channel(tmp_path: Path) -> None:
    """The wiring, end to end through the real build path and the real adapter.

    `RecordingService` can only say anything if `_build` handed it an
    announcer, and it can only say it in the right place if that announcer
    resolves the session's own voice channel and sends there. None of that
    is visible to `tests/application/test_recording.py`, which constructs
    the service itself around a fake -- exactly the kind of gap that let a
    `sessions_to_announce` with no caller anywhere sit in this codebase
    with passing tests (Defect 3).

    So nothing between `reconcile_guild` and `VoiceChannel.send` is
    substituted here: the real `DiscordAnnouncer` runs, and only the
    channel it resolves is a stand-in. `_voice_channel` produces a
    `MagicMock(spec=discord.VoiceChannel)`, which really does satisfy the
    adapter's `isinstance(..., discord.abc.Messageable)` check, so even
    the "can this channel receive messages" branch is the live one.

    Thirty seconds of level-less audio is what a microphone muted at
    system level produces: packets arrive, decode, and contain nothing.
    """
    clock = FakeClock(T0)
    sessions, jobs = FakeSessions(), FakeJobs()
    store = _configured_store()
    client = _client(
        clock, config_store=store, sessions=sessions, jobs=jobs, recording_dir=tmp_path
    )
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    _in_guild(client, guild)
    await client._tick_all(clock.now())

    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    await _start_session(client, guild, anna)

    # What the gateway's channel cache would hand back. Assigned onto the
    # instance rather than reaching into `_connection`, because the only
    # thing under test here is what the announcer does with the id it is
    # given.
    channel = _voice_channel(CHANNEL_ID, members=[anna])
    client.get_channel = MagicMock(return_value=channel)  # type: ignore[method-assign]

    service = client._guilds[GUILD_ID].service
    silence = b"\x00" * (48_000 * 4)  # one second of 48 kHz stereo 16-bit zeroes
    for second in range(30):
        await service.voice_packet(
            ANNA, "anna", 1, 48_000 * (second + 1), silence, T0 + timedelta(seconds=second)
        )

    channel.send.assert_awaited_once_with(  # type: ignore[attr-defined]
        "Audio is arriving from <@100> but at no audible level. The microphone is "
        "most likely muted at system level. Recording continues."
    )
    assert [(user_id, at) for _, user_id, at in sessions.silent_audio] == [
        (ANNA, T0 + timedelta(seconds=29))
    ]
