from datetime import UTC, datetime

from sturnus.domain.consent import ConsentRecord, is_consent_active, may_record

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
POLICY = "2026-08-01"


def granted(version: str = POLICY) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=None, policy_version=version)


def test_granted_consent_is_active() -> None:
    assert is_consent_active(granted(), POLICY) is True


def test_missing_record_is_not_active() -> None:
    assert is_consent_active(None, POLICY) is False


def test_revoked_consent_is_not_active() -> None:
    record = ConsentRecord(granted_at=T0, revoked_at=T0, policy_version=POLICY)
    assert is_consent_active(record, POLICY) is False


def test_outdated_policy_version_invalidates_consent() -> None:
    assert is_consent_active(granted("2026-01-01"), POLICY) is False


def test_recording_requires_both_role_and_consent() -> None:
    # The role check alone isn't enough: administrators bypass channel
    # permissions, which is why the record is also checked.
    assert may_record(granted(), POLICY, has_consent_role=True) is True
    assert may_record(granted(), POLICY, has_consent_role=False) is False
    assert may_record(None, POLICY, has_consent_role=True) is False


def test_revoked_user_with_stale_role_may_not_be_recorded() -> None:
    record = ConsentRecord(granted_at=T0, revoked_at=T0, policy_version=POLICY)
    assert may_record(record, POLICY, has_consent_role=True) is False


def test_blank_current_policy_version_is_never_active() -> None:
    # A record with policy_version=None is forbidden by the schema, and the
    # type signature forbids passing None as current_policy_version - but an
    # empty string satisfies both and must not be treated as "no policy set
    # yet equals no policy required".
    record = ConsentRecord(granted_at=T0, revoked_at=None, policy_version="")
    assert is_consent_active(record, "") is False
