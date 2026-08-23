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
    channel_ids: tuple[int, ...] = (CHANNEL,),
    role_id: int = ROLE,
    empty_grace_seconds: int = 60,
    idle_timeout_minutes: int = 15,
    max_session_hours: int = 4,
    retention_days: int = 30,
) -> GuildRuntimeConfig:
    return GuildRuntimeConfig(
        channel_ids=channel_ids,
        role_id=role_id,
        timeouts=SessionTimeouts(
            empty_grace_seconds=empty_grace_seconds,
            idle_timeout_minutes=idle_timeout_minutes,
            max_session_hours=max_session_hours,
        ),
        retention_days=retention_days,
    )


def test_nothing_configured_and_nothing_running() -> None:
    plan = plan_reconfigure(current=None, desired=None, recording_channel_ids=())
    assert plan.action is ReconfigureAction.NOTHING
    assert plan.retune is False


def test_configuration_appears_for_a_guild_with_no_pipeline() -> None:
    """The reported defect's path: nothing running, configuration now present."""
    plan = plan_reconfigure(current=None, desired=config(), recording_channel_ids=())
    assert plan.action is ReconfigureAction.BUILD
    # A fresh pipeline is constructed *with* these values, so there is
    # nothing left to retune afterwards and nothing to defer.
    assert plan.retune is False
    assert set(plan.applied_keys) == set(IDENTITY_KEYS) | set(TUNABLE_KEYS)
    assert plan.deferred_keys == ()


def test_configuration_disappears_while_idle() -> None:
    plan = plan_reconfigure(current=config(), desired=None, recording_channel_ids=())
    assert plan.action is ReconfigureAction.TEARDOWN
    assert plan.retune is False
    assert plan.applied_keys == IDENTITY_KEYS


def test_configuration_disappears_while_recording() -> None:
    """The recording finishes first; there is nothing to retune with."""
    plan = plan_reconfigure(current=config(), desired=None, recording_channel_ids=(CHANNEL,))
    assert plan.action is ReconfigureAction.DEFER_TEARDOWN
    assert plan.retune is False
    assert plan.deferred_keys == IDENTITY_KEYS


def test_nothing_changed_at_all() -> None:
    """The every-ten-seconds common case, which must stay a no-op."""
    plan = plan_reconfigure(current=config(), desired=config(), recording_channel_ids=(CHANNEL,))
    assert plan.action is ReconfigureAction.NOTHING
    assert plan.retune is False
    assert plan.applied_keys == ()


def test_only_a_tunable_changed_while_recording() -> None:
    """No structural change, but the new value is in force immediately."""
    plan = plan_reconfigure(
        current=config(), desired=config(idle_timeout_minutes=5), recording_channel_ids=(CHANNEL,)
    )
    assert plan.action is ReconfigureAction.NOTHING
    assert plan.retune is True
    assert plan.applied_keys == (settings.IDLE_TIMEOUT_MINUTES,)
    assert plan.deferred_keys == ()


def test_retention_is_a_tunable_too() -> None:
    plan = plan_reconfigure(
        current=config(), desired=config(retention_days=7), recording_channel_ids=(CHANNEL,)
    )
    assert plan.retune is True
    assert plan.applied_keys == (settings.AUDIO_RETENTION_DAYS,)


def test_identity_changed_while_idle() -> None:
    plan = plan_reconfigure(
        current=config(), desired=config(channel_ids=(999,)), recording_channel_ids=()
    )
    assert plan.action is ReconfigureAction.RETARGET
    assert plan.retune is True
    assert plan.applied_keys == (settings.VOICE_CHANNEL_IDS,)
    assert plan.deferred_keys == ()


def test_identity_changed_while_recording_is_deferred() -> None:
    """The load-bearing row: the channel waits, so the recording survives."""
    plan = plan_reconfigure(
        current=config(), desired=config(channel_ids=(999,)), recording_channel_ids=(CHANNEL,)
    )
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.deferred_keys == (settings.VOICE_CHANNEL_IDS,)


def test_a_deferred_identity_change_does_not_hold_up_the_tunables() -> None:
    """The reason `retune` is a separate flag from `action`.

    A shortened idle timeout must not have to wait behind a channel move
    for up to `max_session_hours`; the two travel independently.
    """
    plan = plan_reconfigure(
        current=config(),
        desired=config(channel_ids=(999,), idle_timeout_minutes=5),
        recording_channel_ids=(CHANNEL,),
    )
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.retune is True
    assert plan.applied_keys == (settings.IDLE_TIMEOUT_MINUTES,)
    assert plan.deferred_keys == (settings.VOICE_CHANNEL_IDS,)


