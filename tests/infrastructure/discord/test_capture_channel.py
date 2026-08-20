"""The crossing from the extension's threads to the event loop.

The incident this branch exists for was a failure that nobody heard
about. `CaptureChannel` is where that lesson is structural: the messages
that report capture failing do not travel on the same terms as the audio
they are reporting on, so the load that produces an alarm cannot also be
what discards it.

No voice connection anywhere -- `submit` is what the extension's threads
call, `receive` is what the drain task calls, and both are ordinary
methods.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sturnus.infrastructure.discord.capture_channel import CaptureChannel
from sturnus.infrastructure.discord.sink import (
    CapturedFrame,
    CaptureStopped,
    DecodeTotalFailure,
)
from sturnus.infrastructure.metrics import QUEUE_DROPPED, Counters

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA_ID, ANNA_SSRC = 100, 111


def frame(rtp_timestamp: int = 960) -> CapturedFrame:
    return CapturedFrame(
        discord_user_id=ANNA_ID,
        display_name="anna",
        ssrc=ANNA_SSRC,
        rtp_timestamp=rtp_timestamp,
        pcm=b"pcm",
        captured_at=T0,
    )


def channel(*, frame_limit: int = 3, counters: Counters | None = None) -> CaptureChannel:
    return CaptureChannel(
        asyncio.get_running_loop(), frame_limit=frame_limit, counters=counters or Counters()
    )


async def test_the_alarm_is_not_dropped_along_with_the_audio() -> None:
    """The defect, stated as an outcome.

    `CaptureStopped`, `DecodeTotalFailure` and `StreamStateChanged` exist
    *because* capture failed, and they used to share the audio queue's
    bound -- so the load that produced them was exactly what discarded
    them. Here the audio lane is saturated many times over before the
    alarm is raised, and the alarm still arrives.
    """
    counters = Counters()
    capture = channel(frame_limit=3, counters=counters)

    for _ in range(300):
        capture.submit(frame())
    capture.submit(CaptureStopped(RuntimeError("router died")))
    capture.submit(DecodeTotalFailure())
    await asyncio.sleep(0)

    assert counters.get(QUEUE_DROPPED) == 297, "the audio lane is well past its bound"
    delivered = [await capture.receive(), await capture.receive()]
    assert [type(message) for message in delivered] == [CaptureStopped, DecodeTotalFailure]


async def test_control_is_delivered_before_audio_that_is_already_queued() -> None:
    """Not sharing fate means not queueing behind it either.

    An alarm that waits behind everything already in flight is delivered
    late by however long that backlog takes to process -- and the backlog
    is exactly what is wrong.
    """
    capture = channel(frame_limit=100)

    for index in range(50):
        capture.submit(frame(rtp_timestamp=index))
    capture.submit(CaptureStopped(None))
    await asyncio.sleep(0)

    assert isinstance(await capture.receive(), CaptureStopped)
    assert isinstance(await capture.receive(), CapturedFrame)


async def test_frames_are_dropped_while_the_loop_is_genuinely_stalled() -> None:
    """The only situation the bound exists for is the one it must work in.

    A bound read off the destination queue cannot fire here: nothing has
    run on the loop to move a single message into it, so its size is
    zero while the callbacks pile up without limit. The count that
    matters is frames submitted and not yet drained.
    """
    counters = Counters()
    capture = channel(frame_limit=10, counters=counters)

    # No `await` anywhere in this block: the loop is stalled, exactly as
    # it would be by a slow handler.
    for _ in range(25):
        capture.submit(frame())

    assert counters.get(QUEUE_DROPPED) == 15
    assert capture.pending_frames == 10


async def test_the_audio_lane_recovers_once_the_drain_catches_up() -> None:
    """The bound is backpressure, not a fuse."""
    counters = Counters()
    capture = channel(frame_limit=2, counters=counters)

    for _ in range(4):
        capture.submit(frame())
    await asyncio.sleep(0)
    assert counters.get(QUEUE_DROPPED) == 2

    await capture.receive()
    await capture.receive()
    capture.submit(frame())
    await asyncio.sleep(0)

    assert counters.get(QUEUE_DROPPED) == 2
    assert isinstance(await capture.receive(), CapturedFrame)


async def test_receive_waits_rather_than_spinning_on_an_empty_channel() -> None:
    capture = channel()

    pending = asyncio.ensure_future(capture.receive())
    await asyncio.sleep(0)
    assert not pending.done()

    capture.submit(DecodeTotalFailure())
    assert isinstance(await asyncio.wait_for(pending, timeout=1), DecodeTotalFailure)


async def test_submitting_into_a_closed_loop_never_raises() -> None:
    """`submit` is reached from `RecordingSink.write`, which must not raise.

    An exception there propagates into `PacketRouter.run`, which stops
    capture for every speaker -- the incident itself.
    """
    closed = asyncio.new_event_loop()
    closed.close()
    capture = CaptureChannel(closed, frame_limit=2, counters=Counters())

    capture.submit(frame())
    capture.submit(CaptureStopped(None))

    assert capture.pending_frames == 0, "a frame that could not be handed over is not in flight"


async def test_a_frame_limit_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="frame_limit"):
        CaptureChannel(asyncio.get_running_loop(), frame_limit=0)
