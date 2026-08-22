"""Measuring what actually arrives from Discord, one recording at a time.

Written to settle one question that offline analysis of stored recordings
could not: **is the noise already in the Opus frame, or does Sturnus put it
there?**

Four production tracks measured the same way -- autocorrelation 0.21-0.26
where clean speech sits at 0.4-0.8, a mean sample-to-sample step of 3438
against an RMS of 8000, and 19-24% of the energy above 4 kHz. That is
high-frequency noise carrying some speech structure. Per-frame resampling,
integer overflow, frame stitching and the encryption mode were each ruled
out by measurement, which leaves the decoder and its input -- and neither
can be observed in a finished WAV, because both live upstream of it.

The awkward part is that libopus *does not complain* about a frame it
cannot make sense of. `opus_decode` returns noise for a plausible-looking
packet as readily as it returns speech, so "frames decoded, nothing
discarded" is exactly what a broken input stream looks like from
`ResilientOpusDecoder`. The counters that already exist cannot tell the two
apart. These can:

- **libopus's own reading of each packet.** `packet_get_nb_frames`,
  `packet_get_samples_per_frame` and `packet_get_nb_channels` parse the TOC
  byte without decoding anything. Discord sends one 20 ms stereo frame per
  packet, so anything other than 1 frame / 960 samples / 2 channels means
  the bytes handed to the decoder are not the packet Discord sent -- a
  header not stripped, a payload truncated, an offset off by a few.
- **The size distribution of those bytes.** Discord's encoder produces
  roughly 20-200 bytes for 20 ms of voice. A stream whose sizes are all
  equal, or implausibly large, is not voice.
- **The PCM the decoder returns, measured before anything else touches
  it.** This is the line that decides the question. Noise here means the
  input is wrong; clean speech here means Sturnus degrades it afterwards.

**Nothing recorded here is audio.** Sizes, counts, and three aggregate
numbers over a window of samples -- no frame is kept, no PCM is stored, and
nothing that could be reassembled into speech leaves this object. That is
what makes it safe to run against a real conversation, which is the only
kind that reproduces the defect.

**Off unless asked for.** `STURNUS_CAPTURE_DIAGNOSTICS=true`. The per-frame
work on the hot path is a dictionary bump and three integer comparisons;
the arithmetic that costs anything runs on one frame in `SAMPLE_EVERY`, and
`ResilientOpusDecoder` is handed `None` when the setting is off, so the
whole thing compiles out to an `if self._diagnostics is not None`.
"""

from __future__ import annotations

import logging
import math
import threading
from array import array
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: What Discord sends, and therefore what a healthy packet reads as.
EXPECTED_FRAMES_PER_PACKET = 1
EXPECTED_SAMPLES_PER_FRAME = 960
EXPECTED_CHANNELS = 2
_EXPECTED_SHAPE = (
    EXPECTED_FRAMES_PER_PACKET,
    EXPECTED_SAMPLES_PER_FRAME,
    EXPECTED_CHANNELS,
)

#: One frame in this many is measured for signal shape. 50 is one a second
#: per speaker, which is plenty to characterise a stream and keeps the
#: autocorrelation off the packet-router thread's critical path.
SAMPLE_EVERY = 50

#: How many sampled frames to keep statistics over before reporting. 300 is
#: five minutes of one speaker; a report that never arrives because the
#: session ran long is a report nobody reads.
REPORT_EVERY = 300

#: Samples that count as silence, matching `sturnus.domain.silence`.
_SILENCE_FLOOR = 32

#: Leading-byte offsets tried when a stream's packet shapes come out wrong.
#:
#: A packet whose shapes are scattered across `1f/480spf/1ch`,
#: `2f/960spf/2ch` and so on is not a damaged Opus stream -- it is bytes
#: that are not an Opus packet being read as one. Almost every byte value
#: is a formally valid TOC, which is why libopus reports no error and the
#: decoder returns noise instead of failing.
#:
#: So the useful question is not "is this packet valid" but "where does the
#: real packet start". Reading the TOC at each small offset and counting
#: which one yields the expected shape answers it directly, and the answer
#: is the number of bytes that need stripping.
OFFSET_SCAN = range(0, 17)

#: How many leading bytes of a malformed packet to record, and from how
#: many packets. Four bytes cannot carry recognisable audio; eight packets
#: is enough to see whether the same prefix repeats.
LEADING_BYTES = 4
LEADING_SAMPLES = 8


