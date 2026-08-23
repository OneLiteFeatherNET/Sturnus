"""The decisions behind mirroring a guild's names for the console.

The gateway reads and the database writes are tested elsewhere
(`tests/infrastructure/discord/test_directory_sync.py`,
`tests/infrastructure/test_directory.py`). What is decided -- which
people may be named at all, and when a guild's mirror is left alone
rather than emptied -- needs no Discord connection and is tested here.
"""

from __future__ import annotations

from sturnus.application.directory_mirror import (
    DirectorySyncDecision,
    MirroredMember,
    decide_member_mirror,
    members_to_mirror,
    parse_role_id,
)

ANNA, BEN, CARA = 100, 200, 300


def _member(discord_user_id: int, display_name: str = "somebody") -> MirroredMember:
    return MirroredMember(discord_user_id=discord_user_id, display_name=display_name)


def test_a_guild_that_configured_neither_naming_role_is_left_alone() -> None:
    """Mid-`/setup` is not the same fact as "nobody holds either role",
    and an empty write would make the two indistinguishable.
    """
    assert decide_member_mirror([None, None]) is DirectorySyncDecision.SKIP


def test_a_blank_setting_counts_as_unconfigured() -> None:
    """`guild_config` stores text, and a cleared value arrives as an empty
    string rather than as an absent row.
    """
    assert decide_member_mirror(["", "   "]) is DirectorySyncDecision.SKIP


def test_one_configured_role_is_enough_to_start_mirroring() -> None:
    """A guild that has a consent role but no admin role yet still has a
    consent roster the console can name.
    """
    assert decide_member_mirror([None, "42"]) is DirectorySyncDecision.SYNC


def test_a_configured_role_that_now_names_nobody_still_writes() -> None:
    """The clear half of skip-versus-clear: a role that was deleted, or
    that lost its last member, must stop naming people rather than go on
    naming them out of a mirror nothing refreshes.
    """
    assert decide_member_mirror(["42", "43"]) is DirectorySyncDecision.SYNC
    assert members_to_mirror([], []) == []


def test_the_two_role_memberships_are_merged() -> None:
    assert members_to_mirror([_member(ANNA)], [_member(BEN)]) == [_member(ANNA), _member(BEN)]


def test_somebody_who_both_consented_and_administers_is_named_once() -> None:
    """The two groups overlap by design, and `guild_member` is keyed by
    person -- a duplicate would abort the whole guild's write on a
    primary-key violation.
    """
    assert members_to_mirror([_member(ANNA, "Anna")], [_member(ANNA, "Anna")]) == [
        _member(ANNA, "Anna")
    ]


def test_the_same_membership_twice_produces_the_same_write() -> None:
    """Ordered by id: unordered, an unchanged guild would write a
    differently-ordered list on every sweep, and nothing downstream could
    tell a real change from iteration order.
    """
    assert members_to_mirror([_member(CARA), _member(ANNA)], [_member(BEN)]) == [
        _member(ANNA),
        _member(BEN),
        _member(CARA),
    ]


def test_nobody_outside_the_two_roles_is_named() -> None:
    """The bound is the privacy story: mirroring the whole guild would
    copy a Discord user directory into a database that exists to hold
    recordings, for people who consented to nothing.
    """
    assert members_to_mirror([_member(ANNA)], [_member(BEN)]) == [_member(ANNA), _member(BEN)]
    assert not any(member.discord_user_id == CARA for member in members_to_mirror([_member(ANNA)]))


def test_a_role_id_is_read_through_the_whitespace_around_it() -> None:
    """Copy-pasting out of Discord and hand-editing `guild_config` both
    produce this, and neither changes which role is meant.
    """
    assert parse_role_id("  42 ") == 42


def test_a_setting_that_cannot_be_a_role_id_reads_as_no_role() -> None:
    """`guild_config` stores text, so a hand-edited row can hold anything.
    A value nobody can interpret must not stop a sweep that has other
    work to do.
    """
    assert parse_role_id("nonsense") is None


def test_an_absent_or_blank_setting_reads_as_no_role() -> None:
    assert parse_role_id(None) is None
    assert parse_role_id("") is None
