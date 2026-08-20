"""The decoding layer, drivable from a plain list of frames.

No voice connection anywhere: `ResilientOpusDecoder` takes its decoder
factory as a parameter precisely so the whole failure policy can be
exercised deterministically against a fake.

libopus does still have to be *present*, and for a reason worth writing
down: `discord.opus.OpusError.__init__` calls `_lib.opus_strerror(code)`
to build its message, and `_lib` is loaded lazily by `_OpusStruct.
get_opus_version()`. Constructing the very exception this module is about
therefore fails with `AttributeError: 'NoneType' object has no attribute
'opus_strerror'` until something has built a real `Decoder` or `Encoder`
first. In production the startup probe in `VoiceReceiveAdapter.join` is
what guarantees that ordering; here it is the autouse fixture below, and
in CI it is the `libopus0` install added to `.github/workflows/build.yml`.

The test this file exists for is the last one. It encodes real audio,
splices a corrupt frame into the middle, pushes the lot through, and
asserts that the good frames still come back. That is the production
incident, in a unit test.
"""

from __future__ import annotations

import logging
import math
import struct

import pytest
from discord.opus import OpusError, OpusNotLoaded

from sturnus.infrastructure.discord.decoding import (
    FAILING_AFTER_CONSECUTIVE_FAILURES,
    MAX_CONSECUTIVE_CONCEALED,
    ResilientOpusDecoder,
    new_opus_decoder,
)

ANNA_SSRC, BEN_SSRC = 111, 222
CORRUPTED_STREAM = -4

BAD = b"corrupt"


@pytest.fixture(autouse=True)
def libopus_loaded() -> None:
    """Loads libopus, or skips -- see the module docstring for why it is needed."""
    try:
        new_opus_decoder()
    except OpusNotLoaded:  # pragma: no cover - depends on the host
        pytest.skip("libopus is not available on this host")


class FakeFrameDecoder:
    """Satisfies `FrameDecoder` without libopus.

    Marks its output with the decoder's own identity, which is how the
    per-speaker isolation test can tell two decoders apart -- Opus is
    stateful, and one decoder shared between two speakers corrupts both
    streams.
    """

    def __init__(self, name: str, bad: frozenset[bytes] = frozenset()) -> None:
        self.name = name
        self.bad = bad
        self.concealed = 0
        self.decoded: list[bytes] = []

    def decode(self, data: bytes | None, *, fec: bool = False) -> bytes:  # noqa: ARG002
        if data is None:
            self.concealed += 1
            return b"plc:" + self.name.encode()
        if data in self.bad:
            raise OpusError(CORRUPTED_STREAM)
        self.decoded.append(data)
        return b"pcm:" + self.name.encode() + b":" + data


class DecoderFactorySpy:
    """Hands out a fresh `FakeFrameDecoder` per call and remembers each one."""

    def __init__(self, bad: frozenset[bytes] = frozenset()) -> None:
        self.bad = bad
        self.built: list[FakeFrameDecoder] = []

    def __call__(self) -> FakeFrameDecoder:
        decoder = FakeFrameDecoder(f"d{len(self.built)}", self.bad)
        self.built.append(decoder)
        return decoder


def fail(decoder: ResilientOpusDecoder, ssrc: int, times: int) -> None:
    """Pushes `times` unreadable frames through one stream."""
    for _ in range(times):
        assert decoder.decode(ssrc, BAD) is None


# --- the defect: a frame that will not decode is skipped, capture continues ---


def test_a_bad_frame_is_skipped_and_the_next_frame_still_decodes() -> None:
    """The whole point. One unreadable frame must cost one frame, not the session."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    decoder = ResilientOpusDecoder(factory=factory)

    results = [decoder.decode(ANNA_SSRC, frame) for frame in (b"a", BAD, b"b")]

    assert results[0] is not None
    assert results[1] is None, "the unreadable frame is discarded"
    assert results[2] is not None, "and the stream keeps working afterwards"
    assert len(factory.built) == 1, "the same decoder instance carried on"


def test_two_speakers_never_share_a_decoder() -> None:
    """Opus is stateful; one decoder for two streams corrupts both."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory)

    anna = decoder.decode(ANNA_SSRC, b"a")
    ben = decoder.decode(BEN_SSRC, b"a")

    assert len(factory.built) == 2
    assert anna != ben, "each speaker's PCM came out of their own decoder"


def test_one_speaker_failing_leaves_every_other_speaker_untouched() -> None:
    """Isolation is what turns 'the recording died' into 'one person lost 5s'."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    decoder = ResilientOpusDecoder(factory=factory)

    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES * 2)

    assert decoder.decode(BEN_SSRC, b"b") is not None


def test_dropping_a_stream_forgets_its_decoder() -> None:
    """Mirrors the library evicting its own `PacketDecoder` on disconnect."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory)

    decoder.decode(ANNA_SSRC, b"a")
    decoder.drop(ANNA_SSRC)
    decoder.decode(ANNA_SSRC, b"a")

    assert len(factory.built) == 2, "the returning SSRC got a fresh decoder"


