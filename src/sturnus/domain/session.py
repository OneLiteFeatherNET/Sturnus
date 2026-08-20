"""State machine for a recording session.

Knows nothing about Discord or the database; time is passed in on every
call so that all transitions are deterministically testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ._time import require_aware as _require_aware
from .timeline import MAX_SUPPORTED_SESSION_HOURS


class SessionState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    GRACE = "grace"
    CLOSING = "closing"


class EndReason(StrEnum):
    EMPTY = "empty"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"
    #: The process ended without going through `SessionMachine` at all --
    #: a hard kill, an evicted pod -- observed only after the fact, by
    #: `recover_orphans` scanning what was left on disk.
    CRASHED = "crashed"
    #: An orderly shutdown (e.g. `SIGTERM`) closed the session while it
    #: was still recording, before any of the machine's own timeouts fired.
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class SessionTimeouts:
    empty_grace_seconds: int = 60
    idle_timeout_minutes: int = 15
    max_session_hours: int = 4

    def __post_init__(self) -> None:
        if self.empty_grace_seconds <= 0:
            raise ValueError("empty_grace_seconds must be positive")
        if self.idle_timeout_minutes <= 0:
            raise ValueError("idle_timeout_minutes must be positive")
        if self.max_session_hours <= 0:
            raise ValueError("max_session_hours must be positive")
        if self.max_session_hours > MAX_SUPPORTED_SESSION_HOURS:
            raise ValueError(
                "max_session_hours must not exceed "
                f"{MAX_SUPPORTED_SESSION_HOURS} (RTP wraparound safety margin, "
                "see timeline.MAX_SUPPORTED_SESSION_HOURS)"
            )


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
            elif self.state is SessionState.GRACE:
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

    def reset(self) -> None:
        """Returns the machine from CLOSING to IDLE, ready for a new session.

        CLOSING is otherwise a dead end -- nothing in this class ever
        leaves it on its own, which is exactly what left a guild deaf
        forever after its first recorded session. This is the explicit
        exit edge: called once the caller has finished acting on the
        reason `tick()` reported (closing files, uploading, enqueuing),
        never automatically by `tick()` itself, since a caller that is
        shutting down for good (Spec 6.4's `SHUTDOWN` reason) must be free
        to leave the machine in CLOSING rather than implicitly offering a
        session that will never be recorded.
        """
        assert self.state is SessionState.CLOSING, (
            "reset() must only follow a reason reported by tick()"
        )
        self.state = SessionState.IDLE
        self.started_at = None
        self.end_reason = None
        self._last_audio_at = None
        self._grace_since = None

    def _due_reason(self, now: datetime) -> EndReason | None:
        assert self.started_at is not None
        if now - self.started_at > timedelta(hours=self._timeouts.max_session_hours):
            return EndReason.MAX_DURATION
        if self._grace_since is not None and now - self._grace_since > timedelta(
            seconds=self._timeouts.empty_grace_seconds
        ):
            return EndReason.EMPTY
        if self._last_audio_at is not None and now - self._last_audio_at > timedelta(
            minutes=self._timeouts.idle_timeout_minutes
        ):
            return EndReason.IDLE_TIMEOUT
        return None
