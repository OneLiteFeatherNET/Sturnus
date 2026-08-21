"""The boundary the adapter owns, without a gateway connection.

The thread hop and the failure path, driven directly: `_emit` is what the
extension's threads call and `_drain` is the task on the other side.
Neither needs a voice connection to be wrong, so neither needs one to be
tested.

The tests that matter here are about one disease rather than several
defects. Capture stopped in production and nothing noticed. A capture
failure that ends as an ordinary timeout, and a join that fails leaving a
session open with nothing behind it, are both new ways of arriving at that
same afternoon -- so these assert on outcomes, on what the session row
ends up saying, rather than on a code path having been entered. "A branch
was taken" is exactly the kind of evidence that was available last time
and told nobody anything.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.opus import OpusNotLoaded

from sturnus.application.ports import SessionKey
from sturnus.application.recording import RecordingService
from sturnus.domain.session import EndReason, SessionTimeouts
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.decoding import FrameDecoder
from sturnus.infrastructure.discord.sink import (
    CapturedFrame,
    CaptureStopped,
    DecodeFailure,
    SpeakerStreamEnded,
)
from sturnus.infrastructure.discord.voice import VoiceReceiveAdapter

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD_ID, CHANNEL_ID, ROLE_ID = 1, 2, 3
ANNA_ID, ANNA_SSRC = 100, 111

VOICE_LOGGER = "sturnus.infrastructure.discord.voice"


class FakeClock:
    def __init__(self) -> None:
        self.value = T0

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class FakeSessions:
    """Just enough of `SessionRecorder` to see what a session row ends up saying."""

    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def open_session(
        self, _guild_id: int, _channel_id: int, _channel_name: str | None, _now: datetime
    ) -> int:
        return 1

    async def record_session_key(self, _sid: int, _key_id: str, _wrapped: bytes) -> None:
        return None

    async def session_key(self, _session_id: int) -> tuple[str, bytes] | None:
        return None

    async def add_participant(
        self, _sid: int, _user: int, _display_name: str, _now: datetime
    ) -> None:
        return None

    async def set_audio_epoch(self, _sid: int, _user: int, _at: datetime) -> None:
        return None

    async def record_silent_audio(self, _sid: int, _user: int, _at: datetime) -> None:
        return None

    async def close_session(self, session_id: int, _ended_at: datetime, reason: str) -> None:
        self.closed.append((session_id, reason))

    async def session_status(self, _session_id: int) -> str | None:
        return None


class FakeEncryptor:
    key_id = "k1"

    def new_session_key(self) -> SessionKey:
        return SessionKey(plaintext=b"0" * 32, wrapped=b"wrapped")

    def encrypt(self, _source: object, _target: object, _key: bytes) -> None:
        return None


def recording_service(sessions: FakeSessions) -> RecordingService:
    """A real `RecordingService` on fakes, so the end reason is the real one."""
    return RecordingService(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        timeouts=SessionTimeouts(
            empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
        ),
        sessions=sessions,
        jobs=AsyncMock(),
        store=AsyncMock(),
        writers=MagicMock(),
        encryptor=FakeEncryptor(),
        announcer=AsyncMock(),
        retention_days=30,
    )


def adapter(
    *,
    service: RecordingService | None = None,
    clock: FakeClock | None = None,
) -> VoiceReceiveAdapter:
    voice = VoiceReceiveAdapter(
        MagicMock(spec=discord.Client),
        service or MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore),
        clock or FakeClock(),
        MagicMock(spec=ConsentRepository),
    )
    voice._guild_id = GUILD_ID
    return voice


def connected(voice: VoiceReceiveAdapter) -> None:
    """Puts the adapter in the state `join()` leaves it in, minus the gateway."""
    voice._loop = asyncio.get_running_loop()
    voice._queue = asyncio.Queue()
    voice._drain_task = asyncio.create_task(voice._drain(voice._queue))


async def settle() -> None:
    """Lets the drain run to a standstill."""
    for _ in range(20):
        await asyncio.sleep(0)


def frame() -> CapturedFrame:
    return CapturedFrame(
        discord_user_id=ANNA_ID,
        display_name="anna",
        ssrc=ANNA_SSRC,
        rtp_timestamp=960,
        pcm=b"pcm",
        captured_at=T0,
    )


def consenting() -> MagicMock:
    return MagicMock(may_record=AsyncMock(return_value=True))


# --- capture dying must not look like a quiet meeting ---


async def test_capture_death_ends_the_session_as_a_capture_failure() -> None:
    """The incident's exact signature, read from the session row.

    Nothing armed a close when capture died: the session stayed open with
    nothing arriving and eventually closed as `idle_timeout`, indistinguish-
    able in the database from a meeting where nobody happened to speak.
    Whoever reads that row next has to be able to tell "nobody spoke" from
    "we could not hear".
    """
    sessions = FakeSessions()
    service = recording_service(sessions)
    await service.participants_changed(1, T0)
    voice = adapter(service=service)

    await voice._handle(CaptureStopped(RuntimeError("router died")))
    reason = await service.tick(T0 + timedelta(seconds=1))

    assert reason is EndReason.CAPTURE_FAILURE
    assert sessions.closed == [(1, "capture_failure")]


async def test_capture_stopping_on_its_own_is_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure that was invisible in production, made loud."""
    voice = adapter()

    with caplog.at_level(logging.ERROR, logger=VOICE_LOGGER):
        await voice._handle(CaptureStopped(RuntimeError("router died")))

    assert len(caplog.records) == 1
    assert "RuntimeError" in caplog.records[0].getMessage()
    assert caplog.records[0].exc_info is not None, "the cause is carried, not summarised away"


