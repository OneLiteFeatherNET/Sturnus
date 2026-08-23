"""The consent directory, against the real database.

Against PostgreSQL rather than a double because everything worth pinning
here is a property of the statements: which row of several is the current
one, that a guild's listing contains only that guild's people, and that
the count of recordings still held excludes the ones the retention sweep
erased. A double would agree with whatever it was written to agree with.

The authorisation half is here too, and it is here rather than in the
route tests on purpose: the rule lives in the adapter, so this is where a
regression would appear. A handler cannot restore a check the adapter
dropped, because the handler was never given anything to check.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.adapters import (
    ALREADY_REVOKED,
    EFFECTIVE_BEFORE_GRANT,
    NO_CONSENT_ON_RECORD,
    NO_POLICY_VERSION,
    STATE_ACTIVE,
    STATE_POLICY_SUPERSEDED,
    STATE_REVOKED,
    STATE_SCHEDULED,
    VIDEO_CONSENT_NOT_OFFERED,
    ConsoleConsentDirectory,
    ConsolePersonalConsents,
)
from sturnus.domain import settings
from sturnus.domain.consent import ConsentScope
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import (
    Base,
    Consent,
    Session,
    SessionParticipant,
    TranscriptionJob,
)

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
GUILD, OTHER_GUILD = 4711, 9999
ANNA, BEN, CARL = 100, 200, 300
POLICY = "2026-01"


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class Admins:
    """The mirrored administrator membership, per guild.

    Per guild and never a bare set, because "administers something" and
    "administers *this*" are the two questions this whole feature turns
    on, and a double that could not tell them apart would let a test
    claiming the second prove only the first.
    """

    def __init__(self, by_guild: dict[int, set[int]] | None = None) -> None:
        self.by_guild = by_guild if by_guild is not None else {GUILD: {ANNA}}

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        return any(discord_user_id in members for members in self.by_guild.values())

    async def administered_guilds(self, discord_user_id: int) -> tuple[int, ...]:
        return tuple(
            sorted(
                guild_id
                for guild_id, members in self.by_guild.items()
                if discord_user_id in members
            )
        )

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool:
        return discord_user_id in self.by_guild.get(guild_id, set())


def directory(
    factory: async_sessionmaker[AsyncSession],
    *,
    admins: Admins | None = None,
    now: datetime = T0,
) -> ConsoleConsentDirectory:
    return ConsoleConsentDirectory(
        factory,
        admins or Admins(),
        ConfigStore(factory),
        lambda: now,
    )


async def set_policy(
    factory: async_sessionmaker[AsyncSession],
    version: str = POLICY,
    guild_id: int = GUILD,
) -> None:
    await ConfigStore(factory).set(guild_id, settings.POLICY_VERSION, version, T0)


async def grant(
    factory: async_sessionmaker[AsyncSession],
    discord_user_id: int,
    *,
    guild_id: int = GUILD,
    granted_at: datetime = T0,
    revoked_at: datetime | None = None,
    policy_version: str = POLICY,
    scope: ConsentScope = ConsentScope.AUDIO,
) -> None:
    """One `consent` row, written straight to the table.

    Direct inserts rather than `ConsentRepository.record_grant`: what is
    under test is which of several rows the directory reads back, and
    going through the writer would make it a test of two things at once --
    including a writer that cannot produce a revoked row at all.
    """
    async with factory() as db:
        db.add(
            Consent(
                discord_user_id=discord_user_id,
                guild_id=guild_id,
                granted_at=granted_at,
                revoked_at=revoked_at,
                policy_version=policy_version,
                source="button",
                scope=scope.value,
            )
        )
        await db.commit()


async def a_recorded_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: int = GUILD,
    started_at: datetime = T0,
    people: dict[int, str] | None = None,
    audio_deleted: bool = False,
) -> int:
    """A closed session with one participant and one recording per person."""
    async with factory() as db:
        session = Session(
            guild_id=guild_id,
            channel_id=555,
            channel_name="meeting",
            started_at=started_at,
            ended_at=started_at + timedelta(hours=1),
            status="documented",
        )
        db.add(session)
        await db.flush()
        for discord_user_id, name in (people or {ANNA: "anna"}).items():
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    discord_display_name=name,
                    first_seen_at=started_at,
                )
            )
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=discord_user_id,
                    s3_key=f"sessions/{session.id}/speakers/{discord_user_id}.enc",
                    encryption_key_id="k1",
                    wrapped_data_key=b"wrapped",
                    retention_until=started_at + timedelta(days=30),
                    status="done",
                    attempts=1,
                    audio_deleted_at=started_at if audio_deleted else None,
                )
            )
        await db.commit()
        return session.id


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


async def test_an_administrator_of_the_guild_sees_who_consented(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert [holder.discord_user_id for holder in holders] == [BEN]


async def test_an_administrator_of_another_guild_is_nobody_here(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The rule the whole console turns on: an administrator of one guild is
    # not an administrator, they are an administrator *of that guild*.
    await grant(factory, BEN)
    admins = Admins({OTHER_GUILD: {CARL}})

    assert await directory(factory, admins=admins).holders(GUILD, requested_by=CARL) is None


async def test_a_participant_who_administers_nothing_gets_no_listing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await grant(factory, BEN)
    assert await directory(factory).holders(GUILD, requested_by=BEN) is None


async def test_a_guild_nobody_administers_answers_the_same_as_one_that_is_not_yours(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A guild the bot does not serve has no administrators, so "no such
    # guild" needs no separate check -- and must not have one, because a
    # distinct answer is an oracle for which guilds exist.
    assert await directory(factory).holders(123456, requested_by=ANNA) is None


async def test_a_revocation_by_somebody_who_does_not_administer_the_guild_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)

    assert await directory(factory).revoke(GUILD, BEN, requested_by=CARL) is None

    # The refusal is not merely a return value: nothing may have been
    # written on the way to it.
    holders = await directory(factory).holders(GUILD, requested_by=ANNA)
    assert holders is not None
    assert holders[0].revoked_at is None


# ---------------------------------------------------------------------------
# Which row is the current one
# ---------------------------------------------------------------------------


async def test_the_newest_grant_is_the_one_reported(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Somebody who revoked and consented again reads as consenting.

    The same selection `ConsentRepository.current` makes, and it has to
    be: an administrator shown an older row would be shown a decision
    nothing enforces.
    """
    await set_policy(factory)
    await grant(
        factory, BEN, granted_at=T0 - timedelta(days=30), revoked_at=T0 - timedelta(days=20)
    )
    await grant(factory, BEN, granted_at=T0 - timedelta(days=1))

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert len(holders) == 1
    assert holders[0].revoked_at is None
    assert holders[0].active is True


