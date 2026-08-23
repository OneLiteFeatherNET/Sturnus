"""Database adapters for the console's sign-in.

Two narrow additions rather than new stores: the console reuses
`oauth_state` and `account_link` exactly as the link service does, and
these are the two directions neither of them had a reader for yet.

Why a second state store at all, given `LinkStateStore` exists: `/link`
knows who is linking before the browser ever leaves -- a slash command was
run by somebody. A console sign-in does not. Who this is only becomes
known when the provider answers, which is after the round trip, so the row
cannot carry a Discord user id when it is written.

An earlier draft squeezed it into `oauth_state` behind a placeholder id,
and a test caught what that costs: `LinkStateStore.consume` does not
filter by provider, so the link callback consumed a console state and
returned a `PendingLink` for a user id that does not exist. A table of its
own makes that unrepresentable rather than merely unlikely.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Row, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.application.directory_mirror import (
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.application.linking import new_state
from sturnus.application.publishing import DOCUMENTED_STATUS
from sturnus.console.auth import PROVIDER
from sturnus.console.ports import (
    AdminDirectory,
    CollectionListing,
    ConsentHolder,
    GuildDirectory,
    GuildQueue,
    GuildRecording,
    OwnConsent,
    QueuedSession,
    QueueSnapshot,
    QueueSpeaker,
    RequeueOutcome,
    RevocationOutcome,
    ScopeOutcome,
    SettingsStore,
    Track,
)
from sturnus.console.reporting import RecordedSession
from sturnus.domain import settings
from sturnus.domain.consent import (
    ConsentRecord,
    ConsentScope,
    is_consent_active,
    scope_of,
)
from sturnus.infrastructure.db.models import (
    AccountLink,
    Consent,
    ConsoleState,
    GuildChannel,
    GuildMember,
    GuildRole,
    OutlineCollection,
    SessionParticipant,
    SessionTag,
    TranscriptionJob,
)
from sturnus.infrastructure.db.models import Session as SessionRow
from sturnus.infrastructure.db.queue import DEFAULT_LEASE_SECONDS
from sturnus.infrastructure.db.repositories import AccountLinkRepository, ConsentRepository
from sturnus.infrastructure.db.requeue import (
    ActiveSession,
    SessionView,
    apply_requeue,
    load_active_sessions,
    load_requeue_view,
    load_session,
    load_status,
)

#: How long a sign-in may take. Ten minutes is a browser round trip
#: through a login page with room for somebody to be interrupted, and it
#: bounds how long a captured callback URL stays useful.
_DEFAULT_TTL = timedelta(minutes=10)


class ConsoleStateStore:
    """Single-use OAuth states for a sign-in whose subject is not yet known."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl: timedelta = _DEFAULT_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl

    async def new(self, now: datetime) -> str:
        """Issues a fresh unguessable state and stores it."""
        state = new_state()
        await self.issue(state, now)
        return state

    async def issue(self, state: str, now: datetime) -> None:
        async with self._session_factory() as session:
            session.add(ConsoleState(state=state, created_at=now, expires_at=now + self._ttl))
            await session.commit()

    async def consume(self, state: str, now: datetime) -> bool:
        """Consumes the state, reporting whether it was valid.

        `DELETE ... RETURNING` in one statement, the same shape
        `LinkStateStore.consume` uses and for the same reason: two
        callbacks replaying the same state concurrently can never both
        succeed, because only the delete that actually removes the row
        gets a result back.
        """
        async with self._session_factory() as session:
            row = await session.execute(
                delete(ConsoleState)
                .where(ConsoleState.state == state, ConsoleState.expires_at > now)
                .returning(ConsoleState.state)
            )
            consumed = row.scalar_one_or_none() is not None
            await session.commit()
            return consumed


class ConsoleLinkDirectory:
    """From an external identity to the Discord user who linked it.

    The reverse of `AccountLinkRepository.external_identity`, and the only
    bridge the console has between who authenticated and whose recordings
    they may see.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None:
        """The Discord user this identity is linked to, or `None`.

        Ordered by `linked_at` descending because two Discord users can
        point at one external identity over time -- somebody changes
        Discord accounts and links again -- and the useful answer is the
        current link rather than an abandoned one. Without the ordering
        the row returned would be whichever the planner happened to
        reach, which is a login that names a different person on different
        days.
        """
        async with self._session_factory() as session:
            found: int | None = await session.scalar(
                select(AccountLink.discord_user_id)
                .where(
                    AccountLink.provider == provider,
                    AccountLink.external_user_id == external_user_id,
                )
                .order_by(AccountLink.linked_at.desc())
                .limit(1)
            )
            return found


class ConsoleTrackDirectory:
    """One speaker's recording, if the person asking was in the session.

    The authorisation rule for audio, expressed as one statement rather
    than as a check a handler makes and a query a handler makes. The
    `EXISTS` clause naming `requested_by` is not decoration on top of the
    lookup -- it is part of it, and there is no method on this class that
    performs the lookup without it.

    That is the design's rule for the whole console (section 3.3): every
    query is scoped by the signed-in Discord id at the repository layer,
    not filtered afterwards in a handler. A filter that can be forgotten is
    a filter that will be, and the thing being forgotten here is somebody's
    voice.

    `audio_deleted_at IS NULL` belongs in the same statement for a
    different reason: the retention sweep erases the object first and
    stamps the row second, so a row without the stamp is the only claim
    that the object is still there. Offering a stamped row would send a
    participant to S3 for a key that was deleted on purpose.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def track_for(
        self, session_id: int, speaker_id: int, *, requested_by: int
    ) -> Track | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        TranscriptionJob.s3_key,
                        TranscriptionJob.encryption_key_id,
                        TranscriptionJob.wrapped_data_key,
                    ).where(
                        TranscriptionJob.session_id == session_id,
                        TranscriptionJob.discord_user_id == speaker_id,
                        TranscriptionJob.audio_deleted_at.is_(None),
                        select(SessionParticipant.id)
                        .where(
                            SessionParticipant.session_id == session_id,
                            SessionParticipant.discord_user_id == requested_by,
                        )
                        .exists(),
                    )
                )
            ).first()
            if row is None:
                return None
            return Track(
                s3_key=row.s3_key,
                encryption_key_id=row.encryption_key_id,
                wrapped_data_key=row.wrapped_data_key,
            )


