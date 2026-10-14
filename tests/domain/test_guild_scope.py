"""Which guild an installation serves (openspec change `single-guild-instance`).

The predicate is deliberately a comparison of two ids and nothing else: a
list of served guilds of length one invites a list of two.
"""

import pytest

from sturnus.domain.guild_scope import serves_guild


def test_the_configured_guild_is_served() -> None:
    assert serves_guild(configured_guild_id=1234, guild_id=1234) is True


def test_any_other_guild_is_not_served() -> None:
    assert serves_guild(configured_guild_id=1234, guild_id=4321) is False


@pytest.mark.parametrize("guild_id", [0, 1, 2**63 - 1])
def test_no_id_is_special(guild_id: int) -> None:
    """Totality: a guild is served exactly when it is the configured one."""
    assert serves_guild(configured_guild_id=guild_id, guild_id=guild_id) is True
    assert serves_guild(configured_guild_id=guild_id, guild_id=guild_id + 1) is False
