"""State machine for a recording session.

Knows nothing about Discord or the database; time is passed in on every
call so that all transitions are deterministically testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class SessionState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    GRACE = "grace"
    CLOSING = "closing"


class EndReason(StrEnum):
    EMPTY = "empty"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"


@dataclass(frozen=True)
class SessionTimeouts:
    empty_grace_seconds: int = 60
    idle_timeout_minutes: int = 15
    max_session_hours: int = 4


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value


class SessionMachine:
    def __init__(self, timeouts: SessionTimeouts) -> None:
        self._timeouts = timeouts
        self.state: SessionState = SessionState.IDLE
        self.started_at: datetime | None = None
        self.end_reason: EndReason | None = None
        self._last_audio_at: datetime | None = None
        self._grace_since: datetime | None = None

    def participants_changed(self, consented_count: int, now: datetime) -> None:
        """Reports how many consenting participants are in the channel."""
        _require_aware(now)
        if self.state is SessionState.CLOSING:
            return
        if consented_count > 0:
            if self.state is SessionState.IDLE:
                self.started_at = now
                self._last_audio_at = now
            self.state = SessionState.RECORDING
            self._grace_since = None
        elif self.state is SessionState.RECORDING:
            self.state = SessionState.GRACE
            self._grace_since = now

    def audio_received(self, now: datetime) -> None:
        _require_aware(now)
        self._last_audio_at = now

    def tick(self, now: datetime) -> EndReason | None:
        """Checks the time conditions. Returns the reason once the session closes.

        Reports each closure exactly once; further calls return None.
        """
        _require_aware(now)
        if self.state in (SessionState.IDLE, SessionState.CLOSING):
            return None
        assert self.started_at is not None

        reason = self._due_reason(now)
        if reason is None:
            return None
        self.state = SessionState.CLOSING
        self.end_reason = reason
        return reason

    def _due_reason(self, now: datetime) -> EndReason | None:
        assert self.started_at is not None
        if now - self.started_at > timedelta(hours=self._timeouts.max_session_hours):
            return EndReason.MAX_DURATION
        if (
            self._grace_since is not None
            and now - self._grace_since > timedelta(seconds=self._timeouts.empty_grace_seconds)
        ):
            return EndReason.EMPTY
        if (
            self._last_audio_at is not None
            and now - self._last_audio_at > timedelta(minutes=self._timeouts.idle_timeout_minutes)
        ):
            return EndReason.IDLE_TIMEOUT
        return None
