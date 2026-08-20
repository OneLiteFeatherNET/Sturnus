from sturnus.application.setup_plan import PermissionChange, SetupPlan, plan_setup
from sturnus.domain import settings

CHANNEL, ROLE = 111, 222
POLICY_URL = "https://example.org/privacy"
POLICY_VERSION = "2026-08-01"


def plan(current: dict[str, str | None] | None = None, **kw: object) -> SetupPlan:
    defaults: dict[str, object] = {
        "current": current or {},
        "channel_id": CHANNEL,
        "role_id": ROLE,
        "stored_role_valid": False,
        "policy_url": POLICY_URL,
        "policy_version": POLICY_VERSION,
        "everyone_may_speak": True,
        "role_may_speak": False,
    }
    defaults.update(kw)
    return plan_setup(**defaults)  # type: ignore[arg-type]


def test_a_fresh_guild_writes_every_required_key() -> None:
    result = plan(None)
    assert result.writes[settings.VOICE_CHANNEL_ID] == str(CHANNEL)
    assert result.writes[settings.CONSENT_ROLE_ID] == str(ROLE)
    assert result.writes[settings.POLICY_URL] == POLICY_URL


def test_nothing_required_remains_missing_after_a_full_setup() -> None:
    assert plan(None).missing == []


def test_a_missing_document_target_is_reported_not_invented() -> None:
    """The Outline collection cannot be guessed from Discord."""
    result = plan({settings.DOCUMENT_TARGET: None})
    assert settings.DOCUMENT_TARGET in result.missing


def test_everyone_speaking_is_denied() -> None:
    """The primary layer of the consent protection (Spec 3.1)."""
    result = plan(everyone_may_speak=True)
    assert PermissionChange("everyone", allow_speak=False) in result.permission_changes


def test_the_consent_role_is_allowed_to_speak() -> None:
    result = plan(role_may_speak=False)
    assert PermissionChange("consent_role", allow_speak=True) in result.permission_changes


def test_correct_permissions_produce_no_changes() -> None:
    """Re-running against a configured guild must be a no-op, not a rewrite."""
    result = plan(everyone_may_speak=False, role_may_speak=True)
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
    result = plan({settings.VOICE_CHANNEL_ID: str(CHANNEL)})
    assert settings.VOICE_CHANNEL_ID not in result.writes


def test_a_changed_channel_is_rewritten() -> None:
    result = plan({settings.VOICE_CHANNEL_ID: "999"})
    assert result.writes[settings.VOICE_CHANNEL_ID] == str(CHANNEL)
