"""The sink, driven without a gateway connection.

The module this replaces carried a docstring explaining why the sink
callback had no tests: it was invoked by the extension's packet-router
thread, outside anything a fake could stand in for. That excuse is gone.
`RecordingSink` takes its decoder, clock and `emit` as parameters, so it
is constructible with fakes and drivable from a list of packets -- and
since the untested callback is what silently ended a production recording,
using that is the whole point of the redesign.

Real `discord.py` objects read live gateway state through their
properties, so `unittest.mock.MagicMock(spec=...)` stands in for them
here, as it does in `tests/infrastructure/discord/test_client.py`; `spec=`
sets `__class__`, so the `isinstance` check in `write()` still means what
it means in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import discord
import pytest
from discord.ext import voice_recv
from discord.ext.voice_recv.rtp import OPUS_SILENCE, FakePacket

from sturnus.infrastructure.discord.sink import (
    UNATTRIBUTED_NOTICE_EVERY,
    UNATTRIBUTED_NOTICE_LIMIT,
    CapturedFrame,
    CaptureMessage,
    RecordingSink,
    SpeakerStreamEnded,
    UnattributedAudio,
)
from sturnus.infrastructure.metrics import (
    FRAMES_DECODED,
    FRAMES_DISCARDED,
    FRAMES_LOST,
    FRAMES_UNATTRIBUTED,
    SINK_ERRORS,
    Counters,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ROLE_ID = 42
ANNA_SSRC = 111
ANNA_ID = 1001


class FakeClock:
    def now(self) -> datetime:
        return T0


class FakeDecoder:
    """Satisfies `OpusDecoderPool`: records what it saw, returns what it is told."""

    def __init__(self, result: bytes | None = b"pcm") -> None:
        self.result = result
        self.frames: list[tuple[int, bytes | None]] = []
        self.dropped: list[int] = []
        self.cleared = 0

    def decode(self, ssrc: int, frame: bytes | None) -> bytes | None:
        self.frames.append((ssrc, frame))
        return self.result

    def drop(self, ssrc: int) -> None:
        self.dropped.append(ssrc)

    def clear(self) -> None:
        self.cleared += 1


def member(user_id: int = ANNA_ID, *, role_id: int | None = ROLE_ID) -> discord.Member:
    stand_in = MagicMock(spec=discord.Member)
    stand_in.id = user_id
    stand_in.display_name = f"user-{user_id}"
    if role_id is None:
        stand_in.roles = []
    else:
        role = MagicMock(spec=discord.Role)
        role.id = role_id
        stand_in.roles = [role]
    return stand_in


def voice_data(
    payload: bytes = b"opus", *, ssrc: int = ANNA_SSRC, timestamp: int = 960
) -> voice_recv.VoiceData:
    """A `VoiceData` shaped exactly as a `wants_opus` sink receives one.

    `.pcm` is empty and `.opus` returns `packet.decrypted_data`, because
    the library never built a decoder for us -- which is the fix.
    """
    packet = MagicMock()
    packet.ssrc = ssrc
    packet.timestamp = timestamp
    packet.decrypted_data = payload
    return voice_recv.VoiceData(packet, None)


def build(
    decoder: FakeDecoder | None = None,
    counters: Counters | None = None,
) -> tuple[RecordingSink, list[CaptureMessage], FakeDecoder]:
    pool = decoder or FakeDecoder()
    emitted: list[CaptureMessage] = []
    sink = RecordingSink(
        consent_role_id=ROLE_ID,
        decoder=pool,
        clock=FakeClock(),
        emit=emitted.append,
        counters=counters or Counters(),
    )
    return sink, emitted, pool


# --- the fix itself ---


def test_wants_opus_is_true_so_the_library_never_decodes() -> None:
    """This test *is* the incident.

    `PacketDecoder.__init__` builds a `discord.opus.Decoder` only when
    `wants_opus()` is false, and `_process_packet` calls `_decode_packet`
    -- which raised `OpusError: corrupted stream` straight into
    `PacketRouter.run()` -- only on the same condition. A `False` here on
    any code path puts the crash site back, so this asserts the literal.
    """
    sink, _, _ = build()

    assert sink.wants_opus() is True


def test_the_sink_is_instantiable_against_the_installed_library() -> None:
    """A tripwire for a library bump, not a tautology.

    `SinkABC` is an ABC: if a future `discord-ext-voice-recv` adds an
    abstract member, Python refuses to instantiate this class and this
    test fails loudly instead of the sink silently losing a contract.
    `discord-ext-voice-recv` is an alpha release, so that is a real risk.
    """
    sink, _, _ = build()

    assert isinstance(sink, voice_recv.AudioSink)
    assert sink.children == [], "an endpoint sink registers no child"


# --- the consent gate (Spec 3.1) ---


def test_a_member_without_the_role_is_rejected_before_anything_is_decoded() -> None:
    """The legal gate, and its ordering.

    Audio nobody consented to is never even turned into PCM, and no
    decoder object is ever created for that speaker.
    """
    sink, emitted, decoder = build()

    sink.write(member(role_id=None), voice_data())

    assert emitted == []
    assert decoder.frames == [], "the decoder was never reached"


def test_a_member_with_the_role_is_captured() -> None:
    sink, emitted, decoder = build()

    sink.write(member(), voice_data(b"opus", timestamp=1920))

    assert decoder.frames == [(ANNA_SSRC, b"opus")]
    assert emitted == [
        CapturedFrame(
            discord_user_id=ANNA_ID,
            display_name=f"user-{ANNA_ID}",
            ssrc=ANNA_SSRC,
            rtp_timestamp=1920,
            pcm=b"pcm",
            captured_at=T0,
        )
    ]


@pytest.mark.parametrize("user", [None, "not-a-member"])
def test_audio_that_cannot_be_attributed_is_never_decoded(user: object) -> None:
    """No consent record can be checked for an identity we do not know.

    A speaker already talking when the bot connects has no SSRC mapping
    yet -- Discord supplies it only with its speaking event -- so their
    frames arrive with no member attached.
    """
    counters = Counters()
    sink, emitted, decoder = build(counters=counters)

    sink.write(user, voice_data())  # type: ignore[arg-type]

    assert decoder.frames == []
    assert emitted == [UnattributedAudio(ANNA_SSRC, 1)]
    assert counters.get(FRAMES_UNATTRIBUTED) == 1


def test_unattributed_audio_is_reported_but_never_floods() -> None:
    """Silence in the face of dropped audio is the bug; a wall of text is not the fix."""
    sink, emitted, _ = build()

    for _ in range(UNATTRIBUTED_NOTICE_EVERY * (UNATTRIBUTED_NOTICE_LIMIT + 2)):
        sink.write(None, voice_data())

    notices = [message for message in emitted if isinstance(message, UnattributedAudio)]
    assert [notice.frames for notice in notices] == [
        1,
        *(UNATTRIBUTED_NOTICE_EVERY * n for n in range(1, UNATTRIBUTED_NOTICE_LIMIT)),
    ]


# --- what the frame is ---


def test_a_lost_frame_reaches_the_decoder_as_an_empty_payload() -> None:
    """`FakePacket.decrypted_data` is `b""`, and `decode(b"")` raises -1.

    The library manufactures one of these for every gap in the sequence,
    so a `wants_opus` sink is told about loss precisely -- it just has to
    route the empty payload to concealment rather than to `decode()`.
    """
    sink, emitted, decoder = build(FakeDecoder(result=None))
    packet = FakePacket(ANNA_SSRC, 7, 960)
    assert packet.decrypted_data == b""

    sink.write(member(), voice_recv.VoiceData(packet, None))

    assert decoder.frames == [(ANNA_SSRC, b"")]
    assert emitted == []


def test_the_sink_does_not_count_frames_the_decoder_already_counts() -> None:
    """`sturnus_voice_frames_decoded_total` read double when both did.

    The decoder is the one place that knows *why* a frame did not make it
    -- lost, or unreadable and with which libopus code -- so it owns all
    three frame counters; see `test_decoding.py`. This pins that the sink
    does not add a second increment on top.
    """
    counters = Counters()
    sink, _, _ = build(counters=counters)

    sink.write(member(), voice_data())

    assert counters.get(FRAMES_DECODED) == 0
    assert counters.get(FRAMES_DISCARDED) == 0
    assert counters.get(FRAMES_LOST) == 0


def test_a_silence_frame_is_decoded_rather_than_skipped() -> None:
    """Three bytes through libopus is free; skipping them is not.

    Skipping would desynchronise the decoder's last-packet duration, which
    packet-loss concealment reads to size the frame it synthesises.
    """
    sink, emitted, decoder = build()

    sink.write(member(), voice_data(OPUS_SILENCE))

    assert decoder.frames == [(ANNA_SSRC, OPUS_SILENCE)]
    assert len(emitted) == 1


def test_a_frame_that_will_not_decode_emits_nothing() -> None:
    """The frame is gone, and nothing after it shifts: `SpeakerWriter`
    places audio by RTP-derived absolute time, so the gap becomes exactly
    one frame of real silence in the right place.
    """
    sink, emitted, _ = build(FakeDecoder(result=None))

    sink.write(member(), voice_data())

    assert emitted == []


# --- the invariant: nothing escapes write() ---


@pytest.mark.parametrize("broken", ["decoder", "emit", "clock"])
def test_write_never_raises_whatever_breaks_underneath_it(broken: str) -> None:
    """`PacketRouter._do_run` calls `write()` unguarded.

    An exception escaping here reaches `PacketRouter.run()`, which sets
    `reader.error` and calls `stop_listening()` in its `finally` -- the
    exact sequence that stopped capture for every speaker in production.
    """
    counters = Counters()
    decoder = FakeDecoder()
    clock: object = FakeClock()
    emit: object = MagicMock()

    if broken == "decoder":
        decoder.decode = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    elif broken == "emit":
        emit = MagicMock(side_effect=RuntimeError("boom"))
    else:
        clock = MagicMock(**{"now.side_effect": RuntimeError("boom")})

    sink = RecordingSink(
        consent_role_id=ROLE_ID,
        decoder=decoder,
        clock=clock,  # type: ignore[arg-type]
        emit=emit,  # type: ignore[arg-type]
        counters=counters,
    )

    sink.write(member(), voice_data())  # must not raise

    assert counters.get(SINK_ERRORS) == 1


def test_a_broken_packet_does_not_take_down_the_router_thread() -> None:
    """Whatever an alpha library hands us, the router thread survives it."""

    class BrokenPacket:
        decrypted_data = b"opus"

        @property
        def ssrc(self) -> int:
            raise AttributeError("the library changed shape under us")

    sink, emitted, _ = build()

    sink.write(member(), voice_recv.VoiceData(BrokenPacket(), None))  # type: ignore[arg-type]

    assert emitted == []


# --- lifecycle ---


def test_a_departing_speaker_releases_their_decoder_and_rtp_reference() -> None:
    """Mirrors the library, which destroys its own decoder on the same event."""
    sink, emitted, decoder = build()

    sink.on_voice_member_disconnect(member(), ANNA_SSRC)

    assert decoder.dropped == [ANNA_SSRC]
    assert emitted == [SpeakerStreamEnded(ANNA_SSRC)]


def test_a_disconnect_without_a_known_ssrc_is_a_no_op() -> None:
    """The library passes `None` when it never had a mapping for that user."""
    sink, emitted, decoder = build()

    sink.on_voice_member_disconnect(member(), None)

    assert decoder.dropped == []
    assert emitted == []


def test_the_disconnect_listener_is_registered_with_the_library() -> None:
    """`SinkEventRouter` finds listeners through `__sink_listeners__`.

    Registration keys on the entry's first element and dispatch looks up
    `f"on_{event}"`, so the decorator's default name is what makes the
    two meet. Pinned because a decorator that silently registers nothing
    would leave decoders to accumulate for the whole process lifetime,
    with no symptom until memory ran out.
    """
    assert ("on_voice_member_disconnect", "on_voice_member_disconnect") in (
        RecordingSink.__sink_listeners__
    )


def test_cleanup_is_idempotent_and_does_not_close_audio_writers() -> None:
    """Called by `AudioReader._stop` and again by `AudioSink.__del__`.

    Writers belong to `RecordingService.close()`, which the client drives
    on shutdown; closing them here would double-close files on a routine
    SIGTERM.
    """
    sink, _, decoder = build()

    sink.cleanup()
    sink.cleanup()

    assert decoder.cleared == 1


def test_cleanup_survives_a_half_built_instance() -> None:
    """Garbage collection can run `__del__` against a partially constructed object."""
    half_built = RecordingSink.__new__(RecordingSink)

    half_built.cleanup()  # must not raise


def test_cleanup_survives_a_decoder_that_raises() -> None:
    decoder = FakeDecoder()
    decoder.clear = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    sink, _, _ = build(decoder)

    sink.cleanup()  # must not raise
