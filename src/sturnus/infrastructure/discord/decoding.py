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
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Literal, Protocol, overload

from discord.opus import Decoder, OpusError

from sturnus.domain.stream_health import (
    DecodePolicy,
    StreamHealth,
    StreamState,
    StreamStats,
)

log = logging.getLogger(__name__)

#: A hard ceiling on live decoders. One native decoder per SSRC, and an
#: SSRC changes whenever a participant reconnects; the library's own
#: `on_voice_member_disconnect` is the eviction hook we mirror, but this
#: process stays up for hours and an alpha library missing one disconnect
#: event must not turn into an unbounded leak. 64 is far above any real
#: voice channel's concurrent speaker count.
DEFAULT_MAX_STREAMS = 64


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
#: policy can be exercised without libopus, and so one SSRC can rebuild
#: its own decoder without knowing what kind it is.
DecoderFactory = Callable[[], FrameDecoder]

#: Fired once per *transition*, never per failing frame -- see
#: `StreamHealth.record_discarded`.
StreamStateListener = Callable[[int, StreamState, StreamStats], None]


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
    """One SSRC: one native decoder, one `StreamHealth`, and no exceptions out.

    Deliberately does not know about other speakers. A stream falling
    apart says nothing about the one next to it, and the isolation is what
    turns "the recording died" into "400 ms of one person's audio is
    missing".
    """

    def __init__(self, ssrc: int, factory: DecoderFactory, policy: DecodePolicy) -> None:
        self._ssrc = ssrc
        self._factory = factory
        self._health = StreamHealth(policy)
        self._decoder = factory()

    @property
    def state(self) -> StreamState:
        return self._health.state

    @property
    def may_recycle(self) -> bool:
        return self._health.may_recycle

    @property
    def stats(self) -> StreamStats:
        return self._health.stats()

    def decode(self, frame: bytes) -> tuple[bytes | None, StreamState | None]:
        """Decodes one real frame.

        Returns `(pcm, transition)`: `pcm` is 48 kHz 16-bit stereo bytes,
        or `None` meaning "write nothing"; `transition` is the new stream
        state if this frame changed the verdict, otherwise `None`.

        Only `OpusError` is caught. That is the whole family libopus
        reports through `discord.opus._err_lt` -- `-1 BAD_ARG`,
        `-2 BUFFER_TOO_SMALL`, `-3 INTERNAL_ERROR`, `-4 INVALID_PACKET`
        (the production one), `-5 UNIMPLEMENTED`, `-6 INVALID_STATE`,
        `-7 ALLOC_FAIL` -- and it is exactly the set that means "this
        frame is unreadable", nothing more. `OpusNotLoaded` is *not* in
        it and is never caught here; see `new_opus_decoder`.

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
            return None, self._health.record_discarded(error.code)
        self._health.record_decoded()
        return pcm, None

    def conceal(self) -> bytes | None:
        """Fills in one frame the network lost, while policy still allows it.

        The library manufactures a `FakePacket` for every gap in the
        sequence, so a `wants_opus` sink is told about loss precisely; it
        just cannot reach `_buffer.peek_next()`, so FEC (reconstruction
        from the LBRR copy in the *next* packet) is not available to us
        and plain PLC is. Past a handful of consecutive frames PLC is
        noise rather than information, so `DecodePolicy` caps it and
        silence takes over.
        """
        if not self._health.record_lost():
            return None
        try:
            pcm = self._decoder.decode(None, fec=False)
        except OpusError as error:
            # Concealment is best-effort by definition; a failure to
            # invent a frame is not evidence about the input stream, so
            # it is not counted as a discard.
            log.debug("Packet-loss concealment failed for ssrc=%s: %s", self._ssrc, error)
            return None
        self._health.record_concealed()
        return pcm

    def recycle(self) -> bool:
        """Replaces a wedged decoder with a fresh one, once per policy budget.

        A fresh state is the right and cheap answer to a decoder that
        cannot be talked round, and it is local: no socket, no gateway, no
        other speaker. It is explicitly *not* a reconnect, which the brief
        rejected -- it swaps one libopus struct.
        """
        try:
            fresh = self._factory()
        except Exception:
            # Spending the budget even on a failed rebuild is deliberate:
            # otherwise `may_recycle` stays true and we would retry a
            # rebuild that cannot work on every subsequent frame.
            log.exception("Could not rebuild the Opus decoder for ssrc=%s", self._ssrc)
            self._health.record_recycled()
            return False
        self._decoder = fresh
        self._health.record_recycled()
        return True


class ResilientOpusDecoder:
    """Per-SSRC Opus decoding that never raises and never stops the capture.

    The unit the sink talks to. `decode` returns 48 kHz 16-bit stereo PCM
    -- byte-for-byte what the library's own decoder produced, so
    everything downstream of `RecordingService.voice_packet` is unchanged
    -- or `None` for "write nothing". Counters and verdicts leave through
    `on_state_change`, `on_total_failure` and `stats()`, never through the
    hot return value, which keeps the call site in the sink a two-liner.
    """

    def __init__(
        self,
        *,
        factory: DecoderFactory = new_opus_decoder,
        policy: DecodePolicy | None = None,
        max_streams: int = DEFAULT_MAX_STREAMS,
        on_state_change: StreamStateListener | None = None,
        on_total_failure: Callable[[], None] | None = None,
    ) -> None:
        if max_streams <= 0:
            raise ValueError("max_streams must be positive")
        self._factory = factory
        self._policy = policy or DecodePolicy()
        self._max_streams = max_streams
        self._on_state_change = on_state_change
        self._on_total_failure = on_total_failure
        # Ordered by least-recently-used, so the LRU backstop can evict in
        # O(1) without a separate bookkeeping structure.
        self._streams: OrderedDict[int, SpeakerDecoder] = OrderedDict()
        # `write()` runs on the packet-router thread and sink listeners run
        # on the sink-event-router thread, which the library already
        # mutually excludes -- but `cleanup()` arrives from a third thread
        # entirely, and relying on an undocumented lock ordering inside an
        # alpha library is exactly the coupling this design exists to
        # avoid. The lock is uncontended and costs nothing at 50 fps.
        self._lock = threading.Lock()
        self._total_failure_reported = False

    def decode(self, ssrc: int, frame: bytes | None) -> bytes | None:
        """Turns one raw frame into PCM, or into `None` meaning "write nothing".

        `frame` is `None` or `b""` for a frame the network lost (the
        library's `FakePacket`, whose `decrypted_data` is `b""`), real
        bytes otherwise.

        Never raises. The inner layer catches `OpusError`, which is
        expected and counted; this outer layer catches everything else,
        which is not, and logs it loudly under its own message. It exists
        so that no future bug of ours can kill the packet-router thread
        the way the library's own uncaught `OpusError` did.
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

        `discord/ext/voice_recv/gateway.py` destroys its `PacketDecoder`
        on `CLIENT_DISCONNECT`; the sink forwards the matching public
        `voice_member_disconnect` event here so the two stay in step.
        """
        with self._lock:
            self._streams.pop(ssrc, None)

    def clear(self) -> None:
        """Forgets every stream. Idempotent; safe from any thread."""
        with self._lock:
            self._streams.clear()

    def stats(self) -> Mapping[int, StreamStats]:
        """A snapshot per live SSRC.

        The payload behind a log line, a metric, and -- the point of the
        exercise -- a caveat on the published protocol telling a
        participant that some of their audio could not be decoded.
        """
        with self._lock:
            return {ssrc: stream.stats for ssrc, stream in self._streams.items()}

    def _decode(self, ssrc: int, frame: bytes | None) -> bytes | None:
        stream = self._stream(ssrc)
        if not frame:
            return stream.conceal()
        pcm, transition = stream.decode(frame)
        if transition is not None:
            self._escalate(ssrc, stream, transition)
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
            stream = SpeakerDecoder(ssrc, self._factory, self._policy)
            self._streams[ssrc] = stream
            return stream

    def _escalate(self, ssrc: int, stream: SpeakerDecoder, state: StreamState) -> None:
        """Acts on a state transition. Per-speaker degradation never ends a session."""
        if self._on_state_change is not None:
            try:
                self._on_state_change(ssrc, state, stream.stats)
            except Exception:
                log.exception("Stream state listener failed for ssrc=%s", ssrc)
        if stream.may_recycle:
            stream.recycle()
        self._check_total_failure()

    def _check_total_failure(self) -> None:
        """Fires `on_total_failure` once when nothing decodes anywhere.

        The one case that is *not* a per-speaker problem: if every stream
        that has seen enough frames to be judged is `UNUSABLE` or
        `NEVER_DECODED`, the bot is writing empty files while telling
        everyone in the channel they are recorded -- the original bug in a
        new costume. Recycling is already spent by the time a stream can
        stay in one of those states, so this cannot fire while a fresh
        decoder still has a chance.
        """
        if self._total_failure_reported or self._on_total_failure is None:
            return
        with self._lock:
            streams = list(self._streams.values())
        judged = [
            stream
            for stream in streams
            if stream.stats.frames_seen >= self._policy.never_decoded_after
        ]
        if not judged:
            return
        dead = (StreamState.UNUSABLE, StreamState.NEVER_DECODED)
        if any(stream.state not in dead for stream in judged):
            return
        self._total_failure_reported = True
        try:
            self._on_total_failure()
        except Exception:
            log.exception("Total-decode-failure listener failed")


def log_state_change(ssrc: int, state: StreamState, stats: StreamStats) -> None:
    """The default `StreamStateListener`: one structured line per transition.

    `DEGRADED` is a warning about one speaker and nothing else.
    `NEVER_DECODED` gets its own message because it means something
    different from degradation -- the input shape is wrong (we handed the
    decoder the wrong bytes, or Discord changed the payload), not that one
    stream went bad partway through. Distinguishing the two costs one
    counter and saves an hour of the wrong debugging.
    """
    if state is StreamState.DEGRADED:
        log.warning(
            "Opus stream degraded: ssrc=%s consecutive_failures=%d last_error_code=%s "
            "seen=%d decoded=%d discarded=%d. Recording continues for every speaker.",
            ssrc,
            stats.consecutive_failures,
            stats.last_error_code,
            stats.frames_seen,
            stats.frames_decoded,
            stats.frames_discarded,
        )
    elif state is StreamState.UNUSABLE:
        log.error(
            "Opus stream unusable: ssrc=%s consecutive_failures=%d last_error_code=%s "
            "recycles=%d. About %.1fs of this speaker's audio is missing.",
            ssrc,
            stats.consecutive_failures,
            stats.last_error_code,
            stats.decoder_recycles,
            stats.lost_seconds,
        )
    elif state is StreamState.NEVER_DECODED:
        log.error(
            "Opus stream never decoded a single frame: ssrc=%s attempts=%d "
            "last_error_code=%s. This points at the payload we are feeding the decoder, "
            "not at one stream degrading.",
            ssrc,
            stats.frames_discarded,
            stats.last_error_code,
        )
