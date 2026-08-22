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
import threading
import time

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


def test_a_recording_with_no_video_at_all_still_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reversal of the first version, and the reason for it.

    Staying silent was meant to keep noise down. It hid the actual
    finding: a live share produced no announcement, no packets and no log
    line, which is indistinguishable from the probe being off, the switch
    not being set, or the deployment not carrying the probe at all.
    """
    probe = VideoProbe()
    probe.note_request("op15-any", sent=True)
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "0 stream(s) announced" in text
    assert "did not announce a single video stream" in text


def test_a_build_that_never_asked_says_so_rather_than_blaming_discord(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two states this probe must never conflate.

    "We asked and Discord sent nothing" is a finding about Discord.
    "Nothing was sent" is a finding about this deployment, and reading
    the second as the first is how a week gets spent on the wrong
    protocol.
    """
    with caplog.at_level(logging.WARNING):
        VideoProbe().report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "does not ask for video at all" in text
    assert "nothing can be concluded" in text


def test_a_request_that_failed_to_send_is_not_reported_as_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A websocket that refused the payload is the same silence as a
    server that ignored it, and only this distinguishes them."""
    probe = VideoProbe()
    probe.note_request("op12-video", sent=False)
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "op12-video=FAILED" in text
    assert "nothing can be concluded" in text


def test_packets_arriving_is_reported_as_the_green_light(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one outcome that turns this from a question into a project."""
    probe = VideoProbe()
    probe.note_request("op15-any", sent=True)
    _announced(probe)
    probe.observe(SCREEN_SSRC, 1300)
    with caplog.at_level(logging.WARNING):
        probe.report()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Video packets are reaching this bot" in text
    assert "consent role" in text


def test_it_reports_on_a_timer_rather_than_only_at_the_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A two-hour meeting must not have to end before the answer appears.

    The first version reported only from `cleanup()`, so an operator
    testing a share had to leave the call to learn anything.
    """
    probe = VideoProbe(report_every=0.01)
    probe.note_request("op15-any", sent=True)
    with caplog.at_level(logging.WARNING):
        probe.start()
        deadline = time.monotonic() + 2.0
        while not caplog.records and time.monotonic() < deadline:
            time.sleep(0.01)
        probe.clear()

    assert caplog.records, "the timer never reported"


def test_clear_stops_the_timer() -> None:
    """`clear()` runs on the sink-cleanup path, which the garbage
    collector can reach; a probe thread outliving its capture would report
    against a recording that is over."""
    probe = VideoProbe(report_every=0.01)
    probe.start()
    probe.clear()
    assert probe._timer is None
    time.sleep(0.05)
    assert not any(t.name == "video-probe" and t.is_alive() for t in threading.enumerate())


@pytest.mark.parametrize(
    ("size", "band"),
    [(50, "<100"), (250, "<300"), (650, "<700"), (1100, "<1200"), (1400, ">=1200")],
)
def test_sizes_are_reported_as_bands(size: int, band: str) -> None:
    """Video packets run to the MTU and audio does not, so the band alone
    distinguishes them."""
    assert size_band(size) == band
