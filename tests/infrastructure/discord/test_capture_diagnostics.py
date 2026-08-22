"""Does the instrument answer the question it was built for?

It exists to separate two possibilities that a finished WAV cannot: the
Opus frames arriving from Discord are already noise, or Sturnus turns
speech into noise afterwards. So the tests below feed it known speech and
known noise and check that it says which is which -- an instrument that
reports the same numbers for both would send the next investigation down
the same blind alley as the last one.

The signal shapes here are the ones measured on the four degraded
production tracks: autocorrelation 0.21-0.26 against clean speech at
0.4-0.8, and a mean sample step of about 0.44 of RMS.
"""

from __future__ import annotations

import logging
import math

import pytest

from sturnus.infrastructure.discord.capture_diagnostics import (
    CaptureDiagnostics,
    PacketReader,
    size_bucket,
)

RATE = 48_000
FRAME_SAMPLES = 960
HEALTHY_SHAPE = (1, 960, 2)


class FakeReader(PacketReader):
    """Stands in for libopus' TOC parsing, so no codec is needed."""

    def __init__(self, shape: tuple[int, int, int] | None = HEALTHY_SHAPE) -> None:
        self.shape_to_return = shape
        self.seen: list[int] = []

    def shape(self, frame: bytes) -> tuple[int, int, int] | None:
        self.seen.append(len(frame))
        return self.shape_to_return


def _stereo(samples: list[float]) -> bytes:
    """One frame of 48 kHz 16-bit stereo, as the decoder returns it."""
    out = bytearray()
    for value in samples:
        clamped = max(-32768, min(32767, int(value)))
        out += int(clamped).to_bytes(2, "little", signed=True) * 2
    return bytes(out)


def _voiced(seconds: float = FRAME_SAMPLES / RATE, f0: float = 120.0) -> bytes:
    """A harmonic stack: what a vowel looks like to an autocorrelation."""
    n = int(RATE * seconds)
    samples = [
        8000 * sum(math.sin(2 * math.pi * f0 * k * i / RATE) / k for k in range(1, 6))
        for i in range(n)
    ]
    return _stereo(samples)


def _noise(seconds: float = FRAME_SAMPLES / RATE) -> bytes:
    """Deterministic broadband noise -- no pitch to find at any lag."""
    n = int(RATE * seconds)
    state = 12345
    samples = []
    for _ in range(n):
        state = (1103515245 * state + 12345) % (2**31)
        samples.append((state / (2**31) - 0.5) * 16000)
    return _stereo(samples)


def _numbers(caplog: pytest.LogCaptureFixture) -> dict[str, float]:
    """The reported line, parsed back into the numbers it carries."""
    text = "\n".join(r.getMessage() for r in caplog.records)
    out: dict[str, float] = {}
    for key in ("autocorr", "zcr", "step/rms", "rms", "peak"):
        marker = f"{key}="
        if marker in text:
            out[key] = float(text.split(marker)[1].split()[0].rstrip("|"))
    return out


def _run(pcm: bytes, *, frames: int = 3) -> CaptureDiagnostics:
    """Pushes `frames` packets through, every one of them sampled."""
    diagnostics = CaptureDiagnostics(
        sample_every=1, report_every=10_000, packet_reader=FakeReader()
    )
    for _ in range(frames):
        diagnostics.observe_packet(1, b"\xfc" + b"\x00" * 60)
        diagnostics.observe_pcm(1, pcm)
    return diagnostics


# ---------------------------------------------------------------------------
# The one thing it has to get right
# ---------------------------------------------------------------------------


def test_speech_and_noise_do_not_report_the_same_numbers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point. If these agreed, the instrument would be useless."""
    with caplog.at_level(logging.WARNING):
        _run(_voiced()).report()
        speech = _numbers(caplog)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _run(_noise()).report()
        noise = _numbers(caplog)

    assert speech["autocorr"] > 0.4, f"voiced audio read as {speech['autocorr']}"
    assert noise["autocorr"] < 0.25, f"noise read as {noise['autocorr']}"
    assert speech["zcr"] < noise["zcr"]
    assert speech["step/rms"] < noise["step/rms"]


def test_a_healthy_packet_shape_is_reported_as_the_only_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One 20 ms stereo frame per packet is what Discord sends.

    Anything else means the bytes reaching the decoder are not the packet
    that was sent -- a header left on, a payload cut short, an offset out
    by a few -- which is precisely the hypothesis this exists to test.
    """
    with caplog.at_level(logging.WARNING):
        _run(_voiced()).report()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "1f/960spf/2ch" in text
    assert "unexpected shape: 0" in text


