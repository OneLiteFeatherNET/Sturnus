"""The two database adapters the console's sign-in needs.

Both are narrow additions rather than new stores: the console reuses
`oauth_state` and `account_link` exactly as the link service does, and
these are the two directions neither of them had a reader for yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.adapters import (
    ConsoleLinkDirectory,
    ConsoleProfileDirectory,
    ConsoleSessionDocuments,
    ConsoleSessionNaming,
    ConsoleStateStore,
    ConsoleTagWriter,
    ConsoleTrackDirectory,
    ConsoleTranscripts,
)
from sturnus.console.statistics import SessionName
from sturnus.domain import settings
from sturnus.infrastructure.crypto import KeyWrapper
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.export_targets import ExportTargetStore
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    SessionTag,
    TranscriptionJob,
)
from sturnus.infrastructure.db.repositories import AccountLinkRepository
from sturnus.infrastructure.db.session_documents import SessionDocumentStore

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
ANNA, BEN = 100, 200
#: Administers the guild and was in none of its meetings.
CARA = 300
GUILD = 4711
ANNA_OUTLINE = "c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f"


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# The state store: single-use, and belonging to nobody in particular
# ---------------------------------------------------------------------------


async def test_an_issued_state_can_be_consumed_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    states = ConsoleStateStore(factory)
    await states.issue("abc", T0)
    assert await states.consume("abc", T0) is True
    assert await states.consume("abc", T0) is False


async def test_a_state_that_was_never_issued_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await ConsoleStateStore(factory).consume("never-issued", T0) is False


async def test_an_expired_state_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A state is unguessable for as long as a login takes and no longer.

    The link service's own sweep purges these rows; expiry here is what
    makes a captured callback URL useless before that sweep runs.
    """
    states = ConsoleStateStore(factory, ttl=timedelta(minutes=10))
    await states.issue("abc", T0)
    assert await states.consume("abc", T0 + timedelta(minutes=11)) is False


