"""Per-guild runtime configuration with fallback to the defaults."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.domain import settings
from sturnus.domain.session import SessionTimeouts
from sturnus.infrastructure.db.models import GuildConfig


class ConfigStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, guild_id: int, key: str) -> str | None:
        async with self._session_factory() as session:
            stored = await session.scalar(
                select(GuildConfig.value).where(
                    GuildConfig.guild_id == guild_id, GuildConfig.key == key
                )
            )
        if stored is not None:
            return stored
        return settings.DEFAULTS.get(key)

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None:
        """Sets a value; `None` removes it and restores the default.

        For a known integer key, the value must parse as a positive integer.
        Rejecting a bad value here keeps the read path (`_int`) reachable
        only with data that is already known to be sane.
        """
        if value is not None and key in settings.INTEGER_KEYS:
            try:
                parsed = int(value)
            except ValueError as exc:
                raise ValueError(f"{key!r} must be an integer, got {value!r}") from exc
            if parsed <= 0:
                raise ValueError(f"{key!r} must be positive, got {parsed}")

        async with self._session_factory() as session:
            if value is None:
                await session.execute(
                    delete(GuildConfig).where(
                        GuildConfig.guild_id == guild_id, GuildConfig.key == key
                    )
                )
            else:
                statement = insert(GuildConfig).values(
                    guild_id=guild_id, key=key, value=value, updated_at=now
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[GuildConfig.guild_id, GuildConfig.key],
                        set_={"value": value, "updated_at": now},
                    )
                )
            await session.commit()

    async def timeouts(self, guild_id: int) -> SessionTimeouts:
        return SessionTimeouts(
            empty_grace_seconds=await self._int(guild_id, settings.EMPTY_GRACE_SECONDS),
            idle_timeout_minutes=await self._int(guild_id, settings.IDLE_TIMEOUT_MINUTES),
            max_session_hours=await self._int(guild_id, settings.MAX_SESSION_HOURS),
        )

    async def _int(self, guild_id: int, key: str) -> int:
        value = await self.get(guild_id, key)
        if value is None:
            raise KeyError(f"no value and no default for {key!r}")
        return int(value)
