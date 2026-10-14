"""Guild scope at the gateway (openspec change `single-guild-instance`).

Command registration and the guard at the head of the reconcile path, driven
through the real `SturnusClient` with a fake command tree: no network, no
real Discord, no waiting.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import discord
import pytest

from sturnus.domain import settings
from sturnus.observability.events import Event
from tests.infrastructure.discord.test_client import (
    CHANNEL_ID,
    GUILD_ID,
    OTHER_GUILD_ID,
    T0,
    FakeClock,
    FakeSessions,
    _client,
    _configured_store,
    _guild,
    _in_guild,
    _voice_channel,
)

CLIENT_LOGGER = "sturnus.infrastructure.discord.client"


class FakeTree:
    """Records every call made on the command tree, in order."""

    def __init__(self, *, fail_global_sync: bool = False, fail_guild_sync: bool = False) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self._fail_global_sync = fail_global_sync
        self._fail_guild_sync = fail_guild_sync

    def copy_global_to(self, *, guild: discord.abc.Snowflake) -> None:
        self.calls.append(("copy_global_to", guild.id))

    def clear_commands(self, *, guild: discord.abc.Snowflake | None) -> None:
        self.calls.append(("clear_commands", guild.id if guild is not None else None))

    async def sync(self, *, guild: discord.abc.Snowflake | None = None) -> list[object]:
        self.calls.append(("sync", guild.id if guild is not None else None))
        if (guild is None and self._fail_global_sync) or (
            guild is not None and self._fail_guild_sync
        ):
            raise _http_error()
        return []


def _http_error() -> discord.HTTPException:
    response = MagicMock(spec=aiohttp.ClientResponse)
    response.status = 500
    response.reason = "Internal Server Error"
    return discord.HTTPException(response, "boom")


def _tree_of(monkeypatch: pytest.MonkeyPatch, client: object, tree: FakeTree) -> None:
    """Gives this one client's class a fake `tree`; undone by `monkeypatch`.

    discord.py keeps the tree in a name-mangled attribute behind a read-only
    property, so the property is what has to be replaced.
    """
    monkeypatch.setattr(type(client), "tree", property(lambda _self: tree))


async def test_commands_are_registered_for_the_served_guild_before_globals_are_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(FakeClock(T0))
    tree = FakeTree()
    _tree_of(monkeypatch, client, tree)

    await client._sync_commands()

    assert tree.calls == [
        ("copy_global_to", GUILD_ID),
        ("sync", GUILD_ID),
        ("clear_commands", None),
        ("sync", None),
    ], "the guild must hold its commands before the global ones are removed"


async def test_a_failing_global_clear_is_logged_and_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(FakeClock(T0))
    _tree_of(monkeypatch, client, FakeTree(fail_global_sync=True))

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        await client._sync_commands()

    events = [getattr(r, "sturnus_event", None) for r in caplog.records]
    assert str(Event.COMMANDS_SYNC_FAILED) in events


async def test_a_failing_guild_sync_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(FakeClock(T0))
    _tree_of(monkeypatch, client, FakeTree(fail_guild_sync=True))

    with pytest.raises(discord.HTTPException):
        await client._sync_commands()


async def test_a_foreign_guild_opens_no_voice_connection_and_creates_no_session(
    tmp_path: Path,
) -> None:
    store = _configured_store()
    store.write(OTHER_GUILD_ID, settings.VOICE_CHANNEL_IDS, "22")
    store.write(OTHER_GUILD_ID, settings.CONSENT_ROLE_ID, "33")
    sessions = FakeSessions()
    client = _client(FakeClock(T0), config_store=store, sessions=sessions, recording_dir=tmp_path)
    _in_guild(client, _guild(OTHER_GUILD_ID, _voice_channel(22, members=[])))

    await client.reconcile_guild(OTHER_GUILD_ID)

    assert client._recording_of(OTHER_GUILD_ID) is None, "no pipeline for a foreign guild"
    assert client.voices == [], "no voice connection was opened"
    assert sessions.opened == [], "no session was created"


async def test_the_configured_guild_is_still_reconciled(tmp_path: Path) -> None:
    client = _client(FakeClock(T0), config_store=_configured_store(), recording_dir=tmp_path)
    _in_guild(client, _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[])))

    await client.reconcile_guild(GUILD_ID)

    assert client._recording_of(GUILD_ID) is not None


async def test_the_refusal_names_both_ids_and_no_guild_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(FakeClock(T0), config_store=_configured_store(), recording_dir=tmp_path)
    foreign = _guild(OTHER_GUILD_ID, _voice_channel(22, members=[]))
    foreign.name = "Somebody Else's Server"
    _in_guild(client, foreign)

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        await client.reconcile_guild(OTHER_GUILD_ID)

    (record,) = [
        r for r in caplog.records if getattr(r, "sturnus_event", None) == str(Event.GUILD_REFUSED)
    ]
    fields = record.sturnus_fields  # type: ignore[attr-defined]
    assert fields == {"guild_id": OTHER_GUILD_ID, "configured_guild_id": GUILD_ID}
    assert "Somebody Else" not in caplog.text


async def test_the_periodic_tick_does_not_apply_setup_or_mirror_a_foreign_guild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirrored: list[int] = []
    client = _client(FakeClock(T0), config_store=_configured_store(), recording_dir=tmp_path)
    _in_guild(client, _guild(OTHER_GUILD_ID, _voice_channel(22, members=[])))

    async def spy(guild_id: int, _now: object) -> None:
        mirrored.append(guild_id)

    monkeypatch.setattr(client, "_apply_setup_intents", spy)
    monkeypatch.setattr(client, "_mirror_administrators", spy)
    monkeypatch.setattr(client, "_mirror_directory", spy)

    await client._tick_all(client._clock.now())

    assert mirrored == [], "the tick leaves a foreign guild alone"
    assert client._recording_of(OTHER_GUILD_ID) is None


def test_no_code_path_makes_the_bot_leave_a_guild() -> None:
    """The bot never leaves a guild on its own; leaving is a person's decision."""
    src = Path(__file__).parents[3] / "src"
    offenders = [
        f"{path.relative_to(src)}:{node.lineno}"
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute)
        and node.attr == "leave"
        and not (isinstance(node.value, ast.Attribute) and node.value.attr == "voice")
        and not (isinstance(node.value, ast.Name) and node.value.id in {"self", "voice"})
    ]
    assert offenders == [], f"Guild.leave must not be called: {offenders}"