@dataclass
class StreamDiagnostics:
    """What one SSRC's stream looks like, in numbers that are not audio."""

    ssrc: int
    frames: int = 0
    #: `(nb_frames, samples_per_frame, channels)` as libopus reads the TOC,
    #: counted. A healthy stream has one entry.
    packet_shapes: Counter[tuple[int, int, int]] = field(default_factory=Counter)
    #: Packet sizes in bytes, bucketed, because the exact size of one packet
    #: says nothing and the shape of the distribution says a lot.
    size_buckets: Counter[str] = field(default_factory=Counter)
    #: Packets libopus refused to parse at all.
    unreadable_packets: int = 0
    #: For each leading-byte offset tried, how many packets read as the
    #: shape Discord actually sends when the packet is taken to start
    #: there. The offset that dominates is the number of bytes standing in
    #: front of the real Opus packet.
    healthy_at_offset: Counter[int] = field(default_factory=Counter)
    #: The leading bytes of a handful of packets, as hex. Structure, not
    #: content: four bytes is a TOC byte and the first of the compressed
    #: payload, which is far too little to reconstruct anything audible and
    #: exactly enough to see whether an RTP extension is still sitting in
    #: front of the Opus packet.
    leading_bytes: list[str] = field(default_factory=list)

    # -- signal shape, over sampled frames only --
    sampled: int = 0
    peak: int = 0
    sum_squares: float = 0.0
    samples: int = 0
    step_sum: float = 0.0
    step_count: int = 0
    crossings: int = 0
    autocorr_sum: float = 0.0
    autocorr_count: int = 0
    silent_frames: int = 0


def size_bucket(size: int) -> str:
    """A packet size as the band it falls in.

    Bands rather than values: what matters is whether the stream looks like
    variable-bitrate voice at all, and thirty distinct sizes reported
    individually would be noise in the log while saying less.
    """
    if size == 0:
        return "0"
    for upper in (10, 20, 40, 80, 160, 320, 640):
        if size < upper:
            return f"<{upper}"
    return ">=640"


