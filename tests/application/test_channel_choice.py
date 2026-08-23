"""Which allowed channels get recorded, in what order, and how many of them.

One test per clause of the rule in `channel_choice.py`. Pure: no Discord,
no configuration, no session -- headcounts in, a decision out, which is
the point of keeping the rule out of the client.

The rule itself is unchanged: most consenting members first, lowest
channel id to break a tie. What is new is that it now orders *every* busy
room rather than naming one, and that how many of them are served is asked
separately (`take`) instead of being assumed to be one.
"""

from __future__ import annotations

from sturnus.application.channel_choice import (
    MAX_CONCURRENT_SESSIONS_PER_GUILD,
    choose_channels,
)


def test_nobody_consenting_anywhere_is_a_decision_to_record_nothing() -> None:
    served = choose_channels({10: 0, 20: 0}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert served.serving == ()
    assert served.waiting == ()


def test_the_only_channel_with_anyone_in_it_is_the_one_recorded() -> None:
    served = choose_channels({10: 0, 20: 2}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert [ranking.channel_id for ranking in served.serving] == [20]
    assert served.serving[0].consenting == 2
    assert served.waiting == ()


def test_the_larger_meeting_wins() -> None:
    """The people who lose the coin toss should be the fewer of them."""
    served = choose_channels({10: 2, 20: 5}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert served.serving[0].channel_id == 20
    assert served.serving[0].consenting == 5


def test_two_meetings_of_the_same_size_are_split_by_the_lower_channel_id() -> None:
    """Any stable tie-break would do; an unstable one would not.

    Without a deterministic answer here, two passes moments apart could
    disagree and the bot would hop between rooms, opening a session row in
    each and recording neither meeting whole.
    """
    served = choose_channels({20: 3, 10: 3, 30: 3}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert served.serving[0].channel_id == 10


def test_the_same_headcounts_always_produce_the_same_answer() -> None:
    """Insertion order must not reach the decision."""
    forwards = choose_channels({10: 3, 20: 3, 30: 4})
    backwards = choose_channels({30: 4, 20: 3, 10: 3})
    assert forwards == backwards


def test_the_rooms_that_are_not_being_served_are_named() -> None:
    """A person sitting in the second room is owed an explanation.

    Named in the order they would be picked up rather than by id: the
    ranking runs the whole way down the list now, so the first room
    reported as waiting is the one a freed connection would take next.
    """
    served = choose_channels({10: 1, 20: 4, 30: 2}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert served.serving[0].channel_id == 20
    assert served.waiting == (30, 10)


def test_an_empty_channel_is_not_reported_as_waiting() -> None:
    """Nobody is waiting in a room nobody is in."""
    assert choose_channels({10: 0, 20: 4}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD).waiting == ()


def test_a_channel_the_process_cannot_see_is_simply_absent() -> None:
    """The caller drops an unreadable channel rather than passing a zero.

    Zero means "everybody left", which is what starts the empty-grace
    countdown; "I could not look" must never be spelled that way.
    """
    served = choose_channels({20: 1}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert served.serving[0].channel_id == 20
    assert served.waiting == ()


# ---------------------------------------------------------------------------
# The ordering past first place, and the limit that decides how much of it
# is used
# ---------------------------------------------------------------------------


def test_every_busy_room_is_ranked_not_only_the_winner() -> None:
    """Second and third place are decided by the same rule as first.

    Nothing consumes more than first place while one voice connection is
    all there is. It is ranked anyway, because an ordering that only its
    head is defined for is an ordering nobody can lift the limit against.
    """
    selection = choose_channels({10: 1, 20: 4, 30: 2, 40: 4})
    assert [ranking.channel_id for ranking in selection.ranked] == [20, 40, 30, 10]


def test_the_tie_break_applies_below_first_place_too() -> None:
    """A tie for second is settled the same way a tie for first is."""
    selection = choose_channels({30: 2, 10: 5, 20: 2})
    assert [ranking.channel_id for ranking in selection.ranked] == [10, 20, 30]


def test_each_ranked_room_carries_its_own_headcount() -> None:
    """A caller serving more than the first room needs that room's count,
    not the winner's: it is what opens the session against the right
    number of consenting people."""
    selection = choose_channels({10: 1, 20: 4})
    assert [(r.channel_id, r.consenting) for r in selection.ranked] == [(20, 4), (10, 1)]


def test_only_as_many_rooms_as_the_limit_allows_are_served() -> None:
    """The point of the whole split: the ranking is a fact about the
    meetings, the limit is a fact about the process recording them."""
    selection = choose_channels({10: 1, 20: 4, 30: 2})
    served = selection.take(2)
    assert [ranking.channel_id for ranking in served.serving] == [20, 30]
    assert served.waiting == (10,)


def test_the_limit_in_force_today_serves_exactly_one_room() -> None:
    """One bot identity holds one voice connection per guild. Pinned so
    that a change to the constant is a change somebody made on purpose."""
    assert MAX_CONCURRENT_SESSIONS_PER_GUILD == 1
    served = choose_channels({10: 1, 20: 4, 30: 2}).take(MAX_CONCURRENT_SESSIONS_PER_GUILD)
    assert len(served.serving) == 1


def test_a_limit_larger_than_the_number_of_busy_rooms_leaves_nobody_waiting() -> None:
    served = choose_channels({10: 1, 20: 4}).take(5)
    assert [ranking.channel_id for ranking in served.serving] == [20, 10]
    assert served.waiting == ()
