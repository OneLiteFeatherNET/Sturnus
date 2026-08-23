"""Which allowed channel gets the one voice connection a guild has.

One test per clause of the rule in `channel_choice.py`. Pure: no Discord,
no configuration, no session -- headcounts in, a decision out, which is
the point of keeping the rule out of the client.
"""

from __future__ import annotations

from sturnus.application.channel_choice import choose_channel


def test_nobody_consenting_anywhere_is_a_decision_to_record_nothing() -> None:
    choice = choose_channel({10: 0, 20: 0})
    assert choice.channel_id is None
    assert choice.consenting == 0
    assert choice.waiting == ()


def test_the_only_channel_with_anyone_in_it_is_the_one_recorded() -> None:
    choice = choose_channel({10: 0, 20: 2})
    assert choice.channel_id == 20
    assert choice.consenting == 2
    assert choice.waiting == ()


def test_the_larger_meeting_wins() -> None:
    """The people who lose the coin toss should be the fewer of them."""
    choice = choose_channel({10: 2, 20: 5})
    assert choice.channel_id == 20
    assert choice.consenting == 5


def test_two_meetings_of_the_same_size_are_split_by_the_lower_channel_id() -> None:
    """Any stable tie-break would do; an unstable one would not.

    Without a deterministic answer here, two passes moments apart could
    disagree and the bot would hop between rooms, opening a session row in
    each and recording neither meeting whole.
    """
    choice = choose_channel({20: 3, 10: 3, 30: 3})
    assert choice.channel_id == 10


def test_the_same_headcounts_always_produce_the_same_answer() -> None:
    """Insertion order must not reach the decision."""
    forwards = choose_channel({10: 3, 20: 3, 30: 4})
    backwards = choose_channel({30: 4, 20: 3, 10: 3})
    assert forwards == backwards


def test_the_rooms_that_are_not_being_served_are_named() -> None:
    """A person sitting in the second room is owed an explanation."""
    choice = choose_channel({10: 1, 20: 4, 30: 2})
    assert choice.channel_id == 20
    assert choice.waiting == (10, 30)


def test_an_empty_channel_is_not_reported_as_waiting() -> None:
    """Nobody is waiting in a room nobody is in."""
    choice = choose_channel({10: 0, 20: 4})
    assert choice.waiting == ()


def test_a_channel_the_process_cannot_see_is_simply_absent() -> None:
    """The caller drops an unreadable channel rather than passing a zero.

    Zero means "everybody left", which is what starts the empty-grace
    countdown; "I could not look" must never be spelled that way.
    """
    choice = choose_channel({20: 1})
    assert choice.channel_id == 20
    assert choice.waiting == ()
