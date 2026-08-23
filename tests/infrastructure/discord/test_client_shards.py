"""What changes about the client's lifecycle when it holds several shards.

`SturnusClient` extends `commands.AutoShardedBot`, so one process opens N
gateway connections instead of one. **No deployment invariant moves** --
`sturnus.application.sharding` is where that is argued -- but three things
about the gateway lifecycle genuinely do, and each is pinned here rather
than left to be discovered in production:

1. `on_ready` stops firing on a single shard's re-IDENTIFY, so the
   "reconcile everything after a gateway blip" repair had to move to
   `on_shard_ready` or silently stop happening.
2. Readiness stops being a boolean set once and never cleared.
3. The tick loop must be started once for N connections, not once each.

The fakes come from `test_client`, which already builds a `SturnusClient`
entirely against ports -- there is no gateway connection anywhere in this
file. Guild ids are real snowflake shapes rather than `1` and `2`, because
Discord's routing rule is `(guild_id >> 22) % shard_count` and small
integers all land on shard 0, which would make every assertion below pass
for the wrong reason.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest

from sturnus.application.reconfigure import ReconfigureResult
from sturnus.application.sharding import shard_of
from sturnus.domain import settings
from sturnus.observability.events import Event
from sturnus.observability.redaction import scrub_fields
from tests.infrastructure.discord.test_client import (
    CHANNEL_ID,
    ROLE_ID,
    T0,
    FakeClock,
    FakeConfigStore,
    _client,
    _guild,
    _in_guild,
    _voice_channel,
)

CLIENT_LOGGER = "sturnus.infrastructure.discord.client"

#: Two guilds Discord routes to different shards under a two-shard client:
#: `(1 << 22) >> 22 == 1` and `(2 << 22) >> 22 == 2`, which are 1 and 0
#: modulo two. `test_client`'s `GUILD_ID = 1` would put both on shard 0.
GUILD_A = 1 << 22
GUILD_B = 2 << 22


def _sharded(client: discord.AutoShardedClient, shard_count: int) -> None:
    """Tells the client and its cache how many shards this process holds.

    `discord.Client.shard_count` is an instance attribute `launch_shards`
    fills in, and `discord.Guild.shard_id` reads the *connection state's*
    copy of it -- two attributes discord.py keeps in step itself. A test
    with no gateway has to set both, which is exactly what `launch_shards`
    does.
    """
    client.shard_count = shard_count
    client._connection.shard_count = shard_count


def _guild_on_shard(guild_id: int, shard_count: int) -> MagicMock:
    """A guild mock that answers `shard_id` the way a real `Guild` would.

    `MagicMock(spec=discord.Guild)` gives `shard_id` back as a mock rather
    than an integer, because it is a property. Computing it here with the
    same rule discord.py uses keeps the fixture honest instead of letting
    a test pick whichever shard makes it pass.
    """
    guild = _guild(guild_id, _voice_channel(CHANNEL_ID, members=[]))
    guild.shard_id = shard_of(guild_id, shard_count)
    return guild


def _store(*guild_ids: int) -> FakeConfigStore:
    store = FakeConfigStore()
    for guild_id in guild_ids:
        store.write(guild_id, settings.VOICE_CHANNEL_IDS, str(CHANNEL_ID))
        store.write(guild_id, settings.CONSENT_ROLE_ID, str(ROLE_ID))
    return store


def _events(caplog: pytest.LogCaptureFixture, event: Event) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "sturnus_event", None) == str(event)]


def _emitted(record: logging.LogRecord) -> dict[str, object]:
    """The fields as they reach a log line, not as the call site passed them.

    `log_event` attaches the call site's dict to the record untouched; the
    registry is applied by `SturnusFilter` on the way to the formatter.
    Anything asserting on *absence* has to run the same step, or it is
    reading a stage earlier than the one that decides.
    """
    return scrub_fields(getattr(record, "sturnus_fields", {}))


async def test_a_shard_coming_back_reconciles_only_the_guilds_it_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shard's ready is about that shard's guilds, not about all of them.

    `self.guilds` is one cache shared across every connection, so the other
    shards' guilds are sitting right there and it would be easy -- and
    wrong -- to sweep them too. Each shard's own ready covers its own
    guilds exactly once; reconciling everything on every shard would
    multiply the startup database reads by the shard count and buy nothing.
    """
    client = _client(FakeClock(T0), config_store=_store(GUILD_A, GUILD_B))
    _sharded(client, 2)
    first = _guild_on_shard(GUILD_A, 2)
    second = _guild_on_shard(GUILD_B, 2)
    assert first.shard_id != second.shard_id, (
        "the fixture must put the two guilds on different shards, or this asserts nothing"
    )
    _in_guild(client, first)
    _in_guild(client, second)

    reconciled: list[int] = []
    original = client.reconcile_guild

    async def recording_reconcile(guild_id: int) -> ReconfigureResult:
        reconciled.append(guild_id)
        return await original(guild_id)

    monkeypatch.setattr(client, "reconcile_guild", recording_reconcile)

    await client.on_shard_ready(first.shard_id)

    assert reconciled == [GUILD_A]


