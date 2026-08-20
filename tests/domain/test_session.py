from datetime import UTC, datetime, timedelta

import pytest

from sturnus.domain.session import EndReason, SessionMachine, SessionState, SessionTimeouts

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def machine() -> SessionMachine:
    return SessionMachine(
        SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4)
    )


def test_starts_idle() -> None:
    assert machine().state is SessionState.IDLE


def test_first_consented_participant_starts_recording() -> None:
    m = machine()
    m.participants_changed(1, T0)
    assert m.state is SessionState.RECORDING
    assert m.started_at == T0


def test_participant_without_consent_does_not_start() -> None:
    m = machine()
    m.participants_changed(0, T0)
    assert m.state is SessionState.IDLE
    assert m.started_at is None


def test_last_participant_leaving_enters_grace() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=5))
    assert m.state is SessionState.GRACE


def test_return_within_grace_resumes_same_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=5))
    m.participants_changed(1, T0 + timedelta(minutes=5, seconds=30))
    assert m.state is SessionState.RECORDING
    assert m.started_at == T0  # same session, no restart


def test_grace_expiry_closes_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=5))
    assert m.tick(T0 + timedelta(minutes=5, seconds=59)) is None
    assert m.tick(T0 + timedelta(minutes=6, seconds=1)) is EndReason.EMPTY
    assert m.state is SessionState.CLOSING


def test_idle_timeout_closes_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(minutes=1))
    assert m.tick(T0 + timedelta(minutes=15)) is None
    assert m.tick(T0 + timedelta(minutes=16, seconds=1)) is EndReason.IDLE_TIMEOUT


def test_audio_resets_idle_timer() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(minutes=14))
    assert m.tick(T0 + timedelta(minutes=20)) is None


def test_max_duration_closes_even_while_speaking() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(hours=3, minutes=59))
    assert m.tick(T0 + timedelta(hours=4, seconds=1)) is EndReason.MAX_DURATION


def test_tick_is_idempotent_after_closing() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0)
    assert m.tick(T0 + timedelta(seconds=61)) is EndReason.EMPTY
    assert m.tick(T0 + timedelta(seconds=62)) is None  # doesn't report twice
    assert m.end_reason is EndReason.EMPTY


def test_tick_before_start_does_nothing() -> None:
    assert machine().tick(T0 + timedelta(hours=10)) is None


def test_reset_returns_closing_to_idle_ready_for_a_new_session() -> None:
    """CLOSING must not be a dead end -- `reset()` is its only exit edge.

    Before this method existed, nothing ever moved a machine out of
    CLOSING: `is_recording` on `RecordingService` stayed `False` forever
    after the first session closed, which is exactly what let the bot
    record exactly one session per process lifetime.
    """
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0)
    assert m.tick(T0 + timedelta(seconds=61)) is EndReason.EMPTY
    # `tick()` returning a reason is exactly what puts the machine in
    # CLOSING (see its docstring), so that state is not re-asserted here --
    # doing so would pin down a `Literal[SessionState.CLOSING]` type that
    # mypy does not widen back out across the `m.reset()` call below.

    m.reset()

    assert m.state is SessionState.IDLE
    assert m.started_at is None
    assert m.end_reason is None

    # A fresh session on the same machine must behave exactly like a
    # session that never had a predecessor. (`m.state` is not re-asserted
    # by identity here either, for the same reason noted above.)
    later = T0 + timedelta(hours=1)
    m.participants_changed(1, later)
    assert m.started_at == later
    assert m.tick(later + timedelta(minutes=20)) is EndReason.IDLE_TIMEOUT


def test_reset_outside_closing_is_rejected() -> None:
    """`reset()` must only ever follow a reason `tick()` reported.

    Calling it from any other state would silently discard a session that
    is still recording -- an assertion catches that misuse immediately
    instead of corrupting the machine's state.
    """
    m = machine()
    with pytest.raises(AssertionError):
        m.reset()

    m.participants_changed(1, T0)
    with pytest.raises(AssertionError):
        m.reset()