class ConsoleTagWriter:
    """Replaces one person's labels on one session, if it was their session.

    The authorisation is the first statement and there is no path past
    it: a tag may only be written by somebody `session_participant` says
    was in the meeting, and a session that does not exist and one this
    person was not in both answer `None` -- the same 404, for the same
    reason the audio endpoint gives it. A 403 would confirm that a
    meeting exists to somebody just established as having no part in it.

    Whose labels these are is not a filter either: `discord_user_id` is
    in the primary key, so the delete below cannot remove anybody else's
    row even if it were written wrongly, and the insert cannot create a
    row that belongs to somebody else.

    What is stored is exactly what it is handed. Normalisation is
    `sturnus.console.tags`, applied once at the edge, because a second
    copy of "when are two labels the same label" is a second copy that
    will disagree.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace(
        self, session_id: int, *, owner: int, tags: Sequence[str], now: datetime
    ) -> tuple[str, ...] | None:
        """The stored labels afterwards, or `None` for a session not theirs."""
        wanted = set(tags)
        async with self._session_factory() as db:
            was_there = await db.scalar(
                select(SessionParticipant.id)
                .where(
                    SessionParticipant.session_id == session_id,
                    SessionParticipant.discord_user_id == owner,
                )
                .limit(1)
            )
            if was_there is None:
                return None

            held = set(
                (
                    await db.execute(
                        select(SessionTag.tag).where(
                            SessionTag.session_id == session_id,
                            SessionTag.discord_user_id == owner,
                        )
                    )
                )
                .scalars()
                .all()
            )

            # A difference rather than "delete everything, insert the new
            # set". A label that survives an edit keeps the `created_at`
            # it was first written with, which is what makes that column
            # mean anything at all -- rewriting it on every edit would
            # record when somebody last added a *different* tag.
            removed = held - wanted
            if removed:
                await db.execute(
                    delete(SessionTag).where(
                        SessionTag.session_id == session_id,
                        SessionTag.discord_user_id == owner,
                        SessionTag.tag.in_(removed),
                    )
                )
            added = wanted - held
            if added:
                await db.execute(
                    insert(SessionTag)
                    .values(
                        [
                            {
                                "session_id": session_id,
                                "discord_user_id": owner,
                                "tag": tag,
                                "created_at": now,
                            }
                            for tag in sorted(added)
                        ]
                    )
                    # Two tabs saving the same tag at the same moment is
                    # not an error worth a 500: the row they both want
                    # exists either way, which is the whole of what either
                    # of them asked for.
                    .on_conflict_do_nothing(index_elements=["session_id", "discord_user_id", "tag"])
                )
            await db.commit()
        # Sorted, because that is the order every read returns them in and
        # a caller that rendered this answer in another order would show
        # chips that rearrange themselves on the next page load.
        return tuple(sorted(wanted))


class ConsoleQueueControl:
    """Adapts the shared re-queue machinery to the console's `QueueControl`.

    The authorisation is here rather than in a handler, exactly as it is
    for `ConsoleTrackDirectory`: every method resolves the session's guild
    and asks `AdminDirectory` whether this person administers it, and
    answers `None` when they do not. There is no method on this class that
    can be called without `requested_by`, so there is no filter to forget.

    The rule is deliberately *administrator of the guild* rather than
    participant of the session. Playing your own meeting back is a use of
    your own recording; re-running a transcription spends worker time,
    rewrites a shared document and re-announces it, which is an operation
    on the system.

    Everything below the authorisation is
    `sturnus.infrastructure.db.requeue`, unchanged and unwrapped -- the
    same reads and the same locked write the `/queue` command performs. A
    console that reimplemented any of it would be a second definition of
    when a re-queue is safe, and the two would agree right up until one of
    them changed.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admins: AdminDirectory,
    ) -> None:
        self._session_factory = session_factory
        self._admins = admins

    async def status_for(self, session_id: int, *, requested_by: int) -> QueueSnapshot | None:
        guild_id = await self._administered_guild(session_id, requested_by)
        if guild_id is None:
            return None
        view = await load_requeue_view(self._session_factory, guild_id, session_id)
        detail = await load_session(self._session_factory, guild_id, session_id)
        if view is None or detail is None:
            return None
        summary, jobs, names = detail
        return QueueSnapshot(
            session_status=summary.status,
            document_url=summary.document_url,
            speakers=tuple(
                QueueSpeaker(
                    discord_user_id=job.discord_user_id,
                    display_name=names.get(job.discord_user_id),
                    status=job.status,
                    attempts=job.attempts,
                    error=_short_error(job.error),
                )
                for job in jobs
            ),
            can_requeue=not view.is_refused,
            refusal=None if not view.is_refused else refusal_reason(view),
        )

    async def requeue(self, session_id: int, *, requested_by: int) -> RequeueOutcome | None:
        guild_id = await self._administered_guild(session_id, requested_by)
        if guild_id is None:
            return None
        # The plan this returns is the one that was applied under the row
        # lock, or the one that caused the refusal -- never the lock-free
        # read above, which may be seconds stale by now.
        view = await apply_requeue(self._session_factory, guild_id, session_id)
        if view is None:
            return None
        if view.is_refused:
            return RequeueOutcome(False, (), (), refusal_reason(view))
        return RequeueOutcome(
            accepted=True,
            requeued_user_ids=view.plan.resettable_user_ids,
            erased_user_ids=view.plan.erased_user_ids,
            refusal=None,
        )

    async def _administered_guild(self, session_id: int, discord_user_id: int) -> int | None:
        """The session's guild, if this person administers it. `None` otherwise.

        One query for the guild and one for the membership, and the two
        failures are folded into the same `None` on the way out: "no such
        session" and "not yours to touch" must be indistinguishable, for
        the same reason the audio endpoint answers 404 to both.
        """
        async with self._session_factory() as db:
            guild_id = await db.scalar(
                select(SessionRow.guild_id).where(SessionRow.id == session_id)
            )
        if guild_id is None:
            return None
        return guild_id if await self._admins.is_admin(guild_id, discord_user_id) else None


