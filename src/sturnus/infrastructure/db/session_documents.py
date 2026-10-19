"""What a session published, to each destination that took it.

`session.document_url` holds the primary and stays where it is: the
announcement posts it and everything already reading a session reads it.
This is what a guild's second, third and fourth destination get, because
publishing to several places and recording only one of them makes the
others invisible the moment anybody asks where the minutes went.

`record` is an upsert keyed on `(session_id, target_id)`, so a re-export
overwrites its own destination and nothing else. Appending would leave a
session pointing at two documents in the same place, one of them stale,
with nothing saying which is current.

No business logic: which destinations to publish to, in what order, and
what to do when one of them fails all belong above this.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.domain.exports import SessionDocument
from sturnus.infrastructure.db.models import Session as SessionRow
from sturnus.infrastructure.db.models import SessionDocument as SessionDocumentRow


class SessionDocumentStore:
    """Records and reads the documents one session produced."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        session_id: int,
        *,
        target_id: int,
        provider: str,
        document_id: str,
        url: str,
        now: datetime,
    ) -> None:
        """Records that this session was published to this destination.

        `provider` is copied from the target rather than joined to it,
        because it has to outlive the target: once a destination is
        removed `target_id` goes to null, and a row that could not say
        what kind of document it points at would be a URL with no context.

        `created_at` is not moved by a re-export. It is when this session
        first reached this destination, which is the question a list of
        documents is asked; when it was last rewritten is a question
        nobody has asked and would cost this row its order.
        """
        statement = insert(SessionDocumentRow).values(
            session_id=session_id,
            target_id=target_id,
            provider=provider,
            document_id=document_id,
            url=url,
            created_at=now,
        )
        async with self._session_factory() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_document_per_target",
                    set_={
                        "provider": statement.excluded.provider,
                        "document_id": statement.excluded.document_id,
                        "url": statement.excluded.url,
                    },
                )
            )
            await session.commit()

    async def for_session(self, session_id: int) -> Sequence[SessionDocument]:
        """Every document this session produced, oldest first.

        Publication order, so the list reads as the history it is. Ties
        break on id, so two reads of an unchanged session agree.

        Joined to `session` for the guild, which the read model carries
        and this table does not store -- see
        `sturnus.domain.exports.SessionDocument`. An inner join, because a
        `session_document` row without its session cannot exist:
        `session_id` is `ON DELETE CASCADE`.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(SessionDocumentRow, SessionRow.guild_id)
                .join(SessionRow, SessionRow.id == SessionDocumentRow.session_id)
                .where(SessionDocumentRow.session_id == session_id)
                .order_by(SessionDocumentRow.created_at, SessionDocumentRow.id)
            )
            return tuple(
                SessionDocument(
                    session_id=row.session_id,
                    guild_id=guild_id,
                    target_id=row.target_id,
                    provider=row.provider,
                    document_id=row.document_id,
                    url=row.url,
                    created_at=row.created_at,
                )
                for row, guild_id in rows
            )