def test_naive_datetime_is_rejected() -> None:
    m = machine()
    with pytest.raises(ValueError, match="timezone-aware"):
        m.participants_changed(1, datetime(2026, 8, 19, 20, 0, 0))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"empty_grace_seconds": 0},
        {"empty_grace_seconds": -1},
        {"idle_timeout_minutes": 0},
        {"idle_timeout_minutes": -1},
        {"max_session_hours": 0},
        {"max_session_hours": -1},
    ],
)
def test_non_positive_timeouts_are_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SessionTimeouts(**kwargs)


def test_max_session_hours_above_the_rtp_ceiling_is_rejected() -> None:
    # The RTP wraparound safety margin (timeline.MAX_SUPPORTED_SESSION_HOURS)
    # caps how high an admin can push this via /config: above it, the
    # signed-delta interpretation in timeline.py could reinterpret a
    # legitimate forward jump as a backwards step.
    with pytest.raises(ValueError, match="must not exceed"):
        SessionTimeouts(max_session_hours=13)


def test_max_session_hours_at_the_rtp_ceiling_is_accepted() -> None:
    SessionTimeouts(max_session_hours=12)


def test_returning_participant_restarts_the_idle_timer() -> None:
    """Someone returning during grace gets the full idle window.

    Without this the timer keeps counting from before the channel emptied,
    and a returning participant who stays quiet briefly would see the bot
    leave seconds after they arrived.
    """
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(minutes=1))
    m.participants_changed(0, T0 + timedelta(minutes=14))
    m.participants_changed(1, T0 + timedelta(minutes=14, seconds=30))
    # Without the restart the idle window would expire at T0+16min.
    assert m.tick(T0 + timedelta(minutes=20)) is None
    assert m.state is SessionState.RECORDING


def test_idle_still_closes_after_a_return_without_speech() -> None:
    """Restarting the timer delays the close; it does not disable it."""
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=1))
    m.participants_changed(1, T0 + timedelta(minutes=1, seconds=30))
    assert m.tick(T0 + timedelta(minutes=16, seconds=31)) is EndReason.IDLE_TIMEOUT


def test_retime_changes_the_next_decision_mid_recording() -> None:
    """The invariant behind `retime`: timeouts are read only on the next tick."""
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0)
    assert m.tick(T0 + timedelta(minutes=10)) is None

    m.retime(SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=5, max_session_hours=4))

    assert m.state is SessionState.RECORDING, "retiming must not touch the session itself"
    assert m.started_at == T0, "nor its bookkeeping"
    assert m.tick(T0 + timedelta(minutes=10)) is EndReason.IDLE_TIMEOUT


def test_retime_to_an_already_exceeded_value_closes_on_the_next_tick() -> None:
    """Deliberate, and it goes through the ordinary path -- nothing is discarded.

    `tick()` reporting a reason is what makes the caller run `close()`,
    which encrypts, uploads and enqueues everything recorded so far.
    Shortening a timeout ends a recording earlier; it never drops one.
    """
    m = machine()
    m.participants_changed(1, T0)
    m.retime(SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=1))
    assert m.tick(T0 + timedelta(hours=2)) is EndReason.MAX_DURATION
    assert m.state is SessionState.CLOSING


def test_retime_while_closing_does_not_resurrect_the_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    assert m.tick(T0 + timedelta(hours=5)) is EndReason.MAX_DURATION
    m.retime(SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=9))
    assert m.state is SessionState.CLOSING
    assert m.tick(T0 + timedelta(hours=6)) is None


def test_due_reason_reports_without_deciding() -> None:
    """What lets `/config set` warn honestly without being the thing that closes."""
    m = machine()
    m.participants_changed(1, T0)
    m.retime(SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=1))

    assert m.due_reason(T0 + timedelta(hours=2)) is EndReason.MAX_DURATION
    assert m.state is SessionState.RECORDING, "asking must not close the session"
    assert m.end_reason is None
    # ... and asking twice is still just asking.
    assert m.due_reason(T0 + timedelta(hours=2)) is EndReason.MAX_DURATION


def test_due_reason_is_none_while_idle() -> None:
    assert machine().due_reason(T0) is None