async def test_a_stop_we_asked_for_is_not_reported_as_a_failure() -> None:
    """`leave()` calls `stop_listening()`, which fires the same `after=` hook."""
    voice = adapter()
    connected(voice)
    voice._stopping = True

    voice._on_listen_stopped(None)
    await asyncio.sleep(0)

    assert voice._queue is not None
    assert voice._queue.empty()


async def test_decode_failure_ends_the_session_with_its_own_reason() -> None:
    """The only decode failure that ends a session, and why.

    If nothing decodes on any stream the bot is writing empty files while
    telling everyone in the channel they are recorded -- the original
    incident in a new costume. Closing is not a reconnect and retries
    nothing; the reason lands on the session row.
    """
    sessions = FakeSessions()
    service = recording_service(sessions)
    await service.participants_changed(1, T0)
    voice = adapter(service=service)

    await voice._handle(DecodeFailure())
    reason = await service.tick(T0 + timedelta(seconds=1))

    assert reason is EndReason.DECODE_FAILURE
    assert sessions.closed == [(1, "decode_failure")]


# --- the consent gate's second layer, on the loop ---


async def test_a_frame_the_consent_record_rejects_never_reaches_the_service() -> None:
    """Spec 3.1's second layer, still on the loop and still per frame.

    The role check in the sink is not the whole gate: a hand-granted role,
    or one granted under a privacy policy that has since changed, leaves
    the role in place with no active consent record behind it.
    """
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = MagicMock(may_record=AsyncMock(return_value=False))

    await voice._handle(frame())

    service.voice_packet.assert_not_awaited()


async def test_an_allowed_frame_is_recorded_at_its_arrival_time() -> None:
    """`captured_at` comes from the router thread, not from after the hop.

    For a speaker's first frame that value is their audio epoch (Spec
    6.3), so keeping the queue latency out of it is a free accuracy win.
    """
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    clock = FakeClock()
    voice = adapter(service=service, clock=clock)
    voice._consent_cache = consenting()
    clock.advance(timedelta(seconds=5))  # the loop is behind

    await voice._handle(frame())

    service.voice_packet.assert_awaited_once_with(ANNA_ID, "anna", ANNA_SSRC, 960, b"pcm", T0)


async def test_a_departing_speaker_retires_their_rtp_reference() -> None:
    service = MagicMock(spec=RecordingService)
    voice = adapter(service=service)

    await voice._handle(SpeakerStreamEnded(ANNA_SSRC))

    service.speaker_stream_ended.assert_called_once_with(ANNA_SSRC)


# --- the hand-off from the extension's threads to the loop ---


async def test_a_frame_emitted_from_outside_the_loop_reaches_the_service() -> None:
    """`_emit` is what `RecordingSink.write` calls; `_drain` is the other side."""
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = consenting()
    connected(voice)

    await asyncio.to_thread(voice._emit, frame())
    await settle()

    service.voice_packet.assert_awaited_once()


async def test_the_drain_survives_a_handler_that_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One bad message must not stop every later frame from being recorded."""
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = MagicMock(
        may_record=AsyncMock(side_effect=[RuntimeError("database gone"), True])
    )
    connected(voice)

    with caplog.at_level(logging.ERROR, logger=VOICE_LOGGER):
        voice._emit(frame())
        voice._emit(frame())
        await settle()

    service.voice_packet.assert_awaited_once()
    assert len(caplog.records) == 1, "the swallowed failure is still reported"


async def test_emit_before_join_is_a_no_op_rather_than_a_crash() -> None:
    """`_emit` is reached from `write()`; raising there kills the router thread."""
    voice = adapter()

    voice._emit(frame())  # the boundary under test


# --- join and leave ---


async def test_join_refuses_to_enter_the_channel_when_libopus_is_missing() -> None:
    """`OpusNotLoaded` must stay a startup failure.

    Caught per frame instead, every frame would fail, the session would
    run to completion, and the result would be hours of silent WAVs --
    the exact failure this work exists to eliminate, made worse. It has to
    leave `join` as an exception, because that is what
    `SturnusClient._start_capture` turns into `CAPTURE_FAILURE`.
    """
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild = MagicMock(id=GUILD_ID)
    channel.connect = AsyncMock()
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=channel)

    def broken_factory() -> FrameDecoder:
        raise OpusNotLoaded

    voice = VoiceReceiveAdapter(
        client,
        MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value=str(ROLE_ID))),
        FakeClock(),
        MagicMock(spec=ConsentRepository),
        decoder_factory=broken_factory,
    )

    with pytest.raises(OpusNotLoaded):
        await voice.join(CHANNEL_ID)

    channel.connect.assert_not_awaited(), "the bot never sat in a channel recording nothing"


async def test_a_failed_connect_leaves_no_task_running_behind_it() -> None:
    """Nothing that needs tearing down is built before the connection exists.

    The hand-off and its drain task used to be created first, so a
    `connect` that raised left the task running against a channel nobody
    was ever going to speak into -- for the rest of the process's life.
    """
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild = MagicMock(id=GUILD_ID)
    channel.connect = AsyncMock(side_effect=discord.ClientException("no voice"))
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=channel)

    voice = VoiceReceiveAdapter(
        client,
        MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value=str(ROLE_ID))),
        FakeClock(),
        MagicMock(spec=ConsentRepository),
        decoder_factory=lambda: MagicMock(),
    )
    before = asyncio.all_tasks()

    with pytest.raises(discord.ClientException):
        await voice.join(CHANNEL_ID)
    await asyncio.sleep(0)

    assert voice._drain_task is None
    assert voice._queue is None
    assert asyncio.all_tasks() == before


async def test_leave_stops_the_drain() -> None:
    voice = adapter()
    connected(voice)
    drain_task = voice._drain_task

    await voice.leave()

    assert drain_task is not None and drain_task.cancelled()
    assert voice._queue is None
