from datetime import UTC, datetime, timedelta

from sturnus.application.consent_flow import grant_needed, revoke_needed
from sturnus.domain.consent import ConsentRecord

T0 = datetime(2026, 8, 19, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
POLICY = "2026-08-01"


def granted(version: str = POLICY, revoked_at: datetime | None = None) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=revoked_at, policy_version=version)


def test_granting_is_needed_without_a_record() -> None:
    assert grant_needed(None, POLICY, has_role=False, now=NOW) is True


def test_granting_is_needed_when_the_role_is_missing() -> None:
    """The record alone does not let anyone speak; the role is what Discord checks."""
    assert grant_needed(granted(), POLICY, has_role=False, now=NOW) is True


def test_granting_is_needed_after_a_policy_change() -> None:
    assert grant_needed(granted("2026-01-01"), POLICY, has_role=True, now=NOW) is True


def test_granting_is_not_needed_when_fully_consented() -> None:
    assert grant_needed(granted(), POLICY, has_role=True, now=NOW) is False


def test_revoking_is_needed_while_the_role_remains() -> None:
    """A stale role must be removable even once the record has lapsed."""
    assert revoke_needed(granted("2026-01-01"), POLICY, has_role=True, now=NOW) is True


def test_revoking_is_not_needed_when_nothing_is_held() -> None:
    assert revoke_needed(None, POLICY, has_role=False, now=NOW) is False


def test_granting_is_not_needed_while_a_scheduled_withdrawal_has_not_arrived() -> None:
    """`revoked_at` is an instant now, and `/consent grant` reads it as one.

    Somebody who asked to be withdrawn at the end of the month still has
    consent today. Telling them to consent again would write a second row
    saying what the first one already says -- and, worse, it would read as
    though the withdrawal they arranged had failed.
    """
    scheduled = granted(revoked_at=NOW + timedelta(days=10))
    assert grant_needed(scheduled, POLICY, has_role=True, now=NOW) is False


def test_granting_is_needed_once_a_scheduled_withdrawal_has_passed() -> None:
    lapsed = granted(revoked_at=NOW - timedelta(seconds=1))
    assert grant_needed(lapsed, POLICY, has_role=True, now=NOW) is True
