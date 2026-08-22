"""The `AudioSink` Sturnus hands the library, and the messages it emits.

Two invariants live here, and everything else in the module serves them.

**`wants_opus()` returns a literal `True`.** That single method is the
fix. `PacketDecoder.__init__` reads it (and `PacketDecoder.reset()` reads
it again) to decide whether to build a `discord.opus.Decoder`; with `True`
it builds none, and `_process_packet` therefore never calls
`_decode_packet` -- the line that raised `OpusError: corrupted stream` and
killed the packet-router thread in production. It must never become
conditional or configuration-driven: a `False` on any path puts the
crash site back.

**`write()` never raises.** `PacketRouter._do_run` calls `sink.write(...)`
unguarded, inside the same `try` whose `except` sets `reader.error` and
whose `finally` calls `stop_listening()`. An exception escaping our
`write()` would end capture for every speaker in exactly the way the
incident did. So the body is one `try` / `except Exception`; `BaseException`
is deliberately not caught, so `KeyboardInterrupt` and `SystemExit` still
work.

`BasicSink(cb, decode=False)` would already report `wants_opus() == True`,
but it offers nowhere to put that guard and no access to
`on_voice_member_disconnect(member, ssrc)`, which is the supported hook
for evicting a speaker's decoder (the library evicts its own there too,
`voice_recv/gateway.py`). Hence a real `AudioSink` subclass.

Only the documented surface is used: `wants_opus`, `write`, `cleanup`,
`AudioSink.listener`, `VoiceData.opus`, `VoiceData.packet`. Nothing
reaches into an underscore attribute of the library. `_register_child` is
concrete on `AudioSink` and is deliberately *not* overridden -- this sink
is an endpoint with no children.

Threading: `write()` runs on the packet-router thread and listeners run on
the sink-event-router thread, which dispatches while holding the packet
router's lock, so the two are already mutually exclusive and sink-side
state needs no lock of its own. The flip side is that a slow listener
stalls routing for every speaker, so per-frame work stays to small
in-memory maps -- the only logging done here is edge-triggered and
bounded. Everything that has to reach the loop leaves as an immutable
message through `emit`.

Unlike the callback this replaces, all of it is testable without a voice
connection: see `tests/infrastructure/discord/test_sink.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import discord
from discord.ext import voice_recv

from sturnus.application.ports import Clock
from sturnus.infrastructure.discord.capture_diagnostics import CaptureDiagnostics
from sturnus.infrastructure.telemetry import VOICE_PACKETS, record

log = logging.getLogger(__name__)

#: Backstop on the unattributed bookkeeping, for the same reason
#: `ResilientOpusDecoder` caps its decoders: this process stays up for
#: hours and an SSRC is not a stable identity.
MAX_TRACKED_UNATTRIBUTED = 256

#: How often an *unexpected* error inside `write()` is logged with its
#: traceback. The first one always is; after that one in this many, so a
#: fault that breaks every frame cannot bury a pod's log pipeline.
SINK_ERROR_LOG_EVERY = 500


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One consenting speaker's decoded frame, on its way to the event loop.

    `pcm` is 48 kHz 16-bit stereo, exactly what the library's own decoder
    produced, so nothing downstream of `RecordingService.voice_packet`
    changes. `captured_at` is taken on the router thread at arrival rather
    than after the hand-off, because for a speaker's *first* frame that
    value is their audio epoch (Spec 6.3) and keeping the queue latency
    out of it is a small, free accuracy win.
    """

    discord_user_id: int
    display_name: str
    ssrc: int
    rtp_timestamp: int
    pcm: bytes
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class SpeakerStreamEnded:
    """A participant's stream ended, so its RTP reference point is stale."""

    ssrc: int


@dataclass(frozen=True, slots=True)
class DecodeFailure:
    """Nothing decodes on any live stream. The only decode failure that ends a session."""


@dataclass(frozen=True, slots=True)
class CaptureStopped:
    """The library's `after=` hook fired: capture ended without us asking."""

    error: BaseException | None


CaptureMessage = CapturedFrame | SpeakerStreamEnded | DecodeFailure | CaptureStopped


class OpusDecoderPool(Protocol):
    """What the sink needs from the decoding layer, and nothing more.

    `decode` returns PCM or `None` for "write nothing" and never raises;
    `ResilientOpusDecoder` in `.decoding` is the production implementation
    and a four-line fake is the test one.
    """

    def decode(self, ssrc: int, frame: bytes | None) -> bytes | None: ...

    def drop(self, ssrc: int) -> None: ...

    def clear(self) -> None: ...


