"""What a person decided about their own view of the console.

`ConfigStore` keyed by person instead of by guild, and deliberately so:
`user_preference` is `guild_config`'s shape one layer down, so the store
over it is `ConfigStore`'s shape too. A reader who has understood one has
understood the other, and neither has an edge case the other does not.

The one asymmetry is what a bad write is checked against. `guild_config`
holds durations and snowflakes, so `ConfigStore.set` checks that an
integer key parses as a positive integer. Every key here instead selects a
code path -- a stylesheet, a message catalogue -- so the check is
membership of a closed set (`sturnus.domain.preferences.ALLOWED_VALUES`).
Both checks exist for the same reason: the read path should be reachable
only with values that are already known to be usable, because the read
path is a page being rendered and has nowhere to report a problem to.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.domain import preferences
from sturnus.infrastructure.db.models import UserPreference


class PreferenceStore:
    """Reads and writes one person's console preferences."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def snapshot(self, discord_user_id: int) -> dict[str, str]:
        """Every effective preference for one person, in a single query.

        One query rather than one per key, for the reason
        `ConfigStore.snapshot` gives: this is read while a page is being
        rendered, and a handful of round trips per request is a handful
        of round trips per request forever.

        Stored values are layered over `DEFAULTS`, so every known key
        always has an answer -- a caller that had to fall back itself is
        a caller that will eventually forget to.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(UserPreference.key, UserPreference.value).where(
                    UserPreference.discord_user_id == discord_user_id
                )
            )
            stored = {key: value for key, value in rows.all() if value is not None}
        return {**preferences.DEFAULTS, **stored}

    async def set(self, discord_user_id: int, key: str, value: str | None, now: datetime) -> None:
        """Sets one preference; `None` removes it and restores the default.

        The key is checked even when the value is `None`. Skipping the
        check there would turn a misspelled key into a silent no-op --
        a person clicking "reset" and nothing happening, with nothing
        anywhere saying why.

        Removal rather than storing the default string, because the two
        are not the same fact: an absent row is "never expressed", which
        is what lets a future change to `DEFAULTS` reach the people who
        never disagreed with it.
        """
        if key not in preferences.KNOWN_KEYS:
            raise ValueError(f"unknown preference key {key!r}")
        if value is not None and not preferences.is_allowed(key, value):
            raise ValueError(f"{key!r} does not accept {value!r}")

        async with self._session_factory() as session:
            if value is None:
                await session.execute(
                    delete(UserPreference).where(
                        UserPreference.discord_user_id == discord_user_id,
                        UserPreference.key == key,
                    )
                )
            else:
                statement = insert(UserPreference).values(
                    discord_user_id=discord_user_id, key=key, value=value, updated_at=now
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[UserPreference.discord_user_id, UserPreference.key],
                        set_={"value": value, "updated_at": now},
                    )
                )
            await session.commit()