async def test_on_ready_does_no_reconciling_because_the_shards_already_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The move that sharding this client is actually about.

    With a plain client, `on_ready` fired again on every re-IDENTIFY, and
    reconciling every guild there was a working repair for a gateway blip.
    `AutoShardedConnectionState._delay_ready` empties `_ready_tasks` once
    it has fired, and `parse_ready` only re-schedules the whole-bot ready
    when *every* shard has a pending ready task again -- so one shard
    coming back on its own never re-fires it. Left where it was, the
    repair would have quietly stopped happening for exactly the guilds
    that had just lost and rebuilt their caches.

    It is not merely moved: `on_ready` fires only after every shard's own
    ready task has finished, so repeating the sweep here would be a second
    database read per guild that could not change anything.
    """
    client = _client(FakeClock(T0), config_store=_store(GUILD_A))
    _sharded(client, 1)
    _in_guild(client, _guild_on_shard(GUILD_A, 1))

    async def refuse(_guild_id: int) -> ReconfigureResult:
        raise AssertionError("on_ready must not reconcile")

    monkeypatch.setattr(client, "reconcile_guild", refuse)

    await client.on_ready()


async def test_the_shard_lifecycle_drives_readiness_rather_than_on_ready() -> None:
    """Four shards up is four statements, and one of them can stop being true.

    Readiness used to be a boolean set in `on_ready` and never cleared, so
    a gateway lost an hour after startup left `/readyz` green for the rest
    of the process's life. Per shard there is a disconnect event, so there
    is no longer an excuse.
    """
    client = _client(FakeClock(T0), config_store=_store(GUILD_A))
    _sharded(client, 2)
    _in_guild(client, _guild_on_shard(GUILD_A, 2))
    client._readiness.database_reachable = True

    await client.on_shard_ready(0)
    assert client._readiness.ready is False, "one of two shards is not the whole bot"

    await client.on_shard_ready(1)
    assert client._readiness.ready is True

    await client.on_shard_disconnect(1)
    assert client._readiness.ready is False
    assert client._readiness.missing_shards == [1]

    await client.on_shard_resumed(1)
    assert client._readiness.ready is True


async def test_a_shard_is_only_ready_once_its_guilds_have_been_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness hangs off `shard_ready`, never off `shard_connect`.

    discord.py dispatches `shard_connect` the moment the READY payload
    lands -- before the guild caches are filled and before this client has
    read a single guild's configuration. Marking the shard ready there
    would turn the probe green on a connection that cannot yet answer for
    any of the servers it carries.
    """
    client = _client(FakeClock(T0), config_store=_store(GUILD_A))
    _sharded(client, 1)
    _in_guild(client, _guild_on_shard(GUILD_A, 1))
    client._readiness.database_reachable = True

    seen: list[bool] = []

    async def reconcile_and_look(_guild_id: int) -> None:
        seen.append(client._readiness.discord_connected)

    monkeypatch.setattr(client, "reconcile_guild", reconcile_and_look)

    await client.on_shard_ready(0)

    assert seen == [False], "the shard was marked up before its guilds were reconciled"
    assert client._readiness.ready is True


async def test_a_resume_restores_a_shard_without_reconciling_its_guilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RESUME keeps the guild cache, so there is nothing to rebuild.

    That is the whole difference between `on_shard_resumed` and
    `on_shard_ready`. Reconciling here anyway would put a database read
    per guild behind every routine gateway hiccup -- and discord.py
    dispatches `shard_disconnect`/`shard_resumed` for exactly those.
    """
    client = _client(FakeClock(T0), config_store=_store(GUILD_A))
    _sharded(client, 1)
    _in_guild(client, _guild_on_shard(GUILD_A, 1))

    async def refuse(_guild_id: int) -> ReconfigureResult:
        raise AssertionError("a resume must not reconcile")

    monkeypatch.setattr(client, "reconcile_guild", refuse)

    await client.on_shard_resumed(0)

    assert client._readiness.discord_connected is True


async def test_the_tick_loop_is_started_once_however_often_it_is_asked_for() -> None:
    """One loop for N connections, not one per connection.

    `setup_hook` runs exactly once -- discord.py calls it from `login()`,
    before `connect()` launches any shard -- so this guard never fires
    today. It is here because the cost of being wrong is two loops
    sweeping the same `_recordings` dict and closing each other's
    sessions, and the cost of the guard is one comparison at startup.
    """
    client = _client(FakeClock(T0), config_store=_store())

    client._ensure_tick_loop()
    first = client._tick_task
    client._ensure_tick_loop()

    assert first is not None
    assert client._tick_task is first
    first.cancel()


async def test_a_shard_ready_says_which_shard_and_how_many_there_are(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`shard_id` alone does not say whether "3 is down" is a third or a quarter."""
    client = _client(FakeClock(T0), config_store=_store())
    _sharded(client, 4)

    with caplog.at_level(logging.INFO, logger=CLIENT_LOGGER):
        await client.on_shard_ready(2)

    (record,) = _events(caplog, Event.BOT_SHARD_READY)
    fields = getattr(record, "sturnus_fields", {})
    assert fields["shard_id"] == 2
    assert fields["shard_count"] == 4


