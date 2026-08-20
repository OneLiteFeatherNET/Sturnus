"""The one crossing from the extension's threads to the event loop.

Two lanes, and the reason there are two is the incident this whole
branch exists for.

Audio frames are *payload*: there are fifty a second per speaker, they
are worth dropping when the loop falls behind, and dropping the newest
costs 20 ms of one person's audio that `SpeakerWriter` then pads with
real silence. Everything else -- `CaptureStopped`, `DecodeTotalFailure`,
`StreamStateChanged`, `SpeakerStreamEnded`, `UnattributedAudio` -- is a
*control message*: it exists precisely because capture is failing, and
the moment the system is struggling is the moment it is needed. Sharing
one bounded queue meant the alarm was discarded along with the audio it
was reporting on, under exactly the load that produced it. A control
message must not share fate with its payload, so it does not share the
payload's bound and it does not queue behind the payload either.

Control messages need no bound of their own because they are bounded at
the source: `StreamHealth` reports state changes edge-triggered rather
than per frame, `DecodeTotalFailure` fires once per session,
`CaptureStopped` once per stop, `SpeakerStreamEnded` once per departure,
and `RecordingSink` caps unattributed notices per SSRC and caps the
SSRCs it tracks. A flood of them is not a load condition, it is a bug in
one of those rate limits, and silently dropping it would hide that too.

The frame bound counts frames **submitted and not yet drained**, not
frames sitting in a queue. That distinction is the difference between a
bound that works and one that reads well: `call_soon_threadsafe` hands
the message to the loop's callback queue, and while the loop is actually
stalled -- the only situation the bound exists for -- nothing runs to
move it onward, so a bound read off the destination structure stays at
zero while callbacks pile up without limit. The counter is incremented
by the producer, on the producer's thread, before the hand-off.

Threading: `submit` is called from the extension's packet-router and
sink-event-router threads and must never raise, since an exception there
reaches `PacketRouter.run()`, which stops capture for every speaker.
Everything else runs on the event loop, so the two deques are touched
only there -- the counter is the single piece of shared state, and it has
a lock.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque

from sturnus.infrastructure import metrics
from sturnus.infrastructure.discord.sink import CapturedFrame, CaptureMessage
from sturnus.infrastructure.metrics import Counters

log = logging.getLogger(__name__)


class CaptureChannel:
    """A bounded audio lane and an unbounded control lane, drained together."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        frame_limit: int,
        counters: Counters | None = None,
    ) -> None:
        if frame_limit <= 0:
            raise ValueError("frame_limit must be positive")
        self._loop = loop
        self._frame_limit = frame_limit
        self._counters = counters or metrics.COUNTERS
        self._lock = threading.Lock()
        self._inflight_frames = 0
        self._frames: deque[CapturedFrame] = deque()
        self._control: deque[CaptureMessage] = deque()
        self._wakeup = asyncio.Event()

    @property
    def pending_frames(self) -> int:
        """Frames submitted and not yet received. Readable from any thread."""
        with self._lock:
            return self._inflight_frames

    @property
    def pending_control(self) -> int:
        """Control messages waiting on the loop side."""
        return len(self._control)

    def submit(self, message: CaptureMessage) -> None:
        """Hands one message to the event loop. Any thread; never raises.

        Audio is dropped once `frame_limit` frames are already in flight,
        and the drop is counted. A control message is always accepted.
        """
        if isinstance(message, CapturedFrame):
            with self._lock:
                if self._inflight_frames >= self._frame_limit:
                    self._counters.inc(metrics.QUEUE_DROPPED)
                    return
                self._inflight_frames += 1
        try:
            self._loop.call_soon_threadsafe(self._deliver, message)
        except RuntimeError:
            # The loop is closed. There is nothing left to deliver to,
            # and certainly nothing to raise about back into `write()`.
            if isinstance(message, CapturedFrame):
                with self._lock:
                    self._inflight_frames -= 1

    async def receive(self) -> CaptureMessage:
        """Returns the next message, control first. Event loop only.

        Control before audio is the second half of not sharing fate: an
        alarm raised while a thousand frames are queued must not wait
        behind them to be acted on.
        """
        while True:
            if self._control:
                return self._control.popleft()
            if self._frames:
                frame = self._frames.popleft()
                with self._lock:
                    self._inflight_frames -= 1
                return frame
            self._wakeup.clear()
            await self._wakeup.wait()

    def _deliver(self, message: CaptureMessage) -> None:
        """Lands one message on the loop side. Runs as a loop callback."""
        if isinstance(message, CapturedFrame):
            self._frames.append(message)
        else:
            self._control.append(message)
        self._wakeup.set()
