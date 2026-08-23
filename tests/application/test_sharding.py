"""The one place the "one process, every guild" assumption is written down.

These tests are deliberately about a *predicate that is always true today*.
That is the point: `sturnus.application.sharding` exists so that stage two
-- several bot processes, each owning a range of shards -- is a change to
one function body and whatever the type checker then points at, rather than
an archaeology exercise across every sweep that reads rows for all guilds.
"""

from __future__ import annotations

import pytest

from sturnus.application.sharding import (
    MIN_SHARD_COUNT,
    PROCESS_HOLDS_EVERY_SHARD,
    process_serves_guild,
    shard_id_for_logging,
    shard_of,
    shards_this_process_owns,
)

#: Snowflakes whose shard ids differ under a four-shard cluster. Discord's
#: own rule is `(guild_id >> 22) % shard_count`, so the low 22 bits are the
#: timestamp-internal part and change nothing.
FIRST_GUILD = 1 << 22
SECOND_GUILD = 2 << 22
FIFTH_GUILD = 5 << 22


def test_a_guild_lands_on_the_shard_discord_would_route_it_to() -> None:
    """`shard_of` must agree with Discord's own routing rule, or a log line lies.

    The whole value of a `shard_id` field is that an operator can join it
    to what Discord and discord.py believe. `(guild_id >> 22) % shard_count`
    is that rule, and it is the same arithmetic `discord.Guild.shard_id`
    performs.
    """
    assert shard_of(FIRST_GUILD, 4) == 1
    assert shard_of(SECOND_GUILD, 4) == 2
    assert shard_of(FIFTH_GUILD, 4) == 1


def test_an_unlaunched_client_puts_every_guild_on_shard_zero() -> None:
    """A shard count of `None` is "the gateway has not told us yet", not a crash.

    `discord.Client.shard_count` is `None` until `launch_shards` has run,
    and `discord.Guild.shard_id` answers `0` in that window rather than
    dividing by nothing. Anything derived from it here must do the same.
    """
    assert shard_of(FIRST_GUILD, None) == 0
    assert shard_of(SECOND_GUILD, None) == 0


def test_this_process_owns_every_shard_it_was_told_about() -> None:
    """Stage one is one process holding all N shards, and this says so.

    Stage two makes this function return a *range* -- and it is the only
    function that has to change for the predicate below to start refusing
    guilds that belong to another pod.
    """
    assert shards_this_process_owns(4) == frozenset({0, 1, 2, 3})
    assert shards_this_process_owns(1) == frozenset({0})
    assert shards_this_process_owns(None) == frozenset({0})


@pytest.mark.parametrize("guild_id", [FIRST_GUILD, SECOND_GUILD, FIFTH_GUILD])
@pytest.mark.parametrize("shard_count", [None, 1, 4, 16])
def test_every_guild_is_served_by_this_process_today(
    guild_id: int, shard_count: int | None
) -> None:
    """The invariant itself: one process, so every guild is its business.

    A database sweep -- the announcement poll is the one that matters --
    reads rows for every guild there is, and today it is right to act on
    all of them. This test is what turns "obviously" into "asserted", so
    the day `PROCESS_HOLDS_EVERY_SHARD` stops being true, the sweeps that
    depend on it fail here rather than double-posting in production.
    """
    assert PROCESS_HOLDS_EVERY_SHARD is True
    assert process_serves_guild(guild_id, shard_count) is True


def test_a_single_shard_process_adds_no_shard_field_to_a_log_line() -> None:
    """One shard means `shard_id=0` on every line, which is noise, not signal.

    A field whose value never varies costs a key in every Loki stream and
    answers no question anyone could ask. It earns its place only once
    there is more than one shard to tell apart. `None` is how that absence
    is expressed at a call site, because rule R3 forbids `**kwargs` into a
    log event and `scrub_fields` drops a `None` rather than writing null.
    """
    assert shard_id_for_logging(FIRST_GUILD, None) is None
    assert shard_id_for_logging(FIRST_GUILD, 1) is None


def test_more_than_one_shard_attributes_a_guild_line_to_its_shard() -> None:
    """With several shards, "which shard is stalling" becomes answerable.

    `shard_id` is derivable from `guild_id` and the shard count, but not
    in LogQL -- so without the field the grouping an operator actually
    wants cannot be expressed at query time.
    """
    assert shard_id_for_logging(FIRST_GUILD, 4) == 1
    assert shard_id_for_logging(SECOND_GUILD, 4) == 2


def test_the_smallest_meaningful_shard_count_is_one() -> None:
    """Zero shards is not a smaller deployment; it is a bot that never connects."""
    assert MIN_SHARD_COUNT == 1
