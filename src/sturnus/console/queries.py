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

Reading happens in four statements rather than one join, because a join
across participants, jobs and tags multiplies rows: a session with five
speakers and five tracks comes back twenty-five times before tags are
even considered, and the de-duplication that follows is more code than
the other three statements are. Shaping the rows into the dataclasses is left to
`sturnus.console.statistics`, which is where it can be tested without a
database.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.console.filters import LIKE_ESCAPE, NO_FILTER, SessionFilter, like_pattern
from sturnus.console.statistics import (
    AttendedSession,
    Participant,
    SessionPage,
    TagUse,
    Track,
    day_bounds,
    year_bounds,
)
from sturnus.console.tags import tag_counts
from sturnus.infrastructure.db.models import (
    Session,
    SessionParticipant,
    SessionTag,
    TranscriptionJob,
)

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
        """Every session this person was in, newest first.

        Unpaged, and the two callers that use it need it that way: the
        dashboard's figures are over everything somebody ever did, and a
        dashboard whose totals changed when you turned a page would be
        answering a different question on every page. The recordings list
        uses `sessions_page`.
        """
        return await self._sessions(discord_user_id)

    async def sessions_page(
        self,
        discord_user_id: int,
        *,
        limit: int,
        offset: int,
        matching: SessionFilter = NO_FILTER,
    ) -> SessionPage:
        """One window of this person's sessions, and how many there are.

        Two statements rather than a `count(*) OVER ()` on the first.
        The window function would come back attached to the rows -- so a
        request for a window past the end returns no rows and therefore
        no count, and the list could not say "you asked for page five of
        three" because it would not know there were three.

        The count is issued first, so that a session opened between the
        two makes the list one row short of its own total rather than
        one row longer than it: an off-by-one that reads as "there is
        more" is easier to recover from than one that reads as "there is
        less than you can already see".
        """
        narrowing = self._narrowing(discord_user_id, matching)
        total = await self._count(discord_user_id, *narrowing)
        found = await self._sessions(discord_user_id, *narrowing, limit=limit, offset=offset)
        return SessionPage(sessions=found, total=total, limit=limit, offset=offset)

    async def _count(self, discord_user_id: int, *conditions: ColumnElement[bool]) -> int:
        """How many sessions this person was in.

        Scoped by the same subquery as everything else. A count is a
        smaller disclosure than a list and is not therefore a free one:
        "how many meetings are there" asked without a scope answers a
        question about everybody.
        """
        async with self._session_factory() as db:
            counted = await db.scalar(
                select(func.count())
                .select_from(Session)
                # The same conditions the page itself is selected
                # with, passed in rather than re-derived: a total counted
                # under a different filter than the rows is a list saying
                # "1-20 of 47" over twelve results.
                .where(Session.id.in_(self._attended_by(discord_user_id)), *conditions)
            )
            return counted or 0

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

    async def tags_of(self, discord_user_id: int) -> tuple[TagUse, ...]:
        """Every label this person uses, with how many recordings carry it.

        Scoped twice, like everything else here. `discord_user_id` on
        `session_tag` is what makes these their own labels rather than
        everybody's; `session_id.in_(scope)` is what keeps a label from
        outliving the participation that justified it -- somebody removed
        from a session's participants can no longer see that session, and
        a filter chip counting it would say how many meetings they were in
        that they can no longer open.
        """
        scope = self._attended_by(discord_user_id)
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(SessionTag.tag, func.count())
                    .where(
                        SessionTag.discord_user_id == discord_user_id,
                        SessionTag.session_id.in_(scope),
                    )
                    .group_by(SessionTag.tag)
                )
            ).all()
        counted = tag_counts([(row[0], row[1]) for row in rows])
        return tuple(TagUse(tag=tag, sessions=sessions) for tag, sessions in counted)

    def _narrowing(
        self, discord_user_id: int, matching: SessionFilter
    ) -> tuple[ColumnElement[bool], ...]:
        """A filter, as conditions on the session statement.

        In the statement and not applied to its results, for the same
        reason the scope is: a filter in Python is a filter that has
        already fetched what it is about to discard, and on this endpoint
        that means fetching a whole history to show twelve rows of it.

        Nothing here searches a transcript. See the module docstring of
        `sturnus.console.filters` for why that is a decision rather than
        an omission.
        """
        conditions: list[ColumnElement[bool]] = []

        if matching.since is not None:
            conditions.append(Session.started_at >= day_bounds(matching.since)[0])
        if matching.until is not None:
            # The end of the named day, not its start. Somebody who picks
            # "to 21 August" means the whole of the 21st, and a bound at
            # midnight silently drops the day they named.
            conditions.append(Session.started_at <= day_bounds(matching.until)[1])

        if matching.protocol is True:
            conditions.append(Session.document_url.is_not(None))
        elif matching.protocol is False:
            conditions.append(Session.document_url.is_(None))

        # One `EXISTS` per tag, so they combine with AND: a second chip is
        # somebody narrowing a list, and getting more rows from it than
        # from the first alone is the opposite of what pressing it looks
        # like. Each names `discord_user_id`, so the filter can only ever
        # be over labels this reader wrote.
        for tag in matching.tags:
            conditions.append(
                select(SessionTag.tag)
                .where(
                    SessionTag.session_id == Session.id,
                    SessionTag.discord_user_id == discord_user_id,
                    SessionTag.tag == tag,
                )
                .exists()
            )

        if matching.text is not None:
            conditions.append(self._matching_text(discord_user_id, matching.text))

        return tuple(conditions)

    def _matching_text(self, discord_user_id: int, text: str) -> ColumnElement[bool]:
        """Search text, against the things a recording is known by.

        The channel it happened in, what somebody called it and wrote
        about it, the people who were in it, and the labels this reader
        put on it -- and nothing else. Every one of them is already in
        the response this same person gets from `/api/sessions`, so
        searching them narrows what they can see rather than widening it.
        A transcript is not among them, and that is the whole point of
        `sturnus.console.filters`.

        **Not indexed, deliberately, and this is where that decision is
        paid for.** `ILIKE '%…%'` over free text is answered by a GIN
        trigram index and by no btree, and a trigram index needs
        `CREATE EXTENSION pg_trgm` -- a privileged statement, in a
        migration the worker runs in-process at startup, on a deployment
        whose database role may not be permitted to create extensions
        (see migration 0013). A deployment that cannot start is a worse
        outcome than a scan, and the scan here is small: the outer
        statement has already narrowed to the sessions this one person
        was in before any of these patterns is evaluated. If that ever
        stops being true it is a follow-up that creates the extension
        deliberately, outside a startup migration.

        Case-insensitive because nobody types a channel name the way it
        was written, and matched anywhere in the value because "retro" has
        to find "weekly retro".
        """
        pattern = like_pattern(text)
        scope = self._attended_by(discord_user_id)
        return or_(
            Session.channel_name.ilike(pattern, escape=LIKE_ESCAPE),
            # A title and a description are shared, so unlike the tag
            # clause below neither is narrowed by the reader's own id:
            # what a meeting is called is the same for everybody who was
            # in it. The scope on the outer statement is what keeps this
            # to their own meetings.
            Session.title.ilike(pattern, escape=LIKE_ESCAPE),
            Session.description.ilike(pattern, escape=LIKE_ESCAPE),
            select(SessionParticipant.id)
            .where(
                SessionParticipant.session_id == Session.id,
                # Redundant with the outer statement, which already
                # restricts `Session.id` to this person's sessions -- and
                # kept anyway, so that a later edit loosening the outer
                # `WHERE` cannot quietly turn this into a search across
                # other people's meetings.
                SessionParticipant.session_id.in_(scope),
                SessionParticipant.discord_display_name.ilike(pattern, escape=LIKE_ESCAPE),
            )
            .exists(),
            select(SessionTag.tag)
            .where(
                SessionTag.session_id == Session.id,
                SessionTag.discord_user_id == discord_user_id,
                SessionTag.tag.ilike(pattern, escape=LIKE_ESCAPE),
            )
            .exists(),
        )

    def _attended_by(self, discord_user_id: int) -> Select[tuple[int]]:
        """The scope, as a subquery: the ids of sessions this person was in.

        Applied to all four statements below, including the three that
        are already given ids drawn from the first. That looks redundant and
        is not: without it, the only thing keeping the participants and
        the tracks in scope is a property of a *different* statement, and
        a later edit that widens the first one widens all three in
        silence.
        """
        return select(SessionParticipant.session_id).where(
            SessionParticipant.discord_user_id == discord_user_id
        )

    async def _sessions(
        self,
        discord_user_id: int,
        *conditions: ColumnElement[bool],
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AttendedSession, ...]:
        """The sessions matching `conditions`, and everything hanging off them.

        `limit` and `offset` narrow the *first* statement only. The three
        that follow are given the ids it returned, so a page of twenty
        sessions fetches twenty sessions' participants, tags and tracks
        and not a whole history's.
        """
        scope = self._attended_by(discord_user_id)
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        Session.id,
                        Session.channel_id,
                        Session.channel_name,
                        Session.title,
                        Session.description,
                        Session.started_at,
                        Session.ended_at,
                        Session.document_url,
                    )
                    .where(Session.id.in_(scope), *conditions)
                    # By id as well as by time, so two sessions that
                    # opened in the same instant do not swap places
                    # between two page loads -- which for a paged list is
                    # not cosmetic: an order the database is free to vary
                    # is one where the same row can appear on two
                    # consecutive pages and another on neither.
                    .order_by(Session.started_at.desc(), Session.id.desc())
                    # `None` rather than a large number for "no window":
                    # a default limit here would be a second, quieter
                    # place where a list gets truncated.
                    .limit(limit)
                    .offset(offset)
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
                        # What the file is, written down once by the
                        # worker. Read here so a metadata tab costs the
                        # same statement the page already issues instead
                        # of a ranged GET and a chunk decrypt per track.
                        TranscriptionJob.sample_rate,
                        TranscriptionJob.channels,
                        TranscriptionJob.stored_bytes,
                    )
                    .where(
                        TranscriptionJob.session_id.in_(found),
                        TranscriptionJob.session_id.in_(scope),
                    )
                    .order_by(TranscriptionJob.discord_user_id)
                )
            ).all()
            # The fourth statement, and the one that must name the reader
            # twice: `discord_user_id` because a tag belongs to whoever
            # wrote it and this reader may only have their own, and the
            # scope subquery for the same reason the two above carry it --
            # so that widening the session statement later cannot widen
            # this one in silence.
            labels = (
                await db.execute(
                    select(SessionTag.session_id, SessionTag.tag)
                    .where(
                        SessionTag.session_id.in_(found),
                        SessionTag.session_id.in_(scope),
                        SessionTag.discord_user_id == discord_user_id,
                    )
                    # Alphabetical, so a recording's chips do not
                    # rearrange themselves between two page loads.
                    .order_by(SessionTag.tag)
                )
            ).all()

        names: dict[int, dict[int, str]] = defaultdict(dict)
        attendees: dict[int, list[Participant]] = defaultdict(list)
        for session_id, participant_id, display_name in people:
            names[session_id][participant_id] = display_name
            attendees[session_id].append(Participant(participant_id, display_name))

        tagged: dict[int, list[str]] = defaultdict(list)
        for session_id, tag in labels:
            tagged[session_id].append(tag)

        tracks: dict[int, list[Track]] = defaultdict(list)
        for job in jobs:
            tracks[job.session_id].append(
                Track(
                    discord_user_id=job.discord_user_id,
                    # `None` when the speaker has no participant row: a
                    # job that outlived one still has audio and still has
                    # measurements, and dropping it would hide a
                    # recording that exists.
                    display_name=names[job.session_id].get(job.discord_user_id),
                    # Straight through, null included. Null means nobody
                    # ever measured; zero means somebody did, and it was
                    # nothing (see `sturnus.console.statistics`). The
                    # three below follow the same rule: a job finished
                    # before those columns existed has audio that may
                    # already be deleted, so there is nothing left to
                    # read them from.
                    audio_seconds=job.audio_seconds,
                    speech_seconds=job.speech_seconds,
                    segment_count=job.segment_count,
                    sample_rate=job.sample_rate,
                    channels=job.channels,
                    stored_bytes=job.stored_bytes,
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
                title=row.title,
                description=row.description,
                participants=tuple(attendees[row.id]),
                tracks=tuple(tracks[row.id]),
                tags=tuple(tagged[row.id]),
            )
            for row in rows
        )
