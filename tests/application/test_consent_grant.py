"""The consent grant shared by `/consent grant` and the channel prompt.

The role assignment and the record write are fakes that log their calls, so
the tests pin the order and the outcome without a gateway or a database.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sturnus.application.consent_flow import (
    GrantOutcome,
    RoleAssignmentError,
    grant_consent,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
POLICY = "2026-08-01"
GUILD = 900
ANNA = 100


class _Writer:
    """Records every grant written, and logs the write into the shared call list."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.grants: list[tuple[int, int, str, str, datetime]] = []

    async def record_grant(
        self,
        *,
        discord_user_id: int,
        guild_id: int,
        policy_version: str,
        source: str,
        now: datetime,
    ) -> None:
        self._calls.append("record")
        self.grants.append((discord_user_id, guild_id, policy_version, source, now))


def _role(calls: list[str], failure: str | None = None) -> Callable[[], Awaitable[None]]:
    """An `assign_role` that logs the call, or raises the given failure."""

    async def assign() -> None:
        calls.append("role")
        if failure is not None:
            raise RoleAssignmentError(failure)

    return assign


async def test_a_ticked_checkbox_assigns_the_role_before_recording() -> None:
    calls: list[str] = []
    await grant_consent(
        discord_user_id=ANNA,
        guild_id=GUILD,
        policy_version=POLICY,
        source="button",
        now=T0,
        checkbox_ticked=True,
        assign_role=_role(calls),
        consents=_Writer(calls),
    )
    assert calls == ["role", "record"], "the role is assigned first, then the grant is recorded"


async def test_a_ticked_checkbox_reports_the_grant_as_granted() -> None:
    calls: list[str] = []
    result = await grant_consent(
        discord_user_id=ANNA,
        guild_id=GUILD,
        policy_version=POLICY,
        source="button",
        now=T0,
        checkbox_ticked=True,
        assign_role=_role(calls),
        consents=_Writer(calls),
    )
    assert result.outcome is GrantOutcome.GRANTED


async def test_a_ticked_grant_writes_the_same_record_as_the_slash_command() -> None:
    """Same inputs, same row: the policy version and source are passed through unchanged."""
    calls: list[str] = []
    writer = _Writer(calls)
    await grant_consent(
        discord_user_id=ANNA,
        guild_id=GUILD,
        policy_version=POLICY,
        source="button",
        now=T0,
        checkbox_ticked=True,
        assign_role=_role(calls),
        consents=writer,
    )
    assert writer.grants == [(ANNA, GUILD, POLICY, "button", T0)]


async def test_a_role_that_cannot_be_assigned_records_no_grant() -> None:
    """A record without the role would let someone believe they may speak."""
    calls: list[str] = []
    writer = _Writer(calls)
    await grant_consent(
        discord_user_id=ANNA,
        guild_id=GUILD,
        policy_version=POLICY,
        source="button",
        now=T0,
        checkbox_ticked=True,
        assign_role=_role(calls, failure="missing permissions"),
        consents=writer,
    )
    assert writer.grants == [], "no grant may be recorded when the role was not assigned"


async def test_a_role_that_cannot_be_assigned_reports_the_reason() -> None:
    calls: list[str] = []
    result = await grant_consent(
        discord_user_id=ANNA,
        guild_id=GUILD,
        policy_version=POLICY,
        source="button",
        now=T0,
        checkbox_ticked=True,
        assign_role=_role(calls, failure="missing permissions"),
        consents=_Writer(calls),
    )
    assert result.outcome is GrantOutcome.ROLE_FAILED
    assert result.reason == "missing permissions"


async def test_an_unticked_checkbox_writes_nothing() -> None:
    """The guard is in the use case, so no caller can grant without the tick."""
    calls: list[str] = []
    writer = _Writer(calls)
    result = await grant_consent(
        discord_user_id=ANNA,
        guild_id=GUILD,
        policy_version=POLICY,
        source="prompt",
        now=T0,
        checkbox_ticked=False,
        assign_role=_role(calls),
        consents=writer,
    )
    assert calls == [], "neither the role nor the record may be touched without the tick"
    assert writer.grants == []
    assert result.outcome is GrantOutcome.NOT_CONFIRMED