async def test_a_disconnected_shard_is_reported_as_a_warning_naming_the_shard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING, not ERROR: discord.py dispatches this for every RESUME too.

    Most of these are followed by `bot.shard_resumed` within seconds and
    nothing was ever wrong. What makes a genuine outage visible is the
    readiness probe this feeds -- 10-second period, `failureThreshold: 3`,
    so half a minute of continuous absence -- not the level of the line.
    """
    client = _client(FakeClock(T0), config_store=_store())
    _sharded(client, 4)

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        await client.on_shard_disconnect(3)

    (record,) = _events(caplog, Event.BOT_SHARD_DISCONNECTED)
    assert record.levelno == logging.WARNING
    assert getattr(record, "sturnus_fields", {})["shard_id"] == 3


async def test_a_guild_line_names_its_shard_only_when_there_is_more_than_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conditional field, exercised through a real failing tick.

    On a single-shard process `shard_id` would be `0` in every line for
    ever, so it is absent. With four shards it is the grouping an operator
    actually wants when one connection starts misbehaving, and LogQL
    cannot derive it from `guild_id` -- it has no `>>` and no `%`.

    Asserted through `scrub_fields`, because that is the shape the line
    actually reaches Loki in: `log_event` attaches the call site's dict to
    the record untouched, and the registry -- including
    `OMITTED_WHEN_NONE`, which is what turns the call site's `None` into
    an absent key -- is applied on the way to the formatter.
    """
    clock = FakeClock(T0)
    client = _client(clock, config_store=_store(GUILD_A), recording_dir=tmp_path)
    _in_guild(client, _guild_on_shard(GUILD_A, 4))

    async def explode(_guild_id: int, _now: object) -> None:
        raise RuntimeError("the tick could not run")

    monkeypatch.setattr(client, "_tick_guild", explode)

    _sharded(client, 1)
    with caplog.at_level(logging.ERROR, logger=CLIENT_LOGGER):
        await client._tick_all(clock.now())
    (single,) = _events(caplog, Event.GUILD_TICK_FAILED)
    assert "shard_id" not in _emitted(single)

    caplog.clear()
    _sharded(client, 4)
    with caplog.at_level(logging.ERROR, logger=CLIENT_LOGGER):
        await client._tick_all(clock.now())
    (sharded,) = _events(caplog, Event.GUILD_TICK_FAILED)
    assert _emitted(sharded)["shard_id"] == shard_of(GUILD_A, 4)


async def test_an_explicit_shard_count_reaches_the_gateway_client() -> None:
    """The setting is not decoration: it has to arrive where discord.py reads it."""
    client = _client(FakeClock(T0), config_store=_store(), shard_count=4)
    assert client.shard_count == 4


async def test_no_shard_count_leaves_the_choice_to_discord() -> None:
    """`None` is discord.py's own "ask `/gateway/bot`", not a missing value."""
    client = _client(FakeClock(T0), config_store=_store())
    assert client.shard_count is None


async def test_the_client_is_auto_sharded_at_all() -> None:
    """The one assertion the whole file rests on.

    A plain `commands.Bot` would make every handler above dead code: the
    `shard_*` events are dispatched by `AutoShardedConnectionState` and by
    nothing else, so they would simply never fire and readiness would
    never turn green.
    """
    client = _client(FakeClock(T0), config_store=_store())
    assert isinstance(client, discord.AutoShardedClient)


async def test_shutdown_still_cancels_the_one_tick_loop() -> None:
    """Sharding adds connections, not loops; the shutdown path is unchanged."""
    client = _client(FakeClock(T0), config_store=_store())
    client._ensure_tick_loop()
    task = client._tick_task
    assert task is not None

    await client.graceful_shutdown()
    await asyncio.sleep(0)

    assert task.cancelled() or task.done()
