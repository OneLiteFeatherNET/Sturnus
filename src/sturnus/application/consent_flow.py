"""Decisions behind the consent commands, separated from Discord.

The cog turns interactions into these calls and renders their results; all
the branching that matters is here, where it can be tested without a gateway
connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from sturnus.domain.consent import ConsentRecord, is_consent_active


@dataclass(frozen=True)
class ConsentStatus:
    has_role: bool
    consent_active: bool
    policy_version: str | None
    linked: bool


def grant_needed(record: ConsentRecord | None, current_policy: str, has_role: bool) -> bool:
    """True when granting would actually change something."""
    return not (is_consent_active(record, current_policy) and has_role)


def revoke_needed(record: ConsentRecord | None, current_policy: str, has_role: bool) -> bool:
    """True when there is any consent or role left to withdraw."""
    return is_consent_active(record, current_policy) or has_role
