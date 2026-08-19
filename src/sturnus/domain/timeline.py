"""Converts RTP timestamps into absolute time.

Discord sends no packets during silence, so the position of a speech
segment can't be derived from arrival time. The RTP timestamp, however,
keeps running gap-free at 48 kHz.
"""

from __future__ import annotations

from datetime import datetime, timedelta

RTP_CLOCK_HZ = 48_000
_RTP_MODULO = 2**32


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value


class SpeakerClock:
    """Holds, per SSRC, the reference point from the first packet and wall-clock time."""

    def __init__(self) -> None:
        self._references: dict[int, tuple[datetime, int]] = {}

    def absolute_time(self, ssrc: int, rtp_timestamp: int, wall_now: datetime) -> datetime:
        _require_aware(wall_now)
        reference = self._references.get(ssrc)
        if reference is None:
            self._references[ssrc] = (wall_now, rtp_timestamp)
            return wall_now

        wall_first, rtp_first = reference
        # The counter is 32 bits wide and overflows after roughly 24.8 hours.
        # Modular arithmetic yields the correct difference even across the overflow.
        # However, RTP packets often arrive out of order on UDP: a late-arriving packet
        # has a smaller timestamp than a previously seen one. The modulo operation would
        # turn that small backwards step into a huge forward value (near 2**32).
        # Interpret the delta as signed: if it exceeds half the range (2**31 ≈ 12.4 hours),
        # it is unambiguously a backwards step, not a forward wraparound. This is safe
        # because legitimate forward jumps cannot exceed max_session_hours (default 4 hours),
        # which is well below the 12.4-hour threshold. A negative delta correctly positions
        # a late packet slightly before the reference time, which downstream code relies on
        # to maintain chronological order.
        delta_ticks = (rtp_timestamp - rtp_first) % _RTP_MODULO
        if delta_ticks > _RTP_MODULO // 2:
            delta_ticks -= _RTP_MODULO
        return wall_first + timedelta(seconds=delta_ticks / RTP_CLOCK_HZ)

    def reset(self, ssrc: int) -> None:
        """Discards the reference point, e.g. after a reconnection."""
        self._references.pop(ssrc, None)
