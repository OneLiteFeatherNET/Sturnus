"""What a guild's recording adds up to, computed from rows and nothing else.

Pure functions over frozen dataclasses, separated from the SQL in
`sturnus.console.adapters` for the same reason `sturnus.console.statistics`
is separated from `sturnus.console.queries`: the shaping is where the
decisions are, and decisions that need a database to exercise are
decisions nobody exercises.

**This module is deliberately about a guild and never about a person.**

That restraint is the point rather than an oversight. A report over
meetings can be written two ways. One says how much a team recorded, how
long its meetings run and how many people are usually in them -- facts
about the *guild*, useful for deciding whether the bot is configured
sensibly and whether the transcription is keeping up. The other ranks
named individuals by how many meetings they attended and how long they
spoke, which is a different artifact entirely: in Germany and the EU a
per-person readout of attendance and speaking time is a means of
monitoring performance and conduct, and introducing one is a decision for
a works council rather than for a console.

Nothing here forecloses the second. It is simply not this module, so that
switching it on is a visible, separate act rather than a field that
appeared in a payload.

Three rules carry over from `statistics` unchanged, because they are
properties of the same columns:

- **Null is not zero.** `audio_seconds`, `speech_seconds` and
  `segment_count` are nullable, and null means nobody ever measured while
  zero means somebody did and it was nothing. A total that sums null as
  zero understates itself *and* hides how much of itself is missing, so
  every total here is reported beside the number of tracks that had
  nothing to contribute to it.
- **Every id is a string.** A Discord snowflake exceeds JavaScript's safe
  integer range.
- **A session that has not ended has no length.** Answering "now minus
  started_at" renders a meeting that grows every time the page is
  refreshed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import TypedDict


@dataclass(frozen=True)
class RecordedSession:
    """One session of a guild, with only what a report is computed from.

    Deliberately not `AttendedSession`. That one is scoped to a person and
    carries their tracks; this one is scoped to a guild and carries counts.
    A report built from the other shape would have needed a viewer to
    exist, which is exactly the confusion this feature must not introduce.
    """

    id: int
    started_at: datetime
    ended_at: datetime | None
    documented: bool
    #: How many people were in it, from `session_participant`.
    participants: int
    #: How many recordings it produced, whether or not they were measured.
    tracks: int
    #: Summed over the tracks that carry a measurement. `None` when not one
    #: of them does -- which is a different fact from zero and is reported
    #: as one.
    audio_seconds: float | None
    speech_seconds: float | None
    #: Tracks with no `speech_seconds` at all. The size of the hole in the
    #: figure above.
    unmeasured_tracks: int

    @property
    def duration_seconds(self) -> float | None:
        """How long this ran, or `None` while it is still running."""
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class MonthJson(TypedDict):
    """One month of a guild's recording, in the guild's own calendar."""

    month: str
    sessions: int
    recorded_seconds: float
    #: Sessions that produced a protocol. Against `sessions` this is the
    #: pipeline's success rate as the guild experienced it.
    documented: int


class ReportJson(TypedDict):
    guild_id: str
    sessions: int
    documented: int
    open_sessions: int
    recorded_seconds: float
    speech_seconds: float
    unmeasured_tracks: int
    tracks: int
    distinct_participants: int
    average_participants: float | None
    largest_meeting: int | None
    average_duration_seconds: float | None
    longest_duration_seconds: float | None
    first_session_at: str | None
    last_session_at: str | None
    timezone: str
    months: list[MonthJson]


def guild_report(
    sessions: Sequence[RecordedSession],
    *,
    guild_id: int,
    distinct_participants: int,
    zone: tzinfo,
    zone_name: str,
) -> ReportJson:
    """Everything the report page shows, from the guild's sessions.

    `distinct_participants` is passed in rather than derived, because the
    only honest way to count distinct people is over every participant row
    and these values carry counts rather than identities. Deriving it here
    would have meant carrying the identities into this module, and a
    module that holds a list of who attended is one edit away from
    ranking them.
    """
    closed = [session for session in sessions if session.duration_seconds is not None]
    durations = [session.duration_seconds for session in closed if session.duration_seconds]
    participant_counts = [session.participants for session in sessions if session.participants]

    return ReportJson(
        guild_id=str(guild_id),
        sessions=len(sessions),
        documented=sum(1 for session in sessions if session.documented),
        # Reported rather than folded into the total. A guild with four
        # sessions of which one has been "recording" for three days has a
        # problem, and an average length computed over the other three
        # would hide it.
        open_sessions=len(sessions) - len(closed),
        recorded_seconds=sum(durations),
        speech_seconds=sum(
            session.speech_seconds for session in sessions if session.speech_seconds is not None
        ),
        # The size of the hole in the figure above, in the same payload as
        # the figure. A total offered without it invites the reader to
        # treat "we have no measurement" as "they said nothing".
        unmeasured_tracks=sum(session.unmeasured_tracks for session in sessions),
        tracks=sum(session.tracks for session in sessions),
        distinct_participants=distinct_participants,
        average_participants=_mean(participant_counts),
        # How big this guild's meetings get. An aggregate about *meetings*
        # rather than about the people in them.
        largest_meeting=max(participant_counts, default=None),
        average_duration_seconds=_mean(durations),
        longest_duration_seconds=max(durations, default=None),
        first_session_at=_isoformat(min((s.started_at for s in sessions), default=None)),
        last_session_at=_isoformat(max((s.started_at for s in sessions), default=None)),
        # Named in the payload because the months below are cut in it, and
        # a month boundary is a choice: a meeting that opened at half past
        # midnight in Berlin belongs to the month the people in it think it
        # does, not to the previous one UTC would file it under.
        timezone=zone_name,
        months=months(sessions, zone),
    )


def months(sessions: Iterable[RecordedSession], zone: tzinfo) -> list[MonthJson]:
    """One entry per month that had a session, oldest first.

    In the guild's own timezone, the same one the protocols are written in
    (Spec 11). A report that bucketed by UTC would put a late-evening
    meeting in the wrong month twice a year for guilds west of Greenwich
    and every single time for guilds east of it -- and it would disagree
    with the timestamps printed in the protocol of that very meeting.

    Months with no sessions are absent rather than zero-filled. A guild
    that used the bot in March and again in November has eight empty
    months between them, and a chart that draws them is a chart mostly of
    nothing; a client that wants a continuous axis can fill the gaps,
    knowing which months were genuinely empty.
    """
    counted: Counter[str] = Counter()
    documented: Counter[str] = Counter()
    seconds: dict[str, float] = {}
    for session in sessions:
        key = session.started_at.astimezone(zone).strftime("%Y-%m")
        counted[key] += 1
        if session.documented:
            documented[key] += 1
        seconds[key] = seconds.get(key, 0.0) + (session.duration_seconds or 0.0)

    return [
        MonthJson(
            month=key,
            sessions=counted[key],
            recorded_seconds=seconds[key],
            documented=documented[key],
        )
        # Lexicographic on `YYYY-MM` is chronological, which is the whole
        # reason the key is written that way round.
        for key in sorted(counted)
    ]


def _mean(values: Sequence[float] | Sequence[int]) -> float | None:
    """The average, or `None` when there is nothing to average.

    `None` rather than zero, for the reason null is not zero everywhere
    else in this codebase: a guild with no closed sessions has no average
    length, and reporting one as `0` states that its meetings are
    instantaneous.
    """
    if not values:
        return None
    return sum(values) / len(values)


def _isoformat(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()
