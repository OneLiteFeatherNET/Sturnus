"""Decisions behind the consent commands, separated from Discord.

The cog turns interactions into these calls and renders their results; all
the branching that matters is here, where it can be tested without a gateway
connection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sturnus.domain.consent import ConsentRecord, is_consent_active


class RoleAssignmentError(Exception):
    """The consent role could not be assigned. `str(exc)` is the reason shown."""


class ConsentWriter(Protocol):
    async def record_grant(
        self,
        *,
        discord_user_id: int,
        guild_id: int,
        policy_version: str,
        source: str,
        now: datetime,
    ) -> None: ...


class GrantOutcome(StrEnum):
    GRANTED = "granted"
    #: The checkbox was not ticked. Nothing was assigned or written.
    NOT_CONFIRMED = "not_confirmed"
    #: The role could not be assigned, so no grant was written.
    ROLE_FAILED = "role_failed"


@dataclass(frozen=True)
class GrantResult:
    outcome: GrantOutcome
    #: Set only for `ROLE_FAILED`: the reason the role could not be assigned.
    reason: str | None = None


async def grant_consent(
    *,
    discord_user_id: int,
    guild_id: int,
    policy_version: str,
    source: str,
    now: datetime,
    checkbox_ticked: bool,
    assign_role: Callable[[], Awaitable[None]],
    consents: ConsentWriter,
) -> GrantResult:
    """The one grant, shared by `/consent grant` and the channel prompt.

    The role is assigned first, then the grant is written, so a record never
    says consent was given to someone who was not given the role. The
    checkbox guard lives here, not in a view: a caller that did not receive an
    explicit confirmation cannot make this write.
    """
    if not checkbox_ticked:
        return GrantResult(GrantOutcome.NOT_CONFIRMED)
    try:
        await assign_role()
    except RoleAssignmentError as exc:
        return GrantResult(GrantOutcome.ROLE_FAILED, reason=str(exc))
    await consents.record_grant(
        discord_user_id=discord_user_id,
        guild_id=guild_id,
        policy_version=policy_version,
        source=source,
        now=now,
    )
    return GrantResult(GrantOutcome.GRANTED)


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
