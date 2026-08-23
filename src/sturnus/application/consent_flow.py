"""Decisions behind the consent commands, separated from Discord.

The cog turns interactions into these calls and renders their results; all
the branching that matters is here, where it can be tested without a gateway
connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sturnus.domain.consent import ConsentRecord, is_consent_active


@dataclass(frozen=True)
class ConsentStatus:
    has_role: bool
    consent_active: bool
    policy_version: str | None
    linked: bool


def grant_needed(
    record: ConsentRecord | None, current_policy: str, has_role: bool, now: datetime
) -> bool:
    """True when granting would actually change something.

    `now` because `revoked_at` is an effective instant rather than a
    tombstone: somebody who scheduled a withdrawal for the end of the
    month still has consent today, and `/consent grant` must tell them
    there is nothing to grant rather than writing a second row that says
    the same thing. The clock is threaded in from the cog; nothing below
    the application layer reads one for itself.
    """
    return not (is_consent_active(record, current_policy, now) and has_role)


def revoke_needed(
    record: ConsentRecord | None, current_policy: str, has_role: bool, now: datetime
) -> bool:
    """True when there is any consent or role left to withdraw."""
    return is_consent_active(record, current_policy, now) or has_role