#: How much of a stored error the console shows. `transcription_job.error`
#: is `str(exc)` -- arbitrary text of arbitrary length -- and the console
#: needs enough to recognise a failure, not the whole of it.
MAX_ERROR_CHARS = 200


def _short_error(error: str | None) -> str | None:
    if error is None:
        return None
    collapsed = " ".join(error.split())
    if len(collapsed) <= MAX_ERROR_CHARS:
        return collapsed
    return collapsed[: MAX_ERROR_CHARS - 1] + "…"


def refusal_reason(view: SessionView) -> str:
    """Why a re-queue was refused, in one sentence an administrator can act on.

    The three reasons in the order `render_requeue_refusal` reports them,
    which is the order of what somebody can do about it: a blocked session
    only needs the queue to go idle, an unsettled one needs the pipeline
    to finish, and an empty one will never change.

    Derived from the same `SessionView` the Discord reply is derived from,
    so the console and the command refuse for the same reasons in the same
    order. The wording differs because the audiences do; the decision does
    not.
    """
    if view.plan.is_blocked:
        return (
            "A worker is still holding jobs from this session. Re-queueing now would let it "
            "write the old run's transcript back over the reset. Wait for the queue to go idle."
        )
    if not view.is_settled:
        return (
            "This session has not finished its first pass yet. Only a documented session can "
            "be re-queued; one that is genuinely finished gets there on its own."
        )
    return (
        "There is nothing to re-queue: every recording in this session has been erased, or it "
        "never had any."
    )


#: Why a revocation did nothing. Bounded literals from this file rather
#: than sentences, because they travel into a log line as `reason` -- a
#: field the observability registry admits precisely on the grounds that
#: its values are fixed literals from this repository's own source. The
#: sentences a person reads are the console's, next to the button that
#: produced them.
NO_CONSENT_ON_RECORD = "no_consent_on_record"
ALREADY_REVOKED = "already_revoked"
#: An effective instant before the consent was ever given. Nonsense
#: rather than merely unusual: it would claim a grant ended before it
#: started, and every reader of the row would then have to decide what
#: that meant. Refused rather than clamped, because clamping would store
#: an instant nobody asked for and report success.
EFFECTIVE_BEFORE_GRANT = "effective_before_grant"
#: A widening asked for in a guild whose `video_consent_offered` is
#: false. Refused rather than silently narrowed: a person who asked for
#: something and got a success answering a different question has been
#: told the wrong thing about their own consent.
VIDEO_CONSENT_NOT_OFFERED = "video_consent_not_offered"
#: A widening in a guild with no `policy_version` set. A new grant has to
#: name the policy it was given under, and there is nothing to name.
NO_POLICY_VERSION = "no_policy_version"
#: A scope this code cannot name. From the API's point of view a bad
#: request rather than a conflict; see `routes_consent_self`.
UNKNOWN_SCOPE = "unknown_scope"

#: Why a person's consent stands where it does, as bounded literals for
#: the interface to write a sentence from. Four rather than a boolean
#: because the four lead to four different sentences and two of them --
#: `ACTIVE` and `SCHEDULED` -- are both "you are being recorded right
#: now", which no single flag can say.
STATE_ACTIVE = "active"
#: `revoked_at` is in the future: consent stands until then. The recorder
#: needs no notification to honour it -- `is_consent_active` compares the
#: instant against the current time on every read.
STATE_SCHEDULED = "scheduled"
#: `revoked_at` has passed.
STATE_REVOKED = "revoked"
#: Never withdrawn, and inactive anyway: the guild's `policy_version`
#: moved on and this grant names the old one. A distinct state because it
#: is the one nobody expects and the only one the person did not cause.
STATE_POLICY_SUPERSEDED = "policy_superseded"


@dataclass(frozen=True)
class _ConsentRow:
    """One `consent` row, with the columns the table declares NOT NULL.

    Not `ConsentRecord`: that one makes `granted_at` and `policy_version`
    optional, because the *absence* of a record is one of the states it
    represents. A row that was read out of the table is not absent, and
    carrying the optionality forward would push a `None` check into every
    caller that cannot happen.
    """

    granted_at: datetime
    revoked_at: datetime | None
    policy_version: str
    scope: ConsentScope