async def test_a_person_appears_once_however_often_they_consented(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    for day in range(4):
        await grant(factory, BEN, granted_at=T0 - timedelta(days=day))

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert [holder.discord_user_id for holder in holders] == [BEN]


async def test_consent_in_another_guild_is_not_this_guild_s_business(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, CARL, guild_id=OTHER_GUILD)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders == ()


# ---------------------------------------------------------------------------
# Whether it is still in force
# ---------------------------------------------------------------------------


async def test_a_grant_naming_a_superseded_policy_version_is_not_active(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case an administrator would never guess from the columns.

    `revoked_at` is NULL and the consent is over, because the guild's
    policy moved on and a grant names the version it was given under.
    Deriving `active` in the browser from the two dates would have
    reported this person as consenting.
    """
    await set_policy(factory, "2026-02")
    await grant(factory, BEN, policy_version="2026-01")

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].revoked_at is None
    assert holders[0].active is False


async def test_a_guild_with_no_policy_version_has_no_active_consent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # `policy_version` is a required key with no default, so an
    # unconfigured guild is a real state -- and one where nothing may be
    # recorded at all.
    await grant(factory, BEN)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].active is False


async def test_a_withdrawn_grant_is_not_active(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN, revoked_at=T0)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].active is False


# ---------------------------------------------------------------------------
# What the row says about the person
# ---------------------------------------------------------------------------


async def test_a_name_comes_from_the_person_s_most_recent_meeting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)
    await a_recorded_session(factory, started_at=T0 - timedelta(days=9), people={BEN: "old name"})
    await a_recorded_session(factory, started_at=T0 - timedelta(days=1), people={BEN: "ben"})

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].display_name == "ben"


async def test_somebody_who_has_never_been_recorded_has_no_name_to_show(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The state a well-run guild onboards people into: they consented and
    # have not yet been in a meeting. The console shows the id and says so.
    await set_policy(factory)
    await grant(factory, BEN)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].display_name is None


async def test_a_name_is_not_borrowed_from_another_guild(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # A display name is per-guild. Borrowing one would put a nickname from
    # somewhere else next to a decision about this guild.
    await set_policy(factory)
    await grant(factory, BEN)
    await a_recorded_session(factory, guild_id=OTHER_GUILD, people={BEN: "elsewhere"})

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].display_name is None


async def test_the_recordings_still_held_are_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The number that says what revoking will *not* do.

    An administrator not shown it would reasonably assume withdrawing
    consent erases what was recorded under it.
    """
    await set_policy(factory)
    await grant(factory, BEN)
    await a_recorded_session(factory, started_at=T0 - timedelta(days=2), people={BEN: "ben"})
    await a_recorded_session(factory, started_at=T0 - timedelta(days=1), people={BEN: "ben"})

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].recordings_with_audio == 2


async def test_a_recording_the_sweep_erased_is_not_counted_as_still_held(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # `audio_deleted_at` is the only claim that an object is gone. Counting
    # stamped rows would tell an administrator that revoking leaves
    # recordings behind which were erased weeks ago.
    await set_policy(factory)
    await grant(factory, BEN)
    await a_recorded_session(factory, people={BEN: "ben"}, audio_deleted=True)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].recordings_with_audio == 0


async def test_recordings_from_another_guild_are_not_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)
    await a_recorded_session(factory, guild_id=OTHER_GUILD, people={BEN: "ben"})

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert holders[0].recordings_with_audio == 0


