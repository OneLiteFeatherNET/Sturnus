"""Mirroring Outline's collection list so that `api` can name a target.

`document_target` (Spec 11) is an Outline collection UUID an administrator
pasted into the console, and the console shows it back as a UUID. The
Outline API token that could turn it into "Meetings" belongs to `worker`
-- `api` holds S3, the master key and the OAuth secret, and the console
design's Section 2.1 is a list of what each process may hold, not a
suggestion. So `worker` reads the list and writes it down, exactly as
`bot` does for a guild's channels and roles.

**A failure to reach Outline leaves the previous mirror in place.** That
is the whole content of `sweep_outline_collections` and the reason it
exists as a function rather than as two lines in the worker's loop. The
tempting shape -- fetch into a list, write whatever came back -- turns a
timeout into an empty picker, and an empty picker is worse than a stale
one: a stale entry names a collection that has probably not moved, while
an empty list tells an administrator their Outline instance has no
collections at all. The mirror is only ever replaced by a list that
actually arrived.

The sweep runs rarely (see the worker's interval): a collection list
changes when somebody creates or deletes a collection, which is a thing
that happens a handful of times a year, not a thing that happens between
two meetings.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sturnus.observability.events import Event, log_exception

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MirroredCollection:
    """One Outline collection, as the console will eventually offer it.

    `collection_id` is a string and not an integer: Outline issues UUIDs,
    and it is also what `document_target` already stores.
    """

    collection_id: str
    name: str


class CollectionSource(Protocol):
    """Where the collection list is actually read from."""

    async def list_collections(self) -> Sequence[MirroredCollection]: ...


class CollectionMirror(Protocol):
    """Where the collection list is written for `api` to read."""

    async def replace(self, collections: Iterable[MirroredCollection], now: datetime) -> None: ...


async def sweep_outline_collections(
    source: CollectionSource, mirror: CollectionMirror, now: datetime
) -> None:
    """Replaces the mirror with what Outline currently reports, or nothing.

    Survives its own failure deliberately, and the shape of that survival
    is the point: when `list_collections` raises, **nothing is written**.
    The previous mirror stays exactly as it was, so a console rendered
    during an Outline outage still names the collections it named before
    it. Writing an empty list on failure would be worse than not
    sweeping, since an administrator cannot tell a genuinely empty
    instance from an unreachable one.

    The write itself is not wrapped. A database that refuses this write is
    a database the worker's own queue is about to fail on too, and
    swallowing it here would hide that behind a mirror that quietly stops
    updating.
    """
    try:
        collections = await source.list_collections()
    except Exception as exc:
        log_exception(
            log,
            logging.WARNING,
            Event.SWEEP_FAILED,
            "Could not read Outline's collection list; the console keeps the names it had",
            exc,
            reason="outline_collections",
        )
        return
    await mirror.replace(collections, now)
