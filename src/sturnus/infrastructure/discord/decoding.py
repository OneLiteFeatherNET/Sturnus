"""Sturnus owns Opus decoding, so the library never reaches its crash site.

In production one Opus frame that failed to decode ended an entire
recording::

    ERROR:discord.ext.voice_recv.router:Error in <PacketRouter(...)> loop
    discord.opus.OpusError: corrupted stream

`PacketRouter.run()` catches that, logs it, sets `reader.error`, calls
`stop_listening()` in its `finally`, and the thread exits. Capture stops
for *every* speaker at once, the session stays open, and it ends with no
audio and no transcription job. Everyone in that channel believed they
were being recorded.

The seam that makes the fix structural rather than defensive is in
`discord/ext/voice_recv/opus.py`::

    self._decoder = None if self.sink.wants_opus() else Decoder()
    ...
    if not self.sink.wants_opus():
        packet, pcm = self._decode_packet(packet)   # <- the crash site

A sink whose `wants_opus()` returns `True` means the library constructs no
`Decoder` and never calls `_decode_packet`. **The crash site is not
reached on any code path.** The sink instead receives `VoiceData` whose
`.pcm` is empty and whose `.opus` property hands over
`packet.decrypted_data` -- the raw frame, with RTP extension headers
already stripped by the library's decryptor. Decoding it is then ours, and
so is deciding what to do when it fails.

This module is the only place in Sturnus that imports `discord.opus`. It
mirrors `PacketRouter.decoders: Dict[int, PacketDecoder]`: one native
decoder per SSRC, because Opus is a stateful codec and feeding two
speakers through one decoder corrupts both streams.

Three facts below are load-bearing and were measured against the installed
`discord.py` / `discord-ext-voice-recv 0.5.2a`, not recalled:

1. `OpusError(-4)` stringifies to exactly ``"corrupted stream"``. The
   production traceback is `opus_decode` returning `OPUS_INVALID_PACKET`
   through `discord.opus._err_lt`.
2. **A `Decoder` survives an `OpusError` and keeps decoding.** Feeding
   ``[good, garbage, good, garbage, good, good]`` through one instance
   yields ``ok, -4, ok, -4, ok, ok`` with no reset in between. Discard and
   continue is measured behaviour, not a hope.
3. `decode(b"")` raises `OpusError(-1)` "invalid argument", so an empty
   payload must take the loss path and must never reach `decode()`.

Returning `None` from `decode()` means "write nothing", and that is safe
only because of a property the recording path already has:
`SpeakerWriter.write` places audio by RTP-derived absolute time, not by
byte count, so a discarded frame becomes exactly its own duration of real
silence in the WAV and nothing after it shifts. Discarding is lossless
with respect to the timeline and lossy only with respect to 20 ms of one
speaker's audio.

**The accounting is deliberately one counter and one threshold.** Per
stream: how many frames in a row would not decode. A stream crossing the
threshold logs once, at ERROR. If *every* live stream is over it at that
moment, `on_decode_failure` fires once and the session ends with a reason
saying so, because a bot writing empty files while telling a channel it is
recording them is the original incident wearing a different hat. A stream
too young to have crossed the threshold is not evidence of failure -- it
is evidence something might still work -- so it blocks that verdict rather
than being excluded from it. Declaring total failure by mistake ends a
real recording; declining to declare it costs empty files the per-stream
ERROR is already shouting about.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal, Protocol, overload

from discord.opus import Decoder, OpusError

log = logging.getLogger(__name__)

#: A hard ceiling on live decoders. One native decoder per SSRC, and an
#: SSRC changes whenever a participant reconnects; the library's own
#: `on_voice_member_disconnect` is the eviction hook we mirror, but this
#: process stays up for hours and an alpha library missing one disconnect
#: event must not turn into an unbounded leak. 64 is far above any real
#: voice channel's concurrent speaker count.
DEFAULT_MAX_STREAMS = 64

#: Discord sends one Opus frame every 20 ms per speaking participant.
FRAMES_PER_SECOND = 50

#: Consecutive unreadable frames before a stream counts as failing.
#: Discord sends 50 frames a second per speaker, so 250 is five seconds:
#: long enough that isolated corruption on a lossy connection never
#: reaches it, short enough that a dead channel is not recorded for
#: minutes before anyone is told.
FAILING_AFTER_CONSECUTIVE_FAILURES = 250

#: How many lost frames in a row libopus may invent before real silence
#: takes over. Packet-loss concealment is informative for the first frame
#: or two and low-level noise after that.
MAX_CONSECUTIVE_CONCEALED = 5


class FrameDecoder(Protocol):
    """The slice of `discord.opus.Decoder` this module actually uses.

    Spelled with the same overloads as the real class, so
    `discord.opus.Decoder` satisfies it structurally with no adapter and a
    test fake satisfies it in four lines. `decode(None, fec=False)` is
    packet-loss concealment: libopus synthesises one frame from what it
    remembers of the previous one.
    """

    @overload
    def decode(self, data: bytes, *, fec: bool) -> bytes: ...

    @overload
    def decode(self, data: None, *, fec: Literal[False]) -> bytes: ...


#: How a fresh native decoder is obtained. Injected so the whole failure
#: policy can be exercised without libopus.
DecoderFactory = Callable[[], FrameDecoder]


def new_opus_decoder() -> FrameDecoder:
    """Builds one real libopus decoder.

    Also the startup probe. `Decoder.__init__` calls `get_opus_version()`,
    which raises `discord.opus.OpusNotLoaded` when libopus is missing, and
    `VoiceReceiveAdapter.join` calls this once *before* connecting so that
    failure stays a startup failure. If it were only ever discovered
    per-frame, every frame would fail, the session would run to
    completion, and the result would be hours of silent WAVs -- the exact
    failure mode this module exists to eliminate, made worse.
    """
    # `Decoder.__init__` carries no annotations in the installed release.
    return Decoder()  # type: ignore[no-untyped-call]


class SpeakerDecoder:
    """One SSRC: one native decoder, its counters, and no exceptions out.

    Deliberately does not know about other speakers. A stream falling
    apart says nothing about the one next to it, and the isolation is what
    turns "the recording died" into "400 ms of one person's audio is
    missing".
    """

    def __init__(self, ssrc: int, decoder: FrameDecoder) -> None:
        self._ssrc = ssrc
        self._decoder = decoder
        self.frames_decoded = 0
        self.frames_discarded = 0
        self.consecutive_failures = 0
        self.last_error_code: int | None = None
        self._consecutive_lost = 0
        self._unexpected_errors = 0

    @property
    def failing(self) -> bool:
        """Whether this stream has gone long enough without a readable frame."""
        return self.consecutive_failures >= FAILING_AFTER_CONSECUTIVE_FAILURES

    @property
    def lost_seconds(self) -> float:
        """Roughly how much of this speaker's audio never made it into their file."""
        return self.frames_discarded / FRAMES_PER_SECOND

    def decode(self, frame: bytes) -> bytes | None:
        """Decodes one real frame; `None` means "write nothing".

        `OpusError` is the family libopus reports through
        `discord.opus._err_lt` -- `-1 BAD_ARG`, `-2 BUFFER_TOO_SMALL`,
        `-3 INTERNAL_ERROR`, `-4 INVALID_PACKET` (the production one),
        `-5 UNIMPLEMENTED`, `-6 INVALID_STATE`, `-7 ALLOC_FAIL` -- and it
        carries a code worth naming in the log. Anything else the decoder
        raises is accounted for identically, just without a code: a frame
        thrown away is a frame thrown away, whatever the reason, and a
        stream whose every frame is thrown away must not go on looking
        healthy with nothing counted against it. That is the incident's
        own shape -- dead, and reporting fine -- so a non-`OpusError` takes
        exactly the same path.

        `OpusNotLoaded` cannot reach this method: `VoiceReceiveAdapter.join`
        probes for it before connecting (see `new_opus_decoder`).

        The decoder instance is kept, not reset: measured, it decodes the
        very next good frame correctly.
        """
        try:
            pcm = self._decoder.decode(frame, fec=False)
        except OpusError as error:
            # No concealment here, on purpose. `decode(None, fec=False)`
            # would invent audio to paper over a decoder error, and this
            # file is a record people were told they are in. We conceal
            # what the *network* lost, never what our decoder could not
            # read: the writer's gap padding fills this frame with real
            # silence instead.
            self._discard(error.code, error)
            return None
        except Exception as error:
            self._discard(None, error)
            return None
        self.frames_decoded += 1
        self.consecutive_failures = 0
        # Re-arms concealment: the next lost frame is the first of a new
        # run, from a decoder that has just seen real audio.
        self._consecutive_lost = 0
        return pcm

    def conceal(self) -> bytes | None:
        """Fills in one frame the network lost, while the cap still allows it.

        The library manufactures a `FakePacket` for every gap in the
        sequence, so a `wants_opus` sink is told about loss precisely; it
        just cannot reach `_buffer.peek_next()`, so FEC (reconstruction
        from the LBRR copy in the *next* packet) is not available to us and
        plain PLC is. Concealment also needs the decoder to have decoded at
        least once: libopus reconstructs from its memory of the previous
        frame, and a decoder with no memory has nothing to reconstruct from.
        """
        self._consecutive_lost += 1
        if self.frames_decoded == 0 or self._consecutive_lost > MAX_CONSECUTIVE_CONCEALED:
            return None
        try:
            return self._decoder.decode(None, fec=False)
        except Exception as error:
            # Concealment is best-effort by definition; failing to invent a
            # frame says nothing about the input stream, so it is not
            # counted as a discard. Not narrowed to `OpusError` either --
            # nothing may escape towards the packet-router thread.
            log.debug("Packet-loss concealment failed for ssrc=%s: %r", self._ssrc, error)
            return None

    def _discard(self, code: int | None, error: BaseException) -> None:
        """Books one unreadable frame."""
        self.frames_discarded += 1
        self.consecutive_failures += 1
        self.last_error_code = code
        if code is None:
            # A ctypes failure, an alpha library that changed shape under
            # us, a bug of ours. Logged once per stream with its traceback
            # -- at 50 frames a second an unrate-limited one is its own
            # outage -- and after that the failure threshold is what
            # reports it.
            self._unexpected_errors += 1
            if self._unexpected_errors == 1:
                log.error(
                    "Opus decoding for ssrc=%s raised something other than OpusError; "
                    "the frame is discarded and counted like any other unreadable frame",
                    self._ssrc,
                    exc_info=error,
                )


