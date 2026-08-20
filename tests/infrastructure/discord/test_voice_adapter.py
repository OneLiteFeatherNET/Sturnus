"""The two boundaries the adapter owns, without a gateway connection.

The thread hop and the escalation path, driven directly: `_emit` is what
the extension's threads call, `_handle` is what the drain task runs, and
between them sits the bounded queue. Neither needs a voice connection to
be wrong, so neither needs one to be tested.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import voice_recv
from discord.opus import OpusNotLoaded

from sturnus.application.recording import RecordingService
from sturnus.domain.session import EndReason
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


def adapter(
    *,
    service: RecordingService | None = None,
    counters: Counters | None = None,
    clock: FakeClock | None = None,
    queue_maxsize: int = 3,
) -> VoiceReceiveAdapter:
    return VoiceReceiveAdapter(
        MagicMock(spec=discord.Client),
        service or MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore),
        clock or FakeClock(),
        MagicMock(spec=ConsentRepository),
        counters=counters or Counters(),
        queue_maxsize=queue_maxsize,
    )


def frame() -> CapturedFrame:
    return CapturedFrame(
        discord_user_id=ANNA_ID,
        display_name="anna",
        ssrc=ANNA_SSRC,
        rtp_timestamp=960,
        pcm=b"pcm",
        captured_at=T0,
    )


# --- the thread hop ---


async def test_emit_before_join_is_a_no_op_rather_than_a_crash() -> None:
    """`_emit` is reached from `write()`; raising there kills the router thread."""
    voice = adapter()

    voice._emit(frame())  # the boundary under test


async def test_a_stalled_loop_drops_frames_instead_of_accumulating_them() -> None:
    """The bound is on the producer side, on purpose.

    `call_soon_threadsafe` would otherwise keep every over-limit frame
    alive in the loop's callback queue until the loop got round to
    rejecting it, which is exactly the unbounded accumulation the bound
    exists to prevent.
    """
    counters = Counters()
    voice = adapter(counters=counters, queue_maxsize=3)
    voice._loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
    voice._queue = queue

    for _ in range(5):
        voice._emit(frame())
        await asyncio.sleep(0)  # let each call_soon_threadsafe land

    assert queue.qsize() == 3
    assert counters.get(QUEUE_DROPPED) == 2


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
    voice._guild_id = GUILD_ID
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
    voice._guild_id = GUILD_ID
    voice._consent_cache = MagicMock(may_record=AsyncMock(return_value=True))
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

    service.request_close.assert_called_once_with(EndReason.DECODE_FAILURE)
    assert counters.get(DECODE_TOTAL_FAILURES) == 1
    channel.send.assert_awaited_once()


async def test_capture_stopping_on_its_own_is_reported_rather_than_swallowed() -> None:
    """The failure that was invisible in production, made loud."""
    counters = Counters()
    voice = adapter(counters=counters)
    channel = MagicMock(send=AsyncMock())
    voice._channel = channel

    await voice._handle(CaptureStopped(RuntimeError("router died")))

    assert counters.get(CAPTURE_STOPPED) == 1
    channel.send.assert_awaited_once()


async def test_a_stop_we_asked_for_is_not_reported_as_a_failure() -> None:
    """`leave()` calls `stop_listening()`, which fires the same `after=` hook."""
    voice = adapter()
    voice._loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
    voice._queue = queue
    voice._stopping = True

    voice._on_listen_stopped(None)
    await asyncio.sleep(0)

    assert queue.empty()


async def test_notices_are_debounced_so_a_stuck_stream_cannot_spam_the_channel() -> None:
    clock = FakeClock()
    voice = adapter(clock=clock)
    channel = MagicMock(send=AsyncMock())
    voice._channel = channel

    for _ in range(4):
        await voice._handle(UnattributedAudio(ANNA_SSRC, 1))

    assert channel.send.await_count == 1, "same second, same subject: told once"

    clock.advance(timedelta(minutes=2))
    await voice._handle(UnattributedAudio(ANNA_SSRC, 500))

    assert channel.send.await_count == 2


async def test_a_channel_that_refuses_messages_never_breaks_the_drain() -> None:
    voice = adapter()
    voice._channel = MagicMock(
        id=CHANNEL_ID, send=AsyncMock(side_effect=discord.Forbidden(MagicMock(), "nope"))
    )

    await voice._handle(UnattributedAudio(ANNA_SSRC, 1))


async def test_the_drain_survives_a_handler_that_raises() -> None:
    """One bad message must not stop every later frame from being recorded."""
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._guild_id = GUILD_ID
    voice._consent_cache = MagicMock(
        may_record=AsyncMock(side_effect=[RuntimeError("database gone"), True])
    )
    queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
    queue.put_nowait(frame())
    queue.put_nowait(frame())

    task = asyncio.create_task(voice._drain(queue))
    await asyncio.sleep(0.01)
    task.cancel()

    service.voice_packet.assert_awaited_once()


# --- the startup probe ---


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