def test_clear_forgets_everything_and_is_idempotent() -> None:
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory)
    decoder.decode(ANNA_SSRC, b"a")
    decoder.decode(BEN_SSRC, b"a")

    decoder.clear()
    decoder.clear()
    decoder.decode(ANNA_SSRC, b"a")

    assert len(factory.built) == 3


def test_the_max_streams_backstop_evicts_the_least_recently_used() -> None:
    """A missed disconnect event must not become an unbounded decoder leak."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory, max_streams=2)

    decoder.decode(1, b"a")
    decoder.decode(2, b"a")
    decoder.decode(2, b"a")
    decoder.decode(3, b"a")  # evicts ssrc 1, the least recently used
    decoder.decode(2, b"a")
    decoder.decode(1, b"a")  # rebuilt, because it was evicted

    assert len(factory.built) == 4


def test_max_streams_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_streams"):
        ResilientOpusDecoder(max_streams=0)


# --- network loss is concealed; our own decode failures are not ---


@pytest.mark.parametrize("lost", [None, b""])
def test_a_lost_frame_takes_the_concealment_path(lost: bytes | None) -> None:
    """`FakePacket.decrypted_data` is `b""`, and `decode(b"")` raises -- so it must not reach it."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory)
    decoder.decode(ANNA_SSRC, b"a")

    pcm = decoder.decode(ANNA_SSRC, lost)

    assert pcm == b"plc:d0"
    assert factory.built[0].decoded == [b"a"], "the empty payload never reached decode()"


def test_concealment_needs_a_decoder_that_has_heard_something() -> None:
    """libopus reconstructs from its memory of the previous frame; there is none yet."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory)

    assert decoder.decode(ANNA_SSRC, b"") is None
    assert factory.built[0].concealed == 0


def test_concealment_stops_after_the_cap_and_resumes_after_a_success() -> None:
    """Past a handful of frames PLC is noise; real silence is the honest answer."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory)
    decoder.decode(ANNA_SSRC, b"a")

    concealed = [decoder.decode(ANNA_SSRC, b"") for _ in range(MAX_CONSECUTIVE_CONCEALED + 2)]
    assert [pcm is not None for pcm in concealed] == (
        [True] * MAX_CONSECUTIVE_CONCEALED + [False, False]
    )

    decoder.decode(ANNA_SSRC, b"a")
    assert decoder.decode(ANNA_SSRC, b"") is not None, "a real frame re-arms concealment"


def test_a_decode_failure_is_never_concealed() -> None:
    """Inventing audio to cover our own error would put synthesised sound in the record."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    decoder = ResilientOpusDecoder(factory=factory)
    decoder.decode(ANNA_SSRC, b"a")

    assert decoder.decode(ANNA_SSRC, BAD) is None
    assert factory.built[0].concealed == 0


# --- nothing escapes towards the packet-router thread ---


class Exploding:
    """A decoder that fails in a way libopus never would."""

    def decode(self, data: bytes | None, *, fec: bool = False) -> bytes:  # noqa: ARG002
        raise RuntimeError("ctypes went sideways")


def test_a_non_opus_error_is_accounted_for_like_any_other_unreadable_frame() -> None:
    """Dead and reporting fine is the incident's own shape; it must not recur here.

    A failure that is not an `OpusError` used to skip the accounting
    entirely, so a stream where *every* frame was thrown away still looked
    healthy with nothing counted against it -- and could therefore never
    reach the threshold that ends the session.
    """
    failures: list[None] = []
    decoder = ResilientOpusDecoder(
        factory=Exploding,
        on_decode_failure=lambda: failures.append(None),
    )

    for _ in range(FAILING_AFTER_CONSECUTIVE_FAILURES):
        assert decoder.decode(ANNA_SSRC, b"a") is None

    assert failures == [None], "the non-OpusError frames counted towards the verdict"


def test_an_unexpected_decoder_error_never_escapes() -> None:
    decoder = ResilientOpusDecoder(factory=Exploding)
    assert decoder.decode(ANNA_SSRC, b"a") is None


def test_a_factory_that_raises_never_escapes() -> None:
    """A decoder we cannot even build must not reach `PacketRouter.run()`."""

    def broken() -> FakeFrameDecoder:
        raise RuntimeError("no decoder for you")

    decoder = ResilientOpusDecoder(factory=broken)
    assert decoder.decode(ANNA_SSRC, b"a") is None


def test_a_listener_that_raises_never_escapes() -> None:
    def broken() -> None:
        raise RuntimeError("listener is broken")

    decoder = ResilientOpusDecoder(factory=Exploding, on_decode_failure=broken)
    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES)
    assert decoder.decode(ANNA_SSRC, b"a") is None


# --- the one escalation: count consecutive failures, act once at the threshold ---


def test_a_failing_stream_is_reported_once_however_long_the_run_lasts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At 50 frames a second, one ERROR per frame would be its own outage."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    decoder = ResilientOpusDecoder(factory=factory)

    with caplog.at_level(logging.ERROR, logger="sturnus.infrastructure.discord.decoding"):
        fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES * 3)

    reports = [record for record in caplog.records if "consecutive" in record.getMessage()]
    assert len(reports) == 1
    assert str(ANNA_SSRC) in reports[0].getMessage()
    assert str(CORRUPTED_STREAM) in reports[0].getMessage(), "the libopus code is named"


def test_a_stream_below_the_threshold_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Isolated corruption on a lossy connection is a non-event."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    decoder = ResilientOpusDecoder(factory=factory)

    with caplog.at_level(logging.WARNING, logger="sturnus.infrastructure.discord.decoding"):
        fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES - 1)

    assert caplog.records == []


def test_one_failing_speaker_is_not_read_as_nothing_decoding_anywhere() -> None:
    """The mistake that ended a whole channel's recording over one bad stream."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    failures: list[None] = []
    decoder = ResilientOpusDecoder(factory=factory, on_decode_failure=lambda: failures.append(None))

    decoder.decode(BEN_SSRC, b"b")
    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES * 2)

    assert failures == [], "Ben is still being recorded, so the session goes on"