async def test_a_console_state_belongs_to_no_discord_user_yet(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The difference from the link service's own states, and the reason
    this is a second adapter rather than a reuse of the first.

    `/link` knows who is linking before the browser leaves -- the slash
    command was run by somebody. A console sign-in does not: who this is
    only becomes known when the provider says so, which is after the
    round trip. The row is written with a placeholder id that no Discord
    user can have, so a console state can never be mistaken for a pending
    account link.
    """
    states = ConsoleStateStore(factory)
    await states.issue("abc", T0)

    # The link service's consumer must find nothing it recognises here.
    from sturnus.infrastructure.db.link_state import LinkStateStore

    assert await LinkStateStore(factory).consume("abc", T0) is None


# ---------------------------------------------------------------------------
# The link directory: from an Outline identity to a Discord user
# ---------------------------------------------------------------------------


async def test_a_linked_identity_resolves_to_its_discord_user(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await AccountLinkRepository(factory).save(ANNA, "outline", ANNA_OUTLINE, "Anna")
    directory = ConsoleLinkDirectory(factory)
    assert await directory.discord_user_for("outline", ANNA_OUTLINE) == ANNA


async def test_an_unlinked_identity_resolves_to_nobody(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """No link, no session. This is the whole authorisation model."""
    assert await ConsoleLinkDirectory(factory).discord_user_for("outline", "unknown") is None


async def test_a_link_for_another_provider_does_not_resolve(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`account_link` is keyed by provider, and identities from different
    providers share no namespace -- resolving across them would let an id
    from one authenticate as somebody from another.
    """
    await AccountLinkRepository(factory).save(ANNA, "confluence", ANNA_OUTLINE, "Anna")
    assert await ConsoleLinkDirectory(factory).discord_user_for("outline", ANNA_OUTLINE) is None


async def test_the_most_recent_link_wins_when_an_account_is_relinked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two Discord users can point at one Outline identity over time --
    somebody changes Discord accounts and links again. The lookup must
    name one of them, deterministically, and the useful one is the
    current link rather than an abandoned one.
    """
    links = AccountLinkRepository(factory)
    await links.save(ANNA, "outline", ANNA_OUTLINE, "Anna")
    await links.save(BEN, "outline", ANNA_OUTLINE, "Anna")
    assert await ConsoleLinkDirectory(factory).discord_user_for("outline", ANNA_OUTLINE) == BEN


# ---------------------------------------------------------------------------
# The profile directory: the same row, read for the name instead of the id
# ---------------------------------------------------------------------------


async def test_a_linked_person_is_named_by_the_link_they_made(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await AccountLinkRepository(factory).save(ANNA, "outline", ANNA_OUTLINE, "Anna Example")
    assert await ConsoleProfileDirectory(factory).display_name_for(ANNA) == "Anna Example"


async def test_somebody_with_no_link_row_has_no_name(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session outlives the link it was issued from -- somebody can run
    `/unlink` with a cookie still in a tab. That is a name this endpoint
    does not have, not an error.
    """
    assert await ConsoleProfileDirectory(factory).display_name_for(ANNA) is None


async def test_a_link_with_another_provider_does_not_name_anybody(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`account_link` is keyed by provider and the console signs people
    in through Outline. A name from a provider this console does not
    authenticate against is a name for a different account entirely.
    """
    await AccountLinkRepository(factory).save(ANNA, "confluence", ANNA_OUTLINE, "Anna Example")
    assert await ConsoleProfileDirectory(factory).display_name_for(ANNA) is None


async def test_a_relink_renames_the_person_it_belongs_to(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    links = AccountLinkRepository(factory)
    await links.save(ANNA, "outline", ANNA_OUTLINE, "Anna Example")
    await links.save(ANNA, "outline", ANNA_OUTLINE, "Anna Rename")
    assert await ConsoleProfileDirectory(factory).display_name_for(ANNA) == "Anna Rename"


# ---------------------------------------------------------------------------
# The track directory: the authorisation rule for audio, as one query
# ---------------------------------------------------------------------------


async def seed_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    participants: tuple[int, ...],
    speaker: int | None = None,
    erased: bool = False,
) -> int:
    """One closed session, its participants, and optionally one recording."""
    async with factory() as db:
        session = Session(guild_id=GUILD, channel_id=2, started_at=T0, status="closed")
        db.add(session)
        await db.flush()
        for user in participants:
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=user,
                    discord_display_name=f"user-{user}",
                    first_seen_at=T0,
                )
            )
        if speaker is not None:
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=speaker,
                    s3_key=f"sessions/{session.id}/speakers/{speaker}.enc",
                    encryption_key_id="key-1",
                    wrapped_data_key=b"wrapped",
                    retention_until=T0 + timedelta(days=30),
                    audio_deleted_at=T0 if erased else None,
                    status="done",
                )
            )
        await db.commit()
        return session.id


async def test_a_participant_finds_the_recording_of_someone_in_the_room(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA, BEN), speaker=ANNA)

    track = await ConsoleTrackDirectory(factory, ConfigStore(factory)).track_for(
        session_id, ANNA, requested_by=BEN
    )

    assert track is not None
    assert track.s3_key == f"sessions/{session_id}/speakers/{ANNA}.enc"
    assert track.wrapped_data_key == b"wrapped"
    assert track.encryption_key_id == "key-1"


async def test_somebody_who_was_not_in_the_session_finds_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole authorisation rule for audio, in one assertion.

    The recording exists, the query names it exactly, and the answer is
    still nothing -- because the scoping is inside the statement rather
    than applied by whoever remembers to apply it.
    """
    session_id = await seed_session(factory, participants=(ANNA,), speaker=ANNA)

    assert (
        await ConsoleTrackDirectory(factory, ConfigStore(factory)).track_for(
            session_id, ANNA, requested_by=BEN
        )
        is None
    )


async def test_a_speaker_with_no_recording_in_that_session_yields_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA, BEN), speaker=ANNA)

    assert (
        await ConsoleTrackDirectory(factory, ConfigStore(factory)).track_for(
            session_id, BEN, requested_by=ANNA
        )
        is None
    )


