"""Outline's collections, as far as the console's API is allowed to know.

`document_target` is a collection UUID, and `api` has no Outline token to
turn it into a name -- the console design's Section 2.1 gives that token
to `worker` alone. So `worker` sweeps the list and writes it here, and
`api` only ever reads: the same arrangement `AdminMemberStore` and
`DirectoryStore` make for Discord, with a different credential on the
writing side.

Not keyed by guild. One deployment talks to one Outline instance --
`OutlineSink` holds a single `base_url` -- so a collection is a fact about
that instance rather than about whichever guilds happen to point at it.

`replace` is a replacement and never a merge, for the reason the guild
mirrors give: a collection deleted in Outline that lingers here is an
option the console goes on offering, and choosing it configures a
`document_target` every protocol will then fail to be written to. What
this store must never see is a sweep that failed -- an empty write and an
unreachable Outline are indistinguishable once they get this far, which is
why `sturnus.application.collection_mirror.sweep_outline_collections`
stops before calling it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.infrastructure.db.models import OutlineCollection


class OutlineCollectionStore:
    """Reads and replaces the mirrored Outline collection list."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace(self, collections: Iterable[MirroredCollection], now: datetime) -> None:
        """Makes the mirror exactly `collections`, in one transaction."""
        # Deduplicated by id: `collections.list` is paginated and an entry
        # can repeat across a page boundary if somebody creates a
        # collection mid-sweep. A primary-key violation would cost the
        # whole mirror rather than one row.
        wanted = {collection.collection_id: collection for collection in collections}
        removal = delete(OutlineCollection)
        if wanted:
            removal = removal.where(OutlineCollection.collection_id.notin_(wanted))
        rows = [
            {
                "collection_id": collection.collection_id,
                "name": collection.name,
                "synced_at": now,
            }
            for collection in wanted.values()
        ]
        async with self._session_factory() as session:
            await session.execute(removal)
            if rows:
                statement = insert(OutlineCollection).values(rows)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["collection_id"],
                        set_={"name": statement.excluded.name, "synced_at": now},
                    )
                )
            await session.commit()

    async def all(self) -> Sequence[MirroredCollection]:
        """Every mirrored collection, ordered by name.

        By name because that is what somebody choosing a collection is
        reading, and Outline's own sidebar is alphabetical too. Ties break
        on id so the same mirror renders identically on every page load.
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(OutlineCollection.collection_id, OutlineCollection.name).order_by(
                    OutlineCollection.name, OutlineCollection.collection_id
                )
            )
        return [
            MirroredCollection(collection_id=collection_id, name=name)
            for collection_id, name in rows.all()
        ]
