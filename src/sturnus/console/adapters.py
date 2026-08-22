"""Database adapters for the console's sign-in.

Two narrow additions rather than new stores: the console reuses
`oauth_state` and `account_link` exactly as the link service does, and
these are the two directions neither of them had a reader for yet.

Why a second state store at all, given `LinkStateStore` exists: `/link`
knows who is linking before the browser ever leaves -- a slash command was
run by somebody. A console sign-in does not. Who this is only becomes
known when the provider answers, which is after the round trip, so the row
cannot carry a Discord user id when it is written.

An earlier draft squeezed it into `oauth_state` behind a placeholder id,
and a test caught what that costs: `LinkStateStore.consume` does not
filter by provider, so the link callback consumed a console state and
returned a `PendingLink` for a user id that does not exist. A table of its
own makes that unrepresentable rather than merely unlikely.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.linking import new_state
from sturnus.infrastructure.db.models import AccountLink, ConsoleState

#: How long a sign-in may take. Ten minutes is a browser round trip
#: through a login page with room for somebody to be interrupted, and it
#: bounds how long a captured callback URL stays useful.
_DEFAULT_TTL = timedelta(minutes=10)


class ConsoleStateStore:
    """Single-use OAuth states for a sign-in whose subject is not yet known."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl: timedelta = _DEFAULT_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl

    async def new(self, now: datetime) -> str:
        """Issues a fresh unguessable state and stores it."""
        state = new_state()
        await self.issue(state, now)
        return state

    async def issue(self, state: str, now: datetime) -> None:
        async with self._session_factory() as session:
            session.add(ConsoleState(state=state, created_at=now, expires_at=now + self._ttl))
            await session.commit()

    async def consume(self, state: str, now: datetime) -> bool:
        """Consumes the state, reporting whether it was valid.

        `DELETE ... RETURNING` in one statement, the same shape
        `LinkStateStore.consume` uses and for the same reason: two
        callbacks replaying the same state concurrently can never both
        succeed, because only the delete that actually removes the row
        gets a result back.
        """
        async with self._session_factory() as session:
            row = await session.execute(
                delete(ConsoleState)
                .where(ConsoleState.state == state, ConsoleState.expires_at > now)
                .returning(ConsoleState.state)
            )
            consumed = row.scalar_one_or_none() is not None
            await session.commit()
            return consumed


class ConsoleLinkDirectory:
    """From an external identity to the Discord user who linked it.

    The reverse of `AccountLinkRepository.external_identity`, and the only
    bridge the console has between who authenticated and whose recordings
    they may see.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None:
        """The Discord user this identity is linked to, or `None`.

        Ordered by `linked_at` descending because two Discord users can
        point at one external identity over time -- somebody changes
        Discord accounts and links again -- and the useful answer is the
        current link rather than an abandoned one. Without the ordering
        the row returned would be whichever the planner happened to
        reach, which is a login that names a different person on different
        days.
        """
        async with self._session_factory() as session:
            found: int | None = await session.scalar(
                select(AccountLink.discord_user_id)
                .where(
                    AccountLink.provider == provider,
                    AccountLink.external_user_id == external_user_id,
                )
                .order_by(AccountLink.linked_at.desc())
                .limit(1)
            )
            return found
