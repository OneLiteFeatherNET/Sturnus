"""Turning "this one goes first" into the integers the claim reads.

Two rules carry every test here.

**An order is only ever expressed by holding sessions back.** No function
in this module may lower a priority, because the queue is shared with
every other guild: a reorder that could write a smaller number would be a
control by which any one administrator jumps their whole guild ahead of
everybody else's ordinary work.

**Null is not zero.** A session nothing has measured has no length, and a
rule that ordered by length must not read that absence as "nought
seconds", which would promote every unmeasured session to the front on
the strength of knowing nothing about it.
"""

from __future__ import annotations

import pytest

from sturnus.application.priorities import (
    KNOWN_RULES,
    Placement,
    QueuedSession,
    UnknownPriorityRule,
    claim_order,
    order_by_rule,
    order_with,
    priorities_for,
    resolve_rule,
)

# Ids ascending, because the claim breaks a tie by id and every test here
# depends on knowing which way that tie falls.
FIRST, SECOND, THIRD, FOURTH = 10, 20, 30, 40


def session(
    session_id: int,
    priority: int = 0,
    participants: int = 1,
    audio_seconds: float | None = None,
) -> QueuedSession:
    return QueuedSession(
        id=session_id,
        priority=priority,
        participants=participants,
        audio_seconds=audio_seconds,
    )


# ---------------------------------------------------------------------------
# The order the claim would take them in
# ---------------------------------------------------------------------------


def test_an_untouched_queue_runs_oldest_first() -> None:
    """Every session at the ordinary priority is the queue as it shipped."""
    sessions = [session(SECOND), session(FIRST), session(THIRD)]

    assert [row.id for row in claim_order(sessions)] == [FIRST, SECOND, THIRD]


def test_a_lower_priority_runs_before_an_older_session() -> None:
    """Lower first, which is the `nice(1)` sense the column was given."""
    sessions = [session(FIRST, priority=1), session(SECOND, priority=0)]

    assert [row.id for row in claim_order(sessions)] == [SECOND, FIRST]


# ---------------------------------------------------------------------------
# A drag, turned into integers
# ---------------------------------------------------------------------------


def test_a_session_dragged_to_the_front_is_first() -> None:
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    order = order_with(sessions, THIRD, Placement("first"))

    assert order == (THIRD, FIRST, SECOND)


def test_a_session_dragged_behind_another_sits_directly_after_it() -> None:
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    order = order_with(sessions, FIRST, Placement("after", anchor=SECOND))

    assert order == (SECOND, FIRST, THIRD)


def test_a_session_dragged_in_front_of_another_sits_directly_before_it() -> None:
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    order = order_with(sessions, THIRD, Placement("before", anchor=SECOND))

    assert order == (FIRST, THIRD, SECOND)


def test_a_session_dragged_to_the_end_is_last() -> None:
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    order = order_with(sessions, FIRST, Placement("last"))

    assert order == (SECOND, THIRD, FIRST)


def test_a_drag_of_a_session_that_is_no_longer_queued_is_refused() -> None:
    """The list the browser was showing has moved on.

    `None` rather than an exception or a silent no-op: the caller turns it
    into a refusal that carries the queue as it now is, so the page can
    redraw instead of the drag being applied to a queue nobody looked at.
    """
    sessions = [session(FIRST), session(SECOND)]

    assert order_with(sessions, THIRD, Placement("first")) is None


def test_a_drag_against_an_anchor_that_is_no_longer_queued_is_refused() -> None:
    sessions = [session(FIRST), session(SECOND)]

    assert order_with(sessions, FIRST, Placement("after", anchor=THIRD)) is None


# ---------------------------------------------------------------------------
# The integers themselves
# ---------------------------------------------------------------------------


def test_an_order_that_already_holds_writes_nothing() -> None:
    """Idempotence, and it is not a nicety.

    Two administrators who agree, a quick action applied twice, and a page
    that re-sends what it is already showing must all cost nothing --
    otherwise every one of them would push the whole queue further back.
    """
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    assert priorities_for(sessions, (FIRST, SECOND, THIRD)) == {}


def test_going_first_holds_back_only_what_it_overtakes() -> None:
    """The moved session keeps its number; the ones it passed lose theirs.

    THIRD goes to the front of a queue that is entirely ordinary, so
    FIRST and SECOND -- the two it overtook -- move to 1, and THIRD stays
    at 0. Nothing else in the deployment is touched.
    """
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    assert priorities_for(sessions, (THIRD, FIRST, SECOND)) == {FIRST: 1, SECOND: 1}


