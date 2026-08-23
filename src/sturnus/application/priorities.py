"""What order a guild's queued sessions should run in, as arithmetic.

`transcription_job.priority` is one integer per row, **lower first**, and
`JobQueue.claim` reads `ORDER BY priority, id`. This module holds the two
halves of turning a human's intent into those integers: what order was
asked for, and which numbers express it. Neither half touches a database,
so both are tested against plain values and there is exactly one
definition of each rule -- the same arrangement
`sturnus.application.requeue.plan_requeue` and
`sturnus.application.retention.expired_jobs` are in, and for the same
reason.

**A session, never a job, is the unit.** The rows are one per speaker, but
nobody in a console drags a speaker: they drag a meeting. A request that
took job ids would let a caller reorder four of a meeting's five speakers
and leave the fifth behind whatever it was behind -- a queue that is
half-reordered in a way no page renders and nobody could see. So a
session's jobs carry one priority between them, this module reasons in
sessions, and the write applies a session's number to every one of its
jobs at once.

**A reorder only ever holds sessions back; it never moves one forward.**
`priorities_for` may raise a number and may leave one alone. It may not
lower one, and that is a safety property rather than an implementation
detail. The queue is shared by every guild in the deployment, `0` is what
untouched work carries, and this arithmetic is reachable by any guild
administrator through the console -- so a function that could write a
smaller number would be a control by which the first administrator to
find it puts their whole guild in front of everybody else's ordinary
work, permanently and without anyone being told. Going first is therefore
expressed as everything that was ahead going second, which says the same
thing about the guild's own queue and says nothing at all about anybody
else's.

The cost of that choice, stated plainly because it is real: holding a
session back holds it back *globally*, not merely within its guild. A
session at `1` sorts behind every untouched job in the deployment, not
only behind its own guild's. See `docs/operations.md` section 6.2.11.

**Idempotence.** `priorities_for` returns only the sessions whose number
must change, so re-sending an order that already holds writes nothing.
Two administrators who agree, a quick action applied twice, and a page
re-sending what it is already showing all cost one read and no writes --
which matters more than it looks, because the alternative is a queue that
drifts further back every time somebody looks at it.

**Null is not zero.** `short_recordings_first` reads
`transcription_job.audio_seconds`, which is null for a recording nothing
has ever measured -- every session that has not been transcribed yet, and
every job that predates the column. Read as nought, an unmeasured session
would be the shortest recording in the queue and would be promoted to the
front on the strength of nothing being known about it. Unmeasured
sessions therefore rank *after* every measured one and keep the order they
already had, which is the same distinction `sturnus.console.statistics`
refuses to lose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

#: How a session may be placed relative to the queue it is in. Exactly the
#: four things a drag-and-drop list can produce, and deliberately not an
#: index: an index is an absolute claim about a list the browser was
#: showing a moment ago, and two administrators dragging at once would
#: each be numbering a different list. An anchor still means something
#: after somebody else's move landed.
PLACEMENTS: tuple[str, ...] = ("first", "last", "before", "after")

#: The placements that name another session to sit beside.
_ANCHORED: tuple[str, ...] = ("before", "after")


@dataclass(frozen=True)
class QueuedSession:
    """One session with transcription work outstanding, as the queue sees it.

    `priority` is the number its outstanding jobs carry; `participants` is
    how many people `session_participant` recorded; `audio_seconds` is how
    much audio anything has measured for it, and is `None` when nothing
    has -- see the module docstring on why that is not zero.
    """

    id: int
    priority: int
    participants: int
    audio_seconds: float | None


@dataclass(frozen=True)
class Placement:
    """Where a dragged session was dropped.

    `anchor` is required by `before` and `after` and meaningless to the
    other two; `is_valid` is what a boundary checks before building one,
    so a malformed request is refused where it can still be explained.
    """

    where: str
    anchor: int | None = None

    @property
    def is_valid(self) -> bool:
        if self.where not in PLACEMENTS:
            return False
        return (self.anchor is not None) == (self.where in _ANCHORED)


class UnknownPriorityRule(Exception):
    """A quick action nobody has. Names what was asked for and what there is.

    The same shape `sturnus.domain.transcription_models.UnknownTranscriptionModel`
    has, and for the same reason: the registry is closed and its names are
    literals of this repository, so a caller who mistyped one can be shown
    the whole list without anything being disclosed.
    """


#: The sort key a quick action ranks by. A rule is *only* a key function:
#: adding the third one the owner will ask for next month is one function
#: and one line in `KNOWN_RULES`, and it cannot get the tie-breaking, the
#: integers or the write wrong because it does none of them.
Rule = Callable[[QueuedSession], tuple[object, ...]]


def many_participants_first(session: QueuedSession) -> tuple[object, ...]:
    """Biggest meeting first.

    A meeting of eight people is eight jobs and eight speakers waiting on
    one document; a one-person recording is one. Negated rather than
    reverse-sorted so that every rule in this module sorts ascending and
    ties keep the order they arrived in.
    """
    return (-session.participants,)


def short_recordings_first(session: QueuedSession) -> tuple[object, ...]:
    """Least audio first, and unmeasured audio last.

    The leading flag is the whole of the null rule: `False` sorts before
    `True`, so everything with a measurement is ranked before everything
    without one, and the unmeasured sessions then keep the order they had
    among themselves rather than being ordered by a zero nobody measured.
    """
    return (session.audio_seconds is None, session.audio_seconds or 0.0)


#: Every quick action, by the name a request may use. Fixed literals of
#: this repository, which is what makes an unknown one safe to echo back.
KNOWN_RULES: dict[str, Rule] = {
    "many-participants-first": many_participants_first,
    "short-recordings-first": short_recordings_first,
}


def resolve_rule(name: str) -> Rule:
    """The rule that name means, or `UnknownPriorityRule`.

    Refused rather than fallen back on. A quick action whose name was not
    understood and which silently ran a different one would reorder a
    guild's queue by a rule nobody asked for, and the administrator would
    have no way to tell that from the rule working.
    """
    rule = KNOWN_RULES.get(name)
    if rule is None:
        known = ", ".join(sorted(KNOWN_RULES))
        raise UnknownPriorityRule(f"no such queue rule: {name!r}; known rules are {known}")
    return rule


def claim_order(sessions: Iterable[QueuedSession]) -> tuple[QueuedSession, ...]:
    """The sessions in the order `JobQueue.claim` would reach them.

    `(priority, id)` ascending, which is the claim's `ORDER BY priority,
    id` said in Python. Every other function here is defined in terms of
    this one, so "where a session is now" has a single definition and the
    console is shown the same order the worker will actually work in.
    """
    return tuple(sorted(sessions, key=lambda session: (session.priority, session.id)))


def order_with(
    sessions: Sequence[QueuedSession], session_id: int, placement: Placement
) -> tuple[int, ...] | None:
    """The order this queue has after one session is dropped somewhere.

    `None` when the dragged session or the session it was dropped beside
    is no longer in this queue -- documented while the page was open, or
    never in it. That is a refusal for the caller to report along with the
    queue as it now stands, never a silent no-op: a drag that appeared to
    work and changed nothing is the failure this endpoint exists to make
    impossible.
    """
    order = [session.id for session in claim_order(sessions)]
    if session_id not in order:
        return None
    if placement.anchor is not None and placement.anchor not in order:
        return None
    order.remove(session_id)
    if placement.where == "first":
        order.insert(0, session_id)
    elif placement.where == "last":
        order.append(session_id)
    elif placement.where == "before":
        order.insert(order.index(_anchor(placement)), session_id)
    else:
        order.insert(order.index(_anchor(placement)) + 1, session_id)
    return tuple(order)


def _anchor(placement: Placement) -> int:
    assert placement.anchor is not None, "an anchored placement without an anchor"
    return placement.anchor


def order_by_rule(sessions: Sequence[QueuedSession], rule: Rule) -> tuple[int, ...]:
    """The order a quick action asks for.

    Sorted over `claim_order` rather than over the argument, and with a
    stable sort: two sessions a rule has no opinion between keep the order
    the queue already had them in. A quick action reorders what it has
    something to say about and leaves the rest exactly where it was, which
    is what makes applying one twice a no-op.
    """
    return tuple(session.id for session in sorted(claim_order(sessions), key=rule))


def priorities_for(sessions: Iterable[QueuedSession], order: Sequence[int]) -> dict[int, int]:
    """The numbers to write, for the sessions whose numbers must change.

    One forward pass over the wanted order, carrying the sort key
    `(priority, id)` of the session before. A session already sorting
    after its predecessor keeps the number it has; one that does not is
    raised to the smallest number that puts it there -- the predecessor's
    own number when this session's id is the larger (the claim breaks that
    tie by id), and one more than it otherwise.

    Two properties follow directly and both are load-bearing.

    **Nothing is ever lowered.** The pass only ever raises, so a guild can
    express any order at all over its own queue without a single job of
    another guild's being overtaken. See the module docstring.

    **Nothing is written for an order that already holds.** Every session
    keeps its number, the dictionary comes back empty, and the write is a
    transaction that touches no rows.

    Sessions the order does not name are not returned and are not
    touched. The callers all pass the guild's whole outstanding queue, so
    in practice the order names everything; a caller that passed less
    would be reordering a subset against neighbours it had not looked at,
    which is why no caller does.
    """
    by_id = {session.id: session for session in sessions}
    changed: dict[int, int] = {}
    previous: tuple[int, int] | None = None
    for session_id in order:
        session = by_id[session_id]
        priority = session.priority
        if previous is not None and (priority, session.id) <= previous:
            # The least number that sorts after the session before this
            # one: the same number when the tie falls our way on id,
            # otherwise the next one up.
            priority = previous[0] if session.id > previous[1] else previous[0] + 1
        if priority != session.priority:
            changed[session.id] = priority
        previous = (priority, session.id)
    return changed
