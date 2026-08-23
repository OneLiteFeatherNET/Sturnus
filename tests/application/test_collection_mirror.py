"""The sweep that keeps Outline's collection names where `api` can read them.

One property carries this module, and it is the failure case rather than
the success case: an Outline that cannot be reached must leave the
previous mirror standing. A stale collection name is a name; an empty
picker tells an administrator their Outline instance has no collections.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sturnus.application.collection_mirror import (
    MirroredCollection,
    sweep_outline_collections,
)

T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)

MEETINGS = MirroredCollection(collection_id="col-1", name="Meetings")
ARCHIVE = MirroredCollection(collection_id="col-2", name="Archive")


class FakeSource:
    def __init__(self, *pages: list[MirroredCollection] | Exception) -> None:
        self._pages = list(pages)
        self.calls = 0

    async def list_collections(self) -> list[MirroredCollection]:
        self.calls += 1
        answer = self._pages.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeMirror:
    def __init__(self) -> None:
        self.writes: list[list[MirroredCollection]] = []

    async def replace(self, collections: Iterable[MirroredCollection], _now: datetime) -> None:
        self.writes.append(list(collections))


async def test_what_outline_reports_becomes_the_mirror() -> None:
    mirror = FakeMirror()
    await sweep_outline_collections(FakeSource([MEETINGS, ARCHIVE]), mirror, T0)
    assert mirror.writes == [[MEETINGS, ARCHIVE]]


async def test_an_unreachable_outline_leaves_the_previous_mirror_in_place() -> None:
    """The whole reason this is a function. Writing whatever came back
    would turn a timeout into an empty picker, and an administrator cannot
    tell an empty Outline from an unreachable one.
    """
    mirror = FakeMirror()
    await sweep_outline_collections(FakeSource(TimeoutError("no route")), mirror, T0)
    assert mirror.writes == []


async def test_a_failed_sweep_does_not_stop_the_next_one() -> None:
    """A worker that died on a transient Outline error would stop
    transcribing too -- this sweep shares its process with the queue.
    """
    source = FakeSource(TimeoutError("no route"), [MEETINGS])
    mirror = FakeMirror()
    await sweep_outline_collections(source, mirror, T0)
    await sweep_outline_collections(source, mirror, T1)
    assert mirror.writes == [[MEETINGS]]


async def test_an_outline_that_really_has_no_collections_empties_the_mirror() -> None:
    """The difference from the failure case: an empty answer *arrived*.
    Deleting the last collection must stop it being offered.
    """
    mirror = FakeMirror()
    await sweep_outline_collections(FakeSource([]), mirror, T0)
    assert mirror.writes == [[]]