class ConsoleConsentDirectory:
    """Who has consented in a guild, and an administrator's power to end it.

    The authorisation is here rather than in a handler, exactly as it is
    for `ConsoleTrackDirectory` and `ConsoleQueueControl`: every method
    asks `AdminDirectory` whether this person administers *this* guild and
    answers `None` when they do not. There is no method that can be called
    without `requested_by`, so there is no filter to forget.

    **The write is `ConsentRepository.record_revocation`, unwrapped.** That
    is the same statement `/consent revoke` makes -- newest row by
    `granted_at`, `revoked_at` stamped rather than a new row inserted,
    because the history keeps grants and a revocation modifies the grant
    it revokes. A console that reimplemented it would be a second
    definition of what a revocation is, and the two would agree right up
    until one of them changed.

    **What this cannot do, and what follows from that.** Consent is two
    layers (Spec 3.1). The Discord role is checked synchronously on every
    frame with no cache; the stored record is checked on every frame
    through `ConsentCache`'s five second TTL. This process holds no
    Discord token (Spec 13.2) so it writes the record and leaves the role
    alone -- which stops the recording within five seconds, mid-session,
    because the stored record is the layer that exists precisely because
    the role can be bypassed. It does not take the role away, and the
    console says so rather than letting an administrator infer it.

    It also does not erase anything already recorded. `/audio purge`
    does, it is admin-gated, and it is deliberately a separate act:
    withdrawing consent is a decision about the future, and deleting a
    meeting a team has already read is not the same decision. Every
    holder therefore carries `recordings_with_audio`, so nobody has to
    guess which of the two they just did.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admins: AdminDirectory,
        config: SettingsStore,
        now: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._admins = admins
        self._config = config
        self._consents = ConsentRepository(session_factory)
        self._now = now

    async def holders(
        self, guild_id: int, *, requested_by: int
    ) -> tuple[ConsentHolder, ...] | None:
        if not await self._admins.is_admin(guild_id, requested_by):
            return None

        newest = await self._newest_consent_per_person(guild_id)
        if not newest:
            return ()

        people = tuple(newest)
        names = await self._latest_display_names(guild_id, people)
        held = await self._recordings_with_audio(guild_id, people)
        # Read once for the whole listing rather than per person: whether
        # a grant is still active depends on the guild's current policy
        # version, and that is one value, not one per row.
        policy = (await self._config.snapshot(guild_id)).get(settings.POLICY_VERSION, "")

        return tuple(
            ConsentHolder(
                discord_user_id=discord_user_id,
                display_name=names.get(discord_user_id),
                policy_version=row.policy_version,
                granted_at=row.granted_at,
                revoked_at=row.revoked_at,
                # The domain's rule, not a reimplementation of it. An
                # administrator must be shown the same verdict the
                # recorder acts on -- including the case nobody expects,
                # where a policy bump has quietly ended a consent nobody
                # withdrew.
                active=is_consent_active(
                    ConsentRecord(
                        granted_at=row.granted_at,
                        revoked_at=row.revoked_at,
                        policy_version=row.policy_version,
                        scope=row.scope,
                    ),
                    policy,
                    self._now(),
                ),
                recordings_with_audio=held.get(discord_user_id, 0),
                scope=row.scope.value,
            )
            # By id, so two page loads agree. What order a *person*
            # wants to read this in is the console's decision, made in
            # `~/utils/consents` where it can be tested without a
            # database.
            for discord_user_id, row in sorted(newest.items())
        )

    async def revoke(
        self,
        guild_id: int,
        discord_user_id: int,
        *,
        requested_by: int,
        effective_at: datetime | None = None,
    ) -> RevocationOutcome | None:
        """Withdraws one person's consent, from an instant of the caller's choosing.

        `effective_at` absent means now, which is exactly what this method
        did before it existed. A future instant is a scheduled
        withdrawal and needs no machinery to fire: `is_consent_active`
        compares it against the current time, and the recorder re-reads
        the row through a five-second cache, so it takes effect within
        five seconds of the instant it names.

        **A past instant does not delete anything, and the answer says
        how much it did not delete.** Back-dating a revocation is a
        statement about recordings that already exist -- somebody left in
        March and nobody wrote it down until June -- and turning that
        statement into an erasure would mean an administrator correcting a
        date had silently destroyed three months of meetings their team
        has read. `recordings_from_effective_at` is how many recordings
        with audio fall on or after the instant, so the console can offer
        `/audio purge` as the separate, deliberate act it is.
        """
        if not await self._admins.is_admin(guild_id, requested_by):
            return None
        return await _write_revocation(
            self._consents,
            self._session_factory,
            self._now,
            guild_id,
            discord_user_id,
            effective_at,
        )

    async def _newest_consent_per_person(self, guild_id: int) -> dict[int, _ConsentRow]:
        """The newest grant per person in this guild.

        Ordered and folded rather than a window function, and ordered by
        `granted_at` descending because that is the rule
        `ConsentRepository.current` applies -- the console must show the
        row the recorder acts on, not a different one. `id` descending is
        added as a tiebreak the repository does not have: two grants at
        the same instant are not a thing that happens, and a listing whose
        order the planner decides is a listing that changes between two
        refreshes for no reason.
        """
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        Consent.discord_user_id,
                        Consent.granted_at,
                        Consent.revoked_at,
                        Consent.policy_version,
                        Consent.scope,
                    )
                    .where(Consent.guild_id == guild_id)
                    .order_by(
                        Consent.discord_user_id,
                        Consent.granted_at.desc(),
                        Consent.id.desc(),
                    )
                )
            ).all()

        newest: dict[int, _ConsentRow] = {}
        for discord_user_id, granted_at, revoked_at, policy_version, scope in rows:
            newest.setdefault(
                discord_user_id,
                _ConsentRow(granted_at, revoked_at, policy_version, scope_of(scope)),
            )
        return newest

    async def _latest_display_names(self, guild_id: int, people: Sequence[int]) -> dict[int, str]:
        """The name each person last appeared under in this guild.

        `consent` stores no name, and a page of eighteen-digit numbers is
        not a page an administrator can act on. The most recent
        `session_participant` row is the closest thing the system holds --
        the name at the time of somebody's last recorded meeting -- and it
        is scoped to this guild, because a display name is per-guild and
        borrowing one from another guild would put a nickname from
        somewhere else next to a decision about this one.
        """
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        SessionParticipant.discord_user_id,
                        SessionParticipant.discord_display_name,
                        SessionRow.started_at,
                    )
                    .join(SessionRow, SessionRow.id == SessionParticipant.session_id)
                    .where(
                        SessionRow.guild_id == guild_id,
                        SessionParticipant.discord_user_id.in_(people),
                    )
                    .order_by(
                        SessionParticipant.discord_user_id,
                        SessionRow.started_at.desc(),
                    )
                )
            ).all()

        names: dict[int, str] = {}
        for discord_user_id, display_name, _started_at in rows:
            names.setdefault(discord_user_id, display_name)
        return names

    async def _recordings_with_audio(self, guild_id: int, people: Sequence[int]) -> dict[int, int]:
        """How many recordings of each person this guild still holds.

        `audio_deleted_at IS NULL` is the only claim that an object is
        still in the store: the retention sweep erases the object first
        and stamps the row second, so a stamped row is one whose audio is
        already gone. Counting stamped rows would tell an administrator
        that revoking consent leaves recordings behind which were erased
        weeks ago.
        """
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(TranscriptionJob.discord_user_id, func.count())
                    .join(SessionRow, SessionRow.id == TranscriptionJob.session_id)
                    .where(
                        SessionRow.guild_id == guild_id,
                        TranscriptionJob.discord_user_id.in_(people),
                        TranscriptionJob.audio_deleted_at.is_(None),
                    )
                    .group_by(TranscriptionJob.discord_user_id)
                )
            ).all()
        return {discord_user_id: held for discord_user_id, held in rows}


#: What goes in `consent.source` for a grant made from the console. The
#: only other value the column has ever held is `"button"`, from the
#: Discord consent embed. It is not decoration: a grant that widened a
#: scope from a web page and one made by pressing a button under a policy
#: link are different acts, and the column is the only place the
#: difference survives.
_CONSOLE_SOURCE = "console"


async def _write_revocation(
    consents: ConsentRepository,
    session_factory: async_sessionmaker[AsyncSession],
    now: Callable[[], datetime],
    guild_id: int,
    discord_user_id: int,
    effective_at: datetime | None,
) -> RevocationOutcome:
    """What a revocation *is*, once who may ask has been settled elsewhere.

    A module-level function rather than a method because two classes need
    it and neither owns it: `ConsoleConsentDirectory.revoke` reaches it
    after an administrator check, `ConsolePersonalConsents.revoke_own`
    after the session has already named the subject. They differ in who
    may ask and in nothing else, and a second implementation of the rest
    would agree with this one right up until one of them changed -- the
    same argument `ConsoleConsentDirectory` makes for calling
    `ConsentRepository.record_revocation` unwrapped.

    `effective_at` absent means now, which is what every caller meant
    before the parameter existed.
    """
    # Read before writing, only so the answer can say what happened.
    # `record_revocation` is idempotent and silent -- it stamps the newest
    # row or does nothing -- and somebody told "revoked" for a consent
    # that was never given would believe a protection is in place that
    # never was.
    record = await consents.current(discord_user_id, guild_id)
    if record is None or record.granted_at is None:
        return RevocationOutcome(revoked=False, refusal=NO_CONSENT_ON_RECORD)
    if record.revoked_at is not None:
        return RevocationOutcome(revoked=False, refusal=ALREADY_REVOKED)

    instant = now() if effective_at is None else effective_at
    if instant < record.granted_at:
        # Refused rather than clamped to `granted_at`. Clamping would
        # store an instant nobody asked for and report success, and the
        # request is itself evidence that whoever made it is working from
        # a date this system disagrees with -- which is worth saying.
        return RevocationOutcome(revoked=False, refusal=EFFECTIVE_BEFORE_GRANT)

    # A grant naming a superseded `policy_version` is revoked rather than
    # refused, even though it is already inactive. It is inactive
    # *because of a setting*, and a setting can be set back; stamping
    # `revoked_at` is the only thing that survives somebody restoring the
    # old policy version.
    await consents.record_revocation(discord_user_id, guild_id, instant)
    return RevocationOutcome(
        revoked=True,
        refusal=None,
        effective_at=instant,
        recordings_from_effective_at=await _recordings_since(
            session_factory, guild_id, discord_user_id, instant
        ),
    )


async def _recordings_since(
    session_factory: async_sessionmaker[AsyncSession],
    guild_id: int,
    discord_user_id: int,
    instant: datetime,
) -> int:
    """Recordings of this person in this guild that fall on or after `instant`.

    **This is a count, and counting is all that happens.** A back-dated
    revocation says something about recordings that already exist; it does
    not erase them, and the number exists so the console can offer the
    erasure path (`/audio purge`, admin-gated, in Discord) rather than
    quietly taking it. `/audio purge` and the retention sweep are the only
    two things in this system that delete audio, and this change
    deliberately does not add a third.

    By the session's `started_at`, because that is when the person was in
    the room -- no job row carries a time a human would read as "when this
    was recorded". `audio_deleted_at IS NULL` for the reason
    `ConsoleConsentDirectory._recordings_with_audio` gives: the retention
    sweep erases the object before it stamps the row, so a stamped row is
    one whose audio is already gone.
    """
    async with session_factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(TranscriptionJob)
            .join(SessionRow, SessionRow.id == TranscriptionJob.session_id)
            .where(
                SessionRow.guild_id == guild_id,
                TranscriptionJob.discord_user_id == discord_user_id,
                TranscriptionJob.audio_deleted_at.is_(None),
                SessionRow.started_at >= instant,
            )
        )
    return int(count or 0)


def _state_of(record: ConsentRecord, policy: str, now: datetime) -> str:
    """Why this consent stands where it does, as one bounded literal.

    Ordered so the answer names the *cause* rather than the first
    condition that happens to be true: a withdrawal the person chose is
    what they need to be told about even in a guild whose policy has also
    moved on, because it is the one they can act on.
    """
    if record.revoked_at is not None:
        return STATE_REVOKED if now >= record.revoked_at else STATE_SCHEDULED
    if is_consent_active(record, policy, now):
        return STATE_ACTIVE
    return STATE_POLICY_SUPERSEDED


class ConsolePersonalConsents:
    """What one person may see and change about their own consent.

    The mirror image of `ConsoleConsentDirectory`, and a separate class
    for one structural reason: **there is no argument here for somebody
    else.** Every method takes the signed-in person and acts on that
    person, so a handler cannot act on a third party through this object
    even by mistake. The administrator's power to withdraw somebody
    else's consent stays where it is, behind an `AdminDirectory` check,
    and the two never share an entry point.

    **The write for a widening is an insert, not an update.** `consent` is
    an append-only history: a grant is a row, and the newest row by
    `granted_at` is what the recorder acts on. Widening `audio` to
    `audio_video` is a *grant* -- somebody agreeing to something they had
    not agreed to -- so it inserts a row carrying the guild's current
    `policy_version`. Overwriting the scope in place would leave a record
    claiming video consent under a policy document written before video
    was a question, which is precisely the record an append-only history
    exists to make impossible.

    Narrowing is not a grant and does not insert. It withdraws part of
    what was given, and a withdrawal modifies the grant it withdraws from
    -- the same rule `ConsentRepository.record_revocation` follows.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: SettingsStore,
        now: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._consents = ConsentRepository(session_factory)
        self._now = now

    async def for_person(self, discord_user_id: int) -> tuple[OwnConsent, ...]:
        """Every guild this person holds a consent record in.

        One configuration read per guild rather than one query for all of
        them: a person belongs to a handful of guilds, `SettingsStore`
        offers `snapshot` and nothing narrower on purpose (see its
        docstring), and a bespoke join here would be a second way to
        resolve a setting -- the thing that port exists to prevent.
        """
        newest = await self._newest_per_guild(discord_user_id)
        now = self._now()
        answers: list[OwnConsent] = []
        for guild_id, row in sorted(newest.items()):
            stored = await self._config.snapshot(guild_id)
            policy = stored.get(settings.POLICY_VERSION, "")
            record = ConsentRecord(
                granted_at=row.granted_at,
                revoked_at=row.revoked_at,
                policy_version=row.policy_version,
                scope=row.scope,
            )
            answers.append(
                OwnConsent(
                    guild_id=guild_id,
                    state=_state_of(record, policy, now),
                    # The domain's rule, not a second reading of it. A
                    # person must be shown the same verdict the recorder
                    # acts on, including the case nobody expects: a policy
                    # bump has ended a consent they never withdrew.
                    active=is_consent_active(record, policy, now),
                    scope=row.scope.value,
                    policy_version=row.policy_version,
                    guild_policy_version=policy,
                    granted_at=row.granted_at,
                    revoked_at=row.revoked_at,
                    video_consent_offered=settings.is_true(
                        stored.get(settings.VIDEO_CONSENT_OFFERED)
                    ),
                )
            )
        return tuple(answers)

    async def set_scope(self, discord_user_id: int, guild_id: int, scope: str) -> ScopeOutcome:
        """Narrows or widens what this person's consent covers."""
        try:
            wanted = ConsentScope(scope)
        except ValueError:
            return ScopeOutcome(scope=scope, changed=False, refusal=UNKNOWN_SCOPE)

        record = await self._consents.current(discord_user_id, guild_id)
        if record is None or record.granted_at is None:
            return ScopeOutcome(scope=scope, changed=False, refusal=NO_CONSENT_ON_RECORD)
        if record.revoked_at is not None:
            # Not a scope question. Somebody whose consent has ended
            # consents again, in Discord, under the policy in force then
            # -- they do not edit a record that already says it stopped.
            return ScopeOutcome(scope=scope, changed=False, refusal=ALREADY_REVOKED)

        if record.scope == wanted:
            # Neither a refusal nor a write: asking for the scope you
            # already have is a page that was open a while, and answering
            # it with a new grant row would stamp a fresh `granted_at` on
            # a decision nobody made.
            return ScopeOutcome(
                scope=wanted.value,
                changed=False,
                refusal=None,
                policy_version=record.policy_version,
            )

        if wanted is ConsentScope.AUDIO:
            # Narrowing. Immediate, unconditional, and nothing to check:
            # nobody needs permission to consent to less.
            await self._consents.narrow_scope(discord_user_id, guild_id, wanted)
            return ScopeOutcome(
                scope=wanted.value,
                changed=True,
                refusal=None,
                policy_version=record.policy_version,
            )

        stored = await self._config.snapshot(guild_id)
        if not settings.is_true(stored.get(settings.VIDEO_CONSENT_OFFERED)):
            # The guild has not asserted that its policy document names
            # video, and software cannot read the document to check. A
            # silent downgrade to `audio` would report success for a
            # question the person did not ask; this says no.
            return ScopeOutcome(
                scope=record.scope.value,
                changed=False,
                refusal=VIDEO_CONSENT_NOT_OFFERED,
            )
        policy = stored.get(settings.POLICY_VERSION, "")
        if not policy:
            # A guild with no policy version records no active consent at
            # all (`is_consent_active`), so there is nothing for a new
            # grant to name and no wording for it to stand under.
            return ScopeOutcome(
                scope=record.scope.value,
                changed=False,
                refusal=NO_POLICY_VERSION,
            )

        await self._consents.record_grant(
            discord_user_id,
            guild_id,
            policy,
            source=_CONSOLE_SOURCE,
            now=self._now(),
            scope=wanted,
        )
        return ScopeOutcome(scope=wanted.value, changed=True, refusal=None, policy_version=policy)

    async def revoke_own(self, discord_user_id: int, guild_id: int) -> RevocationOutcome:
        """This person withdrawing their own consent, effective now.

        No `effective_at`. Back-dating is an administrator's correction of
        a record -- "they left in March and nobody wrote it down" -- and
        scheduling is a guild's arrangement; a person withdrawing their
        own consent means now, and a date field here would invite somebody
        to withdraw retroactively under the impression that it erases
        something. It does not (see `_recordings_since`).

        The role stays. `api` holds no Discord token (Spec 13.2), so this
        writes `revoked_at` and nothing else: recording stops within the
        consent cache's five seconds, and Discord goes on showing a role
        that no longer means anything. The answer says so rather than
        leaving the person to find out from `/consent status` -- see
        `routes_consent_self`.
        """
        return await _write_revocation(
            self._consents,
            self._session_factory,
            self._now,
            guild_id,
            discord_user_id,
            None,
        )

    async def _newest_per_guild(self, discord_user_id: int) -> dict[int, _ConsentRow]:
        """The newest grant per guild for this person.

        `ConsoleConsentDirectory._newest_consent_per_person` turned ninety
        degrees, and ordered by the same rule for the same reason:
        `ConsentRepository.current` reads the newest by `granted_at`, and
        showing somebody a row the recorder does not act on would show
        them a decision nothing enforces. `id` descending is the tiebreak
        that keeps two page loads agreeing.
        """
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        Consent.guild_id,
                        Consent.granted_at,
                        Consent.revoked_at,
                        Consent.policy_version,
                        Consent.scope,
                    )
                    .where(Consent.discord_user_id == discord_user_id)
                    .order_by(
                        Consent.guild_id,
                        Consent.granted_at.desc(),
                        Consent.id.desc(),
                    )
                )
            ).all()

        newest: dict[int, _ConsentRow] = {}
        for guild_id, granted_at, revoked_at, policy_version, scope in rows:
            newest.setdefault(
                guild_id,
                _ConsentRow(granted_at, revoked_at, policy_version, scope_of(scope)),
            )
        return newest


