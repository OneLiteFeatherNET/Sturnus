"""The bot mirroring its administrators into a table the API can read.

The decision itself is tested in `tests/application/test_admin_mirror.py`;
what is tested here is the adapter around it -- that the gateway is asked
for the right role, that what comes back is written, and that the two
cases which must not be confused (nothing configured, role deleted) reach
the store differently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import discord

from sturnus.infrastructure.discord.admin_sync import sync_administrators

GUILD_ID = 1
ROLE_ID = 42
ANNA, BEN = 100, 200
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


class FakeStore:
    def __init__(self) -> None:
        self.replaced: list[tuple[int, list[int]]] = []

    async def replace(self, guild_id: int, ids: list[int], _now: datetime) -> None:
        self.replaced.append((guild_id, sorted(ids)))


class FakeConfig:
    def __init__(self, role_id: str | None) -> None:
        self._role_id = role_id

    async def get(self, _guild_id: int, _key: str) -> str | None:
        return self._role_id


def _guild(role: object | None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD_ID
    guild.get_role = MagicMock(return_value=role)
    return guild


def _role(*member_ids: int) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.members = [MagicMock(spec=discord.Member, id=member_id) for member_id in member_ids]
    return role


async def test_the_members_of_the_configured_role_are_mirrored() -> None:
    store = FakeStore()
    await sync_administrators(_guild(_role(ANNA, BEN)), FakeConfig(str(ROLE_ID)), store, T0)
    assert store.replaced == [(GUILD_ID, [ANNA, BEN])]


async def test_a_guild_with_no_configured_role_is_not_written_at_all() -> None:
    """Skip, not clear. A guild mid-`/setup` has no administrators yet,
    which is a different fact from having none -- and writing an empty
    membership would make the two indistinguishable.
    """
    store = FakeStore()
    await sync_administrators(_guild(None), FakeConfig(None), store, T0)
    assert store.replaced == []


async def test_a_configured_role_that_was_deleted_clears_the_mirror() -> None:
    """The case where staleness becomes a standing privilege: nobody holds
    a role that does not exist, so its former members must stop being
    administrators rather than stay ones forever.
    """
    store = FakeStore()
    await sync_administrators(_guild(None), FakeConfig(str(ROLE_ID)), store, T0)
    assert store.replaced == [(GUILD_ID, [])]


async def test_an_empty_role_clears_the_mirror() -> None:
    """A role that exists with nobody in it grants nothing."""
    store = FakeStore()
    await sync_administrators(_guild(_role()), FakeConfig(str(ROLE_ID)), store, T0)
    assert store.replaced == [(GUILD_ID, [])]


async def test_the_role_is_looked_up_by_the_configured_id() -> None:
    """A sync that read the wrong role would mirror the wrong people, and
    every downstream check would then be confidently wrong.
    """
    guild = _guild(_role(ANNA))
    await sync_administrators(guild, FakeConfig(str(ROLE_ID)), FakeStore(), T0)
    guild.get_role.assert_called_once_with(ROLE_ID)


async def test_an_unparseable_configured_value_clears_rather_than_raising() -> None:
    """`guild_config` stores text and this key is not integer-validated, so
    a hand-edited row can hold anything. Clearing errs towards removing
    access rather than granting it, and never towards a sweep that dies.
    """
    store = FakeStore()
    await sync_administrators(_guild(_role(ANNA)), FakeConfig("nonsense"), store, T0)
    assert store.replaced == [(GUILD_ID, [])]
