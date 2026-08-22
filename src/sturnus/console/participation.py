"""Who took part in the most of a guild's meetings.

**Read this before extending anything in this module.**

Everything else the console reports is about a guild or about the person
reading it. This is the one thing that is about *other people*, named, and
ranked. `sturnus.console.reporting` says so in as many words and stops
short of it deliberately; this module is where that line is crossed, on
purpose, in a change that can be reverted on its own.

What it produces is a list of colleagues ordered by how many meetings they
attended and how long they spoke. In Germany and the EU that is a
`technische Einrichtung, die dazu bestimmt ist, das Verhalten oder die
Leistung der Arbeitnehmer zu überwachen` -- BetrVG §87(1)(6) -- and
introducing one is subject to co-determination whether or not anybody
intended it as a monitoring tool. The GDPR half is the same point from the
other side: the recordings were collected so a protocol could be written,
and an attendance ranking is a different purpose served from the same
data.

None of that makes it wrong to have. It makes it a decision for the people
who run the guild rather than a field that appeared in a payload. So:

- it is its own port, its own endpoint and its own module, so that not
  having it is one revert rather than an audit of a shared response shape;
- reading it emits an audit line, because "who looked at the attendance
  ranking, and when" is precisely the question anyone reviewing this
  arrangement would ask first;
- the numbers it reports are the ones that answer the stated question and
  no more. There is no words-spoken, no punctuality, no talk-ratio, and
  nothing derived per meeting. Each of those would be a further purpose,
  and each would need deciding again.

The rules from `sturnus.console.statistics` hold here unchanged:

- **Null is not zero.** A track nobody measured contributes nothing to a
  speaking total and is counted separately, so a person whose recordings
  predate the measurement columns does not read as silent.
- **Every id is a string.** A Discord snowflake exceeds JavaScript's safe
  integer range.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict


@dataclass(frozen=True)
class Attendance:
    """One person's participation in one guild's meetings."""

    discord_user_id: int
    #: The name they last appeared under in this guild, from
    #: `session_participant`. Per guild, because a display name is.
    display_name: str | None
    sessions: int
    #: Summed over the tracks that carry a measurement. `None` when not one
    #: of them does -- a different fact from zero, and reported as one.
    speech_seconds: float | None
    #: Tracks of theirs that nobody measured. The size of the hole in the
    #: figure above, for this person.
    unmeasured_tracks: int
    first_seen_at: datetime
    last_seen_at: datetime


class AttendanceJson(TypedDict):
    discord_user_id: str
    display_name: str | None
    sessions: int
    speech_seconds: float | None
    unmeasured_tracks: int
    first_seen_at: str
    last_seen_at: str


class ParticipationJson(TypedDict):
    guild_id: str
    #: How many of the guild's sessions the ranking is computed over. Sent
    #: because "eleven meetings" means one thing out of twelve and quite
    #: another out of four hundred, and a bare rank invites the first
    #: reading regardless.
    sessions: int
    people: list[AttendanceJson]


def participation(
    attendance: Sequence[Attendance], *, guild_id: int, sessions: int
) -> ParticipationJson:
    """The ranking, ordered by attendance and then made stable.

    Most meetings first, because that is the question. Ties break on the
    display name and then on the id -- never left to the order rows came
    back in, which changes between two page loads and would make a list of
    named colleagues appear to reshuffle itself.

    Speaking time is deliberately *not* the sort key and is not offered as
    one. Ordering people by how much they talked is a different claim from
    ordering them by how often they were present, and it is the one that
    reads as a judgement.
    """
    ordered = sorted(
        attendance,
        key=lambda person: (
            -person.sessions,
            # `None` sorts after every name rather than before, so somebody
            # the system has no name for does not head a tie in a list
            # people read top-down.
            person.display_name is None,
            (person.display_name or "").lower(),
            person.discord_user_id,
        ),
    )
    return ParticipationJson(
        guild_id=str(guild_id),
        sessions=sessions,
        people=[_person_json(person) for person in ordered],
    )


def _person_json(person: Attendance) -> AttendanceJson:
    return AttendanceJson(
        # A Discord snowflake exceeds JavaScript's safe integer range,
        # where a JSON number silently loses its last digits and produces
        # an id that looks right and names nobody.
        discord_user_id=str(person.discord_user_id),
        display_name=person.display_name,
        sessions=person.sessions,
        # Straight through, null included. Null means nobody ever
        # measured; zero means somebody did and it was nothing.
        speech_seconds=person.speech_seconds,
        unmeasured_tracks=person.unmeasured_tracks,
        first_seen_at=person.first_seen_at.isoformat(),
        last_seen_at=person.last_seen_at.isoformat(),
    )
