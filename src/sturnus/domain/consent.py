"""Consent resolution.

The Discord role is the first line of defense, but not the only one:
users with administrator rights bypass channel permissions and could
speak without the role. That's why the stored record always decides too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConsentRecord:
    granted_at: datetime | None
    revoked_at: datetime | None
    policy_version: str | None


def is_consent_active(record: ConsentRecord | None, current_policy_version: str) -> bool:
    """Consent expires through revocation and through a changed policy."""
    if not current_policy_version:
        return False
    if record is None or record.granted_at is None:
        return False
    if record.revoked_at is not None:
        return False
    return record.policy_version == current_policy_version


def may_record(
    record: ConsentRecord | None,
    current_policy_version: str,
    has_consent_role: bool,
) -> bool:
    return has_consent_role and is_consent_active(record, current_policy_version)