class RecordingSink(voice_recv.AudioSink):
    """Receives raw Opus, applies the role gate, decodes, and emits frames.

    Knows nothing about asyncio, the database or `RecordingService`: it
    runs a fixed, short pipeline per frame on the router thread and hands
    a plain dataclass to the injected `emit`, which is what does the loop
    hop. That is what makes it constructible with fakes and drivable from
    a list of packets.
    """

    def __init__(
        self,
        *,
        consent_role_id: int,
        decoder: OpusDecoderPool,
        clock: Clock,
        emit: Callable[[CaptureMessage], None],
        guild_id: int | None = None,
        diagnostics: CaptureDiagnostics | None = None,
    ) -> None:
        # No destination: this sink is an endpoint, not a link in a
        # transformer chain, so it registers no child.
        super().__init__()
        # Metric label only, and optional so the sink stays constructible
        # from a list of packets with no guild anywhere in sight -- which
        # is the property `tests/infrastructure/discord/test_sink.py`
        # exists to keep. `guild_id` is the one non-literal in
        # `METRIC_LABEL_FIELDS` (see `sturnus.observability.fields`), and
        # it is what makes "which server stopped being recorded" a query
        # rather than a log search.
        self._guild_id = guild_id
        self._consent_role_id = consent_role_id
        self._decoder = decoder
        self._clock = clock
        self._emit = emit
        # The RTP packet is only visible here -- `ResilientOpusDecoder` is
        # handed bytes and nothing else -- so the arithmetic that cut those
        # bytes out of the packet can only be checked from this side.
        self._diagnostics = diagnostics
        self._unattributed: set[int] = set()
        self._sink_errors = 0
        self._cleaned_up = False

    def wants_opus(self) -> bool:
        """Sturnus decodes; the library must not.

        Returning `True` is the entire fix for the production incident:
        with no `Decoder` constructed, `_decode_packet` is unreachable and
        a corrupt frame can no longer kill the packet-router thread. See
        `test_wants_opus_is_true_so_the_library_never_decodes`.
        """
        return True

    def write(self, user: discord.Member | discord.User | None, data: voice_recv.VoiceData) -> None:
        """Total by construction -- see the module docstring."""
        try:
            self._write(user, data)
        except Exception:
            # Reaching here means something we did not anticipate:
            # everything expected -- a frame that will not decode, a lost
            # frame, an unmapped SSRC -- is handled below without raising.
            # Logged with a traceback the first time and then sparsely,
            # because at 50 frames per second per speaker an unrate-limited
            # traceback is its own outage.
            self._sink_errors += 1
            if self._sink_errors == 1 or self._sink_errors % SINK_ERROR_LOG_EVERY == 0:
                log.exception(
                    "Unexpected error handling a voice frame (%d so far); capture continues",
                    self._sink_errors,
                )

    def cleanup(self) -> None:
        """Releases sink-owned resources only. Idempotent, and safe half-built.

        Called by `AudioReader._stop` on its own thread and again by
        `AudioSink.__del__`, which garbage collection may run at an
        arbitrary moment on an arbitrary thread against a partially
        constructed object -- hence the `getattr` guards and the absence
        of any event-loop access.

        Explicitly does *not* close audio writers: those belong to
        `RecordingService.close()`, which the client drives on shutdown
        (Spec 6.4). Closing them here would double-close files on a
        routine SIGTERM.
        """
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        decoder = getattr(self, "_decoder", None)
        if decoder is not None:
            try:
                decoder.clear()
            except Exception:
                log.exception("Error releasing Opus decoders during sink cleanup")
        unattributed = getattr(self, "_unattributed", None)
        if unattributed is not None:
            unattributed.clear()

    # The library's decorator carries no annotations; the method below
    # is fully typed, and `test_sink.py` pins that it stays registered.
    @voice_recv.AudioSink.listener()  # type: ignore[untyped-decorator]
    def on_voice_member_disconnect(self, member: discord.Member | None, ssrc: int | None) -> None:
        """Evicts the departing speaker's decoder and retires their RTP reference.

        Mirrors the library, which destroys its own `PacketDecoder` for
        this SSRC on the same gateway event. `ssrc` is `None` when the
        library never had a mapping for that user, in which case there is
        nothing to evict.
        """
        del member
        if ssrc is None:
            return
        self._decoder.drop(ssrc)
        self._unattributed.discard(ssrc)
        self._emit(SpeakerStreamEnded(ssrc))

    def _write(
        self, user: discord.Member | discord.User | None, data: voice_recv.VoiceData
    ) -> None:
        ssrc = data.packet.ssrc

        if not isinstance(user, discord.Member):
            # Either the SSRC has no member mapped yet (a speaker who was
            # already talking when the bot joined; Discord only supplies
            # the mapping with its speaking event) or the user is not a
            # guild member at all. Nothing is decoded and nothing is
            # written -- but it is not silent either.
            record(VOICE_PACKETS, 1, outcome="unknown_user", guild_id=self._guild_id)
            self._note_unattributed(ssrc)
            return

        if not any(role.id == self._consent_role_id for role in user.roles):
            # Spec 3.1's first layer: a synchronous, in-memory read of
            # `Member.roles`, on every single frame, with no cache and no
            # staleness window, so revocation-by-role-removal takes effect
            # immediately. Deliberately *before* the decoder: audio nobody
            # consented to is never even turned into PCM, and no decoder
            # object is ever created for that speaker.
            #
            # Counted, because "nobody consented" and "capture is broken"
            # produce the same silence in the recording and the same empty
            # session row. The counter is what separates them without
            # anything per-frame reaching Loki.
            record(VOICE_PACKETS, 1, outcome="no_role", guild_id=self._guild_id)
            return

        # `data.opus` is `packet.decrypted_data`, already stripped of RTP
        # extension headers by the library's decryptor. It is `b""` for the
        # fake packets the jitter buffer manufactures on loss, which the
        # decoder routes to concealment; a silence packet carries the real
        # three-byte OPUS_SILENCE frame and is decoded normally, because
        # skipping it would desynchronise the decoder's last-packet
        # duration, which packet-loss concealment depends on.
        if self._diagnostics is not None:
            self._note_packet_shape(ssrc, data)

        pcm = self._decoder.decode(ssrc, data.opus)
        if pcm is None:
            # The frame is gone. `SpeakerWriter` places audio by
            # RTP-derived absolute time, so this becomes exactly one
            # frame of real silence in the WAV and nothing after it
            # shifts.
            #
            # The rate of this label against `recorded` is the early
            # warning `.decoding`'s threshold deliberately does not give:
            # that fires once, after five consecutive seconds of nothing,
            # and this is visible from the first frame.
            record(VOICE_PACKETS, 1, outcome="undecodable", guild_id=self._guild_id)
            return

        self._emit(
            CapturedFrame(
                discord_user_id=user.id,
                display_name=user.display_name,
                ssrc=ssrc,
                rtp_timestamp=data.packet.timestamp,
                pcm=pcm,
                captured_at=self._clock.now(),
            )
        )

    def _note_packet_shape(self, ssrc: int, data: voice_recv.VoiceData) -> None:
        """Reports how the payload was cut out of the packet around it.

        Reads only the documented attributes of `RTPPacket`, and tolerates
        every one of them being absent: a `FakePacket` and a
        `SilencePacket` carry neither an extension nor a body, and a
        diagnostic that raised on one would stop the capture it exists to
        explain.
        """
        assert self._diagnostics is not None
        packet = data.packet
        extension = getattr(packet, "extension", None)
        body = getattr(packet, "data", b"") or b""
        payload = getattr(packet, "decrypted_data", b"") or b""
        if not body or not payload:
            return
        try:
            self._diagnostics.observe_rtp(
                ssrc,
                extended=bool(getattr(packet, "extended", False)),
                csrc_count=int(getattr(packet, "cc", 0) or 0),
                extension_words=int(getattr(extension, "length", 0) or 0),
                body_bytes=len(body),
                payload_bytes=len(payload),
            )
        except Exception:
            # Never from here: `write()` already guards the frame path, and
            # a measurement is not worth the capture it would take down.
            log.debug("Could not measure the RTP shape for ssrc=%s", ssrc, exc_info=True)

    def _note_unattributed(self, ssrc: int) -> None:
        """Reports unattributed audio once per SSRC, and never more than that.

        Once per SSRC and capped at `MAX_TRACKED_UNATTRIBUTED` distinct
        ones, so a stuck stream at 50 frames a second cannot turn into a
        log flood -- but silence in the face of dropped audio is the bug
        this branch exists to remove, so it is never nothing.
        """
        if ssrc in self._unattributed:
            return
        if len(self._unattributed) >= MAX_TRACKED_UNATTRIBUTED:
            return
        self._unattributed.add(ssrc)
        log.warning(
            "Audio from ssrc=%s cannot be attributed to a guild member, so it is neither "
            "decoded nor recorded: no consent record can be checked for an identity we do "
            "not know. Discord supplies the SSRC-to-user mapping only with its speaking "
            "event, so a participant who was already talking when the bot joined has to "
            "pause and speak again before their audio can be recorded.",
            ssrc,
        )
