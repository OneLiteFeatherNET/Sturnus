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


def test_naive_datetime_is_rejected() -> None:
    m = machine()
    with pytest.raises(ValueError, match="timezone-aware"):
        m.participants_changed(1, datetime(2026, 8, 19, 20, 0, 0))