class CaptureDiagnostics:
    """Collects per-SSRC evidence about a live capture, and reports it.

    Held by `ResilientOpusDecoder`, which calls `observe_packet` with the
    bytes it is about to decode and `observe_pcm` with what came back. Both
    run on the packet-router thread, so both are cheap by construction.
    """

    def __init__(
        self,
        *,
        sample_every: int = SAMPLE_EVERY,
        report_every: int = REPORT_EVERY,
        packet_reader: PacketReader | None = None,
    ) -> None:
        self._sample_every = max(1, sample_every)
        self._report_every = max(1, report_every)
        self._reader = packet_reader if packet_reader is not None else _LibopusPacketReader()
        self._streams: dict[int, StreamDiagnostics] = {}
        # `report()` can be called from the session-ending thread while the
        # router thread is still writing. Uncontended at 50 fps.
        self._lock = threading.Lock()

    def _stream(self, ssrc: int) -> StreamDiagnostics:
        stream = self._streams.get(ssrc)
        if stream is None:
            stream = StreamDiagnostics(ssrc)
            self._streams[ssrc] = stream
        return stream

    def observe_packet(self, ssrc: int, frame: bytes) -> None:
        """Records what libopus makes of one packet, without decoding it."""
        with self._lock:
            stream = self._stream(ssrc)
            stream.frames += 1
            stream.size_buckets[size_bucket(len(frame))] += 1
            shape = self._reader.shape(frame)
            if shape is None:
                stream.unreadable_packets += 1
            else:
                stream.packet_shapes[shape] += 1
            if shape != _EXPECTED_SHAPE and len(stream.leading_bytes) < LEADING_SAMPLES:
                stream.leading_bytes.append(frame[:LEADING_BYTES].hex())
            if shape != _EXPECTED_SHAPE:
                # Only when something is already wrong: on a healthy stream
                # this is 17 extra parses per packet for an answer nobody
                # needs.
                for offset in OFFSET_SCAN:
                    if self._reader.shape(frame[offset:]) == _EXPECTED_SHAPE:
                        stream.healthy_at_offset[offset] += 1
            due = stream.frames % self._report_every == 0
        if due:
            self.report(ssrc)

    def observe_pcm(self, ssrc: int, pcm: bytes) -> None:
        """Measures the decoder's own output, before anything else sees it.

        This is the measurement the whole module exists for: whatever these
        numbers say about the audio is a statement about Opus and its
        input, with none of Sturnus' later handling in it.
        """
        with self._lock:
            stream = self._stream(ssrc)
            if stream.frames % self._sample_every != 0:
                return
            stream.sampled += 1
        _measure(stream, pcm)

    def report(self, ssrc: int | None = None) -> None:
        """Logs what has been collected. Safe to call at any time."""
        with self._lock:
            streams = (
                [self._streams[ssrc]]
                if ssrc is not None and ssrc in self._streams
                else list(self._streams.values())
            )
        for stream in streams:
            _log_stream(stream)

    def drop(self, ssrc: int) -> None:
        """Reports a departing speaker's stream once, then forgets it."""
        with self._lock:
            stream = self._streams.pop(ssrc, None)
        if stream is not None and stream.frames:
            _log_stream(stream, final=True)

    def clear(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            if stream.frames:
                _log_stream(stream, final=True)


class PacketReader:
    """The one thing this module asks of libopus. Injected so tests need none."""

    def shape(self, frame: bytes) -> tuple[int, int, int] | None:
        raise NotImplementedError


class _LibopusPacketReader(PacketReader):
    """Reads a packet's TOC byte through `discord.opus.Decoder`.

    Parsing only -- `opus_packet_get_*` never decodes and never touches
    decoder state, so calling it beside the real decode costs three C calls
    and changes nothing about what is recorded.
    """

    def shape(self, frame: bytes) -> tuple[int, int, int] | None:
        from discord.opus import Decoder, OpusError

        try:
            return (
                Decoder.packet_get_nb_frames(frame),
                Decoder.packet_get_samples_per_frame(frame),
                Decoder.packet_get_nb_channels(frame),
            )
        except (OpusError, Exception):
            # Anything at all: this runs on the capture thread and its
            # failure must never reach it. An unparseable packet is itself
            # the finding, and it is counted as one.
            return None


def _measure(stream: StreamDiagnostics, pcm: bytes) -> None:
    """Accumulates signal shape from one frame of 48 kHz stereo PCM.

    Left channel only, and every fourth sample of it. The question is
    whether this is voice or noise, and both answers survive decimation --
    while the full frame through `array` on every sampled packet would put
    real work on the router thread for no extra certainty.
    """
    if len(pcm) < 16:
        return
    values = array("h")
    values.frombytes(pcm[: len(pcm) // 2 * 2])
    left = values[0::8]  # every 4th stereo pair, left channel
    if len(left) < 8:
        return

    peak: int = 0
    total = 0.0
    steps = 0.0
    crossings = 0
    previous = left[0]
    for value in left:
        magnitude = int(abs(value))
        if magnitude > peak:
            peak = magnitude
        total += float(value) * float(value)
        steps += abs(float(value) - float(previous))
        if (value >= 0) != (previous >= 0):
            crossings += 1
        previous = value

    stream.peak = max(stream.peak, peak)
    stream.sum_squares += total
    stream.samples += len(left)
    stream.step_sum += steps
    stream.step_count += len(left) - 1
    stream.crossings += crossings
    if peak < _SILENCE_FLOOR:
        stream.silent_frames += 1
        return

    # Autocorrelation at the lag of a plausible fundamental. Voiced speech
    # peaks well above 0.3 here; noise does not, whatever its spectrum.
    mean = sum(float(v) for v in left) / len(left)
    centred = [float(v) - mean for v in left]
    energy = sum(v * v for v in centred)
    if energy <= 0:
        return
    best: float = 0.0
    # 12 kHz after decimation: lags 40..150 cover roughly 80-300 Hz.
    for lag in range(40, min(150, len(centred) - 1)):
        acc = 0.0
        for i in range(len(centred) - lag):
            acc += centred[i] * centred[i + lag]
        correlation = float(acc / energy)
        best = max(best, correlation)
    stream.autocorr_sum += best
    stream.autocorr_count += 1


def _log_stream(stream: StreamDiagnostics, *, final: bool = False) -> None:
    """One line per stream, carrying the numbers that decide the question."""
    shapes = ", ".join(
        f"{frames}f/{spf}spf/{ch}ch x{count}"
        for (frames, spf, ch), count in stream.packet_shapes.most_common(4)
    )
    unexpected = sum(c for shape, c in stream.packet_shapes.items() if shape != _EXPECTED_SHAPE)
    offsets = (
        ", ".join(f"+{offset}:{count}" for offset, count in stream.healthy_at_offset.most_common(4))
        or "none"
    )
    sizes = ", ".join(f"{band}:{count}" for band, count in sorted(stream.size_buckets.items()))

    rms = math.sqrt(stream.sum_squares / stream.samples) if stream.samples else 0.0
    mean_step = stream.step_sum / stream.step_count if stream.step_count else 0.0
    zcr = stream.crossings / stream.samples if stream.samples else 0.0
    autocorr = stream.autocorr_sum / stream.autocorr_count if stream.autocorr_count else 0.0

    log.warning(
        "capture diagnostics%s ssrc=%s: %d packets | shapes: %s | unexpected shape: %d | "
        "unreadable: %d | reads correctly at offset: %s | first bytes: %s | sizes: %s || "
        "decoder output over %d sampled frames "
        "(%d silent): peak=%d rms=%.0f mean_step=%.0f step/rms=%.2f zcr=%.3f autocorr=%.3f "
        "|| clean speech reads autocorr>0.4, step/rms<0.3; the four degraded production "
        "tracks read autocorr 0.21-0.26, step/rms about 0.44",
        " (final)" if final else "",
        stream.ssrc,
        stream.frames,
        shapes or "none",
        unexpected,
        stream.unreadable_packets,
        offsets,
        " ".join(stream.leading_bytes) or "none",
        sizes or "none",
        stream.sampled,
        stream.silent_frames,
        stream.peak,
        rms,
        mean_step,
        mean_step / rms if rms > 0 else 0.0,
        zcr,
        autocorr,
    )
