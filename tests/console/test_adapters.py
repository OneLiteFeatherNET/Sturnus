"""The two database adapters the console's sign-in needs.

Both are narrow additions rather than new stores: the console reuses
`oauth_state` and `account_link` exactly as the link service does, and
these are the two directions neither of them had a reader for yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.console.adapters import (
    ConsoleLinkDirectory,
    ConsoleStateStore,
    ConsoleTrackDirectory,
)
from sturnus.infrastructure.db.models import (
    Base,
    Session,
    SessionParticipant,
    TranscriptionJob,
)
from sturnus.infrastructure.db.repositories import AccountLinkRepository

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
ANNA, BEN = 100, 200
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
        session = Session(guild_id=1, channel_id=2, started_at=T0, status="closed")
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

    track = await ConsoleTrackDirectory(factory).track_for(session_id, ANNA, requested_by=BEN)

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
        await ConsoleTrackDirectory(factory).track_for(session_id, ANNA, requested_by=BEN) is None
    )


async def test_a_speaker_with_no_recording_in_that_session_yields_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed_session(factory, participants=(ANNA, BEN), speaker=ANNA)

    assert (
        await ConsoleTrackDirectory(factory).track_for(session_id, BEN, requested_by=ANNA) is None
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
        await ConsoleTrackDirectory(factory).track_for(session_id, ANNA, requested_by=ANNA) is None
    )


async def test_a_session_that_does_not_exist_yields_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await ConsoleTrackDirectory(factory).track_for(9_999, ANNA, requested_by=ANNA) is None
