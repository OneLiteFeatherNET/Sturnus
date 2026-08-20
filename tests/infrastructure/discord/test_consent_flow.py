from datetime import UTC, datetime

from sturnus.application.consent_flow import grant_needed, revoke_needed
from sturnus.domain.consent import ConsentRecord

T0 = datetime(2026, 8, 19, tzinfo=UTC)
POLICY = "2026-08-01"


def granted(version: str = POLICY) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=None, policy_version=version)


def test_granting_is_needed_without_a_record() -> None:
    assert grant_needed(None, POLICY, has_role=False) is True


def test_granting_is_needed_when_the_role_is_missing() -> None:
    """The record alone does not let anyone speak; the role is what Discord checks."""
    assert grant_needed(granted(), POLICY, has_role=False) is True


def test_granting_is_needed_after_a_policy_change() -> None:
    assert grant_needed(granted("2026-01-01"), POLICY, has_role=True) is True


def test_granting_is_not_needed_when_fully_consented() -> None:
    assert grant_needed(granted(), POLICY, has_role=True) is False


def test_revoking_is_needed_while_the_role_remains() -> None:
    """A stale role must be removable even once the record has lapsed."""
    assert revoke_needed(granted("2026-01-01"), POLICY, has_role=True) is True


def test_revoking_is_not_needed_when_nothing_is_held() -> None:
    assert revoke_needed(None, POLICY, has_role=False) is False