def test_a_session_that_already_sorts_after_its_predecessor_keeps_its_number() -> None:
    """Ascending ids need no new integer at all.

    FIRST at 0 and SECOND at 0 already run in that order, because the
    claim breaks the tie by id -- so putting THIRD last writes nothing for
    any of them.
    """
    sessions = [session(FIRST), session(SECOND), session(THIRD)]

    assert priorities_for(sessions, (FIRST, SECOND, THIRD)) == {}


def test_no_reorder_ever_lowers_a_priority() -> None:
    """The property that makes this endpoint safe to expose at all.

    A guild expresses an order over its own queue by holding its own
    sessions back, never by writing a smaller number -- so no
    administrator can move their guild ahead of another guild's ordinary
    work, however they drag.
    """
    sessions = [session(FIRST, priority=3), session(SECOND, priority=3), session(THIRD)]
    before = {row.id: row.priority for row in sessions}

    for order in ((THIRD, FIRST, SECOND), (SECOND, THIRD, FIRST), (FIRST, SECOND, THIRD)):
        for session_id, priority in priorities_for(sessions, order).items():
            assert priority >= before[session_id]


def test_the_written_numbers_reproduce_the_order_that_was_asked_for() -> None:
    """The whole contract, checked by replaying the claim's own sort."""
    sessions = [session(FIRST), session(SECOND), session(THIRD), session(FOURTH)]
    wanted = (THIRD, FIRST, FOURTH, SECOND)

    written = priorities_for(sessions, wanted)
    applied = [session(row.id, priority=written.get(row.id, row.priority)) for row in sessions]

    assert tuple(row.id for row in claim_order(applied)) == wanted


# ---------------------------------------------------------------------------
# The quick actions
# ---------------------------------------------------------------------------


def test_the_biggest_meeting_runs_first() -> None:
    sessions = [
        session(FIRST, participants=2),
        session(SECOND, participants=8),
        session(THIRD, participants=5),
    ]

    order = order_by_rule(sessions, resolve_rule("many-participants-first"))

    assert order == (SECOND, THIRD, FIRST)


def test_meetings_of_the_same_size_keep_the_order_they_had() -> None:
    """A quick action reorders; it does not shuffle what it has no opinion on."""
    sessions = [
        session(SECOND, participants=3),
        session(FIRST, participants=3),
        session(THIRD, participants=9),
    ]

    order = order_by_rule(sessions, resolve_rule("many-participants-first"))

    assert order == (THIRD, FIRST, SECOND)


def test_the_shortest_recording_runs_first() -> None:
    sessions = [
        session(FIRST, audio_seconds=600.0),
        session(SECOND, audio_seconds=60.0),
        session(THIRD, audio_seconds=300.0),
    ]

    order = order_by_rule(sessions, resolve_rule("short-recordings-first"))

    assert order == (SECOND, THIRD, FIRST)


def test_a_recording_nobody_has_measured_is_not_treated_as_the_shortest() -> None:
    """Null is not zero, and here that distinction decides the whole order.

    A session whose tracks have never been transcribed has no
    `audio_seconds` at all. Read as nought it would be the shortest
    recording in the queue and would go to the front on the strength of
    nothing being known about it, which is the opposite of what the rule
    promises.
    """
    sessions = [
        session(FIRST, audio_seconds=None),
        session(SECOND, audio_seconds=90.0),
        session(THIRD, audio_seconds=45.0),
    ]

    order = order_by_rule(sessions, resolve_rule("short-recordings-first"))

    assert order == (THIRD, SECOND, FIRST)


def test_unmeasured_recordings_keep_the_order_they_had_among_themselves() -> None:
    sessions = [
        session(SECOND, audio_seconds=None),
        session(FIRST, audio_seconds=None),
        session(THIRD, audio_seconds=30.0),
    ]

    order = order_by_rule(sessions, resolve_rule("short-recordings-first"))

    assert order == (THIRD, FIRST, SECOND)


def test_a_rule_nobody_has_is_refused_by_name() -> None:
    """The refusal names both what was asked for and what there is.

    The same trade `transcription_models.resolve` makes, for the same
    reason: a rule name is a fixed literal of this repository, so a caller
    who mistyped one can be shown the list without disclosing anything.
    """
    with pytest.raises(UnknownPriorityRule) as refused:
        resolve_rule("longest-first")

    assert "longest-first" in str(refused.value)
    assert "many-participants-first" in str(refused.value)


def test_every_known_rule_can_be_resolved() -> None:
    """The registry and the resolver cannot drift apart."""
    for name in KNOWN_RULES:
        assert resolve_rule(name) is not None