def test_a_stream_too_young_to_judge_blocks_the_verdict_rather_than_being_ignored() -> None:
    """A stream that has barely started is evidence something might still work.

    Judging only the streams that had failed long enough is what once read
    "one dead speaker while everybody else was briefly quiet" as "nothing
    decodes anywhere" and ended the recording for the whole channel.
    """
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    failures: list[None] = []
    decoder = ResilientOpusDecoder(factory=factory, on_decode_failure=lambda: failures.append(None))

    fail(decoder, BEN_SSRC, 1)
    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES * 2)

    assert failures == []


def test_nothing_decoding_anywhere_fires_exactly_once() -> None:
    """Writing empty files while telling a channel it is recorded is the original bug."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    failures: list[None] = []
    decoder = ResilientOpusDecoder(factory=factory, on_decode_failure=lambda: failures.append(None))

    decoder.decode(BEN_SSRC, b"b")
    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES)
    assert failures == [], "one stream is not every stream"

    fail(decoder, BEN_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES)
    assert failures == [None]

    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES * 2)
    assert failures == [None], "reported once per session, not once per run"


def test_a_stream_that_recovers_blocks_the_verdict_again() -> None:
    """A speaker who decodes anything is proof the channel is not dead."""
    factory = DecoderFactorySpy(bad=frozenset({BAD}))
    failures: list[None] = []
    decoder = ResilientOpusDecoder(factory=factory, on_decode_failure=lambda: failures.append(None))

    decoder.decode(BEN_SSRC, b"b")
    fail(decoder, ANNA_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES)
    decoder.decode(ANNA_SSRC, b"a")
    fail(decoder, BEN_SSRC, FAILING_AFTER_CONSECUTIVE_FAILURES)

    assert failures == [], "Anna decoded a frame, so not everything is failing"


# --- the incident, against real libopus ---


def _tone(samples: int = 960, hz: float = 440.0) -> bytes:
    """48 kHz 16-bit stereo, the shape Discord's encoder expects."""
    frames = []
    for index in range(samples):
        value = int(8000 * math.sin(2 * math.pi * hz * index / 48_000))
        frames.append(struct.pack("<hh", value, value))
    return b"".join(frames)


def test_real_libopus_survives_a_corrupt_frame_and_keeps_decoding() -> None:
    """The production incident, reproduced and contained.

    `OpusError(-4)` -- literally "corrupted stream" -- is what ended a
    real recording: the library let it out of `_decode_packet`, it reached
    `PacketRouter.run()`, and the router thread exited in its `finally`.
    Here the same error is raised by the same libopus call and costs
    exactly one frame.

    Also pins that nothing assumes a fixed frame size: a 20 ms frame
    decodes to 3840 bytes, but `b"\\x00"` decodes to 1920 (10 ms), and the
    writer derives sample counts from `len(pcm)` for that reason.
    """
    from discord.opus import Encoder

    encoder = Encoder()
    good = [encoder.encode(_tone(hz=440 + step * 20), 960) for step in range(4)]
    frames = [good[0], b"garbage-not-opus", good[1], good[2], b"\xff" * 8, good[3]]

    decoder = ResilientOpusDecoder()
    results = [decoder.decode(ANNA_SSRC, frame) for frame in frames]

    assert [result is None for result in results] == [
        False,
        True,
        False,
        False,
        True,
        False,
    ]
    assert {len(result) for result in results if result is not None} == {3840}


def test_real_libopus_decodes_a_discord_silence_frame() -> None:
    """`OPUS_SILENCE` is decoded, not skipped.

    Three bytes through libopus is free, and skipping them would
    desynchronise the decoder's last-packet duration, which packet-loss
    concealment reads to size the frame it synthesises. This closes open
    question 1 in `docs/verification/voice-receive-spike.md`.
    """
    from discord.ext.voice_recv.rtp import OPUS_SILENCE

    decoder = ResilientOpusDecoder()
    pcm = decoder.decode(ANNA_SSRC, OPUS_SILENCE)

    assert pcm is not None
    assert len(pcm) == 3840
    assert set(pcm) == {0}