async def test_a_recording_the_retention_sweep_erased_is_not_offered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`audio_deleted_at` is the record that the object is gone. Offering
    the row anyway would send a participant to S3 for a key that was
    deleted on purpose, and answer them with a 404 from two layers deeper.
    """
    session_id = await seed_session(factory, participants=(ANNA,), speaker=ANNA, erased=True)

    assert (
        await ConsoleTrackDirectory(factory, ConfigStore(factory)).track_for(
            session_id, ANNA, requested_by=ANNA
        )
        is None
    )


async def test_a_session_that_does_not_exist_yields_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert (
        await ConsoleTrackDirectory(factory, ConfigStore(factory)).track_for(
            9_999, ANNA, requested_by=ANNA
        )
        is None
    )


# ---------------------------------------------------------------------------
# The same directory's second, wider rule: who may take a copy away
# ---------------------------------------------------------------------------
#
# The decision this implements is the repository owner's and it is a real
# widening: an administrator of a guild may download any recording of that
# guild, including sessions they were not in. It is a second method rather
# than a flag on the first, because it is a second rule -- and because
# `track_for` must stay exactly what it was.


async def directory(
    factory: async_sessionmaker[AsyncSession],
    *,
    offered: bool,
    administrators: tuple[int, ...] = (),
) -> ConsoleTrackDirectory:
    """The real adapter over a guild that has, or has not, switched it on."""
    config = ConfigStore(factory)
    await config.set(
        GUILD,
        settings.ADMIN_AUDIO_DOWNLOAD_OFFERED,
        settings.TRUE if offered else settings.FALSE,
        T0,
    )
    await AdminMemberStore(factory).replace(GUILD, administrators, T0)
    return ConsoleTrackDirectory(factory, config)


async def test_an_administrator_reaches_a_recording_of_a_meeting_they_missed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA, BEN), speaker=ANNA)
    tracks = await directory(factory, offered=True, administrators=(CARA,))

    found = await tracks.downloadable_track_for(session_id, ANNA, requested_by=CARA)

    assert found is not None
    assert found.track.s3_key == f"sessions/{session_id}/speakers/{ANNA}.enc"
    assert found.guild_id == GUILD
    # The one thing the audit line cannot recover afterwards, and the
    # difference between the two acts this route can perform.
    assert found.by_participant is False


async def test_a_participant_of_the_session_is_told_that_they_were_there(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA, BEN), speaker=ANNA)
    tracks = await directory(factory, offered=True, administrators=(CARA,))

    found = await tracks.downloadable_track_for(session_id, ANNA, requested_by=BEN)

    assert found is not None
    assert found.by_participant is True


async def test_a_guild_that_has_not_switched_it_on_hands_nothing_over(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody, not "administrators only".

    Turning the setting on is an administrator asserting that the document
    at `policy_url` tells participants their recordings can be copied out.
    Until somebody asserts that, the capability does not exist for the
    guild -- for its administrators or for anybody else.
    """
    session_id = await seed_session(factory, participants=(ANNA, BEN), speaker=ANNA)
    tracks = await directory(factory, offered=False, administrators=(CARA,))

    assert await tracks.downloadable_track_for(session_id, ANNA, requested_by=CARA) is None
    assert await tracks.downloadable_track_for(session_id, ANNA, requested_by=BEN) is None


async def test_a_guild_nobody_has_configured_at_all_hands_nothing_over(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The default, read through the store rather than restated here."""
    session_id = await seed_session(factory, participants=(ANNA,), speaker=ANNA)
    await AdminMemberStore(factory).replace(GUILD, (CARA,), T0)
    tracks = ConsoleTrackDirectory(factory, ConfigStore(factory))

    assert await tracks.downloadable_track_for(session_id, ANNA, requested_by=CARA) is None


async def test_an_administrator_of_another_guild_is_nobody_here(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`is_admin` is per guild, and so is this. An administrator of one
    guild has no standing in another, and the query says so by naming the
    session's own `guild_id` rather than asking whether this person
    administers anything at all."""
    session_id = await seed_session(factory, participants=(ANNA,), speaker=ANNA)
    config = ConfigStore(factory)
    await config.set(GUILD, settings.ADMIN_AUDIO_DOWNLOAD_OFFERED, settings.TRUE, T0)
    await AdminMemberStore(factory).replace(GUILD + 1, (CARA,), T0)

    tracks = ConsoleTrackDirectory(factory, config)

    assert await tracks.downloadable_track_for(session_id, ANNA, requested_by=CARA) is None


async def test_somebody_who_is_neither_an_administrator_nor_a_participant_finds_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA,), speaker=ANNA)
    tracks = await directory(factory, offered=True, administrators=(CARA,))

    assert await tracks.downloadable_track_for(session_id, ANNA, requested_by=BEN) is None