# ---------------------------------------------------------------------------
# Withdrawing it
# ---------------------------------------------------------------------------


async def test_a_revocation_stamps_the_newest_grant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)
    revoked_at = T0 + timedelta(hours=3)

    outcome = await directory(factory, now=revoked_at).revoke(GUILD, BEN, requested_by=ANNA)

    assert outcome is not None
    assert outcome.revoked is True
    assert outcome.effective_at == revoked_at
    holders = await directory(factory, now=revoked_at).holders(GUILD, requested_by=ANNA)
    assert holders is not None
    assert holders[0].revoked_at == revoked_at
    assert holders[0].active is False


async def test_revoking_twice_says_so_rather_than_pretending(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN, revoked_at=T0)

    outcome = await directory(factory).revoke(GUILD, BEN, requested_by=ANNA)

    assert outcome is not None
    assert outcome.revoked is False
    assert outcome.refusal == ALREADY_REVOKED


async def test_revoking_a_consent_that_was_never_given_says_so(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`record_revocation` is silent about this, and silence is a lie here.

    An administrator told "revoked" for somebody who never consented would
    believe a protection is in place that never was.
    """
    await set_policy(factory)

    outcome = await directory(factory).revoke(GUILD, CARL, requested_by=ANNA)

    assert outcome is not None
    assert outcome.revoked is False
    assert outcome.refusal == NO_CONSENT_ON_RECORD


async def test_a_grant_under_a_superseded_policy_is_still_revoked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inactive is not the same as withdrawn, and only one of them lasts.

    A grant is inactive under a newer policy version *because of a
    setting*, and a setting can be set back. Stamping `revoked_at` is the
    only thing that survives somebody restoring the old version.
    """
    await set_policy(factory, "2026-02")
    await grant(factory, BEN, policy_version="2026-01")

    outcome = await directory(factory).revoke(GUILD, BEN, requested_by=ANNA)

    assert outcome is not None
    assert outcome.revoked is True


async def test_a_revocation_does_not_reach_the_same_person_in_another_guild(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Consent is per guild, so a revocation is too. An administrator of one
    # guild ending somebody's consent in another would be the widest
    # possible reading of "administers a guild".
    await set_policy(factory)
    await set_policy(factory, guild_id=OTHER_GUILD)
    await grant(factory, BEN)
    await grant(factory, BEN, guild_id=OTHER_GUILD)

    await directory(factory).revoke(GUILD, BEN, requested_by=ANNA)

    elsewhere = await directory(factory, admins=Admins({OTHER_GUILD: {CARL}})).holders(
        OTHER_GUILD, requested_by=CARL
    )
    assert elsewhere is not None
    assert elsewhere[0].revoked_at is None
    assert elsewhere[0].active is True


async def test_a_revocation_leaves_the_earlier_grants_alone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The history keeps grants; a revocation modifies the one it revokes.

    Consent rows are kept permanently, revoked ones included, because the
    record *is* the evidence that consent was given (Spec 12.4). A
    revocation that rewrote the history would destroy the thing the table
    exists to hold.
    """
    await set_policy(factory)
    await grant(factory, BEN, granted_at=T0 - timedelta(days=30))
    await grant(factory, BEN, granted_at=T0 - timedelta(days=1))

    await directory(factory).revoke(GUILD, BEN, requested_by=ANNA)

    async with factory() as db:
        rows = (await db.execute(Consent.__table__.select())).all()
    assert len(rows) == 2
    assert sum(1 for row in rows if row.revoked_at is not None) == 1


async def consent_rows(
    factory: async_sessionmaker[AsyncSession], discord_user_id: int
) -> Sequence[Row[Any]]:
    """Every `consent` row for one person.

    Written once because half the tests below turn on *how many rows* a
    change produced: a widening inserts and a narrowing does not, and
    that difference is the whole of what an append-only history means
    here.
    """
    async with factory() as db:
        return (
            await db.execute(
                Consent.__table__.select().where(Consent.discord_user_id == discord_user_id)
            )
        ).all()


# ---------------------------------------------------------------------------
# An effective instant on the administrator's withdrawal
# ---------------------------------------------------------------------------


async def test_a_revocation_that_names_no_instant_still_means_now(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The behaviour every existing client depends on, kept intact.

    `effective_at` is optional precisely so that not sending it is not a
    client that needs updating -- a required field with a documented
    sentinel would have broken the console on the day this shipped.
    """
    await set_policy(factory)
    await grant(factory, BEN)

    outcome = await directory(factory).revoke(GUILD, BEN, requested_by=ANNA)

    assert outcome is not None
    assert outcome.effective_at == T0


async def test_a_withdrawal_dated_for_the_end_of_the_month_leaves_consent_in_force(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A scheduled revocation, and nothing new fires it.

    The stored instant is what `is_consent_active` compares against on
    every read, so the moment it passes the answer changes -- inside the
    consent cache's five seconds, with no timer, no sweep and no job.
    """
    await set_policy(factory)
    await grant(factory, BEN)
    end_of_month = T0 + timedelta(days=10)

    outcome = await directory(factory).revoke(
        GUILD, BEN, requested_by=ANNA, effective_at=end_of_month
    )

    assert outcome is not None
    assert outcome.revoked is True
    still_recording = await directory(factory).holders(GUILD, requested_by=ANNA)
    assert still_recording is not None
    assert still_recording[0].active is True
    after = await directory(factory, now=end_of_month).holders(GUILD, requested_by=ANNA)
    assert after is not None
    assert after[0].active is False


async def test_a_backdated_revocation_says_how_many_recordings_it_did_not_delete(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The number exists so the console can offer `/audio purge`, not so it
    can take it. A back-dated revocation is a statement about recordings
    that already exist; erasing them would mean an administrator
    correcting a date had destroyed months of meetings their team read.
    """
    await set_policy(factory)
    await grant(factory, BEN, granted_at=T0 - timedelta(days=30))
    await a_recorded_session(factory, started_at=T0 - timedelta(days=20), people={BEN: "ben"})
    await a_recorded_session(factory, started_at=T0 - timedelta(days=3), people={BEN: "ben"})
    await a_recorded_session(factory, started_at=T0 - timedelta(days=1), people={BEN: "ben"})

    outcome = await directory(factory).revoke(
        GUILD, BEN, requested_by=ANNA, effective_at=T0 - timedelta(days=5)
    )

    assert outcome is not None
    assert outcome.recordings_from_effective_at == 2
    holders = await directory(factory).holders(GUILD, requested_by=ANNA)
    assert holders is not None
    # And every one of them is still there.
    assert holders[0].recordings_with_audio == 3


async def test_an_instant_before_the_grant_is_refused_rather_than_clamped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """It would claim a grant ended before it began, and every later reader
    of the row would have to decide what that meant. Clamping to
    `granted_at` would store an instant nobody asked for and report
    success -- while the request itself is evidence that whoever made it
    is working from a date this system disagrees with.
    """
    await set_policy(factory)
    await grant(factory, BEN, granted_at=T0)

    outcome = await directory(factory).revoke(
        GUILD, BEN, requested_by=ANNA, effective_at=T0 - timedelta(seconds=1)
    )

    assert outcome is not None
    assert outcome.revoked is False
    assert outcome.refusal == EFFECTIVE_BEFORE_GRANT
    holders = await directory(factory).holders(GUILD, requested_by=ANNA)
    assert holders is not None
    assert holders[0].revoked_at is None


async def test_an_instant_may_be_chosen_only_by_somebody_who_administers_the_guild(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)

    outcome = await directory(factory).revoke(
        GUILD, BEN, requested_by=CARL, effective_at=T0 + timedelta(days=1)
    )

    assert outcome is None


async def test_the_roster_says_what_each_grant_covers(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A setting an administrator can switch on with no readout of who then
    used it is a setting nobody can audit.
    """
    await set_policy(factory)
    await grant(factory, BEN, scope=ConsentScope.AUDIO_VIDEO)
    await grant(factory, CARL)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)

    assert holders is not None
    assert {holder.discord_user_id: holder.scope for holder in holders} == {
        BEN: "audio_video",
        CARL: "audio",
    }


# ---------------------------------------------------------------------------
# A person's own consent
# ---------------------------------------------------------------------------


def mine(
    factory: async_sessionmaker[AsyncSession], *, now: datetime = T0
) -> ConsolePersonalConsents:
    return ConsolePersonalConsents(factory, ConfigStore(factory), lambda: now)


async def offer_video(
    factory: async_sessionmaker[AsyncSession],
    guild_id: int = GUILD,
    offered: bool = True,
) -> None:
    await ConfigStore(factory).set(
        guild_id,
        settings.VIDEO_CONSENT_OFFERED,
        settings.TRUE if offered else settings.FALSE,
        T0,
    )


async def test_a_person_sees_every_guild_they_have_a_record_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await set_policy(factory, guild_id=OTHER_GUILD)
    await grant(factory, BEN)
    await grant(factory, BEN, guild_id=OTHER_GUILD)
    await grant(factory, CARL)

    consents = await mine(factory).for_person(BEN)

    assert [consent.guild_id for consent in consents] == [GUILD, OTHER_GUILD]


async def test_a_person_sees_only_the_newest_grant_per_guild(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The row the recorder acts on, and never an older one: showing
    somebody a decision nothing enforces is worse than showing nothing.
    """
    await set_policy(factory)
    await grant(
        factory, BEN, granted_at=T0 - timedelta(days=30), revoked_at=T0 - timedelta(days=20)
    )
    await grant(factory, BEN, granted_at=T0 - timedelta(days=1))

    consents = await mine(factory).for_person(BEN)

    assert len(consents) == 1
    assert consents[0].state == STATE_ACTIVE


async def test_a_policy_bump_reads_as_its_own_state_rather_than_as_a_withdrawal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The one state nobody expects and the only one the person did not
    cause. Folded into "revoked" it would read as something they did.
    """
    await set_policy(factory, "2026-06")
    await grant(factory, BEN, policy_version="2026-01")

    consents = await mine(factory).for_person(BEN)

    assert consents[0].state == STATE_POLICY_SUPERSEDED
    assert consents[0].active is False
    assert consents[0].policy_version == "2026-01"
    assert consents[0].guild_policy_version == "2026-06"


async def test_a_scheduled_withdrawal_reads_as_scheduled_and_still_active(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN, revoked_at=T0 + timedelta(days=7))

    consents = await mine(factory).for_person(BEN)

    assert consents[0].state == STATE_SCHEDULED
    assert consents[0].active is True


async def test_a_withdrawal_that_has_passed_reads_as_revoked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN, revoked_at=T0 - timedelta(days=1))

    consents = await mine(factory).for_person(BEN)

    assert consents[0].state == STATE_REVOKED
    assert consents[0].active is False


async def test_a_person_is_told_whether_their_guild_offers_video_at_all(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """So the console can leave the control out entirely rather than
    rendering one the API will refuse.
    """
    await set_policy(factory)
    await grant(factory, BEN)
    assert (await mine(factory).for_person(BEN))[0].video_consent_offered is False

    await offer_video(factory)
    assert (await mine(factory).for_person(BEN))[0].video_consent_offered is True


async def test_widening_a_scope_is_refused_while_the_guild_does_not_offer_video(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Refused, not silently narrowed. Software cannot read the document at
    `policy_url`, so it must not pretend to have checked it -- and a
    success answering a question the person did not ask has told them
    something false about their own consent.
    """
    await set_policy(factory)
    await grant(factory, BEN)

    outcome = await mine(factory).set_scope(BEN, GUILD, "audio_video")

    assert outcome.changed is False
    assert outcome.refusal == VIDEO_CONSENT_NOT_OFFERED
    assert (await mine(factory).for_person(BEN))[0].scope == "audio"


async def test_widening_a_scope_inserts_a_new_grant_under_the_current_policy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The heart of the change. `consent` is an append-only history and a
    widening is a *grant*, so it gets a row of its own carrying the policy
    version in force when it was given. Overwriting the scope in place
    would leave a record claiming video consent under a document written
    before video was a question.
    """
    await set_policy(factory, "2026-06")
    await offer_video(factory)
    await grant(factory, BEN, granted_at=T0 - timedelta(days=5), policy_version="2026-06")
    later = T0 + timedelta(days=1)

    outcome = await mine(factory, now=later).set_scope(BEN, GUILD, "audio_video")

    assert outcome.changed is True
    assert outcome.policy_version == "2026-06"
    rows = await consent_rows(factory, BEN)
    assert len(rows) == 2
    record = (await mine(factory, now=later).for_person(BEN))[0]
    assert record.scope == "audio_video"
    assert record.granted_at == later


async def test_narrowing_a_scope_modifies_the_grant_rather_than_adding_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody needs permission to consent to less, and withdrawing part of
    a grant modifies the grant -- exactly as withdrawing all of it does.
    """
    await set_policy(factory)
    await grant(factory, BEN, scope=ConsentScope.AUDIO_VIDEO)

    outcome = await mine(factory).set_scope(BEN, GUILD, "audio")

    assert outcome.changed is True
    rows = await consent_rows(factory, BEN)
    assert len(rows) == 1
    assert (await mine(factory).for_person(BEN))[0].scope == "audio"


async def test_narrowing_needs_nothing_from_the_guild_that_widening_needs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A guild that switched `video_consent_offered` back off must not trap
    the people who consented while it was on.
    """
    await set_policy(factory)
    await offer_video(factory, offered=False)
    await grant(factory, BEN, scope=ConsentScope.AUDIO_VIDEO)

    assert (await mine(factory).set_scope(BEN, GUILD, "audio")).changed is True


async def test_asking_for_the_scope_you_already_have_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A page that was open a while, not a decision. A new grant row here
    would stamp a fresh `granted_at` on something nobody did.
    """
    await set_policy(factory)
    await offer_video(factory)
    await grant(factory, BEN, scope=ConsentScope.AUDIO_VIDEO)

    outcome = await mine(factory).set_scope(BEN, GUILD, "audio_video")

    assert outcome.changed is False
    assert outcome.refusal is None
    rows = await consent_rows(factory, BEN)
    assert len(rows) == 1


async def test_a_guild_with_no_policy_version_has_nothing_for_a_new_grant_to_name(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await offer_video(factory)
    await grant(factory, BEN)

    outcome = await mine(factory).set_scope(BEN, GUILD, "audio_video")

    assert outcome.refusal == NO_POLICY_VERSION


async def test_a_scope_cannot_be_changed_on_a_consent_that_has_ended(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Somebody whose consent has ended consents again, in Discord, under
    the policy in force then -- they do not edit a record that already says
    it stopped.
    """
    await set_policy(factory)
    await offer_video(factory)
    await grant(factory, BEN, revoked_at=T0 - timedelta(days=1))

    outcome = await mine(factory).set_scope(BEN, GUILD, "audio_video")

    assert outcome.refusal == ALREADY_REVOKED


async def test_a_person_with_no_record_in_that_guild_is_told_so(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)

    assert (await mine(factory).set_scope(BEN, GUILD, "audio")).refusal == NO_CONSENT_ON_RECORD
    assert (await mine(factory).revoke_own(BEN, GUILD)).refusal == NO_CONSENT_ON_RECORD


async def test_a_person_may_withdraw_their_own_consent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await set_policy(factory)
    await grant(factory, BEN)

    outcome = await mine(factory).revoke_own(BEN, GUILD)

    assert outcome.revoked is True
    assert outcome.effective_at == T0
    assert (await mine(factory).for_person(BEN))[0].state == STATE_REVOKED


async def test_a_person_withdrawing_their_own_consent_deletes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Withdrawing consent is a decision about the future. Erasing what was
    recorded under the consent that existed at the time is `/audio purge`,
    and it is deliberately a separate act.
    """
    await set_policy(factory)
    await grant(factory, BEN, granted_at=T0 - timedelta(days=10))
    await a_recorded_session(factory, started_at=T0 - timedelta(days=2), people={BEN: "ben"})

    await mine(factory).revoke_own(BEN, GUILD)

    holders = await directory(factory).holders(GUILD, requested_by=ANNA)
    assert holders is not None
    assert holders[0].recordings_with_audio == 1
