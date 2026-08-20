"""What `/config set` and `/config clear` do with Discord's response deadline.

Both commands now write to the database and then await a full reconcile
pass -- which reads the configuration, may build a pipeline, and on the
force path even encrypts, uploads and enqueues -- before they say anything
back. Discord gives an interaction three seconds for its *initial*
response and marks it as failed after that, no matter what the command
eventually does. `/setup` and `/config apply` already deal with this by
calling `interaction.response.defer(...)` first and answering through
`interaction.followup`; these tests hold the other two commands to the
same contract.

The command callbacks are invoked directly (`Command.callback`), as
`test_link_cog` does: that is the coroutine the cog defines, so calling it
exercises the decision without a gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from sturnus.application.reconfigure import ReconfigureAction, ReconfigureResult, RunningState
from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.discord.config_cog import ConfigCog

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD_ID = 4711


class _Response:
    """Discord's initial-response slot: usable exactly once, in one way."""

    def __init__(self) -> None:
        self.deferred = False
        self.deferred_ephemeral: bool | None = None
        self.messages: list[tuple[str, bool]] = []

    async def defer(self, ephemeral: bool = False, thinking: bool = False) -> None:
        assert not self.deferred, "an interaction can only be deferred once"
        assert not self.messages, "already answered; there is nothing left to defer"
        assert thinking, "a deferral with no thinking indicator shows the user nothing"
        self.deferred = True
        self.deferred_ephemeral = ephemeral

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        assert not self.deferred, "a deferred interaction must answer through followup"
        self.messages.append((content, ephemeral))


class _Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))


class _Interaction:
    """Only the three attributes these commands touch."""

    def __init__(self, guild_id: int | None = GUILD_ID) -> None:
        self.guild_id = guild_id
        self.response = _Response()
        self.followup = _Followup()

    @property
    def reply(self) -> str:
        """The single answer the user actually saw, whichever way it went out."""
        answers = self.response.messages + self.followup.messages
        assert len(answers) == 1, f"expected exactly one reply, got {answers}"
        return answers[0][0]

    @property
    def ephemeral(self) -> bool:
        answers = self.response.messages + self.followup.messages
        assert len(answers) == 1
        return answers[0][1]


class _Store:
    """Stands in for `ConfigStore`, recording *when* it was written to."""

    def __init__(self, *, reject: str | None = None) -> None:
        self.values: dict[str, str | None] = {}
        self.writes: list[tuple[str, str | None]] = []
        self.deferred_before_write: list[bool] = []
        self._reject = reject
        self.watch: _Response | None = None

    async def set(self, _guild_id: int, key: str, value: str | None, _now: datetime) -> None:
        if self._reject is not None:
            raise ValueError(self._reject)
        self.deferred_before_write.append(self.watch.deferred if self.watch else False)
        self.writes.append((key, value))
        self.values[key] = value

    async def get(self, _guild_id: int, key: str) -> str | None:
        return self.values.get(key, settings.DEFAULTS.get(key))

    async def get_stored(self, _guild_id: int, key: str) -> str | None:
        return self.values.get(key)


class _Reconcile:
    """Stands in for the client's reconcile, recording when it was called."""

    def __init__(self, watch: _Response, result: ReconfigureResult) -> None:
        self._watch = watch
        self._result = result
        self.calls: list[bool] = []
        self.guild_ids: list[int] = []
        self.deferred_before_reconcile: list[bool] = []

    async def __call__(self, guild_id: int, *, force: bool = False) -> ReconfigureResult:
        self.guild_ids.append(guild_id)
        self.calls.append(force)
        self.deferred_before_reconcile.append(self._watch.deferred)
        return self._result


def _result(
    action: ReconfigureAction = ReconfigureAction.NOTHING,
    applied_keys: tuple[str, ...] = (),
) -> ReconfigureResult:
    return ReconfigureResult(
        action=action,
        applied_keys=applied_keys,
        deferred_keys=(),
        is_live=True,
        is_recording=False,
        became_live=False,
        session_exceeds_timeouts=False,
    )


def _running_state(guild_id: int) -> RunningState:
    """Only `/config show` reads this, and none of these tests take that path."""
    raise AssertionError(f"running state must not be read here (guild {guild_id})")


def _cog(interaction: _Interaction, store: _Store) -> tuple[ConfigCog, _Reconcile]:
    store.watch = interaction.response
    reconcile = _Reconcile(interaction.response, _result())
    cog = ConfigCog(cast(ConfigStore, store), reconcile, _running_state)
    return cog, reconcile


async def _invoke(cog: ConfigCog, command: str, interaction: _Interaction, *args: str) -> None:
    """Calls one command's own coroutine, bypassing Discord's dispatch.

    `app_commands.Command.callback` is typed as though `self` were already
    bound, but on a cog's command it is the plain function and takes the
    cog -- the same mismatch `test_link_cog.invoke` keeps in one place.
    """
    await getattr(cog, command).callback(cog, interaction, *args)


@pytest.mark.parametrize("command", ["set", "clear"])
async def test_a_writing_command_defers_before_it_does_any_work(command: str) -> None:
    """Discord's three second deadline starts when the interaction arrives.

    The reconcile these commands now await reads the database and can
    build a whole pipeline; on a cold connection pool that alone can miss
    the window, and then the user sees "The application did not respond"
    while the value silently *was* written. Deferring first buys fifteen
    minutes -- which is what `/setup` and `/config apply` already do.
    """
    interaction = _Interaction()
    store = _Store()
    cog, reconcile = _cog(interaction, store)

    values = ("123",) if command == "set" else ()
    await _invoke(cog, command, interaction, settings.VOICE_CHANNEL_ID, *values)

    assert interaction.response.deferred is True
    assert interaction.response.deferred_ephemeral is True
    assert store.deferred_before_write == [True], "the write happens behind the deferral"
    assert reconcile.deferred_before_reconcile == [True]
    assert interaction.followup.messages, "the answer arrives as a followup"
    assert interaction.ephemeral is True
    assert f"`{settings.VOICE_CHANNEL_ID}`" in interaction.reply


async def test_a_rejected_value_still_reaches_the_user_after_the_deferral() -> None:
    """Deferring must not lose the error path: `ConfigStore.set` validates."""
    interaction = _Interaction()
    store = _Store(reject="voice_channel_id must be an integer")
    cog, reconcile = _cog(interaction, store)

    await _invoke(cog, "set", interaction, settings.VOICE_CHANNEL_ID, "not-a-number")

    assert "Rejected: voice_channel_id must be an integer" in interaction.reply
    assert interaction.ephemeral is True
    assert reconcile.calls == [], "a rejected write must not reconcile"


async def test_clearing_an_unknown_key_is_answered_without_deferring() -> None:
    """The key check is a frozenset lookup -- no I/O, so no deadline to dodge."""
    interaction = _Interaction()
    store = _Store()
    cog, _ = _cog(interaction, store)

    await _invoke(cog, "clear", interaction, "voice_chanel_id")

    assert interaction.response.deferred is False
    assert "Unknown configuration key" in interaction.reply
    assert store.writes == []


@pytest.mark.parametrize("command", ["set", "clear"])
async def test_a_writing_command_outside_a_guild_is_refused_without_deferring(
    command: str,
) -> None:
    interaction = _Interaction(guild_id=None)
    store = _Store()
    cog, _ = _cog(interaction, store)

    values = ("123",) if command == "set" else ()
    await _invoke(cog, command, interaction, settings.VOICE_CHANNEL_ID, *values)

    assert interaction.response.deferred is False
    assert "only be used in a server" in interaction.reply
    assert store.writes == []
