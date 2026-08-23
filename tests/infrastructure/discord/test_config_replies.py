"""What `/config set`, `/config show` and `/config apply` actually claim.

The reported defect was that a stored value did not reach the running
process. Half of the fix is making it reach; the other half is that the
reply stops implying it always did. A confirmation reading ``
`voice_channel_id` set to `123` `` while the bot keeps recording the old
channel is the same defect one layer up, so the wording is tested rather
than eyeballed.

The renderers are plain functions over a `ReconfigureResult`, so none of
this needs an `Interaction`, a gateway, or a database.
"""

from sturnus.application.channel_choice import MAX_CONCURRENT_SESSIONS_PER_GUILD
from sturnus.application.reconfigure import (
    IDENTITY_KEYS,
    ReconfigureAction,
    ReconfigureResult,
    RunningState,
)
from sturnus.domain import settings
from sturnus.infrastructure.discord.config_cog import (
    RESTART_REQUIRED_KEYS,
    render_apply_result,
    render_running_state,
    render_write_result,
)


def result(
    *,
    action: ReconfigureAction = ReconfigureAction.NOTHING,
    applied_keys: tuple[str, ...] = (),
    deferred_keys: tuple[str, ...] = (),
    is_live: bool = True,
    is_recording: bool = False,
    became_live: bool = False,
    session_exceeds_timeouts: bool = False,
) -> ReconfigureResult:
    return ReconfigureResult(
        action=action,
        applied_keys=applied_keys,
        deferred_keys=deferred_keys,
        is_live=is_live,
        is_recording=is_recording,
        became_live=became_live,
        session_exceeds_timeouts=session_exceeds_timeouts,
    )


def test_a_key_that_took_effect_says_so() -> None:
    reply = render_write_result(
        settings.IDLE_TIMEOUT_MINUTES, "5", result(applied_keys=(settings.IDLE_TIMEOUT_MINUTES,))
    )
    assert "`idle_timeout_minutes` set to `5`." in reply
    assert "In effect now." in reply


def test_a_deferred_key_says_it_is_waiting_and_that_nothing_is_lost() -> None:
    reply = render_write_result(
        settings.VOICE_CHANNEL_IDS,
        "123",
        result(
            action=ReconfigureAction.DEFER_RETARGET,
            deferred_keys=(settings.VOICE_CHANNEL_IDS,),
            is_recording=True,
        ),
    )
    assert "recording is in progress" in reply
    assert "when that session ends" in reply
    assert settings.MAX_SESSION_HOURS in reply, "the wait must be given a bound"
    assert "will not be lost" in reply
    assert "In effect now." not in reply


def test_a_deferred_teardown_promises_the_recording_finishes_first() -> None:
    reply = render_write_result(
        settings.VOICE_CHANNEL_IDS,
        None,
        result(
            action=ReconfigureAction.DEFER_TEARDOWN,
            deferred_keys=IDENTITY_KEYS,
            is_recording=True,
        ),
    )
    assert "`voice_channel_ids` cleared." in reply
    assert "finish and upload" in reply
    assert "will not be lost" in reply


def test_a_key_that_needs_a_restart_says_restart_rather_than_pretending() -> None:
    """`publish_poll_seconds` is read once, at process start (see `bot.py`).

    Reporting a restart requirement honestly is a fix; claiming it applied
    would be the original defect wearing a different key.
    """
    key = next(iter(RESTART_REQUIRED_KEYS))
    reply = render_write_result(key, "10", result())
    assert "restarts" in reply
    assert "In effect now." not in reply


def test_the_key_that_makes_a_guild_live_says_no_restart_is_needed() -> None:
    reply = render_write_result(
        settings.CONSENT_ROLE_ID, "456", result(action=ReconfigureAction.BUILD, became_live=True)
    )
    assert "All required keys are now set" in reply
    assert "no restart needed" in reply


def test_a_key_stored_for_a_guild_that_still_cannot_record_says_so() -> None:
    reply = render_write_result(settings.VOICE_CHANNEL_IDS, "123", result(is_live=False))
    assert "not watching this server yet" in reply
    assert "`/config show`" in reply


def test_a_failed_reconcile_is_neither_success_nor_failure() -> None:
    reply = render_write_result(settings.IDLE_TIMEOUT_MINUTES, "5", None)
    assert "could not re-read" in reply
    assert "In effect now." not in reply


def test_shortening_a_timeout_below_the_running_session_warns() -> None:
    reply = render_write_result(
        settings.MAX_SESSION_HOURS,
        "1",
        result(
            applied_keys=(settings.MAX_SESSION_HOURS,),
            is_recording=True,
            session_exceeds_timeouts=True,
        ),
    )
    assert "already exceeds this" in reply
    assert "uploaded and transcribed normally" in reply


