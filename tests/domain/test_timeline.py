# tests/domain/test_timeline.py
from datetime import datetime, timedelta, timezone

import pytest

from sturnus.domain.timeline import RTP_CLOCK_HZ, SpeakerClock

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
SSRC = 111


def test_first_packet_defines_the_reference() -> None:
    clock = SpeakerClock()
    assert clock.absolute_time(SSRC, 5_000_000, T0) == T0


def test_later_packet_uses_rtp_delta_not_wall_clock() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 5_000_000, T0)
    # one second in RTP ticks, but the wall clock claims 30 seconds
    later = clock.absolute_time(SSRC, 5_000_000 + RTP_CLOCK_HZ, T0 + timedelta(seconds=30))
    assert later == T0 + timedelta(seconds=1)


def test_silence_gap_is_reconstructed_from_timestamps() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 1_000, T0)
    # five minutes of silence: no packets arrived, the timestamp jumps
    resumed = clock.absolute_time(SSRC, 1_000 + RTP_CLOCK_HZ * 300, T0 + timedelta(minutes=99))
    assert resumed == T0 + timedelta(minutes=5)


def test_separate_ssrcs_keep_separate_references() -> None:
    clock = SpeakerClock()
    clock.absolute_time(111, 7_000, T0)
    other = clock.absolute_time(222, 9_999_999, T0 + timedelta(seconds=10))
    assert other == T0 + timedelta(seconds=10)


def test_reconnect_with_new_ssrc_starts_new_reference() -> None:
    clock = SpeakerClock()
    clock.absolute_time(111, 1_000, T0)
    reconnected = clock.absolute_time(333, 42, T0 + timedelta(minutes=2))
    assert reconnected == T0 + timedelta(minutes=2)


def test_timestamp_wraparound_is_handled() -> None:
    clock = SpeakerClock()
    start = 2**32 - RTP_CLOCK_HZ  # one second before the overflow
    clock.absolute_time(SSRC, start, T0)
    wrapped = clock.absolute_time(SSRC, RTP_CLOCK_HZ, T0 + timedelta(seconds=99))
    assert wrapped == T0 + timedelta(seconds=2)


def test_reset_drops_the_reference() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 1_000, T0)
    clock.reset(SSRC)
    again = clock.absolute_time(SSRC, 500_000, T0 + timedelta(minutes=1))
    assert again == T0 + timedelta(minutes=1)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SpeakerClock().absolute_time(SSRC, 1, datetime(2026, 8, 19))
