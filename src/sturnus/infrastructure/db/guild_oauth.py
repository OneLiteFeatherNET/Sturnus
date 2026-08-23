"""A guild's own OAuth client, looked up by the slug in its sign-in link.

`GET /api/auth/login` takes no parameters and reads no cookie -- there is
no session yet, that is what login is for -- so it cannot choose a guild's
client from an identity it does not have. `/g/{slug}/sign-in` puts the
guild in the URL, `by_slug` resolves it before the round trip starts, and
`console_state.guild_id` carries it across so the callback can select the
same client for the code exchange.

The same two-method split `ExportTargetStore` uses, for the same reason:
`save` carries the whole registration and never the secret, and
`set_client_secret` is the only writer of one. Nothing here ever returns a
secret except `client_secret_for`, which is named so that a reader of the
call site can see it happening -- a `GET` on an OAuth configuration
returns the client id, the base URL, the redirect URI and whether a secret
is set, never the secret, not even masked-but-recoverable.

The wrap is bound to the guild and to a purpose distinct from the export
one, so that a guild's Confluence token and its OAuth client secret are
not interchangeable even within the guild that owns both.

**This store serves the console sign-in flow only.** `api` holds the
master key and `link` does not; the chart's `_helpers.tpl` actively
prevents adding it. The Discord account-link flow stays on the
environment-configured client, and that asymmetry is the architecture
rather than an oversight in it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from sturnus.domain.oauth_clients import GuildOAuthClient
from sturnus.infrastructure.crypto import KeyWrapper, secret_context
from sturnus.infrastructure.db.models import GuildOAuthClient as GuildOAuthClientRow

#: What the wrap of a client secret is bound to, alongside the guild id.
PURPOSE = "oauth-client"


class GuildOAuthClientStore:
    """Reads and writes the per-guild console sign-in clients."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        keys: KeyWrapper,
    ) -> None:
        self._session_factory = session_factory
        self._keys = keys

    async def save(
        self,
        guild_id: int,
        *,
        slug: str,
        provider: str,
        base_url: str,
        client_id: str,
        redirect_uri: str | None,
        now: datetime,
    ) -> None:
        """Registers or replaces this guild's client, secret untouched.

        A full replacement keyed on the guild, because a guild has one
        client: two would make "which one does this state select" a
        question the callback cannot answer.

        A slug already taken by another guild raises `IntegrityError`
        from the unique constraint rather than being checked first. The
        check would be a race -- two administrators claiming a slug in
        the same second -- and the constraint is not.

        `created_at` survives a re-save; only `updated_at` moves.
        """
        statement = insert(GuildOAuthClientRow).values(
            guild_id=guild_id,
            slug=slug,
            provider=provider,
            base_url=base_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[GuildOAuthClientRow.guild_id],
                    set_={
                        "slug": statement.excluded.slug,
                        "provider": statement.excluded.provider,
                        "base_url": statement.excluded.base_url,
                        "client_id": statement.excluded.client_id,
                        "redirect_uri": statement.excluded.redirect_uri,
                        "updated_at": now,
                    },
                )
            )
            await session.commit()

    async def set_client_secret(self, guild_id: int, secret: str | None, now: datetime) -> bool:
        """Wraps and stores this guild's client secret; `None` clears it.

        Answers whether there was a client to store it against, so a
        caller cannot mistake "no such guild" for "secret stored".
        """
        wrapped = (
            None
            if secret is None
            else self._keys.wrap(secret.encode(), secret_context(PURPOSE, guild_id))
        )
        async with self._session_factory() as session:
            written = await session.scalar(
                update(GuildOAuthClientRow)
                .where(GuildOAuthClientRow.guild_id == guild_id)
                .values(
                    wrapped_client_secret=wrapped,
                    encryption_key_id=None if wrapped is None else self._keys.key_id,
                    updated_at=now,
                )
                .returning(GuildOAuthClientRow.guild_id)
            )
            await session.commit()
        return written is not None

    async def client_secret_for(self, guild_id: int) -> str | None:
        """This guild's client secret, or `None` if none is stored.

        The one method that returns a secret. A row naming a master key
        this process does not hold raises `ValueError`, so a rotation
        that was not carried through is reported as the configuration
        error it is rather than as a decryption failure.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        GuildOAuthClientRow.wrapped_client_secret,
                        GuildOAuthClientRow.encryption_key_id,
                    ).where(GuildOAuthClientRow.guild_id == guild_id)
                )
            ).one_or_none()
        if row is None:
            return None
        wrapped, key_id = row
        if wrapped is None:
            return None
        if key_id != self._keys.key_id:
            raise ValueError(
                f"the OAuth client of guild {guild_id} is wrapped by master key "
                f"{key_id!r}, this process holds {self._keys.key_id!r}"
            )
        return self._keys.unwrap(wrapped, secret_context(PURPOSE, guild_id)).decode()

    async def by_slug(self, slug: str) -> GuildOAuthClient | None:
        """The client behind `/g/{slug}/sign-in`, or `None`.

        `None` for an unknown slug rather than an error, because a
        deployment that has configured no per-guild client at all is the
        ordinary case: `/sign-in` with no guild keeps working against the
        environment-configured client exactly as it did in v0.15.0.
        """
        return await self._one(GuildOAuthClientRow.slug == slug)

    async def for_guild(self, guild_id: int) -> GuildOAuthClient | None:
        """This guild's client, or `None` -- the settings page's read."""
        return await self._one(GuildOAuthClientRow.guild_id == guild_id)

    async def delete(self, guild_id: int) -> bool:
        """Removes this guild's client and frees its slug.

        Answers whether there was one, so "already gone" and "removed"
        are not the same reply to an administrator who clicked twice.
        """
        async with self._session_factory() as session:
            removed = await session.scalar(
                delete(GuildOAuthClientRow)
                .where(GuildOAuthClientRow.guild_id == guild_id)
                .returning(GuildOAuthClientRow.guild_id)
            )
            await session.commit()
        return removed is not None

    async def _one(self, criterion: ColumnElement[bool]) -> GuildOAuthClient | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(GuildOAuthClientRow).where(criterion))
        if row is None:
            return None
        return GuildOAuthClient(
            guild_id=row.guild_id,
            slug=row.slug,
            provider=row.provider,
            base_url=row.base_url,
            client_id=row.client_id,
            redirect_uri=row.redirect_uri,
            has_secret=row.wrapped_client_secret is not None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