class ResilientOpusDecoder:
    """Per-SSRC Opus decoding that never raises and never stops the capture.

    The unit the sink talks to. `decode` returns 48 kHz 16-bit stereo PCM
    -- byte-for-byte what the library's own decoder produced, so everything
    downstream of `RecordingService.voice_packet` is unchanged -- or `None`
    for "write nothing". The one verdict that leaves this object is
    `on_decode_failure`, fired at most once, when no live stream is
    decoding anything any more.
    """

    def __init__(
        self,
        *,
        factory: DecoderFactory = new_opus_decoder,
        max_streams: int = DEFAULT_MAX_STREAMS,
        on_decode_failure: Callable[[], None] | None = None,
    ) -> None:
        if max_streams <= 0:
            raise ValueError("max_streams must be positive")
        self._factory = factory
        self._max_streams = max_streams
        self._on_decode_failure = on_decode_failure
        # Ordered by least-recently-used, so the backstop can evict in O(1)
        # without a separate bookkeeping structure.
        self._streams: OrderedDict[int, SpeakerDecoder] = OrderedDict()
        # `write()` runs on the packet-router thread and sink listeners run
        # on the sink-event-router thread, which the library already
        # mutually excludes -- but `cleanup()` arrives from a third thread
        # entirely, and relying on an undocumented lock ordering inside an
        # alpha library is exactly the coupling this design exists to
        # avoid. The lock is uncontended and costs nothing at 50 fps.
        self._lock = threading.Lock()
        self._decode_failure_reported = False

    def decode(self, ssrc: int, frame: bytes | None) -> bytes | None:
        """Turns one raw frame into PCM, or into `None` meaning "write nothing".

        `frame` is `None` or `b""` for a frame the network lost (the
        library's `FakePacket`, whose `decrypted_data` is `b""`), real
        bytes otherwise.

        Never raises. The inner layer catches whatever the decoder throws;
        this outer layer catches everything else, so that no future bug of
        ours can kill the packet-router thread the way the library's own
        uncaught `OpusError` did.
        """
        try:
            return self._decode(ssrc, frame)
        except Exception:
            log.exception(
                "Unexpected error decoding ssrc=%s; frame discarded, capture continues", ssrc
            )
            return None

    def drop(self, ssrc: int) -> None:
        """Forgets one stream, mirroring the library's own decoder eviction.

        `discord/ext/voice_recv/gateway.py` destroys its `PacketDecoder` on
        `CLIENT_DISCONNECT`; the sink forwards the matching public
        `voice_member_disconnect` event here so the two stay in step.
        """
        with self._lock:
            self._streams.pop(ssrc, None)

    def clear(self) -> None:
        """Forgets every stream. Idempotent; safe from any thread."""
        with self._lock:
            self._streams.clear()

    def _decode(self, ssrc: int, frame: bytes | None) -> bytes | None:
        stream = self._stream(ssrc)
        if not frame:
            return stream.conceal()
        pcm = stream.decode(frame)
        # Edge-triggered on purpose: `==` fires the report exactly once per
        # run of failures. A stream failing at 50 frames a second would
        # otherwise produce three thousand ERROR lines a minute, which is
        # its own outage.
        if pcm is None and stream.consecutive_failures == FAILING_AFTER_CONSECUTIVE_FAILURES:
            self._report_failing(ssrc, stream)
        return pcm

    def _stream(self, ssrc: int) -> SpeakerDecoder:
        with self._lock:
            stream = self._streams.get(ssrc)
            if stream is not None:
                self._streams.move_to_end(ssrc)
                return stream
            if len(self._streams) >= self._max_streams:
                evicted, _ = self._streams.popitem(last=False)
                log.warning(
                    "Evicting the least recently used Opus decoder (ssrc=%s): %d live streams "
                    "reached the max_streams backstop. Expect a missed disconnect event.",
                    evicted,
                    self._max_streams,
                )
            stream = SpeakerDecoder(ssrc, self._factory())
            self._streams[ssrc] = stream
            return stream

    def _report_failing(self, ssrc: int, stream: SpeakerDecoder) -> None:
        """One ERROR per failure run, then the only verdict that ends a session."""
        log.error(
            "Opus stream ssrc=%s has not produced a readable frame in %d consecutive "
            "attempts (last libopus error code %s; %d decoded and %d discarded so far). "
            "About %.1fs of this speaker's audio is missing. Every other speaker in the "
            "channel is unaffected and still being recorded.",
            ssrc,
            stream.consecutive_failures,
            stream.last_error_code,
            stream.frames_decoded,
            stream.frames_discarded,
            stream.lost_seconds,
        )
        self._check_total_failure()

    def _check_total_failure(self) -> None:
        """Fires `on_decode_failure` once when nothing decodes anywhere."""
        if self._decode_failure_reported or self._on_decode_failure is None:
            return
        with self._lock:
            streams = list(self._streams.values())
        if not streams or not all(stream.failing for stream in streams):
            return
        self._decode_failure_reported = True
        log.error(
            "No voice stream in this channel is decoding any longer (%d live streams, all "
            "failing). Ending the session rather than recording silence; see the per-stream "
            "errors above for the cause.",
            len(streams),
        )
        try:
            self._on_decode_failure()
        except Exception:
            log.exception("Total-decode-failure listener failed")
