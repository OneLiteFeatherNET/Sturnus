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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.linking import new_state
from sturnus.console.ports import (
    AdminDirectory,
    ConsentHolder,
    QueueSnapshot,
    QueueSpeaker,
    RequeueOutcome,
    RevocationOutcome,
    SettingsStore,
    Track,
)
from sturnus.domain import settings
from sturnus.domain.consent import ConsentRecord, is_consent_active
from sturnus.infrastructure.db.models import (
    AccountLink,
    Consent,
    ConsoleState,
    SessionParticipant,
    TranscriptionJob,
)
from sturnus.infrastructure.db.models import Session as SessionRow
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.db.requeue import (
    SessionView,
    apply_requeue,
    load_requeue_view,
    load_session,
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
                    ),
                    policy,
                ),
                recordings_with_audio=held.get(discord_user_id, 0),
            )
            # By id, so two page loads agree. What order a *person*
            # wants to read this in is the console's decision, made in
            # `~/utils/consents` where it can be tested without a
            # database.
            for discord_user_id, row in sorted(newest.items())
        )

    async def revoke(
        self, guild_id: int, discord_user_id: int, *, requested_by: int
    ) -> RevocationOutcome | None:
        if not await self._admins.is_admin(guild_id, requested_by):
            return None

        # Read before writing, only so the answer can say what happened.
        # `record_revocation` is idempotent and silent -- it stamps the
        # newest row or does nothing -- and an administrator told
        # "revoked" for somebody who never consented would believe a
        # protection is in place that never was.
        record = await self._consents.current(discord_user_id, guild_id)
        if record is None or record.granted_at is None:
            return RevocationOutcome(revoked=False, refusal=NO_CONSENT_ON_RECORD)
        if record.revoked_at is not None:
            return RevocationOutcome(revoked=False, refusal=ALREADY_REVOKED)

        # A grant naming a superseded `policy_version` is revoked rather
        # than refused, even though it is already inactive. It is inactive
        # *because of a setting*, and a setting can be set back; stamping
        # `revoked_at` is the only thing that survives somebody restoring
        # the old policy version.
        await self._consents.record_revocation(discord_user_id, guild_id, self._now())
        return RevocationOutcome(revoked=True, refusal=None)

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
        for discord_user_id, granted_at, revoked_at, policy_version in rows:
            newest.setdefault(
                discord_user_id,
                _ConsentRow(granted_at, revoked_at, policy_version),
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
