"""What `/setup` decides, with no guild in sight.

The load-bearing change here is that `/setup` **adds** its channel to the
allowed list instead of replacing it. A guild that ran `/setup` for its
second meeting room used to stop recording the first one, silently -- and
"silently" is the whole problem: nobody finds out until the meeting that
should have been recorded was not.
"""

from sturnus.application.setup_plan import (
    ChannelPermissions,
    PermissionChange,
    SetupPlan,
    plan_setup,
)
from sturnus.domain import settings

CHANNEL, OTHER_CHANNEL, ROLE = 111, 333, 222
POLICY_URL = "https://example.org/privacy"
POLICY_VERSION = "2026-08-01"


def permissions(
    *channel_ids: int, everyone_may_speak: bool = True, role_may_speak: bool = False
) -> list[ChannelPermissions]:
    return [
        ChannelPermissions(
            channel_id=channel_id,
            everyone_may_speak=everyone_may_speak,
            role_may_speak=role_may_speak,
        )
        for channel_id in channel_ids
    ]


def plan(current: dict[str, str | None] | None = None, **kw: object) -> SetupPlan:
    defaults: dict[str, object] = {
        "current": current or {},
        "channel_id": CHANNEL,
        "stored_channel_ids": (),
        "channel_permissions": permissions(CHANNEL),
        "role_id": ROLE,
        "stored_role_valid": False,
        "policy_url": POLICY_URL,
        "policy_version": POLICY_VERSION,
    }
    defaults.update(kw)
    return plan_setup(**defaults)  # type: ignore[arg-type]


def test_a_fresh_guild_writes_every_required_key() -> None:
    result = plan(None)
    assert result.writes[settings.VOICE_CHANNEL_IDS] == str(CHANNEL)
    assert result.writes[settings.CONSENT_ROLE_ID] == str(ROLE)
    assert result.writes[settings.POLICY_URL] == POLICY_URL


def test_setting_up_a_second_room_keeps_recording_the_first() -> None:
    """The defect this change exists for: `/setup` used to overwrite."""
    result = plan(
        {settings.VOICE_CHANNEL_IDS: str(OTHER_CHANNEL)},
        stored_channel_ids=(OTHER_CHANNEL,),
        channel_permissions=permissions(CHANNEL, OTHER_CHANNEL),
    )
    assert result.writes[settings.VOICE_CHANNEL_IDS] == f"{CHANNEL},{OTHER_CHANNEL}"
    assert result.channel_ids == (CHANNEL, OTHER_CHANNEL)


def test_a_guild_still_on_the_old_key_is_added_to_not_replaced() -> None:
    """The migration nobody has to run: the legacy channel survives.

    The caller resolves the deprecated key through
    `settings.recording_channel_ids`, so what arrives here is the channel
    that key named -- and it must end up in the list that gets written.
    """
    result = plan(
        {settings.VOICE_CHANNEL_ID: str(OTHER_CHANNEL)},
        stored_channel_ids=(OTHER_CHANNEL,),
        channel_permissions=permissions(CHANNEL, OTHER_CHANNEL),
    )
    assert result.channel_ids == (CHANNEL, OTHER_CHANNEL)
    # The new key is written; the old row is left where it is, inert.
    assert result.writes[settings.VOICE_CHANNEL_IDS] == f"{CHANNEL},{OTHER_CHANNEL}"
    assert settings.VOICE_CHANNEL_ID not in result.writes


def test_adding_a_channel_that_is_already_allowed_changes_nothing() -> None:
    """`/setup` must stay safe to run twice."""
    result = plan(
        {settings.VOICE_CHANNEL_IDS: f"{CHANNEL},{OTHER_CHANNEL}"},
        stored_channel_ids=(CHANNEL, OTHER_CHANNEL),
        channel_permissions=permissions(CHANNEL, OTHER_CHANNEL),
    )
    assert settings.VOICE_CHANNEL_IDS not in result.writes


def test_nothing_required_remains_missing_after_a_full_setup() -> None:
    assert plan(None).missing == []


