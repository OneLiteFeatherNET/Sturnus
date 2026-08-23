"""Where a guild publishes, and the credential that opens the door.

`guild_config` was the obvious home and is the wrong one twice over: the
settings API renders every value in that registry straight back to
whichever administrator asks for it, and a destination has structure a
flat text registry cannot hold. So destinations get a table, and the
token in it is wrapped.

**Two methods write, and only one of them writes a secret.** `save`
carries the whole configuration and never touches the credential;
`set_secret` is the only way a credential is written or cleared. That
split is not tidiness -- the edit form cannot render the token back,
because nothing may render the token back, so it cannot re-submit it
either. A `save` that also replaced the secret would therefore clear it
every time somebody renamed a destination.

**The wrap is bound to the guild and to the purpose**
(`sturnus.infrastructure.crypto.secret_context`). Without that binding a
wrapped blob is portable: anybody who can write a row but not decrypt one
-- a restored backup, a support script, a bulk import with a bug -- could
move one guild's token into another guild's target and have it publish
under that credential. With it the move fails to authenticate. It is not
a defence against somebody who holds the master key; nothing in this
process is.

No business logic. Which format may be configured, whether a target is
reachable, and what happens when publishing fails all belong above this.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.domain.exports import ExportTarget
from sturnus.infrastructure.crypto import KeyWrapper, secret_context
from sturnus.infrastructure.db.models import GuildExportTarget

#: What the wrap of an export credential is bound to, alongside the guild
#: id. Distinct from the OAuth purpose so that a guild's two kinds of
#: secret are not interchangeable with each other.
PURPOSE = "export-target"


class ExportTargetStore:
    """Reads and writes one guild's export destinations."""

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
        format: str,
        name: str,
        target: str,
        config: Mapping[str, Any],
        enabled: bool = True,
        now: datetime,
    ) -> int:
        """Stores this guild's destination called `name`, and returns its id.

        A full replacement of the configuration, keyed on
        `(guild_id, name)`: saving twice under one name corrects the
        destination rather than adding a second one, which is what an
        administrator who fixed a typo meant. The credential is not part
        of it -- see the module docstring.

        `created_at` survives a re-save; only `updated_at` moves.
        """
        statement = insert(GuildExportTarget).values(
            guild_id=guild_id,
            format=format,
            name=name,
            target=target,
            config=dict(config),
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            stored = await session.scalar(
                statement.on_conflict_do_update(
                    constraint="uq_export_target_name",
                    set_={
                        "format": statement.excluded.format,
                        "target": statement.excluded.target,
                        "config": statement.excluded.config,
                        "enabled": statement.excluded.enabled,
                        "updated_at": now,
                    },
                ).returning(GuildExportTarget.id)
            )
            await session.commit()
        # `RETURNING` on a conflicting upsert always yields the surviving
        # row, so this is never `None`; asserting it keeps the signature
        # honest rather than widening it to `int | None` for a case that
        # cannot happen.
        assert stored is not None
        return stored

    async def set_secret(
        self, guild_id: int, target_id: int, secret: str | None, now: datetime
    ) -> bool:
        """Wraps and stores this destination's credential; `None` clears it.

        Answers whether a target of this guild was actually written, so a
        caller cannot mistake "no such destination" for "credential
        stored". The guild is part of the `WHERE` and not merely of the
        wrap: a store that let one guild write into another's row and
        relied on the binding to make the result useless would be relying
        on the wrong thing.
        """
        wrapped = (
            None
            if secret is None
            else self._keys.wrap(secret.encode(), secret_context(PURPOSE, guild_id))
        )
        async with self._session_factory() as session:
            written = await session.scalar(
                update(GuildExportTarget)
                .where(
                    GuildExportTarget.id == target_id,
                    GuildExportTarget.guild_id == guild_id,
                )
                .values(
                    wrapped_secret=wrapped,
                    encryption_key_id=None if wrapped is None else self._keys.key_id,
                    updated_at=now,
                )
                .returning(GuildExportTarget.id)
            )
            await session.commit()
        return written is not None

    async def secret_for(self, guild_id: int, target_id: int) -> str | None:
        """This destination's credential, or `None` if it has none.

        The one method that returns a secret, named so that a reader of
        the call site can see that is what is happening. Raises
        `ValueError` when the row names a master key this process does not
        hold -- a configuration error reported as itself rather than as an
        authentication-tag failure three layers down, which is the
        argument `console.ports.KeyUnwrapper` already makes.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        GuildExportTarget.wrapped_secret,
                        GuildExportTarget.encryption_key_id,
                    ).where(
                        GuildExportTarget.id == target_id,
                        GuildExportTarget.guild_id == guild_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        wrapped, key_id = row
        if wrapped is None:
            return None
        if key_id != self._keys.key_id:
            raise ValueError(
                f"export target {target_id} is wrapped by master key {key_id!r}, "
                f"this process holds {self._keys.key_id!r}"
            )
        return self._keys.unwrap(wrapped, secret_context(PURPOSE, guild_id)).decode()

    async def get(self, guild_id: int, target_id: int) -> ExportTarget | None:
        """One destination of this guild, or `None`.

        The guild is part of the lookup rather than checked afterwards,
        so a target id belonging to somebody else reads exactly like a
        target id belonging to nobody.
        """
        async with self._session_factory() as session:
            row = await session.scalar(
                select(GuildExportTarget).where(
                    GuildExportTarget.id == target_id,
                    GuildExportTarget.guild_id == guild_id,
                )
            )
        return None if row is None else _view(row)

    async def all_for(self, guild_id: int) -> Sequence[ExportTarget]:
        """Every destination of this guild, enabled or not, ordered by name.

        By name because that is what somebody reading the settings page
        is reading; ties break on id so two page loads of an unchanged
        guild render identically.
        """
        return await self._read(guild_id, enabled_only=False)

    async def enabled_for(self, guild_id: int) -> Sequence[ExportTarget]:
        """The destinations a publish should actually write to."""
        return await self._read(guild_id, enabled_only=True)

    async def delete(self, guild_id: int, target_id: int) -> bool:
        """Removes a destination, and answers whether there was one.

        What it published survives: `session_document.target_id` is
        `ON DELETE SET NULL`, because the document still exists in the
        other system and the link to it is what somebody follows later.
        """
        async with self._session_factory() as session:
            removed = await session.scalar(
                delete(GuildExportTarget)
                .where(
                    GuildExportTarget.id == target_id,
                    GuildExportTarget.guild_id == guild_id,
                )
                .returning(GuildExportTarget.id)
            )
            await session.commit()
        return removed is not None

    async def _read(self, guild_id: int, *, enabled_only: bool) -> Sequence[ExportTarget]:
        statement = select(GuildExportTarget).where(GuildExportTarget.guild_id == guild_id)
        if enabled_only:
            statement = statement.where(GuildExportTarget.enabled.is_(True))
        async with self._session_factory() as session:
            rows = await session.scalars(
                statement.order_by(GuildExportTarget.name, GuildExportTarget.id)
            )
            return tuple(_view(row) for row in rows)


def _view(row: GuildExportTarget) -> ExportTarget:
    """The read model, which has nowhere to put the credential."""
    return ExportTarget(
        id=row.id,
        guild_id=row.guild_id,
        format=row.format,
        name=row.name,
        target=row.target,
        config=dict(row.config),
        has_secret=row.wrapped_secret is not None,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