def test_a_wrong_packet_shape_is_counted_as_unexpected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostics = CaptureDiagnostics(
        sample_every=1, report_every=10_000, packet_reader=FakeReader((1, 480, 1))
    )
    for _ in range(4):
        diagnostics.observe_packet(1, b"\x00" * 50)
    with caplog.at_level(logging.WARNING):
        diagnostics.report()
    assert "unexpected shape: 4" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_packet_libopus_cannot_parse_at_all_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostics = CaptureDiagnostics(
        sample_every=1, report_every=10_000, packet_reader=FakeReader(None)
    )
    for _ in range(3):
        diagnostics.observe_packet(1, b"\x00" * 50)
    with caplog.at_level(logging.WARNING):
        diagnostics.report()
    assert "unreadable: 3" in "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# What it must never do
# ---------------------------------------------------------------------------


def test_nothing_it_reports_could_be_reassembled_into_audio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It runs against real conversations, so this is not a style rule.

    Sizes, counts and aggregates only: no sample, no frame, and nothing
    per-packet that a reader could line back up into speech.
    """
    with caplog.at_level(logging.WARNING):
        _run(_voiced(), frames=20).report()
    text = "\n".join(r.getMessage() for r in caplog.records)
    # One line per stream, whatever the packet count.
    assert len(caplog.records) == 1
    # It reports how many packets fell in each size band, never a sequence.
    assert "sizes:" in text and "<80:" in text


def test_it_survives_a_frame_too_short_to_measure() -> None:
    """Short reads happen; a diagnostic that raises would stop capture."""
    diagnostics = CaptureDiagnostics(sample_every=1, packet_reader=FakeReader())
    diagnostics.observe_packet(1, b"\x00" * 8)
    diagnostics.observe_pcm(1, b"\x00\x01")  # one byte short of a sample pair
    diagnostics.report()


def test_a_silent_frame_is_counted_rather_than_measured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence has no pitch, and averaging its zero into the autocorrelation
    would drag a healthy stream's number below the threshold it is read
    against."""
    with caplog.at_level(logging.WARNING):
        _run(_stereo([0.0] * FRAME_SAMPLES), frames=5).report()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "(5 silent)" in text
    assert "autocorr=0.000" in text


def test_a_departing_speaker_is_reported_once_and_forgotten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostics = _run(_voiced())
    with caplog.at_level(logging.WARNING):
        diagnostics.drop(1)
        diagnostics.drop(1)
    assert len([r for r in caplog.records if "final" in r.getMessage()]) == 1


@pytest.mark.parametrize(
    ("size", "band"),
    [(0, "0"), (5, "<10"), (35, "<40"), (79, "<80"), (200, "<320"), (1500, ">=640")],
)
def test_packet_sizes_are_reported_as_bands(size: int, band: str) -> None:
    assert size_bucket(size) == band


# ---------------------------------------------------------------------------
# Finding where the real packet starts
# ---------------------------------------------------------------------------


class ShiftedReader(PacketReader):
    """A stream whose packets are preceded by `pad` bytes of something else.

    Reads as the expected shape only when those bytes are skipped, which is
    exactly what a capture reading `1f/480spf/1ch` one packet and
    `2f/960spf/2ch` the next looks like: not a damaged Opus stream, but
    bytes that are not an Opus packet being parsed as one.
    """

    def __init__(self, pad: int) -> None:
        self.pad = pad

    def shape(self, frame: bytes) -> tuple[int, int, int] | None:
        if not frame:
            return None
        if frame[:1] == b"\xff" * min(1, self.pad) and self.pad:
            # Still standing on the padding.
            return (2, 480, 1)
        return HEALTHY_SHAPE


