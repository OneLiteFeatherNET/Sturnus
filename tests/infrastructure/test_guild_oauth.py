"""A guild's own OAuth client, addressed by the slug in its sign-in link.

`GET /api/auth/login` takes no parameters and reads no cookie -- there is
no session yet, that is what login is for. `/g/{slug}/sign-in` is what
carries the guild into the round trip, so the slug is the lookup key and
has to be unique across the deployment.

The client secret is wrapped, and wrapped *to this guild*: the same
binding `guild_export_target` needs, for the same reason and against the
same move.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.crypto import KeyWrapper
from sturnus.infrastructure.db.export_targets import ExportTargetStore
from sturnus.infrastructure.db.guild_oauth import GuildOAuthClientStore
from sturnus.infrastructure.db.models import Base, GuildExportTarget, GuildOAuthClient

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
def store(sessions: async_sessionmaker[AsyncSession]) -> GuildOAuthClientStore:
    return GuildOAuthClientStore(sessions, KeyWrapper(MASTER, "master-1"))


async def save(store: GuildOAuthClientStore, guild_id: int, slug: str) -> None:
    await store.save(
        guild_id,
        slug=slug,
        provider="outline",
        base_url="https://outline.example",
        client_id="client-1",
        redirect_uri="https://console.example/callback",
        now=T0,
    )


async def test_a_deployment_that_configured_nothing_resolves_no_slug(
    store: GuildOAuthClientStore,
) -> None:
    """`/sign-in` with no guild keeps working, so an unknown slug is a `None`."""
    assert await store.by_slug("acme") is None
    assert await store.for_guild(GUILD) is None


async def test_a_saved_client_resolves_from_its_slug(store: GuildOAuthClientStore) -> None:
    await save(store, GUILD, "acme")
    client = await store.by_slug("acme")
    assert client is not None
    assert client.guild_id == GUILD
    assert client.provider == "outline"
    assert client.base_url == "https://outline.example"
    assert client.client_id == "client-1"
    assert client.redirect_uri == "https://console.example/callback"


async def test_a_guild_has_one_client_and_saving_replaces_it(
    store: GuildOAuthClientStore,
) -> None:
    """One client per guild: the row is keyed by the guild, not by the slug.

    A guild with two clients would make "which one does this state
    select" a question the callback cannot answer.
    """
    await save(store, GUILD, "acme")
    await store.save(
        GUILD,
        slug="acme-corp",
        provider="outline",
        base_url="https://wiki.example",
        client_id="client-2",
        redirect_uri=None,
        now=T0 + minutes(5),
    )
    assert await store.by_slug("acme") is None
    renamed = await store.by_slug("acme-corp")
    assert renamed is not None
    assert renamed.client_id == "client-2"
    assert renamed.redirect_uri is None
    assert renamed.created_at == T0
    assert renamed.updated_at == T0 + minutes(5)


async def test_two_guilds_cannot_share_a_slug(store: GuildOAuthClientStore) -> None:
    """The slug is a public URL segment and must name exactly one guild.

    Two guilds behind `/g/acme/sign-in` would send one of them through
    the other's identity provider.
    """
    await save(store, GUILD, "acme")
    with pytest.raises(IntegrityError):
        await save(store, OTHER_GUILD, "acme")


async def test_a_client_reads_back_without_its_secret(store: GuildOAuthClientStore) -> None:
    """Never send a secret back -- not even masked-but-recoverable (2.2)."""
    await save(store, GUILD, "acme")
    await store.set_client_secret(GUILD, "hunter2", T0)

    client = await store.by_slug("acme")
    assert client is not None
    assert client.has_secret is True
    assert "hunter2" not in repr(client)


async def test_a_secret_round_trips_for_the_guild_that_set_it(
    store: GuildOAuthClientStore,
) -> None:
    await save(store, GUILD, "acme")
    await store.set_client_secret(GUILD, "hunter2", T0)
    assert await store.client_secret_for(GUILD) == "hunter2"


async def test_a_client_registered_without_a_secret_yet_says_so(
    store: GuildOAuthClientStore,
) -> None:
    """Registering the client and supplying its secret are two steps.

    An administrator copies the id and the base URL out of one screen and
    the secret out of another, and the interface has to be able to say
    which half it is still missing.
    """
    await save(store, GUILD, "acme")
    client = await store.by_slug("acme")
    assert client is not None
    assert client.has_secret is False
    assert await store.client_secret_for(GUILD) is None


async def test_saving_a_client_again_does_not_disturb_its_secret(
    store: GuildOAuthClientStore,
) -> None:
    await save(store, GUILD, "acme")
    await store.set_client_secret(GUILD, "hunter2", T0)
    await save(store, GUILD, "acme")
    assert await store.client_secret_for(GUILD) == "hunter2"


async def test_a_secret_moved_into_another_guilds_row_does_not_decrypt(
    store: GuildOAuthClientStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The binding again, on the credential that grants sign-in itself.

    Worse here than for an export target: a client secret that decrypts
    under the wrong guild lets that guild complete a code exchange as
    somebody else's OAuth application.
    """
    await save(store, GUILD, "acme")
    await save(store, OTHER_GUILD, "other")
    await store.set_client_secret(GUILD, "hunter2", T0)

    async with sessions() as session:
        stolen = await session.scalar(
            select(GuildOAuthClient.wrapped_client_secret).where(GuildOAuthClient.guild_id == GUILD)
        )
        await session.execute(
            update(GuildOAuthClient)
            .where(GuildOAuthClient.guild_id == OTHER_GUILD)
            .values(wrapped_client_secret=stolen, encryption_key_id="master-1")
        )
        await session.commit()

    with pytest.raises(InvalidTag):
        await store.client_secret_for(OTHER_GUILD)


async def test_an_export_secret_does_not_decrypt_as_an_oauth_secret(
    store: GuildOAuthClientStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The binding names what the secret is for, not only whose it is.

    One guild holds both a Confluence token and an OAuth client secret,
    so binding to the guild id alone would leave the two interchangeable
    within that guild. They are wrapped under different contexts.
    """
    targets = ExportTargetStore(sessions, KeyWrapper(MASTER, "master-1"))
    target_id = await targets.save(
        GUILD, format="confluence", name="Wiki", target="ENG", config={}, now=T0
    )
    await targets.set_secret(GUILD, target_id, "hunter2", T0)
    await save(store, GUILD, "acme")

    async with sessions() as session:
        stolen = await session.scalar(
            select(GuildExportTarget.wrapped_secret).where(GuildExportTarget.id == target_id)
        )
        await session.execute(
            update(GuildOAuthClient)
            .where(GuildOAuthClient.guild_id == GUILD)
            .values(wrapped_client_secret=stolen, encryption_key_id="master-1")
        )
        await session.commit()

    with pytest.raises(InvalidTag):
        await store.client_secret_for(GUILD)


async def test_deleting_a_client_frees_its_slug(store: GuildOAuthClientStore) -> None:
    await save(store, GUILD, "acme")
    assert await store.delete(GUILD) is True
    assert await store.by_slug("acme") is None
    assert await store.delete(GUILD) is False
    await save(store, OTHER_GUILD, "acme")
    assert await store.by_slug("acme") is not None
