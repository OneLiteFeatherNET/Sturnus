"""Where a guild publishes, and the secret that must not live in `guild_config`.

An export target has structure -- a base URL, a space key, a token -- and
the settings API renders every `guild_config` value straight back to the
administrator who asked for it. A Confluence token in that registry would
be a token the API hands out on request, so targets get a table and the
token gets wrapped.

The wrapping is bound to the guild it belongs to. Without that binding a
wrapped blob is just bytes: moved from one guild's row into another's it
would decrypt cleanly, and one guild would publish into another's space
with the other's credential. The test that matters most here is the one
that moves it.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.crypto import KeyWrapper
from sturnus.infrastructure.db.export_targets import ExportTargetStore
from sturnus.infrastructure.db.models import Base, GuildExportTarget

GUILD = 1
OTHER_GUILD = 2
MASTER = b"m" * 32
T0 = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def minutes(count: int) -> timedelta:
    return timedelta(minutes=count)


@pytest.fixture
async def sessions(clean_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def store(sessions: async_sessionmaker[AsyncSession]) -> ExportTargetStore:
    return ExportTargetStore(sessions, KeyWrapper(MASTER, "master-1"))


async def test_a_guild_that_configured_nothing_publishes_nowhere(
    store: ExportTargetStore,
) -> None:
    """The empty list is the answer, never a missing row to fall back from."""
    assert await store.all_for(GUILD) == ()
    assert await store.enabled_for(GUILD) == ()


async def test_a_saved_target_reads_back(store: ExportTargetStore) -> None:
    target_id = await store.save(
        GUILD,
        format="confluence",
        name="Engineering space",
        target="ENG",
        config={"base_url": "https://wiki.example"},
        now=T0,
    )
    (stored,) = await store.all_for(GUILD)
    assert stored.id == target_id
    assert stored.format == "confluence"
    assert stored.name == "Engineering space"
    assert stored.target == "ENG"
    assert stored.config == {"base_url": "https://wiki.example"}
    assert stored.enabled is True


async def test_saving_the_same_name_again_replaces_the_target(
    store: ExportTargetStore,
) -> None:
    """A name identifies a destination within a guild, so a save is a save.

    The alternative -- appending -- makes an administrator who corrected
    a typo publish twice, to the old place as well as the new one.
    """
    first = await store.save(
        GUILD, format="outline", name="Minutes", target="col-1", config={}, now=T0
    )
    second = await store.save(
        GUILD,
        format="outline",
        name="Minutes",
        target="col-2",
        config={},
        now=T0 + minutes(5),
    )
    assert first == second
    (stored,) = await store.all_for(GUILD)
    assert stored.target == "col-2"
    assert stored.updated_at == T0 + minutes(5)
    assert stored.created_at == T0


async def test_two_guilds_may_use_the_same_target_name(store: ExportTargetStore) -> None:
    """The name is unique within a guild, never across the deployment."""
    await store.save(GUILD, format="outline", name="Minutes", target="a", config={}, now=T0)
    await store.save(OTHER_GUILD, format="outline", name="Minutes", target="b", config={}, now=T0)
    assert len(await store.all_for(GUILD)) == 1
    assert len(await store.all_for(OTHER_GUILD)) == 1


async def test_a_disabled_target_is_configured_but_not_published_to(
    store: ExportTargetStore,
) -> None:
    """Disabling is not deleting: the configuration survives being switched off."""
    await store.save(
        GUILD, format="outline", name="Minutes", target="col-1", config={}, enabled=False, now=T0
    )
    (stored,) = await store.all_for(GUILD)
    assert stored.enabled is False
    assert await store.enabled_for(GUILD) == ()


async def test_a_target_is_only_readable_by_its_own_guild(store: ExportTargetStore) -> None:
    target_id = await store.save(
        GUILD, format="outline", name="Minutes", target="col-1", config={}, now=T0
    )
    assert await store.get(GUILD, target_id) is not None
    assert await store.get(OTHER_GUILD, target_id) is None


async def test_deleting_a_target_removes_it(store: ExportTargetStore) -> None:
    target_id = await store.save(
        GUILD, format="outline", name="Minutes", target="col-1", config={}, now=T0
    )
    assert await store.delete(GUILD, target_id) is True
    assert await store.all_for(GUILD) == ()
    assert await store.delete(GUILD, target_id) is False


async def test_deleting_another_guilds_target_does_nothing(store: ExportTargetStore) -> None:
    target_id = await store.save(
        GUILD, format="outline", name="Minutes", target="col-1", config={}, now=T0
    )
    assert await store.delete(OTHER_GUILD, target_id) is False
    assert len(await store.all_for(GUILD)) == 1


async def test_a_target_reads_back_without_its_secret(store: ExportTargetStore) -> None:
    """`has_secret` and nothing more.

    The read model is what the settings API renders, and it must be
    incapable of carrying the token -- not merely trusted not to.
    """
    target_id = await store.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.set_secret(GUILD, target_id, "hunter2", T0)

    (stored,) = await store.all_for(GUILD)
    assert stored.has_secret is True
    assert "hunter2" not in repr(stored)


async def test_a_secret_round_trips_for_the_guild_that_set_it(
    store: ExportTargetStore,
) -> None:
    target_id = await store.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.set_secret(GUILD, target_id, "hunter2", T0)
    assert await store.secret_for(GUILD, target_id) == "hunter2"


async def test_a_target_without_a_secret_says_so(store: ExportTargetStore) -> None:
    """A destination that needs no credential is ordinary, not an error."""
    target_id = await store.save(
        GUILD, format="markdown", name="Files", target="bucket", config={}, now=T0
    )
    (stored,) = await store.all_for(GUILD)
    assert stored.has_secret is False
    assert await store.secret_for(GUILD, target_id) is None


async def test_clearing_a_secret_leaves_the_target_configured(
    store: ExportTargetStore,
) -> None:
    target_id = await store.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.set_secret(GUILD, target_id, "hunter2", T0)
    await store.set_secret(GUILD, target_id, None, T0 + minutes(1))

    (stored,) = await store.all_for(GUILD)
    assert stored.has_secret is False
    assert stored.name == "Wiki"


async def test_saving_a_target_again_does_not_disturb_its_secret(
    store: ExportTargetStore,
) -> None:
    """The one method that writes a secret is the one method named for it.

    An edit form that never renders the token back cannot re-submit it,
    so a save that also replaced the secret would silently clear it every
    time somebody renamed a destination.
    """
    target_id = await store.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.set_secret(GUILD, target_id, "hunter2", T0)
    await store.save(
        GUILD,
        format="confluence",
        name="Wiki",
        target="OPS",
        config={},
        now=T0 + minutes(1),
    )
    assert await store.secret_for(GUILD, target_id) == "hunter2"


async def test_a_secret_moved_into_another_guilds_row_does_not_decrypt(
    store: ExportTargetStore,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The whole reason the guild id is passed as associated data.

    `KeyWrapper` unbound wraps bytes under the master key and nothing
    else, so a wrapped blob is portable: anybody who can write a row --
    a direct `UPDATE`, a restored backup, a bug in a bulk import -- could
    move one guild's credential into another guild's target and have it
    publish under that credential. Binding the wrap to the guild id turns
    that from a silent success into an authentication failure.
    """
    mine = await store.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    theirs = await store.save(
        OTHER_GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.set_secret(GUILD, mine, "hunter2", T0)

    async with sessions() as session:
        stolen = await session.scalar(
            select(GuildExportTarget.wrapped_secret).where(GuildExportTarget.id == mine)
        )
        await session.execute(
            update(GuildExportTarget)
            .where(GuildExportTarget.id == theirs)
            .values(wrapped_secret=stolen, encryption_key_id="master-1")
        )
        await session.commit()

    with pytest.raises(InvalidTag):
        await store.secret_for(OTHER_GUILD, theirs)


async def test_a_secret_wrapped_by_a_master_key_this_process_lacks_is_refused(
    store: ExportTargetStore,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A key-id mismatch is a configuration error, not a corrupt secret.

    Reported as itself rather than as an authentication-tag failure three
    layers down, for the reason `console.ports.KeyUnwrapper` gives.
    """
    target_id = await store.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await store.set_secret(GUILD, target_id, "hunter2", T0)
    async with sessions() as session:
        await session.execute(
            update(GuildExportTarget)
            .where(GuildExportTarget.id == target_id)
            .values(encryption_key_id="master-0")
        )
        await session.commit()

    with pytest.raises(ValueError, match="master-0"):
        await store.secret_for(GUILD, target_id)


async def test_targets_read_back_in_a_stable_order(store: ExportTargetStore) -> None:
    """Ordered by name, so a settings page does not reshuffle itself."""
    await store.save(GUILD, format="outline", name="Zulu", target="z", config={}, now=T0)
    await store.save(GUILD, format="outline", name="Alfa", target="a", config={}, now=T0)
    assert [target.name for target in await store.all_for(GUILD)] == ["Alfa", "Zulu"]
