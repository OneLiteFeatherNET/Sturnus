"""The reconcile decision, one test per row of the table in `reconfigure.py`.

Pure: no Discord, no database, no `RecordingService`. That is the point of
separating the decision from its execution -- the mid-session behaviour
that must never lose a recording is decided here, so it can be pinned down
exhaustively without a gateway connection.
"""

from sturnus.application.reconfigure import (
    IDENTITY_KEYS,
    TUNABLE_KEYS,
    GuildRuntimeConfig,
    ReconfigureAction,
    plan_reconfigure,
)
from sturnus.domain import settings
from sturnus.domain.session import SessionTimeouts

CHANNEL, ROLE = 100, 200


def config(
    *,
    channel_id: int = CHANNEL,
    role_id: int = ROLE,
    empty_grace_seconds: int = 60,
    idle_timeout_minutes: int = 15,
    max_session_hours: int = 4,
    retention_days: int = 30,
) -> GuildRuntimeConfig:
    return GuildRuntimeConfig(
        channel_id=channel_id,
        role_id=role_id,
        timeouts=SessionTimeouts(
            empty_grace_seconds=empty_grace_seconds,
            idle_timeout_minutes=idle_timeout_minutes,
            max_session_hours=max_session_hours,
        ),
        retention_days=retention_days,
    )


def test_nothing_configured_and_nothing_running() -> None:
    plan = plan_reconfigure(current=None, desired=None, is_recording=False)
    assert plan.action is ReconfigureAction.NOTHING
    assert plan.retune is False


def test_configuration_appears_for_a_guild_with_no_pipeline() -> None:
    """The reported defect's path: nothing running, configuration now present."""
    plan = plan_reconfigure(current=None, desired=config(), is_recording=False)
    assert plan.action is ReconfigureAction.BUILD
    # A fresh pipeline is constructed *with* these values, so there is
    # nothing left to retune afterwards and nothing to defer.
    assert plan.retune is False
    assert set(plan.applied_keys) == set(IDENTITY_KEYS) | set(TUNABLE_KEYS)
    assert plan.deferred_keys == ()


def test_configuration_disappears_while_idle() -> None:
    plan = plan_reconfigure(current=config(), desired=None, is_recording=False)
    assert plan.action is ReconfigureAction.TEARDOWN
    assert plan.retune is False
    assert plan.applied_keys == IDENTITY_KEYS


def test_configuration_disappears_while_recording() -> None:
    """The recording finishes first; there is nothing to retune with."""
    plan = plan_reconfigure(current=config(), desired=None, is_recording=True)
    assert plan.action is ReconfigureAction.DEFER_TEARDOWN
    assert plan.retune is False
    assert plan.deferred_keys == IDENTITY_KEYS


def test_nothing_changed_at_all() -> None:
    """The every-ten-seconds common case, which must stay a no-op."""
    plan = plan_reconfigure(current=config(), desired=config(), is_recording=True)
    assert plan.action is ReconfigureAction.NOTHING
    assert plan.retune is False
    assert plan.applied_keys == ()


def test_only_a_tunable_changed_while_recording() -> None:
    """No structural change, but the new value is in force immediately."""
    plan = plan_reconfigure(
        current=config(), desired=config(idle_timeout_minutes=5), is_recording=True
    )
    assert plan.action is ReconfigureAction.NOTHING
    assert plan.retune is True
    assert plan.applied_keys == (settings.IDLE_TIMEOUT_MINUTES,)
    assert plan.deferred_keys == ()


def test_retention_is_a_tunable_too() -> None:
    plan = plan_reconfigure(current=config(), desired=config(retention_days=7), is_recording=True)
    assert plan.retune is True
    assert plan.applied_keys == (settings.AUDIO_RETENTION_DAYS,)


def test_identity_changed_while_idle() -> None:
    plan = plan_reconfigure(current=config(), desired=config(channel_id=999), is_recording=False)
    assert plan.action is ReconfigureAction.RETARGET
    assert plan.retune is True
    assert plan.applied_keys == (settings.VOICE_CHANNEL_ID,)
    assert plan.deferred_keys == ()


def test_identity_changed_while_recording_is_deferred() -> None:
    """The load-bearing row: the channel waits, so the recording survives."""
    plan = plan_reconfigure(current=config(), desired=config(channel_id=999), is_recording=True)
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.deferred_keys == (settings.VOICE_CHANNEL_ID,)


def test_a_deferred_identity_change_does_not_hold_up_the_tunables() -> None:
    """The reason `retune` is a separate flag from `action`.

    A shortened idle timeout must not have to wait behind a channel move
    for up to `max_session_hours`; the two travel independently.
    """
    plan = plan_reconfigure(
        current=config(),
        desired=config(channel_id=999, idle_timeout_minutes=5),
        is_recording=True,
    )
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.retune is True
    assert plan.applied_keys == (settings.IDLE_TIMEOUT_MINUTES,)
    assert plan.deferred_keys == (settings.VOICE_CHANNEL_ID,)


def test_the_consent_role_moves_with_the_channel_not_with_the_tunables() -> None:
    """`consent_role_id` is half-live today, which is worse than fully stale.

    `VoiceReceiveAdapter.join` re-reads it per join while the client's
    headcount used a value frozen at startup, so the filter and the
    headcount could disagree about who counts. They must move together.
    """
    plan = plan_reconfigure(current=config(), desired=config(role_id=999), is_recording=True)
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.deferred_keys == (settings.CONSENT_ROLE_ID,)


def test_both_identity_keys_change_at_once() -> None:
    plan = plan_reconfigure(
        current=config(), desired=config(channel_id=9, role_id=8), is_recording=False
    )
    assert plan.action is ReconfigureAction.RETARGET
    assert plan.applied_keys == IDENTITY_KEYS
