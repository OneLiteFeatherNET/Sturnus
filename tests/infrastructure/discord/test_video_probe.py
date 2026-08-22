"""Does Discord send a bot the video it announces?

The probe exists to answer that before anybody writes an H.264
depacketiser, and its only job is to keep two counts apart: streams the
gateway announced, and packets that actually arrived on them. A probe that
conflated the two would answer "yes, video works" on the strength of a
gateway message alone -- which is exactly the mistake that would cost
weeks.
"""

from __future__ import annotations

import logging

import pytest

from sturnus.infrastructure.discord.video_probe import VideoProbe, size_band

SCREEN_SSRC = 5001
ANNA = 100


def _announced(probe: VideoProbe, ssrc: int = SCREEN_SSRC, kind: str = "screen") -> None:
    probe.announce(ssrc=ssrc, discord_user_id=ANNA, kind=kind, active=True, resolution="1920x1080")


def test_an_announced_stream_that_never_arrives_is_reported_as_such(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The finding that would stop the project, and the one most worth
    getting right: Discord announcing a stream is not Discord sending it."""
    probe = VideoProbe()
    _announced(probe)
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "1 stream(s) announced, 0 delivered any packet" in text
    assert "packets=0" in text


def test_packets_on_an_announced_stream_are_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The finding that would green-light it."""
    probe = VideoProbe()
    _announced(probe)
    for size in (1100, 1150, 400):
        assert probe.observe(SCREEN_SSRC, size) is True
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "1 stream(s) announced, 1 delivered any packet" in text
    assert "packets=3" in text
    assert "<1200:2" in text and "<700:1" in text


def test_a_packet_on_an_unknown_ssrc_is_not_claimed_as_video() -> None:
    """Audio from somebody the bot cannot attribute looks identical from
    inside the sink, and treating it as video would both lose the audio and
    fake the answer."""
    probe = VideoProbe()
    _announced(probe)
    assert probe.observe(9999, 200) is False


def test_packets_before_any_announcement_are_counted_separately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If video arrived before the gateway said so, the SSRC mapping would
    have to be built somewhere else entirely."""
    probe = VideoProbe()
    for _ in range(3):
        probe.note_unannounced()
    with caplog.at_level(logging.WARNING):
        probe.report()
    assert "packets on unannounced ssrcs: 3" in "\n".join(r.getMessage() for r in caplog.records)


def test_the_kind_of_stream_is_kept_verbatim(caplog: pytest.LogCaptureFixture) -> None:
    """`screen` and `video` are what the consent model has to tell apart.

    Discord's own label is recorded rather than interpreted: a guess baked
    in here would be invisible later, and consent for a shared screen is
    not consent for a camera.
    """
    probe = VideoProbe()
    _announced(probe, kind="screen")
    _announced(probe, ssrc=SCREEN_SSRC + 1, kind="video")
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/screen" in text and "/video" in text


def test_a_stream_that_restarts_keeps_what_it_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Somebody stopping and restarting a share re-announces the SSRC.

    The question is whether anything ever arrived on it, so the counts
    survive; only the mutable description is refreshed.
    """
    probe = VideoProbe()
    _announced(probe)
    probe.observe(SCREEN_SSRC, 900)
    probe.announce(
        ssrc=SCREEN_SSRC, discord_user_id=ANNA, kind="screen", active=False, resolution="1280x720"
    )
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "packets=1" in text
    assert "(inactive)" in text and "1280x720" in text


def test_nothing_is_reported_when_no_video_happened(caplog: pytest.LogCaptureFixture) -> None:
    """Most recordings have no video at all, and a line saying so on every
    one of them is noise."""
    with caplog.at_level(logging.WARNING):
        VideoProbe().report()
    assert caplog.records == []


@pytest.mark.parametrize(
    ("size", "band"),
    [(50, "<100"), (250, "<300"), (650, "<700"), (1100, "<1200"), (1400, ">=1200")],
)
def test_sizes_are_reported_as_bands(size: int, band: str) -> None:
    """Video packets run to the MTU and audio does not, so the band alone
    distinguishes them."""
    assert size_band(size) == band
