"""Regression test: `LinkStateStore.purge_expired` had no caller anywhere in
the process (`sturnus.infrastructure.db.link_state`'s own tests exercise it
as a pure function, which is not enough -- a passing unit test proves
nothing about whether anything in production ever calls it). Every
abandoned `/link start` left its row in `oauth_state` forever.

`_publish_loop` (`sturnus.entrypoints.bot`) is where it is wired in --
`bot.py` already constructs the `LinkStateStore` this needs and already
runs this exact poll/stop-event loop, so no fourth loop was added. This
test proves the wiring against a real Postgres-backed `LinkStateStore`,
not a fake that could silently no-op.
"""

from __future__ import annotations

import asyncio
import typing
from datetime import UTC, datetime, timedelta

import discord
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.entrypoints.bot import _publish_loop
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.models import Base

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


class _FakeClient:
    """Only what `_publish_loop` and `_DiscordAnnouncer` need: this test's
    `FakeSessions` reports no announcement candidates, so `_DiscordAnnouncer`
    is constructed but never actually calls `get_channel`/`fetch_channel`.
    """

    async def wait_until_ready(self) -> None:
        return None


class _FakeSessions:
    """Satisfies `sturnus.application.publishing.SessionReader` with no
    candidates -- this test is about the purge sweep, not the announce one.
    """

    async def candidates_for_announcement(self) -> list[dict[str, object]]:
        return []

    async def mark_announced(self, _session_id: int, _now: datetime) -> None:
        raise AssertionError("no candidate was ever reported; this must not be called")


async def test_publish_loop_purges_expired_oauth_states(clean_database: str) -> None:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    link_states = LinkStateStore(factory)

    await link_states.issue(1, "outline", T0 - timedelta(hours=1), timedelta(minutes=10))

    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.02)
        stop.set()

    asyncio.create_task(stop_soon())
    await asyncio.wait_for(
        _publish_loop(
            typing.cast(discord.Client, _FakeClient()),
            _FakeSessions(),
            link_states,
            stop,
            poll_seconds=0.5,
        ),
        timeout=2.0,
    )

    # If `_publish_loop` never called `purge_expired`, this second, direct
    # call would still find the row and remove it (1); the loop having
    # already removed it is what makes this 0 -- a stronger check than
    # `consume(expired, ...)` returning `None`, since `consume` refuses an
    # expired state regardless of whether it was ever purged.
    assert await link_states.purge_expired(T0) == 0