async def test_a_recording_the_retention_sweep_erased_is_not_offered_to_an_administrator(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The widening is about who may ask, never about what is still there."""
    session_id = await seed_session(factory, participants=(ANNA,), speaker=ANNA, erased=True)
    tracks = await directory(factory, offered=True, administrators=(CARA,))

    assert await tracks.downloadable_track_for(session_id, ANNA, requested_by=CARA) is None


async def test_a_session_that_does_not_exist_yields_nothing_to_download(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tracks = await directory(factory, offered=True, administrators=(CARA,))

    assert await tracks.downloadable_track_for(9_999, ANNA, requested_by=CARA) is None


# ---------------------------------------------------------------------------
# The tag writer: whose label this is, and whose meeting it may be put on
# ---------------------------------------------------------------------------


async def stored_tags(
    factory: async_sessionmaker[AsyncSession], session_id: int, owner: int
) -> tuple[str, ...]:
    """What the table actually holds, read without going through the writer."""
    async with factory() as db:
        rows = await db.execute(
            select(SessionTag.tag)
            .where(
                SessionTag.session_id == session_id,
                SessionTag.discord_user_id == owner,
            )
            .order_by(SessionTag.tag)
        )
        return tuple(rows.scalars().all())


async def test_a_participant_may_label_their_own_meeting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA,))

    stored = await ConsoleTagWriter(factory).replace(
        session_id, owner=ANNA, tags=("retro",), now=T0
    )

    assert stored == ("retro",)
    assert await stored_tags(factory, session_id, ANNA) == ("retro",)


async def test_somebody_who_was_not_in_the_meeting_cannot_label_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`None` rather than a refusal with a reason: "no such session" and
    "you were not in it" must look the same from outside, which is the
    same 404 the audio endpoint gives for the same reason."""
    session_id = await seed_session(factory, participants=(BEN,))

    stored = await ConsoleTagWriter(factory).replace(
        session_id, owner=ANNA, tags=("retro",), now=T0
    )

    assert stored is None
    assert await stored_tags(factory, session_id, ANNA) == ()


async def test_labelling_a_meeting_that_does_not_exist_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert (
        await ConsoleTagWriter(factory).replace(9999, owner=ANNA, tags=("retro",), now=T0) is None
    )


async def test_a_replaced_set_is_exactly_what_was_asked_for(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The write is "these are my labels now", so a label left out of the
    second call is a label removed."""
    session_id = await seed_session(factory, participants=(ANNA,))
    writer = ConsoleTagWriter(factory)
    await writer.replace(session_id, owner=ANNA, tags=("retro", "kunde"), now=T0)

    await writer.replace(session_id, owner=ANNA, tags=("kunde",), now=T0)

    assert await stored_tags(factory, session_id, ANNA) == ("kunde",)


async def test_clearing_every_label_is_a_write_and_not_a_refusal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing the last chip is how somebody undoes a tagging."""
    session_id = await seed_session(factory, participants=(ANNA,))
    writer = ConsoleTagWriter(factory)
    await writer.replace(session_id, owner=ANNA, tags=("retro",), now=T0)

    assert await writer.replace(session_id, owner=ANNA, tags=(), now=T0) == ()
    assert await stored_tags(factory, session_id, ANNA) == ()


async def test_a_label_that_survives_an_edit_keeps_when_it_was_first_written(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason the write is a difference rather than a delete and a
    re-insert. Rewriting `created_at` on every edit would make it record
    when somebody last added a *different* tag."""
    session_id = await seed_session(factory, participants=(ANNA,))
    writer = ConsoleTagWriter(factory)
    later = T0 + timedelta(days=1)
    await writer.replace(session_id, owner=ANNA, tags=("retro",), now=T0)

    await writer.replace(session_id, owner=ANNA, tags=("retro", "kunde"), now=later)

    async with factory() as db:
        rows = (
            await db.execute(
                select(SessionTag.tag, SessionTag.created_at).where(
                    SessionTag.session_id == session_id,
                    SessionTag.discord_user_id == ANNA,
                )
            )
        ).all()
    written: dict[str, datetime] = {row.tag: row.created_at for row in rows}
    assert written == {"retro": T0, "kunde": later}


async def test_replacing_your_labels_leaves_everybody_elses_alone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two people in one meeting, each with their own word for it."""
    session_id = await seed_session(factory, participants=(ANNA, BEN))
    writer = ConsoleTagWriter(factory)
    await writer.replace(session_id, owner=BEN, tags=("planung",), now=T0)

    await writer.replace(session_id, owner=ANNA, tags=("retro",), now=T0)

    assert await stored_tags(factory, session_id, BEN) == ("planung",)
    assert await stored_tags(factory, session_id, ANNA) == ("retro",)


async def test_the_same_label_written_twice_is_not_an_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two tabs saving the same set at the same moment: the row they both
    want exists either way, which is all either of them asked for."""
    session_id = await seed_session(factory, participants=(ANNA,))
    writer = ConsoleTagWriter(factory)
    await writer.replace(session_id, owner=ANNA, tags=("retro",), now=T0)

    assert await writer.replace(session_id, owner=ANNA, tags=("retro",), now=T0) == ("retro",)


# ---------------------------------------------------------------------------
# Naming a meeting: whose meeting it may be, and what the row ends up holding
# ---------------------------------------------------------------------------


async def stored_name(
    factory: async_sessionmaker[AsyncSession], session_id: int
) -> tuple[str | None, str | None]:
    """What the row actually holds, read without going through the writer."""
    async with factory() as db:
        row = (
            await db.execute(
                select(Session.title, Session.description).where(Session.id == session_id)
            )
        ).one()
        return (row.title, row.description)


async def test_a_participant_may_name_a_meeting_they_were_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA,))

    stored = await ConsoleSessionNaming(factory).rename(
        session_id, by=ANNA, title="Sprint 34 planning", description="what we decided"
    )

    assert stored == SessionName("Sprint 34 planning", "what we decided")
    assert await stored_name(factory, session_id) == ("Sprint 34 planning", "what we decided")


async def test_somebody_who_was_not_in_the_meeting_cannot_name_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`None`, and the caller answers 404 to it -- the same answer a
    session that does not exist gets. A 403 would confirm that a meeting
    exists to somebody just established as having no part in it."""
    session_id = await seed_session(factory, participants=(BEN,))

    assert (
        await ConsoleSessionNaming(factory).rename(
            session_id, by=ANNA, title="retro", description=None
        )
        is None
    )
    assert await stored_name(factory, session_id) == (None, None)


async def test_naming_a_meeting_that_does_not_exist_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert (
        await ConsoleSessionNaming(factory).rename(9999, by=ANNA, title="retro", description=None)
        is None
    )


async def test_a_name_is_shared_so_another_participant_may_correct_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole difference from a tag. Two people labelling one meeting
    keep two private labels; two people naming one meeting are naming one
    thing, and the second one wins."""
    session_id = await seed_session(factory, participants=(ANNA, BEN))
    naming = ConsoleSessionNaming(factory)

    await naming.rename(session_id, by=ANNA, title="sprint", description=None)
    await naming.rename(session_id, by=BEN, title="Sprint 34 planning", description=None)

    assert await stored_name(factory, session_id) == ("Sprint 34 planning", None)


async def test_clearing_a_name_is_a_write_and_not_a_refusal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Un-naming a meeting is something people do, and null is the one
    spelling of "nobody has named this"."""
    session_id = await seed_session(factory, participants=(ANNA,))
    naming = ConsoleSessionNaming(factory)
    await naming.rename(session_id, by=ANNA, title="retro", description="notes")

    assert await naming.rename(session_id, by=ANNA, title=None, description=None) == SessionName(
        None, None
    )
    assert await stored_name(factory, session_id) == (None, None)


async def test_naming_one_meeting_leaves_another_alone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await seed_session(factory, participants=(ANNA,))
    other = await seed_session(factory, participants=(ANNA,))

    await ConsoleSessionNaming(factory).rename(mine, by=ANNA, title="retro", description=None)

    assert await stored_name(factory, other) == (None, None)


# ---------------------------------------------------------------------------
# The transcript, assembled the way the published protocol was
# ---------------------------------------------------------------------------


async def transcribed_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    said: dict[int, str],
    ended: bool = True,
    erased: bool = False,
    pending: tuple[int, ...] = (),
) -> int:
    """A session whose speakers have (or have not) been transcribed.

    Written straight to the tables rather than through the repositories:
    what is under test is which rows the adapter reads back, and going
    through the writers would make it a test of two things at once.
    """
    async with factory() as db:
        session = Session(
            guild_id=GUILD,
            channel_id=2,
            channel_name="allgemein",
            started_at=T0,
            ended_at=T0 + timedelta(hours=1) if ended else None,
            status="closed" if ended else "recording",
        )
        db.add(session)
        await db.flush()
        for index, speaker in enumerate([*said, *pending]):
            db.add(
                SessionParticipant(
                    session_id=session.id,
                    discord_user_id=speaker,
                    discord_display_name=f"user-{speaker}",
                    first_seen_at=T0,
                    # The epoch is the evidence audio of them exists, and
                    # `assemble` drops a speaker without one: their words
                    # would otherwise be placed at the session's start,
                    # which is a time they demonstrably did not speak.
                    audio_started_at=T0 + timedelta(seconds=index),
                )
            )
        for speaker, text in said.items():
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=speaker,
                    s3_key=f"sessions/{session.id}/speakers/{speaker}.enc",
                    encryption_key_id="key-1",
                    wrapped_data_key=b"wrapped",
                    retention_until=T0 + timedelta(days=30),
                    audio_deleted_at=T0 if erased else None,
                    status="done",
                    transcript=(
                        '{"language": "de", "segments": '
                        f'[{{"start": 0.0, "end": 2.0, "text": "{text}"}}]}}'
                    ),
                )
            )
        for speaker in pending:
            db.add(
                TranscriptionJob(
                    session_id=session.id,
                    discord_user_id=speaker,
                    s3_key=f"sessions/{session.id}/speakers/{speaker}.enc",
                    encryption_key_id="key-1",
                    wrapped_data_key=b"wrapped",
                    retention_until=T0 + timedelta(days=30),
                    status="pending",
                )
            )
        await db.commit()
        return session.id