def test_the_consent_role_moves_with_the_channel_not_with_the_tunables() -> None:
    """`consent_role_id` is half-live today, which is worse than fully stale.

    `VoiceReceiveAdapter.join` re-reads it per join while the client's
    headcount used a value frozen at startup, so the filter and the
    headcount could disagree about who counts. They must move together.
    """
    plan = plan_reconfigure(
        current=config(), desired=config(role_id=999), recording_channel_ids=(CHANNEL,)
    )
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.deferred_keys == (settings.CONSENT_ROLE_ID,)


def test_both_identity_keys_change_at_once() -> None:
    plan = plan_reconfigure(
        current=config(), desired=config(channel_ids=(9,), role_id=8), recording_channel_ids=()
    )
    assert plan.action is ReconfigureAction.RETARGET
    assert plan.applied_keys == IDENTITY_KEYS


def test_adding_a_second_allowed_channel_is_an_identity_change() -> None:
    """A list change decides which channels a session may open against, so
    it is deferred behind a recording exactly as a channel move always was."""
    plan = plan_reconfigure(
        current=config(),
        desired=config(channel_ids=(CHANNEL, 999)),
        recording_channel_ids=(CHANNEL,),
    )
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.deferred_keys == (settings.VOICE_CHANNEL_IDS,)


def test_the_same_channels_in_a_different_order_are_not_a_change() -> None:
    """`parse_channel_ids` sorts, so re-typing a list cannot retarget a guild."""
    plan = plan_reconfigure(
        current=config(channel_ids=(10, 20)),
        desired=config(channel_ids=(10, 20)),
        recording_channel_ids=(),
    )
    assert plan.action is ReconfigureAction.NOTHING


# ---------------------------------------------------------------------------
# The rooms a deferral is waiting on
# ---------------------------------------------------------------------------


def test_a_deferral_names_the_room_it_is_waiting_on() -> None:
    """The runtime is keyed per room, so "waiting" has an address now.

    It is what the log line prints and what `_apply_pending` checks, and
    both were previously gestures at "the guild".
    """
    plan = plan_reconfigure(
        current=config(),
        desired=config(channel_ids=(999,)),
        recording_channel_ids=(CHANNEL,),
    )
    assert plan.deferred_for_channel_ids == (CHANNEL,)


def test_a_deferral_names_every_room_it_is_waiting_on() -> None:
    """A change to an identity key belongs to the guild, not to one room.

    `consent_role_id` decides whose voice is recorded in every room at
    once and `voice_channel_ids` decides which rooms exist to record in,
    so a second room recording is a second reason to wait -- and the
    change may land only when both are done, not when the first is.
    """
    plan = plan_reconfigure(
        current=config(channel_ids=(CHANNEL, 999)),
        desired=config(channel_ids=(CHANNEL, 999), role_id=7),
        recording_channel_ids=(CHANNEL, 999),
    )
    assert plan.action is ReconfigureAction.DEFER_RETARGET
    assert plan.deferred_for_channel_ids == (CHANNEL, 999)


def test_a_deferred_teardown_names_the_rooms_too() -> None:
    plan = plan_reconfigure(current=config(), desired=None, recording_channel_ids=(CHANNEL, 999))
    assert plan.action is ReconfigureAction.DEFER_TEARDOWN
    assert plan.deferred_for_channel_ids == (CHANNEL, 999)


def test_nothing_deferred_names_no_rooms() -> None:
    """The field is about the deferral, not about what is recording."""
    plan = plan_reconfigure(
        current=config(), desired=config(idle_timeout_minutes=5), recording_channel_ids=(CHANNEL,)
    )
    assert plan.deferred_keys == ()
    assert plan.deferred_for_channel_ids == ()


def test_a_guild_with_no_pipeline_is_built_even_while_another_room_records() -> None:
    """A change affecting one room must not defer a build for another.

    A room this process holds no pipeline for cannot itself be recording,
    so a session elsewhere in the guild is no reason to refuse to build
    one. What refuses is the connection limit, and the client asks that
    (`SturnusClient._may_open_another`) rather than this function
    pretending a configuration question has been answered.
    """
    plan = plan_reconfigure(
        current=None, desired=config(channel_ids=(CHANNEL, 999)), recording_channel_ids=(999,)
    )
    assert plan.action is ReconfigureAction.BUILD
    assert plan.deferred_keys == ()
