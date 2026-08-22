"""What the console shows, computed from what a person took part in.

Pure functions over frozen dataclasses, deliberately separated from the
SQL in `sturnus.console.queries`. Three rules live here and are worth the
separation on their own:

- **Null is not zero.** `audio_seconds`, `speech_seconds` and
  `segment_count` are nullable, and the two states they hold are not the
  same fact. Null means the job predates the columns and nobody ever
  measured it; zero means somebody measured, and it was nothing. Summing
  null as zero understates a total *and* destroys the distinction, which
  is exactly what `sturnus.domain.measurements` exists to keep --
  `segment_count` is what separates "said nothing" from "was never
  transcribed", and both of those leave an empty transcript.
- **Every id is a string.** A Discord snowflake exceeds JavaScript's safe
  integer range, where a JSON number silently loses its last digits and
  produces an id that looks right and names nobody. Session ids follow,
  not because they need to but because two id shapes in one payload is
  how the one that matters gets parsed with the wrong one.
- **The viewer is in every session they can see**, so whether to count
  them is a decision each figure makes for itself: "people spoken with"
  excludes them, a day's participant count does not.

The `*Json` types below are the API contract. They are pinned here, on
the Python side, because that is where it is decided -- the Nuxt console
consumes it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TypedDict


@dataclass(frozen=True)
class Participant:
    """Somebody who was in a session, as `session_participant` names them."""

    discord_user_id: int
    display_name: str


@dataclass(frozen=True)
class Track:
    """One speaker's recording within a session, and what it measured.

    `display_name` is nullable because a track is a `transcription_job`
    row and there is no foreign key from it to `session_participant`: a
    job whose participant row is gone still has audio and still has
    measurements, and dropping it from the session would hide a recording
    that exists.
    """

    discord_user_id: int
    display_name: str | None
    audio_seconds: float | None
    speech_seconds: float | None
    segment_count: int | None


@dataclass(frozen=True)
class TagUse:
    """One label this person uses, and how many of their recordings carry it.

    The count is what orders the filter bar: the labels somebody reaches
    for are the ones they have already used, and it counts only their own
    recordings -- a count across everybody's tags would be a statement
    about how other people label their meetings, which is exactly what
    keeping tags private means not doing.
    """

    tag: str
    sessions: int


@dataclass(frozen=True)
class AttendedSession:
    """A session the signed-in person was in.

    Named for the scope rather than for the table: there is no way to
    obtain one of these for a session you were not in, because the only
    thing that builds them (`sturnus.console.queries`) scopes every
    statement by `session_participant`.
    """

    id: int
    channel_id: int
    channel_name: str | None
    started_at: datetime
    ended_at: datetime | None
    document_url: str | None
    participants: tuple[Participant, ...]
    tracks: tuple[Track, ...]
    #: The labels the *viewer* put on this session, alphabetical. Never
    #: anybody else's: `session_tag` is keyed by its owner and the query
    #: that fills this names the signed-in person, so a session two people
    #: both tagged carries only the reader's own words (see
    #: `sturnus.console.tags` for why that is the decision).
    #:
    #: Defaulted so that the many places which build one of these to talk
    #: about durations do not have to say "and no tags"; a session with no
    #: labels and a session whose labels were not asked for look the same
    #: here, and nothing distinguishes them because nothing needs to.
    tags: tuple[str, ...] = ()

    @property
    def duration_seconds(self) -> float | None:
        """How long this ran, or `None` while it is still running.

        An open session is one being recorded right now, or one whose
        process died before it could be closed. Neither has a length yet,
        and answering "now minus started_at" would render a meeting that
        grows longer every time the page is refreshed.
        """
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class ParticipantJson(TypedDict):
    discord_user_id: str
    display_name: str


class TrackJson(TypedDict):
    discord_user_id: str
    display_name: str | None
    audio_seconds: float | None
    speech_seconds: float | None
    segment_count: int | None


class SessionSummaryJson(TypedDict):
    """A session named without its contents, for pointing at one."""

    id: str
    started_at: str
    ended_at: str | None
    duration_seconds: float | None
    channel_id: str
    channel_name: str | None


class TagUseJson(TypedDict):
    tag: str
    sessions: int


class SessionJson(TypedDict):
    id: str
    started_at: str
    ended_at: str | None
    duration_seconds: float | None
    channel_id: str
    channel_name: str | None
    document_url: str | None
    other_participants: list[ParticipantJson]
    tracks: list[TrackJson]
    tags: list[str]


class DashboardJson(TypedDict):
    total_speech_seconds: float | None
    unmeasured_tracks: int
    sessions_attended: int
    sessions_with_protocol: int
    people_spoken_with: int
    words_transcribed: int
    longest_session: SessionSummaryJson | None
    most_recent_session: SessionSummaryJson | None
    first_session: SessionSummaryJson | None


class CalendarDayJson(TypedDict):
    date: str
    sessions: int
    total_duration_seconds: float
    participants: int


class TimelineEntryJson(TypedDict):
    id: str
    started_at: str
    duration_seconds: float | None
    channel_id: str
    channel_name: str | None


def _moment(value: datetime | None) -> str | None:
    """ISO 8601 with the offset, always.

    Without the offset a browser reads the string as local time, and every
    session in the console silently moves by the viewer's own timezone.
    """
    return None if value is None else value.isoformat()


def session_summary(session: AttendedSession) -> SessionSummaryJson:
    return SessionSummaryJson(
        id=str(session.id),
        started_at=session.started_at.isoformat(),
        ended_at=_moment(session.ended_at),
        duration_seconds=session.duration_seconds,
        channel_id=str(session.channel_id),
        channel_name=session.channel_name,
    )


def session_json(session: AttendedSession, viewer: int) -> SessionJson:
    """One session as the console renders it.

    The participant list is everybody *else*: the viewer already knows
    they were there, and the question the row answers is "who else was in
    this". The track list keeps the viewer's own, because the tracks are
    the recording rather than the roster, and their own is the one they
    are most likely to want.
    """
    return SessionJson(
        id=str(session.id),
        started_at=session.started_at.isoformat(),
        ended_at=_moment(session.ended_at),
        duration_seconds=session.duration_seconds,
        channel_id=str(session.channel_id),
        channel_name=session.channel_name,
        document_url=session.document_url,
        other_participants=[
            ParticipantJson(
                discord_user_id=str(person.discord_user_id),
                display_name=person.display_name,
            )
            for person in session.participants
            if person.discord_user_id != viewer
        ],
        # The viewer's own labels and nobody else's. The query that
        # filled these named the signed-in person; this serialiser could
        # not widen that if it tried, which is the point of doing the
        # scoping there rather than here.
        tags=list(session.tags),
        tracks=[
            TrackJson(
                discord_user_id=str(track.discord_user_id),
                display_name=track.display_name,
                # Passed through untouched, null included. See the module
                # docstring: `or 0.0` here would be a measurement nobody
                # took.
                audio_seconds=track.audio_seconds,
                speech_seconds=track.speech_seconds,
                segment_count=track.segment_count,
            )
            for track in session.tracks
        ],
    )


def tags_json(uses: Sequence[TagUse]) -> list[TagUseJson]:
    """The signed-in person's labels, as the filter bar consumes them.

    A plain list rather than an object keyed by tag: a tag is text
    somebody typed, and text somebody typed makes a poor JSON key --
    `constructor` and `__proto__` are valid tags and neither behaves like
    a key in every client that will ever read this.
    """
    return [TagUseJson(tag=use.tag, sessions=use.sessions) for use in uses]


def count_words(transcripts: Iterable[str]) -> int:
    """Whitespace-separated tokens across every segment of every transcript.

    A proxy, and the dashboard calls it one. Whisper's segment text is
    already normalised prose, so splitting on whitespace is close enough
    to be useful and cheap enough to run over a person's whole history.

    The encoding is the one `sturnus.application.assembly` defines, read
    here directly rather than through `deserialize_transcript` because the
    only thing wanted is the text: rebuilding a `TranscriptionResult` per
    row to throw away its timings is work for nothing.
    """
    words = 0
    for transcript in transcripts:
        for segment in json.loads(transcript)["segments"]:
            words += len(segment["text"].split())
    return words


def dashboard(
    sessions: Sequence[AttendedSession], viewer: int, transcripts: Iterable[str]
) -> DashboardJson:
    """Everything the signed-in person has accumulated, in one object.

    Aggregated in Python rather than in SQL on purpose. These are one
    person's numbers -- tens of sessions, not millions of rows -- and
    keeping the arithmetic here is what makes the null rule a single
    decision in one testable place instead of a `COALESCE` repeated across
    five statements, where the first one written without it is a silent
    lie.
    """
    own_tracks = [
        track for session in sessions for track in session.tracks if track.discord_user_id == viewer
    ]
    measured = [track.speech_seconds for track in own_tracks if track.speech_seconds is not None]
    closed = _with_a_length(sessions)
    others = {
        person.discord_user_id
        for session in sessions
        for person in session.participants
        if person.discord_user_id != viewer
    }
    return DashboardJson(
        # `None`, not `0.0`, when nothing was ever measured: zero is the
        # claim "we measured, and you said nothing", and there is no
        # measurement here to make it with.
        total_speech_seconds=sum(measured) if measured else None,
        unmeasured_tracks=len(own_tracks) - len(measured),
        sessions_attended=len(sessions),
        sessions_with_protocol=sum(1 for session in sessions if session.document_url is not None),
        people_spoken_with=len(others),
        words_transcribed=count_words(transcripts),
        # `max` over the closed sessions only: an open one has no length,
        # so it cannot win a comparison of lengths.
        longest_session=(
            session_summary(max(closed, key=lambda pair: pair[0])[1]) if closed else None
        ),
        most_recent_session=(session_summary(max(sessions, key=_start)) if sessions else None),
        first_session=session_summary(min(sessions, key=_start)) if sessions else None,
    )


def _with_a_length(
    sessions: Sequence[AttendedSession],
) -> list[tuple[float, AttendedSession]]:
    """The sessions that have ended, each paired with how long it ran.

    A list of pairs rather than a filter plus a `key=` that reaches for
    `duration_seconds` again: the second spelling has to answer "what if
    it is None" a second time, in a lambda, where the honest answer is
    that it cannot happen and the convenient one is `or 0`.
    """
    lengths: list[tuple[float, AttendedSession]] = []
    for session in sessions:
        duration = session.duration_seconds
        if duration is not None:
            lengths.append((duration, session))
    return lengths


def _start(session: AttendedSession) -> datetime:
    return session.started_at


def calendar_year(sessions: Sequence[AttendedSession]) -> list[CalendarDayJson]:
    """One entry per day that had recordings, in calendar order.

    Days are UTC days. A person can be in sessions across several guilds,
    and `timezone` is per-guild configuration -- so there is no single
    guild timezone that is right for a calendar spanning them, and picking
    one guild's would shift the other guilds' meetings across midnight.
    UTC is one consistent grid; rendering it in the viewer's own zone is
    the console's job, where the viewer's zone is actually known.
    """
    by_day: dict[date, list[AttendedSession]] = {}
    for session in sessions:
        by_day.setdefault(session.started_at.astimezone(UTC).date(), []).append(session)
    return [
        CalendarDayJson(
            date=day.isoformat(),
            sessions=len(on_that_day),
            # Open sessions contribute nothing, because they have no
            # length yet. A day whose only session is still running shows
            # as a day that happened with no time in it, which is honest.
            total_duration_seconds=sum(duration for duration, _ in _with_a_length(on_that_day)),
            # Everybody who was there, the viewer included: the question a
            # heatmap tooltip answers is "how many people were in this",
            # and they were one of them. The dashboard's
            # `people_spoken_with` is the other question and excludes them.
            participants=len(
                {
                    person.discord_user_id
                    for session in on_that_day
                    for person in session.participants
                }
            ),
        )
        for day, on_that_day in sorted(by_day.items())
    ]


def day_timeline(sessions: Sequence[AttendedSession]) -> list[TimelineEntryJson]:
    """One day's sessions in the order they happened.

    Ascending, unlike every other list here: a timeline is drawn left to
    right, and the newest-first order the session list uses would draw the
    day backwards.
    """
    return [
        TimelineEntryJson(
            id=str(session.id),
            started_at=session.started_at.isoformat(),
            duration_seconds=session.duration_seconds,
            channel_id=str(session.channel_id),
            channel_name=session.channel_name,
        )
        for session in sorted(sessions, key=_start)
    ]


def year_bounds(year: int) -> tuple[datetime, datetime]:
    """The first and last instant of a year, both inclusive.

    Inclusive rather than the more usual half-open `[Jan 1, next Jan 1)`
    because `datetime(year + 1, 1, 1)` is not a datetime at all for year
    9999 -- and a nonsense year in a query string should be an empty
    calendar, not a 500.
    """
    return (
        datetime(year, 1, 1, tzinfo=UTC),
        datetime.combine(date(year, 12, 31), time.max, tzinfo=UTC),
    )


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The first and last instant of a UTC day, both inclusive.

    Inclusive for the same reason as `year_bounds`: `day + one day` is
    not a date on 9999-12-31.
    """
    return (
        datetime.combine(day, time.min, tzinfo=UTC),
        datetime.combine(day, time.max, tzinfo=UTC),
    )
