"""Does Discord actually send a bot the video it is told about?

Recording a shared screen depends entirely on that question, and nothing
in the library answers it. `discord-ext-voice-recv` handles the `VIDEO`
gateway op -- it learns who is sharing, at what resolution, on which SSRC
-- and then registers **only the audio SSRC**::

    elif op == VIDEO:
        vc._add_ssrc(uid, data['audio_ssrc'])          # gateway.py:93
        vc.dispatch("voice_member_video", member, streams)

So a video packet, if one arrives, reaches `AudioReader.callback` with an
SSRC nobody has mapped. It is logged as "unknown ssrc" and then routed on
anyway, which means it would land in `RecordingSink` and be discarded as
unattributed audio. Invisible either way.

The uncertainty this measures is not a detail. A Discord client
*subscribes* to the video streams it wants; the server does not push them
unasked. If nothing arrives, no amount of H.264 depacketisation, DAVE
`MediaType.video` decryption or ffmpeg plumbing will produce a recording
-- and all of that is weeks of work resting on an assumption nobody has
checked.

The first run of this probe found something blunter than "announced but
not delivered": **nothing was announced either.** What the connection was
missing is now known and is sent by `.video_subscription` -- `video: true`
in `IDENTIFY`, op 12, op 15 -- so the probe has to report what was asked
for as well as what came back. "We asked and got nothing" and "we never
asked" are the same log line otherwise, and they mean opposite things.

**So this checks it, and nothing else.** It pairs what the gateway
announces with what the socket delivers:

- every `voice_member_video` event: who, which SSRC, what kind of stream,
  at what resolution;
- every RTP packet whose SSRC matches one of those, counted by size.

Packets arriving means the rest is worth building. Announcements with no
packets means Discord is not sending them to us, and the next question is
subscription rather than decoding -- a completely different problem, found
in one recording instead of after the fact.

**It reports on a timer, not at the end.** The first version only spoke
from `cleanup()`, so the answer to "is this working" arrived after the
call was over and after the operator had stopped watching. A share that
starts twenty minutes in is worth knowing about at minute twenty-one.

**It records no video and cannot.** SSRCs, counts and sizes; not one byte
of any payload is read, let alone kept. It also changes nothing: packets
are counted where they are already being discarded, so a capture behaves
exactly as it did before.

Off unless `STURNUS_CAPTURE_DIAGNOSTICS` is on, like everything else in
this pair of modules.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: How many announced streams to describe per report. A screen share and a
#: camera from four people is already eight, and the shape repeats.
MAX_DESCRIBED_STREAMS = 6

#: Seconds between reports. Long enough that a two-hour meeting costs a
#: couple of hundred log lines, short enough that somebody who starts a
#: share to test this does not have to end the call to find out.
DEFAULT_REPORT_INTERVAL = 60.0

#: How long `clear()` waits for the timer thread. It only ever sits in
#: `Event.wait`, so it returns at once; the timeout is there because
#: `clear()` can be reached from the garbage collector.
TIMER_JOIN_TIMEOUT = 2.0


@dataclass
class AnnouncedStream:
    """One video stream the gateway told us about."""

    ssrc: int
    discord_user_id: int | None
    #: Discord's own label. `"screen"` for a shared screen, `"video"` for a
    #: camera -- which is the distinction the consent model turns on, so it
    #: is recorded verbatim rather than interpreted here.
    kind: str
    active: bool
    resolution: str
    #: Packets seen on this SSRC, bucketed by size. Empty means the
    #: gateway announced a stream that never arrived.
    packet_sizes: Counter[str] = field(default_factory=Counter)
    #: Whether this connection asked for the stream. `None` until the
    #: consent behind it has been resolved, `False` for a speaker whose
    #: consent does not name video. It is the difference between "we
    #: asked and nothing came" and "we did not ask, on purpose", which
    #: are the same zero in every other column here and mean opposite
    #: things about whether video reaches a bot at all.
    subscribed: bool | None = None

    @property
    def packets(self) -> int:
        return sum(self.packet_sizes.values())


def size_band(size: int) -> str:
    """A packet size as a band. Video packets run to the MTU; audio does not."""
    for upper in (100, 300, 700, 1200):
        if size < upper:
            return f"<{upper}"
    return ">=1200"


class VideoProbe:
    """Pairs announced video streams with packets that actually arrive."""

    def __init__(self, *, report_every: float = DEFAULT_REPORT_INTERVAL) -> None:
        self._streams: dict[int, AnnouncedStream] = {}
        # `announce` runs on the sink-event-router thread, `observe` on the
        # packet-router thread, `note_request` on the event loop and
        # `report` on the timer thread below -- four, so the lock is not
        # optional.
        self._lock = threading.Lock()
        #: Packets on an SSRC nobody announced. A non-zero count with zero
        #: announced packets would mean video arrives before the gateway
        #: says so, which changes where the mapping has to be built.
        self.unannounced_packets = 0
        #: What was actually sent to ask for video, by name. Empty is the
        #: single most misreadable state this probe can be in: it looks
        #: exactly like "Discord refused us" and means "we never asked".
        self._requests: dict[str, bool] = {}
        self._report_every = report_every
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None

    def start(self) -> None:
        """Begins reporting on a timer. Idempotent."""
        if self._timer is not None or self._report_every <= 0:
            return
        # Daemon, because this must never be the thread that keeps a pod
        # from shutting down. `clear()` stops it properly on the normal
        # path; the daemon flag only covers the paths where it is not
        # reached at all.
        self._timer = threading.Thread(
            target=self._report_periodically, name="video-probe", daemon=True
        )
        self._timer.start()

    def _report_periodically(self) -> None:
        while not self._stop.wait(self._report_every):
            try:
                self.report()
            except Exception:
                # A diagnostic thread that dies takes the diagnosis with
                # it, silently, for the rest of the capture.
                log.exception("Error reporting the video probe")

    def note_request(self, what: str, *, sent: bool) -> None:
        """Records that a video request was attempted, and whether it went out."""
        with self._lock:
            self._requests[what] = sent

    def announce(
        self,
        *,
        ssrc: int,
        discord_user_id: int | None,
        kind: str,
        active: bool,
        resolution: str,
    ) -> None:
        """Records one stream the gateway announced."""
        with self._lock:
            existing = self._streams.get(ssrc)
            if existing is None:
                self._streams[ssrc] = AnnouncedStream(
                    ssrc=ssrc,
                    discord_user_id=discord_user_id,
                    kind=kind,
                    active=active,
                    resolution=resolution,
                )
                return
            # A stream that stops and restarts keeps its packet counts:
            # the question is whether anything ever arrived on this SSRC,
            # not what its latest resolution was.
            existing.active = active
            existing.resolution = resolution
            existing.kind = kind

    def observe(self, ssrc: int, size: int) -> bool:
        """Counts one packet. Returns whether it belongs to an announced stream.

        The return value is what lets the caller distinguish "video we were
        told about" from "audio from somebody we cannot attribute", which
        otherwise look identical from inside the sink.
        """
        with self._lock:
            stream = self._streams.get(ssrc)
            if stream is None:
                return False
            stream.packet_sizes[size_band(size)] += 1
            return True

    def note_subscription(self, ssrcs: list[int], *, subscribed: bool) -> None:
        """Records whether this connection asked for these streams.

        Called from the event loop once the speaker's consent scope has
        been read (`voice.VoiceReceiveAdapter._on_video_announced`). An
        SSRC nobody announced is recorded anyway, as a stream with no
        other detail, so a refusal is never lost to an ordering the
        gateway chose.
        """
        with self._lock:
            for ssrc in ssrcs:
                stream = self._streams.get(ssrc)
                if stream is None:
                    stream = AnnouncedStream(
                        ssrc=ssrc,
                        discord_user_id=None,
                        kind="?",
                        active=False,
                        resolution="?",
                    )
                    self._streams[ssrc] = stream
                stream.subscribed = subscribed

    def is_refused(self, ssrc: int) -> bool:
        """Whether this stream was deliberately not asked for.

        `False` for a stream that was asked for **and** for one nobody
        has decided about yet: only an explicit refusal is a refusal, and
        reporting an undecided stream as refused would put a consent
        verdict in a metric that no consent produced.
        """
        with self._lock:
            stream = self._streams.get(ssrc)
            return stream is not None and stream.subscribed is False

    def note_unannounced(self) -> None:
        with self._lock:
            self.unannounced_packets += 1

    def report(self) -> None:
        """Says what was asked for and what came back.

        **Reports even when there is nothing to report**, which the first
        version did not. Silence was indistinguishable from the probe
        being off, from the switch not being set, and from the deployment
        not carrying the probe at all -- and "nothing was announced" is
        not the absence of a finding, it is the finding.
        """
        with self._lock:
            streams = list(self._streams.values())
            unannounced = self.unannounced_packets
            requests = dict(self._requests)

        arrived = sum(1 for stream in streams if stream.packets)
        # Counted and reported separately from everything else: a run in
        # which every announced stream was refused looks exactly like a
        # run in which Discord sent nothing, and only one of those is a
        # finding about Discord.
        refused = sum(1 for stream in streams if stream.subscribed is False)
        described = ", ".join(
            f"ssrc={stream.ssrc}/{stream.kind}"
            f"{'' if stream.active else '(inactive)'}"
            f"{'' if stream.subscribed is not False else '(no video consent)'}"
            f"@{stream.resolution} user={stream.discord_user_id} "
            f"packets={stream.packets}"
            + (
                f" [{', '.join(f'{b}:{c}' for b, c in sorted(stream.packet_sizes.items()))}]"
                if stream.packets
                else ""
            )
            for stream in streams[:MAX_DESCRIBED_STREAMS]
        )
        log.warning(
            "video probe: asked for video with [%s] | %d stream(s) announced, %d refused for "
            "lack of video consent, %d delivered any packet | %s | packets on unannounced "
            "ssrcs: %d || %s",
            _describe_requests(requests),
            len(streams),
            refused,
            arrived,
            described or "none",
            unannounced,
            _verdict(requests, len(streams), arrived, refused),
        )

    def clear(self) -> None:
        """Reports once and forgets, at the end of a capture."""
        self._stop.set()
        timer, self._timer = self._timer, None
        if timer is not None:
            # Bounded, and on the sink-cleanup path: `AudioSink.__del__`
            # can reach `cleanup()` from the garbage collector, and a
            # cleanup that blocked there would stall an arbitrary thread.
            timer.join(timeout=TIMER_JOIN_TIMEOUT)
        self.report()
        with self._lock:
            self._streams.clear()
            self.unannounced_packets = 0
            self._requests.clear()


def _describe_requests(requests: dict[str, bool]) -> str:
    """The request state, so a reader can tell refusal from omission."""
    if not requests:
        return "nothing -- this build does not ask for video at all"
    return ", ".join(
        f"{what}={'sent' if sent else 'FAILED'}" for what, sent in sorted(requests.items())
    )


def _verdict(requests: dict[str, bool], announced: int, arrived: int, refused: int) -> str:
    """One sentence naming what the numbers above decide.

    Written out rather than left to the reader because the outcomes lead
    to different projects, and the difference between them is not obvious
    from three integers.
    """
    if not arrived and announced and refused == announced:
        return (
            "Every announced stream was refused because nobody in this session has "
            "consented to video, so this run says nothing about whether Discord would "
            "have sent it. A measurement needs a speaker whose consent scope is "
            "audio_video in a guild with video_consent_offered turned on."
        )
    if arrived:
        return (
            "Video packets are reaching this bot. Depacketisation, DAVE decryption for "
            "MediaType.video and storage are worth building -- and a consent role for "
            "screen sharing is needed before any of it records anything."
        )
    if announced:
        return (
            "Discord announces video to this bot but sends none of it. The next question "
            "is subscription -- whether op 15 named the right SSRCs -- not decoding."
        )
    if not any(requests.values()):
        return (
            "Nothing was asked for, so nothing can be concluded. Check that "
            "STURNUS_CAPTURE_DIAGNOSTICS is set and that this build carries "
            "video_subscription."
        )
    return (
        "Discord did not announce a single video stream even though this connection "
        "declared video support and asked for every stream. If somebody was sharing a "
        "screen, note that Go Live is a separate RTC connection reached through an "
        "undocumented user-client gateway opcode, which this bot does not attempt -- so "
        "a camera test tells the two failures apart."
    )