def test_the_exceeded_warning_is_not_attached_to_unrelated_keys() -> None:
    reply = render_write_result(
        settings.DOCUMENT_TARGET, "abc", result(is_recording=True, session_exceeds_timeouts=True)
    )
    assert "already exceeds this" not in reply


def _state(
    *,
    is_live: bool = True,
    is_recording: bool = True,
    channel_ids: tuple[int, ...] = (1,),
    session_limit: int = MAX_CONCURRENT_SESSIONS_PER_GUILD,
    allowed_channel_ids: tuple[int, ...] = (1,),
    waiting_channel_ids: tuple[int, ...] = (),
    pending_keys: tuple[str, ...] = (),
    pending_teardown: bool = False,
) -> RunningState:
    return RunningState(
        is_live=is_live,
        is_recording=is_recording,
        channel_ids=channel_ids,
        session_limit=session_limit,
        allowed_channel_ids=allowed_channel_ids,
        waiting_channel_ids=waiting_channel_ids,
        pending_keys=pending_keys,
        pending_teardown=pending_teardown,
    )


def test_show_reports_the_running_configuration_as_in_effect() -> None:
    assert render_running_state(_state(), has_missing_keys=False).endswith(
        "Running configuration: in effect."
    )


def test_show_names_the_one_channel_being_recorded() -> None:
    line = render_running_state(_state(), has_missing_keys=False)
    assert "Recording channel: <#1>." in line


def test_show_names_the_allowed_channels_that_are_not_being_recorded() -> None:
    """A person sitting in the second allowed room is owed the reason.

    Without this the honest answer -- "the bot is in the other room,
    because more consenting people are in it, and it can only be in one" --
    is available nowhere an administrator can reach it.
    """
    line = render_running_state(
        _state(channel_ids=(1,), allowed_channel_ids=(1, 2, 3)), has_missing_keys=False
    )
    assert "Recording channel: <#1>" in line
    assert "<#2>" in line
    assert "<#3>" in line
    assert "one voice connection per server" in line


def test_show_says_how_many_of_the_allowed_channels_are_being_served() -> None:
    """ "One of three" beats a list of rooms that are "also allowed".

    The second reads as though Sturnus is choosing to leave them alone.
    The reason it is not in them is that it has run out of voice
    connections, and the number is what says so.
    """
    line = render_running_state(
        _state(channel_ids=(1,), allowed_channel_ids=(1, 2, 3)), has_missing_keys=False
    )
    assert "serving 1 of 3 allowed channels" in line
    assert "records 1 at a time" in line


def test_show_marks_an_allowed_channel_that_has_people_waiting_in_it() -> None:
    line = render_running_state(
        _state(channel_ids=(1,), allowed_channel_ids=(1, 2), waiting_channel_ids=(2,)),
        has_missing_keys=False,
    )
    assert "<#2> (people waiting)" in line


def test_show_names_the_keys_that_are_still_waiting() -> None:
    """The line that stops `/config show` from insisting a value is in use."""
    line = render_running_state(
        _state(pending_keys=(settings.VOICE_CHANNEL_IDS,)), has_missing_keys=False
    )
    assert "waiting for the current recording to end" in line
    assert "`voice_channel_ids`" in line


def test_show_reports_a_guild_that_is_not_being_watched() -> None:
    state = _state(is_live=False, is_recording=False, channel_ids=(), allowed_channel_ids=())
    assert "not applied" in render_running_state(state, has_missing_keys=True)


def test_apply_with_force_says_up_front_what_it_did_to_the_recording() -> None:
    reply = render_apply_result(result(action=ReconfigureAction.RETARGET), force=True)
    assert reply.splitlines()[0].startswith("**`force` was used:**")
    assert "uploaded and transcribed normally" in reply


def test_apply_without_force_offers_force_when_something_is_deferred() -> None:
    reply = render_apply_result(
        result(
            action=ReconfigureAction.DEFER_RETARGET, deferred_keys=(settings.VOICE_CHANNEL_IDS,)
        ),
        force=False,
    )
    assert "`/config apply force:true`" in reply
    assert "still uploaded" in reply


def test_apply_on_an_already_correct_guild_says_nothing_changed() -> None:
    reply = render_apply_result(result(), force=False)
    assert "already correct" in reply


def test_writing_the_deprecated_key_mid_session_still_says_it_is_waiting() -> None:
    """The two spellings are one setting, so the reply must recognise itself.

    A write to `voice_channel_id` produces a reconcile result naming
    `voice_channel_ids`. Comparing the raw strings would tell the
    administrator their change is in force while the bot is in fact still
    recording the old channel -- the exact lie this rendering exists to
    prevent, wearing the other name.
    """
    reply = render_write_result(
        settings.VOICE_CHANNEL_ID,
        "123",
        result(
            action=ReconfigureAction.DEFER_RETARGET,
            deferred_keys=(settings.VOICE_CHANNEL_IDS,),
            is_recording=True,
        ),
    )
    assert "recording is in progress" in reply
    assert "In effect now." not in reply