class ConsoleQueueOverview:
    """A guild's transcription queue, for an administrator of that guild.

    The guild-wide companion to `ConsoleQueueControl`, and built the same
    way: the administrator check is part of the one call, and everything
    below it is `sturnus.infrastructure.db.requeue` unchanged -- the same
    `load_status` the `/queue status` command reads. A console that
    counted the jobs itself would be a second definition of "how much work
    is outstanding", and the two would agree until one of them changed.

    `load_active_sessions` is new machinery rather than reused, because
    Discord never needed it: a slash command answers in one message and
    reports totals, while a page has room to say *which* sessions the
    totals are made of. It lives beside the other reads for the same
    reason they do -- so both callers ask the same questions.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admins: AdminDirectory,
        now: Callable[[], datetime],
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._admins = admins
        self._now = now
        #: The lease this process *assumes*. The one that actually applies
        #: is `job_lease_seconds` in the worker's settings, which this
        #: process cannot see -- so the number travels out with the answer
        #: and the console names it rather than presenting a count derived
        #: from a guess as a fact. The same caveat `/queue status` prints.
        self._lease_seconds = lease_seconds

    async def for_guild(self, guild_id: int, *, requested_by: int) -> GuildQueue | None:
        if not await self._admins.is_admin(guild_id, requested_by):
            return None

        now = self._now()
        status = await load_status(self._session_factory, guild_id, now, self._lease_seconds)
        sessions, truncated = await load_active_sessions(self._session_factory, guild_id)
        return GuildQueue(
            pending=status.counts.get("pending", 0),
            running=status.counts.get("running", 0),
            done=status.counts.get("done", 0),
            dead=status.counts.get("dead", 0),
            running_past_lease=status.running_past_lease,
            oldest_pending_session_ended_at=status.oldest_pending_session_ended_at,
            closed_undocumented=status.closed_undocumented,
            lease_seconds=self._lease_seconds,
            sessions=tuple(_queued(session) for session in sessions),
            truncated=truncated,
        )


def _queued(session: ActiveSession) -> QueuedSession:
    return QueuedSession(
        id=session.id,
        channel_id=session.channel_id,
        channel_name=session.channel_name,
        started_at=session.started_at,
        ended_at=session.ended_at,
        status=session.status,
        document_url=session.document_url,
        pending=session.counts.get("pending", 0),
        running=session.counts.get("running", 0),
        done=session.counts.get("done", 0),
        dead=session.counts.get("dead", 0),
    )


class ConsoleGuildReports:
    """A guild's recorded sessions, counted rather than listed.

    The authorisation is here, as it is in every other directory in this
    module: one `is_admin(guild_id, ...)` at the top of the one method,
    and `None` for both of the reasons somebody might not get an answer.

    **What the statements deliberately do not select.** Nothing here reads
    `session_participant.discord_user_id` into a value that leaves this
    class. The participant rows are counted -- per session, and distinctly
    across the guild -- and the identities stay in the database. That is
    what keeps `sturnus.console.reporting` able to say it is about a guild
    rather than about its people: a report module handed a list of who
    attended is one edit away from ranking them, and a ranking of
    colleagues by meeting attendance is a works-council decision rather
    than a console feature.

    Three statements rather than one join, the same trade
    `ConsoleQueries` makes: a join across participants and jobs multiplies
    rows, and a session with five speakers and five tracks comes back
    twenty-five times.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admins: AdminDirectory,
        config: SettingsStore,
    ) -> None:
        self._session_factory = session_factory
        self._admins = admins
        self._config = config

    async def recording_of(self, guild_id: int, *, requested_by: int) -> GuildRecording | None:
        if not await self._admins.is_admin(guild_id, requested_by):
            return None

        zone, zone_name = _zone(
            (await self._config.snapshot(guild_id)).get(settings.TIMEZONE) or "UTC"
        )
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        SessionRow.id,
                        SessionRow.started_at,
                        SessionRow.ended_at,
                        SessionRow.status,
                    )
                    .where(SessionRow.guild_id == guild_id)
                    .order_by(SessionRow.started_at, SessionRow.id)
                )
            ).all()
            if not rows:
                return GuildRecording((), 0, zone, zone_name)

            found = [row.id for row in rows]
            people = (
                await db.execute(
                    select(SessionParticipant.session_id, func.count())
                    .where(SessionParticipant.session_id.in_(found))
                    .group_by(SessionParticipant.session_id)
                )
            ).all()
            # Counted in the statement rather than by collecting ids and
            # taking a set: `COUNT(DISTINCT ...)` answers the question
            # without any identity crossing into Python.
            distinct = await db.scalar(
                select(func.count(func.distinct(SessionParticipant.discord_user_id))).where(
                    SessionParticipant.session_id.in_(found)
                )
            )
            tracks = (
                await db.execute(
                    select(
                        TranscriptionJob.session_id,
                        func.count(),
                        func.sum(TranscriptionJob.audio_seconds),
                        func.sum(TranscriptionJob.speech_seconds),
                        # Null is not zero. `SUM` skips nulls silently, so
                        # the number of rows it skipped is counted beside
                        # it -- otherwise "we never measured this" and
                        # "they said nothing" arrive as the same total.
                        func.count().filter(TranscriptionJob.speech_seconds.is_(None)),
                    )
                    .where(TranscriptionJob.session_id.in_(found))
                    .group_by(TranscriptionJob.session_id)
                )
            ).all()

        attendance = {session_id: int(count) for session_id, count in people}
        measured = {
            session_id: (int(count), audio, speech, int(unmeasured))
            for session_id, count, audio, speech, unmeasured in tracks
        }
        return GuildRecording(
            sessions=tuple(
                _recorded(row, attendance.get(row.id, 0), measured.get(row.id)) for row in rows
            ),
            distinct_participants=int(distinct or 0),
            zone=zone,
            zone_name=zone_name,
        )


