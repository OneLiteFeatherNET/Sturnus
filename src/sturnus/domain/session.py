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

    def retime(self, timeouts: SessionTimeouts) -> None:
        """Swaps the timeouts in force, valid in *any* state, mid-session included.

        Safe because of one invariant, which anything added to
        `SessionTimeouts` must preserve: the timeouts are read **only**
        inside `_due_reason`, which runs on the next `tick()`/`due_reason()`
        and nowhere else. Nothing is captured at construction, no
        transition already taken is revisited, and no bookkeeping
        (`started_at`, `_last_audio_at`, `_grace_since`) is touched -- a
        swap therefore changes the *next* decision and nothing in flight.
        A future field that is captured at construction time would break
        that guarantee and must not be added without revisiting this.

        Shortening a timeout below the running session's elapsed time is
        deliberately allowed: the session then closes on the very next
        `tick()`, but through the ordinary path -- `tick()` reports a
        reason, and the caller's `close()` still encrypts, uploads and
        enqueues everything recorded so far. A recording is never
        discarded by a configuration change, only ended earlier.
        """
        self._timeouts = timeouts

    def due_reason(self, now: datetime) -> EndReason | None:
        """Reports what `tick()` would decide right now, without deciding it.

        Pure: no state changes, so a caller can ask "would this close the
        session?" -- e.g. an admin command reporting that the value just
        set is already exceeded -- without closing it as a side effect.
        """
        _require_aware(now)
        if self.state in (SessionState.IDLE, SessionState.CLOSING):
            return None
        return self._due_reason(now)

    def tick(self, now: datetime) -> EndReason | None:
        """Checks the time conditions. Returns the reason once the session closes.

        Reports each closure exactly once; further calls return None.
        """
        reason = self.due_reason(now)
        if reason is None:
            return None
        self.state = SessionState.CLOSING
        self.end_reason = reason
        return reason

    def end_now(self, reason: EndReason) -> None:
        """Moves a running session to CLOSING for a reason the clock never reports.

        `tick()` is the only other way into CLOSING, and it can only ever
        decide one of its own timeout reasons. Two reasons are genuinely
        external and unobservable from in here: `SIGTERM` (Spec 6.4), and
        an administrator ending the recording deliberately so a deferred
        channel or role change can land now (`/config apply force:true`).
        This is their edge into the same state, so both leave the machine
        exactly where a timeout would have -- which is what makes the
        ordinary `close()` -> `reset()` sequence, and with it a further
        session on the same objects, usable for them too. Without it a
        caller could only call `close()`, leaving the machine in RECORDING
        and `reset()`'s guard to fire.

        Idempotent, and a no-op when there is no session to end: CLOSING
        keeps the reason already decided, and IDLE has nothing to close.
        Leaving CLOSING is `reset()`'s job alone, here as everywhere.
        """
        if self.state is SessionState.IDLE or self.state is SessionState.CLOSING:
            return
        self.state = SessionState.CLOSING
        self.end_reason = reason

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

        The reason may equally have come from `end_now()` rather than
        `tick()` -- both leave CLOSING behind them, deliberately, so that
        an externally ended session is as reusable as a timed-out one.
        """
        assert self.state is SessionState.CLOSING, (
            "reset() must only follow a reason reported by tick() or end_now()"
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
