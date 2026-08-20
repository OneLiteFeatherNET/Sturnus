"""Tests for the admin-access check (Spec 11).

`_has_admin_access` is exercised directly rather than through
`app_commands.check`'s wrapping machinery -- it is the free function
`require_admin()` installs as the check predicate, so calling it directly
tests exactly the code that decides who may run administrative commands.
"""

from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.discord.permissions import _has_admin_access

GUILD_ID = 4711
ROLE_ID = 999


class _StubStore(ConfigStore):
    """A `ConfigStore` that returns a fixed value without touching a database.

    `isinstance(store, ConfigStore)` (checked by `_config_store`) must pass,
    which requires a real subclass rather than a bare mock; the base
    `__init__` is deliberately not called since nothing here ever reaches
    `_session_factory`.
    """

    def __init__(self, stored_admin_role_id: str | None) -> None:
        self._stored = stored_admin_role_id

    async def get(self, _guild_id: int, key: str) -> str | None:
        assert key == settings.ADMIN_ROLE_ID
        return self._stored


def _member(*, is_admin: bool, role_ids: list[int]) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.guild_permissions.administrator = is_admin
    member.guild.id = GUILD_ID
    member.roles = [MagicMock(spec=discord.Role, id=role_id) for role_id in role_ids]
    return member


def _interaction(member: discord.Member, store: ConfigStore | None) -> discord.Interaction:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.client = MagicMock()
    interaction.client._config_store = store
    return interaction


async def test_an_unparseable_stored_value_denies_rather_than_raises() -> None:
    """A bad value settable through `/config set` must not crash the check."""
    member = _member(is_admin=False, role_ids=[ROLE_ID])
    store = _StubStore("moderator")
    interaction = _interaction(member, store)

    with pytest.raises(app_commands.MissingRole):
        await _has_admin_access(interaction)


async def test_discord_administrator_passes_without_a_configured_role() -> None:
    """A guild that has not set `admin_role_id` must not lock itself out."""
    member = _member(is_admin=True, role_ids=[])
    interaction = _interaction(member, store=None)

    assert await _has_admin_access(interaction) is True


async def test_a_holder_of_the_configured_role_passes() -> None:
    member = _member(is_admin=False, role_ids=[ROLE_ID])
    store = _StubStore(str(ROLE_ID))
    interaction = _interaction(member, store)

    assert await _has_admin_access(interaction) is True


async def test_nothing_configured_denies_every_non_administrator() -> None:
    member = _member(is_admin=False, role_ids=[])
    interaction = _interaction(member, store=None)

    with pytest.raises(app_commands.MissingRole):
        await _has_admin_access(interaction)


async def test_a_non_member_user_is_rejected_as_a_private_message() -> None:
    user = MagicMock(spec=discord.User)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user

    with pytest.raises(app_commands.NoPrivateMessage):
        await _has_admin_access(interaction)