def test_the_offset_that_reads_correctly_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The measurement that turns "the packets are wrong" into a fix.

    Reading the TOC at each small offset and counting which one yields the
    shape Discord actually sends names the number of bytes standing in
    front of the real packet -- and that number is the bug.
    """
    pad = 4
    diagnostics = CaptureDiagnostics(
        sample_every=1, report_every=10_000, packet_reader=ShiftedReader(pad)
    )
    for _ in range(6):
        diagnostics.observe_packet(1, b"\xff" * pad + b"\xfc" + b"\x11" * 40)
    with caplog.at_level(logging.WARNING):
        diagnostics.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"reads correctly at offset: +{pad}:6" in text, text


def test_a_healthy_stream_costs_no_offset_scan(caplog: pytest.LogCaptureFixture) -> None:
    """17 extra parses per packet for an answer nobody needs would be a
    real cost on the capture thread, so the scan runs only once something
    is already wrong."""
    with caplog.at_level(logging.WARNING):
        _run(_voiced(), frames=5).report()
    assert "reads correctly at offset: none" in "\n".join(r.getMessage() for r in caplog.records)


def test_only_a_few_leading_bytes_of_a_broken_packet_are_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structure, never content.

    Four bytes is a TOC byte and the start of a compressed payload -- far
    too little to reconstruct anything audible, and exactly enough to see
    whether an RTP extension is still sitting in front of the packet.
    """
    diagnostics = CaptureDiagnostics(
        sample_every=1, report_every=10_000, packet_reader=ShiftedReader(2)
    )
    for _ in range(40):
        diagnostics.observe_packet(1, b"\xff\xff" + bytes(range(64)))
    with caplog.at_level(logging.WARNING):
        diagnostics.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    recorded = text.split("first bytes: ")[1].split(" | ")[0].split()
    assert len(recorded) <= 8, "recorded more packets than the cap allows"
    assert all(len(sample) == 8 for sample in recorded), "recorded more than four bytes each"


# ---------------------------------------------------------------------------
# The arithmetic that cut the payload out of the packet
# ---------------------------------------------------------------------------


def _rtp(diagnostics: CaptureDiagnostics, **over: object) -> None:
    """One RTP observation, with a healthy packet as the default."""
    call: dict[str, object] = {
        "extended": True,
        "csrc_count": 0,
        "extension_words": 1,
        # 200 bytes of Opus, a 16-byte AEAD tag and one 4-word... one
        # 4-byte extension word inside the ciphertext.
        "body_bytes": 200 + 16 + 4,
        "payload_bytes": 200,
    }
    call.update(over)
    diagnostics.observe_rtp(1, **call)  # type: ignore[arg-type]


def test_a_trim_the_header_explains_is_not_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """Tag plus extension words is the whole of it.

    With a 16-byte AEAD tag and an `n`-word extension whose data sits
    inside the ciphertext, body minus payload must be exactly `16 + 4n`.
    """
    diagnostics = CaptureDiagnostics(packet_reader=FakeReader())
    for _ in range(5):
        _rtp(diagnostics)
    with caplog.at_level(logging.WARNING):
        diagnostics.report()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "trims the header cannot explain: 0" in text
    assert "payload trimmed by: 20B x5" in text


def test_a_trim_the_header_cannot_explain_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The finding this measurement exists for.

    Removing the wrong number of bytes leaves the remainder starting in
    the middle of the packet -- which is read as a random TOC byte, decodes
    without error, and comes out as noise. Exactly the production symptom.
    """
    diagnostics = CaptureDiagnostics(packet_reader=FakeReader())
    for _ in range(4):
        _rtp(diagnostics, payload_bytes=196)  # four bytes too few
    with caplog.at_level(logging.WARNING):
        diagnostics.report()
    assert "trims the header cannot explain: 4" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_packet_with_no_extension_is_trimmed_by_the_tag_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostics = CaptureDiagnostics(packet_reader=FakeReader())
    for _ in range(3):
        _rtp(diagnostics, extended=False, extension_words=0, body_bytes=216, payload_bytes=200)
    with caplog.at_level(logging.WARNING):
        diagnostics.report()
    assert "trims the header cannot explain: 0" in "\n".join(r.getMessage() for r in caplog.records)


def test_the_rtp_shape_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """A CSRC count above zero would move where the payload begins, so it
    is reported rather than assumed away."""
    diagnostics = CaptureDiagnostics(packet_reader=FakeReader())
    for _ in range(6):
        _rtp(diagnostics, csrc_count=2)
    with caplog.at_level(logging.WARNING):
        diagnostics.report()
    assert "ext=y/cc=2/words=1 x6" in "\n".join(r.getMessage() for r in caplog.records)
