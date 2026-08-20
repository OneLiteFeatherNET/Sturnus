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

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.ports import AudioWriter, Clock, SessionKey
from sturnus.application.recording import RecordingService
from sturnus.domain.session import SessionTimeouts
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.repositories import (
    AccountLinkRepository,
    ConsentRepository,
    JobRepository,
    SessionRepository,
)
from sturnus.infrastructure.discord.client import SturnusClient, _GuildRecording
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.health import ReadinessState

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD_ID, CHANNEL_ID, ROLE_ID = 1, 2, 3
ANNA, BEN = 100, 200
RTP = 48_000


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
        self._participants: dict[int, set[int]] = {}
        self._next = 1

    async def open_session(self, _guild_id: int, _channel_id: int, _now: datetime) -> int:
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


def _client(clock: Clock) -> SturnusClient:
    """A `SturnusClient` with every dependency `on_voice_state_update`/`_tick_all`
    do not touch stubbed out -- this test drives the guild's pipeline directly
    rather than through `on_ready`/`_configure_guild`, which are already
    exercised by `ConfigStore`-backed tests elsewhere.
    """
    return SturnusClient(
        clock=clock,
        config_store=MagicMock(spec=ConfigStore),
        consent_repo=MagicMock(spec=ConsentRepository),
        session_repo=MagicMock(spec=SessionRepository),
        job_repo=MagicMock(spec=JobRepository),
        audio_store=FakeStore(),
        writer_factory=FakeAudioWriterFactory(Path("/tmp")),
        encryptor=FakeEncryptor(),
        readiness=ReadinessState(),
        database_ping=_never_pinged,
        session_factory=MagicMock(spec=async_sessionmaker[AsyncSession]),
        outline_oauth=MagicMock(spec=OutlineOAuth),
        link_states=MagicMock(spec=LinkStateStore),
        account_links=MagicMock(spec=AccountLinkRepository),
    )


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
        retention_days=30,
    )
    voice = FakeVoiceReceiver()
    client = _client(clock)
    client._guilds[GUILD_ID] = _GuildRecording(
        channel_id=CHANNEL_ID, role_id=ROLE_ID, service=service, voice=voice
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
    service = RecordingService(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        timeouts=SessionTimeouts(
            empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
        ),
        sessions=sessions,
        jobs=FakeJobs(),
        store=FakeStore(),
        writers=FakeAudioWriterFactory(tmp_path),
        encryptor=FakeEncryptor(),
        retention_days=30,
    )
    voice = FakeVoiceReceiver(join_fails=True)
    client = _client(clock)
    client._guilds[GUILD_ID] = _GuildRecording(
        channel_id=CHANNEL_ID, role_id=ROLE_ID, service=service, voice=voice
    )

    empty_channel = _voice_channel(CHANNEL_ID, members=[])
    guild = _guild(GUILD_ID, empty_channel)
    anna = _member(ANNA, guild, role_ids=[ROLE_ID])
    guild.get_channel.return_value = _voice_channel(CHANNEL_ID, members=[anna])

    with caplog.at_level(logging.ERROR, logger="sturnus.infrastructure.discord.client"):
        await client.on_voice_state_update(anna, _voice_state(None), _voice_state(empty_channel))

    assert len(caplog.records) == 1, "a capture that never started is never silent"
    assert sessions.opened == [1], "the session row was already open by then"

    clock.advance(timedelta(seconds=1))
    await client._tick_all(clock.now())

    assert sessions.closed == [(1, "capture_failure")], "not idle_timeout, fifteen minutes later"
    assert voice.left == 1
    assert service.is_recording is False
