"""The two boundaries the adapter owns, without a gateway connection.

The thread hop and the escalation path, driven directly: `_emit` is what
the extension's threads call, `_drain` is the task on the other side, and
between them sits `CaptureChannel`. Neither needs a voice connection to be
wrong, so neither needs one to be tested.

Three of the tests here are about the same disease rather than three
separate defects. Capture stopped in production and nothing noticed; a
control message dropped under load, a dead stream reported as healthy, and
a capture failure that ends as an ordinary timeout are all new ways of
arriving at that same afternoon. They assert on outcomes -- what the
session row says, what reaches the channel, what reaches the service --
because "a code path was entered" is exactly the kind of evidence that was
available last time and told nobody anything.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import voice_recv
from discord.opus import OpusNotLoaded

from sturnus.application.ports import SessionKey
from sturnus.application.recording import RecordingService
from sturnus.domain.session import EndReason, SessionTimeouts
from sturnus.domain.stream_health import StreamHealth, StreamState
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.sink import (
    CapturedFrame,
    CaptureStopped,
    DecodeTotalFailure,
    SpeakerStreamEnded,
    StreamStateChanged,
    UnattributedAudio,
)
from sturnus.infrastructure.discord.voice import VoiceReceiveAdapter
from sturnus.infrastructure.metrics import (
    CAPTURE_STOPPED,
    DECODE_TOTAL_FAILURES,
    FRAMES_AWAITING_CONSENT,
    QUEUE_DROPPED,
    Counters,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD_ID, CHANNEL_ID, ROLE_ID = 1, 2, 3
ANNA_ID, ANNA_SSRC = 100, 111


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

    async def open_session(self, _guild_id: int, _channel_id: int, _now: datetime) -> int:
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


async def never_returns(*_args: object) -> None:
    """Stands in for a call that hangs: a rate-limited HTTP request, a stuck query."""
    await asyncio.Event().wait()


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
        retention_days=30,
    )


def adapter(
    *,
    service: RecordingService | None = None,
    counters: Counters | None = None,
    clock: FakeClock | None = None,
    queue_maxsize: int = 3,
) -> VoiceReceiveAdapter:
    voice = VoiceReceiveAdapter(
        MagicMock(spec=discord.Client),
        service or MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore),
        clock or FakeClock(),
        MagicMock(spec=ConsentRepository),
        counters=counters or Counters(),
        queue_maxsize=queue_maxsize,
    )
    voice._guild_id = GUILD_ID
    return voice


def connected(voice: VoiceReceiveAdapter) -> None:
    """Puts the adapter in the state `join()` leaves it in, minus the gateway."""
    from sturnus.infrastructure.discord.capture_channel import CaptureChannel

    voice._capture = CaptureChannel(
        asyncio.get_running_loop(), frame_limit=voice._queue_maxsize, counters=voice._counters
    )
    voice._drain_task = asyncio.create_task(voice._drain(voice._capture))


async def settle() -> None:
    """Lets the drain and any task it spawned run to a standstill."""
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
    return MagicMock(verdict=MagicMock(return_value=True))


# --- the alarm must not share fate with the audio ---


async def test_the_capture_alarm_arrives_even_while_frames_are_being_dropped() -> None:
    """The defect, as an outcome rather than a code path.

    `CaptureStopped` used to travel through the audio queue and share its
    bound, so a loop far enough behind to be dropping frames dropped the
    message reporting that capture had died as well -- the alarm
    discarded by exactly the condition that raised it. Here the frame
    lane overflows several times over first, and the channel is still
    told and the session still ends.
    """
    counters = Counters()
    sessions = FakeSessions()
    service = recording_service(sessions)
    await service.participants_changed(1, T0)
    voice = adapter(service=service, counters=counters, queue_maxsize=3)
    voice._consent_cache = consenting()
    channel = MagicMock(send=AsyncMock())
    voice._channel = channel
    connected(voice)

    for _ in range(100):
        voice._emit(frame())
    voice._emit(CaptureStopped(RuntimeError("router died")))
    await settle()

    assert counters.get(QUEUE_DROPPED) > 0, "the frame lane really did overflow"
    assert counters.get(CAPTURE_STOPPED) == 1
    channel.send.assert_awaited_once()
    assert await service.tick(T0 + timedelta(seconds=1)) is EndReason.CAPTURE_FAILURE


async def test_a_total_decode_failure_is_reported_from_a_saturated_channel() -> None:
    """Same shape, other message: the one alarm that ends a session."""
    counters = Counters()
    service = MagicMock(spec=RecordingService)
    voice = adapter(service=service, counters=counters, queue_maxsize=2)
    voice._consent_cache = consenting()
    voice._channel = MagicMock(send=AsyncMock())
    connected(voice)

    for _ in range(50):
        voice._emit(frame())
    voice._emit(DecodeTotalFailure())
    await settle()

    assert counters.get(DECODE_TOTAL_FAILURES) == 1
    service.request_close.assert_called_once_with(EndReason.DECODE_FAILURE)


# --- capture death must not look like a meeting that ended ---


async def test_capture_death_that_cannot_be_resumed_ends_as_a_capture_failure() -> None:
    """The incident's exact signature, read from the session row.

    Nothing armed a close when capture died unrecoverably: the session
    stayed open with nothing arriving and eventually closed as
    `idle_timeout`, indistinguishable in the database from a meeting
    where nobody happened to speak. Whoever reads that row next has to be
    able to tell "nobody spoke" from "we could not hear".
    """
    sessions = FakeSessions()
    service = recording_service(sessions)
    await service.participants_changed(1, T0)
    voice = adapter(service=service)
    voice._relisten_used = True  # the one re-listen is already spent
    voice._channel = MagicMock(send=AsyncMock())

    await voice._handle(CaptureStopped(RuntimeError("router died")))
    reason = await service.tick(T0 + timedelta(seconds=1))

    assert reason is EndReason.CAPTURE_FAILURE
    assert sessions.closed == [(1, "capture_failure")]


async def test_a_capture_stop_we_resumed_from_does_not_end_the_session() -> None:
    """The backstop is a backstop: resuming means the recording continues."""
    service = MagicMock(spec=RecordingService)
    voice = adapter(service=service)
    voice._channel = MagicMock(send=AsyncMock())
    voice._voice_client = MagicMock(is_connected=MagicMock(return_value=True))
    voice._consent_role_id = ROLE_ID

    await voice._handle(CaptureStopped(None))

    service.request_close.assert_not_called()
    assert voice._relisten_used is True


async def test_capture_stopping_on_its_own_is_reported_rather_than_swallowed() -> None:
    """The failure that was invisible in production, made loud."""
    counters = Counters()
    voice = adapter(counters=counters)
    channel = MagicMock(send=AsyncMock())
    voice._channel = channel

    await voice._handle(CaptureStopped(RuntimeError("router died")))
    await settle()

    assert counters.get(CAPTURE_STOPPED) == 1
    channel.send.assert_awaited_once()


async def test_a_stop_we_asked_for_is_not_reported_as_a_failure() -> None:
    """`leave()` calls `stop_listening()`, which fires the same `after=` hook."""
    voice = adapter()
    connected(voice)
    voice._stopping = True

    voice._on_listen_stopped(None)
    await asyncio.sleep(0)

    assert voice._capture is not None
    assert voice._capture.pending_control == 0


# --- nothing on the drain waits on the network or the database ---


async def test_a_rate_limited_channel_message_never_stalls_audio() -> None:
    """The drain is single-consumer: everything behind it is somebody's audio.

    `channel.send` is rate-limited by Discord, in seconds. Awaiting it on
    the drain stalls every speaker in the channel behind a courtesy
    message.
    """
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = consenting()
    voice._channel = MagicMock(send=AsyncMock(side_effect=never_returns))
    connected(voice)

    voice._emit(UnattributedAudio(ANNA_SSRC, 1))
    voice._emit(frame())
    await settle()

    service.voice_packet.assert_awaited_once()


async def test_a_slow_consent_lookup_never_stalls_audio() -> None:
    """Same rule, other dependency: the cache refresh is a database read.

    A speaker whose record is not cached yet has their frame dropped and
    counted while the refresh runs beside the drain -- audio we cannot
    vouch for is not written, and the drain keeps moving.
    """
    counters = Counters()

    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    consent_repo = MagicMock(spec=ConsentRepository)
    consent_repo.current = AsyncMock(side_effect=never_returns)
    voice = VoiceReceiveAdapter(
        MagicMock(spec=discord.Client),
        service,
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value="2026-08-01")),
        FakeClock(),
        consent_repo,
        counters=counters,
    )
    voice._guild_id = GUILD_ID
    connected(voice)

    for _ in range(5):
        voice._emit(frame())
    voice._emit(SpeakerStreamEnded(ANNA_SSRC))
    await settle()

    service.speaker_stream_ended.assert_called_once_with(ANNA_SSRC), "the drain kept moving"
    assert counters.get(FRAMES_AWAITING_CONSENT) == 5
    service.voice_packet.assert_not_awaited()
    voice._consent_cache.cancel_refreshes()


# --- the escalation path ---


async def test_a_frame_the_consent_record_rejects_never_reaches_the_service() -> None:
    """Spec 3.1's second layer, still on the loop and still per frame.

    The role check in the sink is not the whole gate: a hand-granted role,
    or one granted under a privacy policy that has since changed, leaves
    the role in place with no active consent record behind it.
    """
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = MagicMock(verdict=MagicMock(return_value=False))

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


async def test_a_degraded_stream_is_reported_and_never_ends_the_session() -> None:
    """One speaker losing audio is not a reason to stop recording everyone else."""
    service = MagicMock(spec=RecordingService)
    voice = adapter(service=service)
    voice._channel = MagicMock(send=AsyncMock())

    await voice._handle(StreamStateChanged(ANNA_SSRC, StreamState.DEGRADED, StreamHealth().stats()))

    service.request_close.assert_not_called()


async def test_total_decode_failure_closes_the_session_and_says_so() -> None:
    """The only decode failure that ends a session, and why.

    If nothing decodes on any stream the bot is writing empty files while
    telling everyone in the channel they are recorded -- the original
    incident in a new costume. Closing is not a reconnect and retries
    nothing; the reason is recorded on the session row.
    """
    service = MagicMock(spec=RecordingService)
    counters = Counters()
    voice = adapter(service=service, counters=counters)
    channel = MagicMock(send=AsyncMock())
    voice._channel = channel

    await voice._handle(DecodeTotalFailure())
    await settle()

    service.request_close.assert_called_once_with(EndReason.DECODE_FAILURE)
    assert counters.get(DECODE_TOTAL_FAILURES) == 1
    channel.send.assert_awaited_once()


async def test_notices_are_debounced_so_a_stuck_stream_cannot_spam_the_channel() -> None:
    clock = FakeClock()
    voice = adapter(clock=clock)
    channel = MagicMock(send=AsyncMock())
    voice._channel = channel

    for _ in range(4):
        await voice._handle(UnattributedAudio(ANNA_SSRC, 1))
    await settle()

    assert channel.send.await_count == 1, "same second, same subject: told once"

    clock.advance(timedelta(minutes=2))
    await voice._handle(UnattributedAudio(ANNA_SSRC, 500))
    await settle()

    assert channel.send.await_count == 2


async def test_a_channel_that_refuses_messages_never_breaks_the_drain() -> None:
    voice = adapter()
    voice._channel = MagicMock(
        id=CHANNEL_ID, send=AsyncMock(side_effect=discord.Forbidden(MagicMock(), "nope"))
    )
    connected(voice)

    voice._emit(UnattributedAudio(ANNA_SSRC, 1))
    voice._emit(SpeakerStreamEnded(ANNA_SSRC))
    await settle()

    assert voice._drain_task is not None and not voice._drain_task.done()


async def test_the_drain_survives_a_handler_that_raises() -> None:
    """One bad message must not stop every later frame from being recorded."""
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = MagicMock(
        verdict=MagicMock(side_effect=[RuntimeError("database gone"), True])
    )
    connected(voice)

    voice._emit(frame())
    voice._emit(frame())
    await settle()

    service.voice_packet.assert_awaited_once()


async def test_emit_before_join_is_a_no_op_rather_than_a_crash() -> None:
    """`_emit` is reached from `write()`; raising there kills the router thread."""
    voice = adapter()

    voice._emit(frame())  # the boundary under test


# --- join and leave ---


async def test_join_refuses_to_enter_the_channel_when_libopus_is_missing() -> None:
    """`OpusNotLoaded` must stay a startup failure.

    Caught per frame instead, every frame would fail, the session would
    run to completion, and the result would be hours of silent WAVs --
    the exact failure this work exists to eliminate, made worse.
    """
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild = MagicMock(id=GUILD_ID)
    channel.connect = AsyncMock()
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=channel)

    def broken_factory() -> voice_recv.AudioSink:
        raise OpusNotLoaded

    voice = VoiceReceiveAdapter(
        client,
        MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value=str(ROLE_ID))),
        FakeClock(),
        MagicMock(spec=ConsentRepository),
        decoder_factory=broken_factory,  # type: ignore[arg-type]
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
    assert voice._capture is None
    assert asyncio.all_tasks() == before


async def test_leave_stops_the_drain_and_everything_beside_it() -> None:
    voice = adapter()
    voice._channel = MagicMock(id=CHANNEL_ID, send=AsyncMock(side_effect=never_returns))
    connected(voice)
    voice._notify("attribution-hint", "hello")
    drain_task = voice._drain_task
    side_tasks = list(voice._side_tasks)
    assert side_tasks, "the notice really was posted from a task of its own"

    await voice.leave()

    assert drain_task is not None and drain_task.cancelled()
    assert all(task.cancelled() for task in side_tasks)
    assert voice._capture is None
