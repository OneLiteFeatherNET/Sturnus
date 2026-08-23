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

    async def get_stored(self, guild_id: int, key: str) -> str | None:
        """Returns only the stored value, without falling back to the default.

        Lets a caller distinguish "explicitly set" from "using the default" —
        `get` alone cannot, since both cases return the same string.
        """
        async with self._session_factory() as session:
            return await session.scalar(
                select(GuildConfig.value).where(
                    GuildConfig.guild_id == guild_id, GuildConfig.key == key
                )
            )

    async def snapshot(self, guild_id: int) -> dict[str, str]:
        """Reads every effective value for a guild in a single query.

        Exists because the reconcile pass now runs once per guild every ten
        seconds (`SturnusClient._tick_guild`): the five separate `get()`
        round-trips the one-shot startup path used to make would become
        five queries per guild per tick, on the same task as the readiness
        heartbeat. This is one `SELECT` of a handful of rows instead.

        Stored values win over `DEFAULTS`, exactly as `get` resolves them;
        keys with neither are simply absent, so the caller can still tell
        "unset" from "set to the default".
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(GuildConfig.key, GuildConfig.value).where(GuildConfig.guild_id == guild_id)
            )
            stored = {key: value for key, value in rows.all()}
        return {**settings.DEFAULTS, **stored}

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None:
        """Sets a value; `None` removes it and restores the default.

        `key` must be a known key — a member of `settings.KNOWN_KEYS` —
        otherwise a typo would silently store a setting nobody reads.

        For a known integer key, the value must parse as a positive integer.
        Rejecting a bad value here keeps the read path (`_int`) reachable
        only with data that is already known to be sane.

        `voice_channel_ids` is validated for the same reason and at the
        same moment, by the parser the bot itself reads it with. A list
        that cannot be parsed must be refused at the write, where an
        administrator is looking at the reply — discovering it at the join
        means a guild that reports itself configured and records nothing.

        A known boolean key must be exactly `"true"` or `"false"`
        (`settings.BOOLEAN_VALUES`). The same argument as for integers,
        one step stronger: `settings.is_true` reads anything else as
        false, so an unvalidated `"yes"` would not fail anywhere -- it
        would quietly mean the opposite of what whoever typed it meant.
        The one boolean key today decides whether a guild may offer video
        consent at all, which is not a question to answer by accident.
        """
        if key not in settings.KNOWN_KEYS:
            raise ValueError(f"unknown configuration key {key!r}")

        if value is not None and key == settings.VOICE_CHANNEL_IDS:
            # Raises `settings.InvalidChannelList`, which is a `ValueError`
            # — so `/config set` and the console's 400 both catch it
            # already, and it says which entry it could not read.
            settings.parse_channel_ids(value)

        if value is not None and key in settings.INTEGER_KEYS:
            try:
                parsed = int(value)
            except ValueError as exc:
                raise ValueError(f"{key!r} must be an integer, got {value!r}") from exc
            if parsed <= 0:
                raise ValueError(f"{key!r} must be positive, got {parsed}")

        if (
            value is not None
            and key in settings.BOOLEAN_KEYS
            and value not in settings.BOOLEAN_VALUES
        ):
            allowed = " or ".join(sorted(settings.BOOLEAN_VALUES))
            raise ValueError(f"{key!r} must be {allowed}, got {value!r}")

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