def _recorded(
    row: Row[tuple[int, datetime, datetime | None, str]],
    participants: int,
    measured: tuple[int, float | None, float | None, int] | None,
) -> RecordedSession:
    tracks, audio_seconds, speech_seconds, unmeasured = measured or (0, None, None, 0)
    return RecordedSession(
        id=row.id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        documented=row.status == DOCUMENTED_STATUS,
        participants=participants,
        tracks=tracks,
        audio_seconds=audio_seconds,
        speech_seconds=speech_seconds,
        unmeasured_tracks=unmeasured,
    )


def _zone(name: str) -> tuple[tzinfo, str]:
    """The guild's timezone, falling back to UTC on an unusable value.

    The same fallback the worker applies when writing a protocol, and for
    the same reason: a report with the wrong month boundary is a smaller
    loss than no report, and the value that caused it is a `/config` away
    from being fixed. The name travels with the zone so the page can say
    which calendar it cut the months in rather than leaving the reader to
    assume theirs.
    """
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        return UTC, "UTC"


class ConsoleProfileDirectory:
    """The Outline display name behind a Discord id.

    Built on `AccountLinkRepository` rather than on a query of its own.
    That repository already reads `account_link` by Discord id and
    provider -- it is what `/link status` answers from -- and the console
    has no different question to ask of the row. A fourth `select` over
    the same two columns would be a second place to remember that
    `account_link` is keyed by provider.

    The provider is fixed at construction, because there is exactly one
    identity provider for the console and a caller that could name it
    could name the wrong one.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._links = AccountLinkRepository(session_factory, provider=PROVIDER)

    async def display_name_for(self, discord_user_id: int) -> str | None:
        """What to call this person, or `None` if nothing links them.

        The name is what the person's Outline account is called, which is
        what they saw on screen when they linked. It is never logged:
        `display_name` is in `sturnus.observability.fields.DENIED_NAMES`,
        and a name reaching a log line is a name in a log aggregator
        nobody consented to being in.
        """
        found = await self._links.external_identity(discord_user_id)
        return None if found is None else found[1]


class ConsoleGuildNames:
    """A guild's mirrored channels, roles and named people, for an administrator.

    Three statements over the three mirrors, with the administrator check
    where every other directory in this module puts it: inside the one
    call, so a handler cannot serve a guild by forgetting to ask.

    **Read here rather than through `DirectoryStore`.** That store is the
    bot's writer and its readers exist to let a sweep compare; they order
    rows the way Discord stores them and they do not carry `synced_at`.
    What a console needs is the order a human scans in and the age of what
    they are scanning, and bending the writer's readers to serve both
    would give the sweep an ordering it has no use for.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admins: AdminDirectory,
    ) -> None:
        self._session_factory = session_factory
        self._admins = admins

    async def for_guild(self, guild_id: int, *, requested_by: int) -> GuildDirectory | None:
        if not await self._admins.is_admin(guild_id, requested_by):
            return None

        async with self._session_factory() as db:
            # Ordered in the statement and never afterwards. A handler
            # that sorted the answer would be a second ordering to keep
            # in step with this one, and the database is where the rows
            # already are.
            channels = (
                await db.execute(
                    select(
                        GuildChannel.channel_id,
                        GuildChannel.name,
                        GuildChannel.kind,
                        GuildChannel.position,
                        GuildChannel.synced_at,
                    )
                    .where(GuildChannel.guild_id == guild_id)
                    # Kind first: somebody looking for a voice channel is
                    # not reading past the text ones. Then position,
                    # which is the order in the other window. Then name,
                    # and finally id, so two channels sharing a position
                    # never swap places between two page loads.
                    .order_by(
                        GuildChannel.kind,
                        GuildChannel.position,
                        GuildChannel.name,
                        GuildChannel.channel_id,
                    )
                )
            ).all()
            roles = (
                await db.execute(
                    select(
                        GuildRole.role_id,
                        GuildRole.name,
                        GuildRole.position,
                        GuildRole.synced_at,
                    )
                    .where(GuildRole.guild_id == guild_id)
                    # Descending, which is Discord's own sense of
                    # importance: the role at the top of the server
                    # settings is the one an administrator means first.
                    .order_by(GuildRole.position.desc(), GuildRole.name, GuildRole.role_id)
                )
            ).all()
            members = (
                await db.execute(
                    select(
                        GuildMember.discord_user_id,
                        GuildMember.display_name,
                        GuildMember.synced_at,
                    )
                    .where(GuildMember.guild_id == guild_id)
                    # By name rather than by id: a person scanning this
                    # list is reading names, and an id order is a shuffle
                    # to everybody but the database.
                    .order_by(GuildMember.display_name, GuildMember.discord_user_id)
                )
            ).all()

        return GuildDirectory(
            channels=tuple(
                MirroredChannel(row.channel_id, row.name, row.kind, row.position)
                for row in channels
            ),
            roles=tuple(MirroredRole(row.role_id, row.name, row.position) for row in roles),
            members=tuple(MirroredMember(row.discord_user_id, row.display_name) for row in members),
            # Read off all three lists rather than queried separately:
            # the rows are already here, and a fourth statement to
            # aggregate what three statements just returned is a round
            # trip bought for nothing.
            synced_at=_oldest(
                [
                    *(row.synced_at for row in channels),
                    *(row.synced_at for row in roles),
                    *(row.synced_at for row in members),
                ]
            ),
        )


