"""Repository for `oauth_state`: single-use OAuth state tying a callback back to a Discord user.

The state is the only thing standing between an attacker and linking their
own external account to someone else's Discord identity, so it is a
security boundary, not bookkeeping. Three properties matter:

- **Unguessable**: `sturnus.application.linking.new_state` generates it with
  `secrets`, never `random` -- predicting a state would let an attacker
  forge a callback for a user they don't control.
- **Single use**: `consume` deletes the row and returns its content in one
  round trip, so a replayed callback (the browser back button, a retried
  webhook, a malicious resend) can never link twice.
- **Expiring, indistinguishably from unknown**: an expired state and a
  state that never existed both make `consume` return `None`. Returning
  anything that let a caller tell the two apart would let them probe
  whether a given state was ever issued.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.linking import PendingLink, new_state
from sturnus.infrastructure.db.models import AccountLink, OAuthState


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
                insert(OAuthState).values(
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
                    delete(OAuthState)
                    .where(OAuthState.state == state, OAuthState.expires_at > now)
                    .returning(OAuthState.discord_user_id, OAuthState.provider)
                )
            ).first()
            await session.commit()
        if row is None:
            return None
        return PendingLink(discord_user_id=row.discord_user_id, provider=row.provider)

    async def purge_expired(self, now: datetime) -> int:
        """Deletes every state that has expired as of `now`; returns how many."""
        async with self._session_factory() as session:
            result = await session.execute(delete(OAuthState).where(OAuthState.expires_at <= now))
            await session.commit()
        # `execute` on a Core DELETE always yields a `CursorResult` at
        # runtime; the assertion narrows the statically-typed `Result[Any]`
        # so `.rowcount` is available without an unchecked cast.
        assert isinstance(result, CursorResult)
        return result.rowcount


class AccountLinkRepository:
    """Writes `account_link`: the mapping the link service's OAuth callback produces.

    `sturnus.infrastructure.db.repositories.AccountLinkRepository` already
    reads this table (`external_identity`), fixed to one provider at
    construction, for the parts of the system that already needed a read.
    This class holds only the write side the link service (Task 3) and the
    `/link remove` command (Task 4) need, and lives here rather than
    alongside its read-only sibling because `repositories.py` belongs to a
    different task in this wave -- a second class of the same name in a
    different module, not a replacement for the first.

    Unlike its sibling, `provider` is not fixed at construction: both
    methods take it per call, because the caller (the OAuth callback) only
    learns which provider a link is for from the consumed `PendingLink`,
    not from how this repository was wired up.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(
        self, discord_user_id: int, provider: str, external_user_id: str, display_name: str
    ) -> None:
        """Upserts the mapping for `(discord_user_id, provider)`.

        Someone re-linking after changing their external account would
        otherwise hit a primary-key violation on the second attempt --
        "link my account again" reads as replace, not fail, so this writes
        `INSERT ... ON CONFLICT DO UPDATE` rather than a plain insert.
        """
        async with self._session_factory() as session:
            statement = pg_insert(AccountLink).values(
                discord_user_id=discord_user_id,
                provider=provider,
                external_user_id=external_user_id,
                display_name=display_name,
                linked_at=datetime.now(UTC),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["discord_user_id", "provider"],
                    set_={
                        "external_user_id": statement.excluded.external_user_id,
                        "display_name": statement.excluded.display_name,
                        "linked_at": statement.excluded.linked_at,
                    },
                )
            )
            await session.commit()

    async def delete(self, discord_user_id: int, provider: str) -> bool:
        """Deletes the mapping for `(discord_user_id, provider)`.

        Returns whether a row actually existed to remove, which `/link
        remove` reports back to the user rather than claiming success
        unconditionally.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                delete(AccountLink).where(
                    AccountLink.discord_user_id == discord_user_id,
                    AccountLink.provider == provider,
                )
            )
            await session.commit()
        assert isinstance(result, CursorResult)
        return result.rowcount > 0