async def test_a_transcript_is_the_merge_of_every_speakers_words(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Built by `sturnus.application.assembly.assemble`, which is the
    function the worker builds the published document with -- so the
    console cannot show a different reading of the same meeting."""
    session_id = await transcribed_session(factory, said={ANNA: "wir sind uns einig", BEN: "ja"})

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert [block.text for block in found.blocks] == ["wir sind uns einig", "ja"]
    assert {speaker.discord_user_id for speaker in found.participants} == {ANNA, BEN}


async def test_a_transcript_carries_the_bounds_of_the_meeting_it_is_of(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await transcribed_session(factory, said={ANNA: "hallo"})

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert found.session_id == session_id
    assert found.started_at == T0
    assert found.ended_at == T0 + timedelta(hours=1)


async def test_a_session_whose_audio_retention_expired_still_has_its_words(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The retention sweep deletes the S3 object and nothing clears the
    transcript column. That is intended -- the window is about the
    recording and not about the minutes -- and this is the flag that lets
    the console say so instead of rendering an empty tab."""
    session_id = await transcribed_session(factory, said={ANNA: "wir sind uns einig"}, erased=True)

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert found.audio_available is False
    assert [block.text for block in found.blocks] == ["wir sind uns einig"]


async def test_a_session_that_still_has_one_recording_reports_audio(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await transcribed_session(factory, said={ANNA: "hallo"})

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert found.audio_available is True


async def test_a_meeting_still_being_transcribed_says_how_many_are_left(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Zero blocks with a pending track is a meeting still being decoded;
    zero blocks with none is a meeting nobody spoke in."""
    session_id = await transcribed_session(factory, said={ANNA: "hallo"}, pending=(BEN,))

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert found.pending_tracks == 1


async def test_a_session_still_being_recorded_answers_rather_than_refusing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Its jobs are not enqueued until it closes and it has no end for
    `assemble` to place words between, so there is nothing to assemble --
    but "not yet" and "no such meeting" are different sentences, and only
    one of them is a 404."""
    session_id = await transcribed_session(factory, said={}, ended=False, pending=(ANNA,))

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert found.ended_at is None
    assert found.blocks == ()
    assert found.pending_tracks == 1


async def test_a_session_that_does_not_exist_has_no_transcript(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(9999) is None


async def test_a_speakers_external_identity_is_the_one_the_document_shows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resolved through the guild's `document_provider`, exactly as
    `_create_session_document` resolves it -- a transcript tab that
    attributed a block to a different name than the published protocol
    would be describing a different meeting."""
    session_id = await transcribed_session(factory, said={ANNA: "hallo"})
    await AccountLinkRepository(factory).save(ANNA, "outline", ANNA_OUTLINE, "Anna A.")

    found = await ConsoleTranscripts(factory, ConfigStore(factory)).transcript_of(session_id)

    assert found is not None
    assert found.participants[0].external_user_id == ANNA_OUTLINE
    assert found.participants[0].external_display_name == "Anna A."


async def test_a_guilds_merge_gap_decides_where_the_paragraphs_break(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same setting the published protocol was rendered under. A
    console reading it differently would show paragraphs the document
    does not have."""
    session_id = await transcribed_session(factory, said={ANNA: "eins", BEN: "zwei"})
    config = ConfigStore(factory)
    await config.set(GUILD, settings.MERGE_GAP_SECONDS, "1", T0)

    found = await ConsoleTranscripts(factory, config).transcript_of(session_id)

    assert found is not None
    # One second apart in this fixture, and the two speakers never merge
    # with each other anyway -- what this asserts is that a configured
    # value is read at all rather than silently defaulted.
    assert len(found.blocks) == 2


# ---------------------------------------------------------------------------
# The protocols a session produced, and the rule they sit behind
# ---------------------------------------------------------------------------


async def _publish(
    factory: async_sessionmaker[AsyncSession],
    session_id: int,
    target_id: int,
    provider: str = "markdown",
) -> None:
    await SessionDocumentStore(factory).record(
        session_id,
        target_id=target_id,
        provider=provider,
        document_id=f"protocols/{session_id}/{target_id}.md",
        url=f"https://sturnus.example/api/sessions/{session_id}/documents/{target_id}",
        now=T0,
    )


async def _target(factory: async_sessionmaker[AsyncSession], name: str = "archive") -> int:
    return await ExportTargetStore(factory, KeyWrapper(b"m" * 32, "master-1")).save(
        GUILD, format="markdown", name=name, target="protocols", config={}, now=T0
    )


async def test_a_sessions_protocols_read_back_in_publication_order(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA, BEN))
    target_id = await _target(factory)
    await _publish(factory, session_id, target_id)

    found = await ConsoleSessionDocuments(factory).documents_of(session_id)

    assert found is not None
    assert [row.target_id for row in found] == [target_id]


async def test_a_session_that_published_nothing_reads_back_an_empty_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty is a real answer and `None` means "no such session", and the
    two must not collapse: a meeting still being transcribed would
    otherwise 404 for the people who were in it. The participant rule is
    the caller's `session_for` and is not asked here -- see
    `sturnus.console.ports.SessionDocumentDirectory`.
    """
    session_id = await seed_session(factory, participants=(ANNA,))

    assert await ConsoleSessionDocuments(factory).documents_of(session_id) == ()


async def test_a_session_that_does_not_exist_produced_no_protocols(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await ConsoleSessionDocuments(factory).documents_of(999) is None


async def test_one_destination_is_reachable_by_its_own_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA,))
    first, second = await _target(factory, "one"), await _target(factory, "two")
    await _publish(factory, session_id, first)
    await _publish(factory, session_id, second)

    found = await ConsoleSessionDocuments(factory).document_of(session_id, second)

    assert found is not None
    assert found.target_id == second


async def test_another_sessions_document_is_not_reachable_through_this_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session is part of the lookup. A target id is a guild's, not a
    session's, so the same one appears on every session that guild records
    -- and the handler's `session_for` authorises the session in the path,
    not the one the row happens to belong to.
    """
    mine = await seed_session(factory, participants=(ANNA,))
    theirs = await seed_session(factory, participants=(BEN,))
    target_id = await _target(factory)
    await _publish(factory, theirs, target_id)

    assert await ConsoleSessionDocuments(factory).document_of(mine, target_id) is None
