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

import math
import struct

import pytest
from discord.opus import OpusError, OpusNotLoaded

from sturnus.domain.stream_health import DecodePolicy, StreamState, StreamStats
from sturnus.infrastructure.discord.decoding import (
    ResilientOpusDecoder,
    SpeakerDecoder,
    new_opus_decoder,
)

ANNA_SSRC, BEN_SSRC = 111, 222
CORRUPTED_STREAM = -4

#: Small enough to keep the tests readable, ordered the same way the real
#: policy is so nothing about the escalation shape is special-cased away.
FAST = DecodePolicy(
    degraded_after_consecutive=3,
    unusable_after_consecutive=6,
    never_decoded_after=4,
    conceal_max_consecutive=2,
    recycle_attempts=1,
)


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


class TransitionLog:
    """Collects `StreamStateListener` calls."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, StreamState]] = []

    def __call__(self, ssrc: int, state: StreamState, stats: StreamStats) -> None:
        del stats
        self.entries.append((ssrc, state))

    def states(self, ssrc: int) -> list[StreamState]:
        return [state for seen, state in self.entries if seen == ssrc]


# --- the defect: a frame that will not decode is skipped, capture continues ---


def test_a_bad_frame_is_skipped_and_the_next_frame_still_decodes() -> None:
    """The whole point. One unreadable frame must cost one frame, not the session."""
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)

    results = [
        decoder.decode(ANNA_SSRC, frame)
        for frame in (b"a", b"corrupt", b"b", b"corrupt", b"c", b"d")
    ]

    assert results == [
        b"pcm:d0:a",
        None,
        b"pcm:d0:b",
        None,
        b"pcm:d0:c",
        b"pcm:d0:d",
    ]
    # And on the same decoder instance, with no reset in between.
    assert len(factory.built) == 1


# --- per-speaker isolation ---


def test_two_speakers_never_share_a_decoder() -> None:
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)

    anna = decoder.decode(ANNA_SSRC, b"a")
    ben = decoder.decode(BEN_SSRC, b"a")
    anna_again = decoder.decode(ANNA_SSRC, b"a")

    assert len(factory.built) == 2
    assert anna == b"pcm:d0:a"
    assert ben == b"pcm:d1:a"
    assert anna_again == b"pcm:d0:a"


def test_one_speaker_failing_leaves_every_other_speaker_untouched() -> None:
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    transitions = TransitionLog()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST, on_state_change=transitions)
    decoder.decode(ANNA_SSRC, b"a")
    decoder.decode(BEN_SSRC, b"a")

    for _ in range(FAST.unusable_after_consecutive):
        assert decoder.decode(ANNA_SSRC, b"corrupt") is None
        assert decoder.decode(BEN_SSRC, b"b") == b"pcm:d1:b"

    assert transitions.states(BEN_SSRC) == []
    assert decoder.stats()[BEN_SSRC].state is StreamState.HEALTHY


# --- lifecycle ---


def test_dropping_a_stream_forgets_its_decoder() -> None:
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)
    decoder.decode(ANNA_SSRC, b"a")

    decoder.drop(ANNA_SSRC)
    assert decoder.stats() == {}

    assert decoder.decode(ANNA_SSRC, b"a") == b"pcm:d1:a"


def test_clear_forgets_everything_and_is_idempotent() -> None:
    decoder = ResilientOpusDecoder(factory=DecoderFactorySpy(), policy=FAST)
    decoder.decode(ANNA_SSRC, b"a")
    decoder.decode(BEN_SSRC, b"a")

    decoder.clear()
    decoder.clear()

    assert decoder.stats() == {}


def test_the_max_streams_backstop_evicts_the_least_recently_used() -> None:
    """An alpha library missing one disconnect event must not become a leak."""
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST, max_streams=2)

    decoder.decode(1, b"a")
    decoder.decode(2, b"a")
    decoder.decode(1, b"a")  # 1 is now the most recently used
    decoder.decode(3, b"a")

    assert set(decoder.stats()) == {1, 3}


# --- escalation ---


def test_degraded_fires_once_however_long_the_failure_run_lasts() -> None:
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    transitions = TransitionLog()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST, on_state_change=transitions)
    decoder.decode(ANNA_SSRC, b"a")

    for _ in range(FAST.degraded_after_consecutive):
        decoder.decode(ANNA_SSRC, b"corrupt")
    assert transitions.states(ANNA_SSRC) == [StreamState.DEGRADED]

    for _ in range(FAST.unusable_after_consecutive - FAST.degraded_after_consecutive - 1):
        decoder.decode(ANNA_SSRC, b"corrupt")
    assert transitions.states(ANNA_SSRC) == [StreamState.DEGRADED]


def test_unusable_rebuilds_the_decoder_exactly_once() -> None:
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    transitions = TransitionLog()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST, on_state_change=transitions)
    decoder.decode(ANNA_SSRC, b"a")
    assert len(factory.built) == 1

    for _ in range(FAST.unusable_after_consecutive):
        decoder.decode(ANNA_SSRC, b"corrupt")
    assert transitions.states(ANNA_SSRC) == [StreamState.DEGRADED, StreamState.UNUSABLE]
    assert len(factory.built) == 2, "one fresh decoder for the wedged stream"

    # A second run of failures escalates again -- it stays visible -- but
    # the rebuild budget is spent, so we do not retry forever.
    for _ in range(FAST.unusable_after_consecutive):
        decoder.decode(ANNA_SSRC, b"corrupt")
    assert transitions.states(ANNA_SSRC).count(StreamState.UNUSABLE) == 2
    assert len(factory.built) == 2


def test_never_decoded_is_reported_instead_of_degraded_and_rebuilds_nothing() -> None:
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    transitions = TransitionLog()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST, on_state_change=transitions)

    for _ in range(FAST.never_decoded_after):
        decoder.decode(ANNA_SSRC, b"corrupt")

    assert transitions.states(ANNA_SSRC) == [StreamState.NEVER_DECODED]
    assert len(factory.built) == 1


def test_total_failure_fires_once_and_only_when_nothing_decodes_anywhere() -> None:
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    fired: list[None] = []
    decoder = ResilientOpusDecoder(
        factory=factory, policy=FAST, on_total_failure=lambda: fired.append(None)
    )

    # Anna's stream dies completely while Ben's keeps working.
    decoder.decode(BEN_SSRC, b"a")
    for _ in range(FAST.unusable_after_consecutive * 3):
        decoder.decode(ANNA_SSRC, b"corrupt")
        decoder.decode(BEN_SSRC, b"b")
    assert fired == [], "one dead speaker is never a session failure"

    # Now Ben's stream dies too.
    for _ in range(FAST.unusable_after_consecutive * 3):
        decoder.decode(BEN_SSRC, b"corrupt")

    assert fired == [None], "reported once, not once per frame"


# --- packet loss ---


@pytest.mark.parametrize("lost", [None, b""])
def test_a_lost_frame_takes_the_concealment_path(lost: bytes | None) -> None:
    """`FakePacket.decrypted_data` is `b""`, and `decode(b"")` raises -1.

    Empty must therefore never reach `decode()`; it is loss, and loss is
    what packet-loss concealment is for.
    """
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)
    decoder.decode(ANNA_SSRC, b"a")

    assert decoder.decode(ANNA_SSRC, lost) == b"plc:d0"
    assert factory.built[0].concealed == 1


def test_concealment_stops_after_the_cap_and_resumes_after_a_success() -> None:
    factory = DecoderFactorySpy()
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)
    decoder.decode(ANNA_SSRC, b"a")

    results = [decoder.decode(ANNA_SSRC, None) for _ in range(FAST.conceal_max_consecutive + 2)]
    assert results == [b"plc:d0"] * FAST.conceal_max_consecutive + [None, None]

    decoder.decode(ANNA_SSRC, b"b")
    assert decoder.decode(ANNA_SSRC, None) == b"plc:d0"


def test_a_decode_failure_is_never_concealed() -> None:
    """We conceal what the network lost, never what our decoder could not read.

    Inventing audio to paper over a decoder error would put synthesised
    sound into a file people were told is a record of what they said.
    """
    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)
    decoder.decode(ANNA_SSRC, b"a")

    assert decoder.decode(ANNA_SSRC, b"corrupt") is None
    assert factory.built[0].concealed == 0


# --- containment ---


def test_an_unexpected_decoder_error_never_escapes() -> None:
    class Exploding:
        def decode(self, data: bytes | None, *, fec: bool = False) -> bytes:  # noqa: ARG002
            raise MemoryError("not an OpusError")

    decoder = ResilientOpusDecoder(factory=Exploding, policy=FAST)

    assert decoder.decode(ANNA_SSRC, b"a") is None


def test_a_factory_that_raises_never_escapes() -> None:
    def factory() -> object:
        raise OpusNotLoaded

    decoder = ResilientOpusDecoder(factory=factory, policy=FAST)  # type: ignore[arg-type]

    assert decoder.decode(ANNA_SSRC, b"a") is None


def test_a_listener_that_raises_never_escapes() -> None:
    def boom(ssrc: int, state: StreamState, stats: StreamStats) -> None:  # noqa: ARG001
        raise RuntimeError("listener bug")

    factory = DecoderFactorySpy(bad=frozenset({b"corrupt"}))
    decoder = ResilientOpusDecoder(factory=factory, policy=FAST, on_state_change=boom)
    decoder.decode(ANNA_SSRC, b"a")

    for _ in range(FAST.degraded_after_consecutive):
        assert decoder.decode(ANNA_SSRC, b"corrupt") is None


def test_a_rebuild_that_fails_spends_the_budget_instead_of_retrying_forever() -> None:
    class OneShotFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> FakeFrameDecoder:
            self.calls += 1
            if self.calls > 1:
                raise OpusNotLoaded
            return FakeFrameDecoder("d0", frozenset({b"corrupt"}))

    factory = OneShotFactory()
    speaker = SpeakerDecoder(ANNA_SSRC, factory, FAST)
    speaker.decode(b"a")
    for _ in range(FAST.unusable_after_consecutive):
        speaker.decode(b"corrupt")

    assert speaker.may_recycle is True
    assert speaker.recycle() is False
    assert speaker.may_recycle is False


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

    stats = decoder.stats()[ANNA_SSRC]
    assert stats.frames_decoded == 4
    assert stats.frames_discarded == 2
    assert stats.last_error_code == CORRUPTED_STREAM
    assert stats.state is StreamState.HEALTHY, "isolated failures are not an escalation"


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
