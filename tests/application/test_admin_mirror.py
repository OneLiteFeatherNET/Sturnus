"""Deciding which Discord members the bot should mirror as administrators.

The decision is pure and lives here; the gateway lookup and the database
write are the adapter's business. The reason for the split is that every
interesting case -- no role configured, a role that no longer exists, an
empty role -- is a decision, and none of them needs a Discord connection
to test.
"""

import pytest

from sturnus.application.admin_mirror import AdminSyncDecision, decide_admin_sync


def test_a_guild_with_a_configured_role_is_synced() -> None:
    decision = decide_admin_sync(configured_role_id="42", role_exists=True)
    assert decision == AdminSyncDecision.SYNC


def test_a_guild_with_no_configured_role_is_left_alone() -> None:
    """Not cleared: a guild that has not finished `/setup` has no
    administrators *yet*, which is different from having none.

    Clearing here would be indistinguishable from a real removal, and the
    difference matters -- an unconfigured guild is mid-setup, a cleared one
    is a decision somebody made.
    """
    assert decide_admin_sync(configured_role_id=None, role_exists=False) == AdminSyncDecision.SKIP


def test_a_configured_role_that_no_longer_exists_clears_the_mirror() -> None:
    """A deleted role grants nothing, so the mirror must not keep granting
    on its behalf. This is the case where staleness becomes a standing
    privilege: nobody holds a role that does not exist, and leaving the
    rows would let its former members administer forever.
    """
    assert decide_admin_sync(configured_role_id="42", role_exists=False) == AdminSyncDecision.CLEAR


def test_a_role_id_that_is_not_a_number_clears_rather_than_crashes() -> None:
    """`guild_config` stores text, and `admin_role_id` is not in
    `INTEGER_KEYS` on this branch -- so a hand-edited row can hold
    anything. Treating unparseable as "no valid role" clears the mirror,
    which is the safe direction: it removes access rather than granting it.
    """
    assert decide_admin_sync(configured_role_id="nonsense", role_exists=True) == (
        AdminSyncDecision.CLEAR
    )


def test_an_empty_configured_value_is_the_same_as_none() -> None:
    assert decide_admin_sync(configured_role_id="", role_exists=False) == AdminSyncDecision.SKIP
    assert decide_admin_sync(configured_role_id="   ", role_exists=False) == AdminSyncDecision.SKIP


@pytest.mark.parametrize("role_id", ["42", " 42 ", "0042"])
def test_a_role_id_is_read_leniently(role_id: str) -> None:
    """Whitespace and leading zeroes come from hand-editing and from
    copy-pasting out of Discord, and neither changes which role is meant.
    """
    assert decide_admin_sync(configured_role_id=role_id, role_exists=True) == AdminSyncDecision.SYNC
