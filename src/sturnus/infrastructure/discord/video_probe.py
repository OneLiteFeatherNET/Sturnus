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
unasked. Neither this library nor `discord.py` sends that subscription,
and the behaviour for bots is undocumented. If nothing arrives, no amount
of H.264 depacketisation, DAVE `MediaType.video` decryption or ffmpeg
plumbing will produce a recording -- and all of that is weeks of work
resting on an assumption nobody has checked.

**So this checks it, and nothing else.** It pairs what the gateway
announces with what the socket delivers:

- every `voice_member_video` event: who, which SSRC, what kind of stream,
  at what resolution;
- every RTP packet whose SSRC matches one of those, counted by size.

Packets arriving means the rest is worth building. Announcements with no
packets means Discord is not sending them to us, and the next question is
subscription rather than decoding -- a completely different problem, found
in one recording instead of after the fact.

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

    def __init__(self) -> None:
        self._streams: dict[int, AnnouncedStream] = {}
        # `announce` runs on the sink-event-router thread and `observe` on
        # the packet-router thread.
        self._lock = threading.Lock()
        #: Packets on an SSRC nobody announced. A non-zero count with zero
        #: announced packets would mean video arrives before the gateway
        #: says so, which changes where the mapping has to be built.
        self.unannounced_packets = 0

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

    def note_unannounced(self) -> None:
        with self._lock:
            self.unannounced_packets += 1

    def report(self) -> None:
        """Says whether the announced streams ever delivered anything."""
        with self._lock:
            streams = list(self._streams.values())
            unannounced = self.unannounced_packets
        if not streams and not unannounced:
            return

        arrived = sum(1 for stream in streams if stream.packets)
        described = ", ".join(
            f"ssrc={stream.ssrc}/{stream.kind}"
            f"{'' if stream.active else '(inactive)'}"
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
            "video probe: %d stream(s) announced, %d delivered any packet | %s | "
            "packets on unannounced ssrcs: %d || Packets arriving means recording a shared "
            "screen is worth building. Announcements with no packets means Discord is not "
            "sending them to this bot, and the next question is stream subscription rather "
            "than decoding.",
            len(streams),
            arrived,
            described or "none",
            unannounced,
        )

    def clear(self) -> None:
        """Reports once and forgets, at the end of a capture."""
        self.report()
        with self._lock:
            self._streams.clear()
            self.unannounced_packets = 0