def test_a_missing_document_target_is_reported_not_invented() -> None:
    """The Outline collection cannot be guessed from Discord."""
    result = plan({settings.DOCUMENT_TARGET: None})
    assert settings.DOCUMENT_TARGET in result.missing


def test_everyone_speaking_is_denied() -> None:
    """The primary layer of the consent protection (Spec 3.1)."""
    result = plan(channel_permissions=permissions(CHANNEL, everyone_may_speak=True))
    assert PermissionChange(CHANNEL, "everyone", allow_speak=False) in result.permission_changes


def test_the_consent_role_is_allowed_to_speak() -> None:
    result = plan(channel_permissions=permissions(CHANNEL, role_may_speak=False))
    assert PermissionChange(CHANNEL, "consent_role", allow_speak=True) in result.permission_changes


def test_a_channel_that_was_already_allowed_is_protected_too() -> None:
    """An allowed channel whose @everyone may still Speak is a hole in the
    consent protection, whichever call put it in the list."""
    result = plan(
        {settings.VOICE_CHANNEL_IDS: str(OTHER_CHANNEL)},
        stored_channel_ids=(OTHER_CHANNEL,),
        channel_permissions=permissions(CHANNEL, OTHER_CHANNEL),
    )
    assert (
        PermissionChange(OTHER_CHANNEL, "everyone", allow_speak=False) in result.permission_changes
    )


def test_a_channel_that_is_already_correct_is_left_alone() -> None:
    """Per channel, so one room needing a fix does not rewrite the other."""
    result = plan(
        {settings.VOICE_CHANNEL_IDS: str(OTHER_CHANNEL)},
        stored_channel_ids=(OTHER_CHANNEL,),
        channel_permissions=[
            ChannelPermissions(CHANNEL, everyone_may_speak=True, role_may_speak=False),
            ChannelPermissions(OTHER_CHANNEL, everyone_may_speak=False, role_may_speak=True),
        ],
    )
    assert [change.channel_id for change in result.permission_changes] == [CHANNEL, CHANNEL]


def test_correct_permissions_produce_no_changes() -> None:
    """Re-running against a configured guild must be a no-op, not a rewrite."""
    result = plan(
        channel_permissions=permissions(CHANNEL, everyone_may_speak=False, role_may_speak=True)
    )
    assert result.permission_changes == []


def test_a_missing_role_is_requested_for_creation() -> None:
    result = plan(role_id=None)
    assert result.role_to_create is not None
    assert settings.CONSENT_ROLE_ID not in result.writes


def test_a_stored_role_is_reused_when_the_argument_is_omitted() -> None:
    """Re-running `/setup` with fewer arguments must not be the destructive path (Spec 10.1)."""
    result = plan(
        {settings.CONSENT_ROLE_ID: str(ROLE)},
        role_id=None,
        stored_role_valid=True,
    )
    assert result.role_action == "reused"
    assert result.role_to_create is None
    assert settings.CONSENT_ROLE_ID not in result.writes


def test_a_role_is_created_only_when_nothing_is_stored() -> None:
    result = plan(role_id=None, stored_role_valid=False)
    assert result.role_action == "created"
    assert result.role_to_create is not None
    assert settings.CONSENT_ROLE_ID not in result.writes


def test_an_explicitly_supplied_role_overrides_a_stored_one() -> None:
    other_role = ROLE + 1
    result = plan(
        {settings.CONSENT_ROLE_ID: str(ROLE)},
        role_id=other_role,
        stored_role_valid=True,
    )
    assert result.role_action == "replaced"
    assert result.role_to_create is None
    assert result.writes[settings.CONSENT_ROLE_ID] == str(other_role)


def test_an_unchanged_value_is_not_rewritten() -> None:
    result = plan({settings.VOICE_CHANNEL_IDS: str(CHANNEL)}, stored_channel_ids=(CHANNEL,))
    assert settings.VOICE_CHANNEL_IDS not in result.writes
