"""Single-use OAuth state for tying a callback back to a Discord user.

The state is the only thing standing between an attacker and linking their
own external account to someone else's Discord identity, so it is a
security boundary, not bookkeeping. Three properties matter:

- **Unguessable**: generated with `secrets`, never `random` -- predicting a
  state would let an attacker forge a callback for a user they don't
  control.
- **Single use**: `consume` deletes the row and returns its content in one
  round trip, so a replayed callback (the browser back button, a retried
  webhook, a malicious resend) can never link twice.
- **Expiring, indistinguishably from unknown**: an expired state and a
  state that never existed both make `consume` return `None`. Returning
  anything that let a caller tell the two apart would let them probe
  whether a given state was ever issued.

This talks to the `oauth_state` table directly with SQLAlchemy Core rather
than importing the mapped model from `sturnus.infrastructure.db.models`:
`sturnus.application` may never import a concrete adapter (see
`tests/test_architecture.py`), and the ORM model lives in `infrastructure`.
The table shape below must stay in sync with `OAuthState` there -- both
describe the same physical table.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Column,
    CursorResult,
    DateTime,
    MetaData,
    Table,
    Text,
    delete,
    insert,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_metadata = MetaData()

_oauth_state = Table(
    "oauth_state",
    _metadata,
    Column("state", Text, primary_key=True),
    Column("discord_user_id", BigInteger, nullable=False),
    Column("provider", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


def new_state() -> str:
    """Generates a cryptographically random, unguessable state token.

    Uses `secrets.token_urlsafe`, never `random` -- this token is a
    security boundary, and `random` is not safe against an adversary
    trying to predict it.
    """
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class PendingLink:
    """The Discord identity a successfully consumed state resolves to."""

    discord_user_id: int
    provider: str


class LinkStateStore:
    """Issues and consumes single-use OAuth states backed by `oauth_state`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def issue(
        self, discord_user_id: int, provider: str, now: datetime, ttl: timedelta
    ) -> str:
        """Creates a new state for `discord_user_id`, valid until `now + ttl`."""
        state = new_state()
        async with self._session_factory() as session:
            await session.execute(
                insert(_oauth_state).values(
                    state=state,
                    discord_user_id=discord_user_id,
                    provider=provider,
                    created_at=now,
                    expires_at=now + ttl,
                )
            )
            await session.commit()
        return state

    async def consume(self, state: str, now: datetime) -> PendingLink | None:
        """Consumes `state` and returns who it belonged to, or `None`.

        Deletes the matching, still-valid row and returns its content in
        one statement (`DELETE ... RETURNING`), so two concurrent callbacks
        replaying the same state can never both succeed: only the delete
        that actually removes the row gets a result back, the other finds
        nothing left to delete.

        An expired state and an unknown state both return `None` --
        distinguishing them would tell the caller whether a given state
        was ever issued at all.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    delete(_oauth_state)
                    .where(_oauth_state.c.state == state, _oauth_state.c.expires_at > now)
                    .returning(_oauth_state.c.discord_user_id, _oauth_state.c.provider)
                )
            ).first()
            await session.commit()
        if row is None:
            return None
        return PendingLink(discord_user_id=row.discord_user_id, provider=row.provider)

    async def purge_expired(self, now: datetime) -> int:
        """Deletes every state that has expired as of `now`; returns how many."""
        async with self._session_factory() as session:
            result = await session.execute(
                delete(_oauth_state).where(_oauth_state.c.expires_at <= now)
            )
            await session.commit()
        # `execute` on a Core DELETE always yields a `CursorResult` at
        # runtime; the assertion narrows the statically-typed `Result[Any]`
        # so `.rowcount` is available without an unchecked cast.
        assert isinstance(result, CursorResult)
        return result.rowcount