class ConsoleCollectionNames:
    """Outline's mirrored collections, for anybody who administers a guild.

    The only authorisation question this can ask, because the mirror is
    not per guild: one deployment talks to one Outline instance. Somebody
    who administers nothing never configures a `document_target`, so they
    get the same `None` a guild directory gives -- and the endpoint turns
    it into the same 404.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admins: AdminDirectory,
    ) -> None:
        self._session_factory = session_factory
        self._admins = admins

    async def mirrored(self, *, requested_by: int) -> CollectionListing | None:
        if not await self._admins.is_admin_anywhere(requested_by):
            return None

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        OutlineCollection.collection_id,
                        OutlineCollection.name,
                        OutlineCollection.synced_at,
                    )
                    # By name, which is what somebody choosing a
                    # collection is reading and what Outline's own sidebar
                    # is ordered by. Ties break on id so the same mirror
                    # renders identically on every page load.
                    .order_by(OutlineCollection.name, OutlineCollection.collection_id)
                )
            ).all()

        return CollectionListing(
            collections=tuple(MirroredCollection(row.collection_id, row.name) for row in rows),
            synced_at=_oldest(row.synced_at for row in rows),
        )


def _oldest(moments: Iterable[datetime]) -> datetime | None:
    """The stalest of several mirror timestamps, or `None` if there are none.

    The oldest rather than the newest, deliberately: see `GuildDirectory`
    on why a freshness claim is only as good as the stalest part of what
    it describes.
    """
    return min(moments, default=None)
