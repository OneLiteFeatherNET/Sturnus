"""The console's reads, scoped to one person by the statement itself.

Every method here takes `discord_user_id` first, and every statement it
issues carries `session_participant` in its `WHERE`. That is not a style
choice. The alternative -- fetch, then filter in the handler -- puts the
authorisation decision somewhere other than the thing it authorises, and
the failure mode of forgetting one is not an error but a disclosure. A
statement that forgets the scope returns nothing at all, which fails
loudly on the first request instead of quietly on the wrong one.

There is deliberately no "all sessions" method for a handler to reach
for. The narrowest thing this class can express is already scoped, so
nothing wider exists to be misused later.

Reading happens in three statements rather than one join, because a join
across participants and jobs multiplies rows: a session with five
speakers and five tracks comes back twenty-five times, and the
de-duplication that follows is more code than the second and third
statements are. Shaping the rows into the dataclasses is left to
`sturnus.console.statistics`, which is where it can be tested without a
database.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.console.statistics import (
    AttendedSession,
    Participant,
    Track,
    day_bounds,
    year_bounds,
)
from sturnus.infrastructure.db.models import Session, SessionParticipant, TranscriptionJob

#: The one job status whose `transcript` column holds a transcript.
#: `pending` and `running` have not written one yet, and `dead` (a job
#: that exhausted its retries, see `JobQueue.fail`) never will -- the same
#: rule `JobRepository.transcripts_for` applies for the same reason.
_TRANSCRIBED = "done"


class ConsoleQueries:
    """Everything the console reads, already narrowed to the signed-in user."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def sessions_for(self, discord_user_id: int) -> tuple[AttendedSession, ...]:
        """Every session this person was in, newest first."""
        return await self._sessions(discord_user_id)

    async def session_for(self, discord_user_id: int, session_id: int) -> AttendedSession | None:
        """One session, or `None` if this person was not in it.

        `None` covers both "no such session" and "not yours", and the
        handler answers 404 to both. Telling them apart would let somebody
        walk the id space and learn which sessions exist.
        """
        found = await self._sessions(discord_user_id, Session.id == session_id)
        return found[0] if found else None

    async def sessions_in_year(
        self, discord_user_id: int, year: int
    ) -> tuple[AttendedSession, ...]:
        first, last = year_bounds(year)
        return await self._sessions(
            discord_user_id, Session.started_at >= first, Session.started_at <= last
        )

    async def sessions_on_day(self, discord_user_id: int, day: date) -> tuple[AttendedSession, ...]:
        first, last = day_bounds(day)
        return await self._sessions(
            discord_user_id, Session.started_at >= first, Session.started_at <= last
        )

    async def transcripts_of(self, discord_user_id: int) -> tuple[str, ...]:
        """This person's own transcripts, encoded as the column stores them.

        Their own, and never the whole session's: the dashboard's word
        count answers "how much did I say", and the transcript is the
        protected content -- a wider read here would be a wider
        disclosure for a number that does not need one.

        Returned as the raw column rather than parsed, so the counting
        stays in `statistics` where it is testable without a database.
        """
        async with self._session_factory() as db:
            rows = await db.execute(
                select(TranscriptionJob.transcript).where(
                    TranscriptionJob.discord_user_id == discord_user_id,
                    TranscriptionJob.status == _TRANSCRIBED,
                    TranscriptionJob.transcript.is_not(None),
                )
            )
            return tuple(transcript for transcript in rows.scalars() if transcript is not None)

    def _attended_by(self, discord_user_id: int) -> Select[tuple[int]]:
        """The scope, as a subquery: the ids of sessions this person was in.

        Applied to all three statements below, including the two that are
        already given ids drawn from the first. That looks redundant and
        is not: without it, the only thing keeping the participants and
        the tracks in scope is a property of a *different* statement, and
        a later edit that widens the first one widens all three in
        silence.
        """
        return select(SessionParticipant.session_id).where(
            SessionParticipant.discord_user_id == discord_user_id
        )

    async def _sessions(
        self, discord_user_id: int, *conditions: ColumnElement[bool]
    ) -> tuple[AttendedSession, ...]:
        scope = self._attended_by(discord_user_id)
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        Session.id,
                        Session.channel_id,
                        Session.channel_name,
                        Session.started_at,
                        Session.ended_at,
                        Session.document_url,
                    )
                    .where(Session.id.in_(scope), *conditions)
                    # By id as well as by time, so two sessions that
                    # opened in the same instant do not swap places
                    # between two page loads.
                    .order_by(Session.started_at.desc(), Session.id.desc())
                )
            ).all()
            if not rows:
                return ()

            found = [row.id for row in rows]
            people = (
                await db.execute(
                    select(
                        SessionParticipant.session_id,
                        SessionParticipant.discord_user_id,
                        SessionParticipant.discord_display_name,
                    )
                    .where(
                        SessionParticipant.session_id.in_(found),
                        SessionParticipant.session_id.in_(scope),
                    )
                    .order_by(SessionParticipant.discord_user_id)
                )
            ).all()
            jobs = (
                await db.execute(
                    select(
                        TranscriptionJob.session_id,
                        TranscriptionJob.discord_user_id,
                        TranscriptionJob.audio_seconds,
                        TranscriptionJob.speech_seconds,
                        TranscriptionJob.segment_count,
                    )
                    .where(
                        TranscriptionJob.session_id.in_(found),
                        TranscriptionJob.session_id.in_(scope),
                    )
                    .order_by(TranscriptionJob.discord_user_id)
                )
            ).all()

        names: dict[int, dict[int, str]] = defaultdict(dict)
        attendees: dict[int, list[Participant]] = defaultdict(list)
        for session_id, participant_id, display_name in people:
            names[session_id][participant_id] = display_name
            attendees[session_id].append(Participant(participant_id, display_name))

        tracks: dict[int, list[Track]] = defaultdict(list)
        for session_id, speaker_id, audio_seconds, speech_seconds, segment_count in jobs:
            tracks[session_id].append(
                Track(
                    discord_user_id=speaker_id,
                    # `None` when the speaker has no participant row: a
                    # job that outlived one still has audio and still has
                    # measurements, and dropping it would hide a
                    # recording that exists.
                    display_name=names[session_id].get(speaker_id),
                    # Straight through, null included. Null means nobody
                    # ever measured; zero means somebody did, and it was
                    # nothing (see `sturnus.console.statistics`).
                    audio_seconds=audio_seconds,
                    speech_seconds=speech_seconds,
                    segment_count=segment_count,
                )
            )

        return tuple(
            AttendedSession(
                id=row.id,
                channel_id=row.channel_id,
                channel_name=row.channel_name,
                started_at=row.started_at,
                ended_at=row.ended_at,
                document_url=row.document_url,
                participants=tuple(attendees[row.id]),
                tracks=tuple(tracks[row.id]),
            )
            for row in rows
        )
