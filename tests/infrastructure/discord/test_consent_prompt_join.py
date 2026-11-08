"""The join hook that asks a member for consent in a recording channel (tasks 4.1-4.3, 5.3).

The hook lives on `SturnusClient.on_voice_state_update`, so these tests drive
the real handler with the client, pipeline and fake voice connection from
`test_client`. The prompt's own decisions and its post are exercised through
the same handler: nothing here calls the internals directly.

The post runs as a task the handler starts and does not await, so each test
waits for the tasks its event started with `gather`, never with a sleep. Time
comes from the fake clock; no test reads the system clock or the network.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import ui

from sturnus.application.consent_prompt import PromptRecord
from sturnus.application.recording import RecordingService
from sturnus.domain.consent import ConsentRecord
from sturnus.domain.session import SessionTimeouts
from sturnus.infrastructure.discord import consent_prompt as prompt_module
from sturnus.infrastructure.discord.client import _ChannelRecording
from sturnus.infrastructure.discord.consent_prompt import (
    ConsentPromptServices,
    PromptOutcome,
)
from sturnus.infrastructure.discord.views import CONSENT_STRINGS
from tests.infrastructure.discord.test_client import (
    CHANNEL_ID,
    GUILD_ID,
    ROLE_ID,
    FakeAnnouncer,
    FakeAudioWriterFactory,
    FakeClock,
    FakeEncryptor,
    FakeJobs,
    FakeSessions,
    FakeStore,
    FakeVoiceReceiver,
    _client,
    _configured_store,
    _guild,
    _install,
    _member,
    _runtime_config,
    _voice_channel,
    _voice_state,
)
from tests.infrastructure.discord.test_consent_prompt_message import (
    HOST,
    POLICY,
    POLICY_URL,
    FakeConsents,
    FakeLinks,
    FakeOAuth,
    FakeStates,
)

OTHER_CHANNEL_ID = 25
JOINER = 300
ANNA = 100
BOT = 999
PROMPT_LOG = "sturnus.infrastructure.discord.consent_prompt"
CLIENT_LOG = "sturnus.infrastructure.discord.client"
PERMISSION_TEXT = "Missing Permissions"


class Outcomes:
    """Stands in for the counter: records every outcome the prompt reports."""

    def __init__(self) -> None:
        self.seen: list[PromptOutcome] = []

    def __call__(self, outcome: PromptOutcome) -> None:
        self.seen.append(outcome)


async def _configured_policy(_guild_id: int) -> tuple[str | None, str | None]:
    return POLICY, POLICY_URL


def _services(
    *,
    consents: FakeConsents | None = None,
    links: FakeLinks | None = None,
    policy: Any = None,
) -> tuple[ConsentPromptServices, PromptRecord, Outcomes]:
    prompts = PromptRecord()
    outcomes = Outcomes()
    services = ConsentPromptServices(
        consents=consents or FakeConsents(),
        links=links or FakeLinks(),
        link_states=FakeStates(),
        oauth=FakeOAuth(),
        clock=FakeClock(datetime(2026, 11, 8, 20, 0, tzinfo=UTC)),
        policy=policy or _configured_policy,
        transcription_host=HOST,
        consent_role=AsyncMock(return_value=None),
        prompts=prompts,
        count=outcomes,
    )
    return services, prompts, outcomes


def _active() -> ConsentRecord:
    return ConsentRecord(
        granted_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC), revoked_at=None, policy_version=POLICY
    )


def _channel(
    channel_id: int,
    members: list[discord.Member],
    *,
    send: AsyncMock | None = None,
) -> Any:
    channel: Any = _voice_channel(channel_id, members=members)
    channel.send = send if send is not None else AsyncMock(return_value=MagicMock())
    return channel


def _human(member_id: int, guild: MagicMock, role_ids: list[int] | None = None) -> Any:
    member: Any = _member(member_id, guild, role_ids=role_ids or [])
    member.bot = False
    return member


def _bot(member_id: int, guild: MagicMock) -> Any:
    member: Any = _member(member_id, guild, role_ids=[])
    member.bot = True
    return member


def _response(status: int) -> Any:
    """The one attribute discord.py reads from a response when it builds an error."""
    return SimpleNamespace(status=status, reason="x")


def _refusal() -> AsyncMock:
    """A post Discord refuses for lack of permission."""
    return AsyncMock(side_effect=discord.Forbidden(_response(403), PERMISSION_TEXT))


class Harness:
    """A client with one recording pipeline serving CHANNEL_ID and OTHER_CHANNEL_ID.

    Built the way `test_client` builds its clients, so a join that starts a
    session goes through the real participant sync. The prompt services are
    attached only when a test asks for them through `attach`.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock(datetime(2026, 8, 19, 20, 0, tzinfo=UTC))
        self.sessions = FakeSessions()
        self.service = RecordingService(
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            timeouts=SessionTimeouts(
                empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
            ),
            sessions=self.sessions,
            jobs=FakeJobs(),
            store=FakeStore(),
            writers=FakeAudioWriterFactory(tmp_path),
            encryptor=FakeEncryptor(),
            announcer=FakeAnnouncer(),
            retention_days=30,
        )
        self.voice = FakeVoiceReceiver()
        self.client = _client(self.clock, config_store=_configured_store())
        _install(
            self.client,
            _ChannelRecording(
                config=_runtime_config(channel_ids=(CHANNEL_ID, OTHER_CHANNEL_ID)),
                service=self.service,
                voice=self.voice,
            ),
        )
        self.guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))

    def attach(self, services: ConsentPromptServices) -> None:
        self.client.consent_prompt_services = services

    async def join(
        self,
        member: discord.Member,
        *,
        into: MagicMock,
        frm: MagicMock | None = None,
    ) -> None:
        await self.client.on_voice_state_update(member, _voice_state(frm), _voice_state(into))

    async def leave(self, member: discord.Member, *, frm: MagicMock) -> None:
        await self.client.on_voice_state_update(member, _voice_state(frm), _voice_state(None))

    def pending(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self.client._consent_prompt_tasks)


async def _settled(harness: Harness) -> None:
    """Waits for the prompt tasks the last event started. Never sleeps."""
    await asyncio.gather(*harness.pending())


# 4.1 The join hook: who is prompted, and where


async def test_a_member_joining_a_recording_channel_without_consent_is_pinged_there(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    channel.send.assert_awaited_once()


async def test_the_prompt_is_posted_with_no_content_and_pings_only_the_joiner(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    kwargs = channel.send.await_args.kwargs
    assert "content" not in kwargs
    mentions = kwargs["allowed_mentions"]
    assert [user.id for user in mentions.users] == [JOINER]
    assert mentions.everyone is False


async def test_a_bot_joining_a_recording_channel_is_not_pinged(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_bot(BOT, harness.guild), into=channel)
    await _settled(harness)

    channel.send.assert_not_awaited()


async def test_a_member_with_consent_and_a_link_is_not_pinged(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    services, _, _ = _services(
        consents=FakeConsents(record=_active()),
        links=FakeLinks(identity=("ext-1", "Anna")),
    )
    harness.attach(services)
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    channel.send.assert_not_awaited()


async def test_a_member_with_consent_but_no_link_is_pinged_for_the_link_only(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services(consents=FakeConsents(record=_active()))[0])
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    view = channel.send.await_args.kwargs["view"]
    assert isinstance(view, ui.LayoutView)
    custom_ids = [
        str(item.item.custom_id)
        for item in view.walk_children()
        if isinstance(item, ui.DynamicItem)
    ]
    assert custom_ids == ["sturnus:consent-prompt:link"]


async def test_a_member_moving_into_a_recording_channel_without_a_record_is_pinged(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    other = _channel(OTHER_CHANNEL_ID, members=[])
    channel = _channel(CHANNEL_ID, members=[])
    joiner = _human(JOINER, harness.guild)

    await harness.join(joiner, into=other)
    await _settled(harness)
    await harness.join(joiner, into=channel, frm=other)
    await _settled(harness)

    channel.send.assert_awaited_once()


async def test_a_member_moving_into_a_channel_already_prompted_is_not_pinged_again(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    services, prompts, _ = _services()
    harness.attach(services)
    prompts.mark(CHANNEL_ID, JOINER)
    other = _channel(OTHER_CHANNEL_ID, members=[])
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel, frm=other)
    await _settled(harness)

    channel.send.assert_not_awaited()


async def test_a_member_who_rejoins_while_a_human_stays_is_not_pinged_again(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    anna = _human(ANNA, harness.guild, role_ids=[ROLE_ID])
    joiner = _human(JOINER, harness.guild)
    channel = _channel(CHANNEL_ID, members=[anna])

    await harness.join(joiner, into=channel)
    await _settled(harness)
    await harness.leave(joiner, frm=channel)
    await _settled(harness)
    await harness.join(joiner, into=channel)
    await _settled(harness)

    assert channel.send.await_count == 1


async def test_the_channel_record_is_cleared_when_its_last_human_leaves(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    joiner = _human(JOINER, harness.guild)
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(joiner, into=channel)
    await _settled(harness)
    # The joiner has left and no human is left in the channel, so the record is cleared.
    await harness.leave(joiner, frm=channel)
    await harness.join(joiner, into=channel)
    await _settled(harness)

    assert channel.send.await_count == 2


async def test_the_channel_record_is_kept_while_another_human_stays(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    joiner = _human(JOINER, harness.guild)
    anna = _human(ANNA, harness.guild, role_ids=[ROLE_ID])
    channel = _channel(CHANNEL_ID, members=[anna])

    await harness.join(joiner, into=channel)
    await _settled(harness)
    await harness.leave(joiner, frm=channel)
    await harness.join(joiner, into=channel)
    await _settled(harness)

    assert channel.send.await_count == 1


async def test_a_bot_leaving_an_empty_channel_clears_the_record_of_its_prompts(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    services, prompts, _ = _services()
    harness.attach(services)
    prompts.mark(CHANNEL_ID, JOINER)
    empty = _channel(CHANNEL_ID, members=[])

    await harness.leave(_bot(BOT, harness.guild), frm=empty)

    assert prompts.should_prompt(CHANNEL_ID, JOINER)


async def test_nothing_is_posted_when_the_guild_has_no_policy(tmp_path: Path) -> None:
    async def unconfigured(_guild_id: int) -> tuple[str | None, str | None]:
        return None, None

    harness = Harness(tmp_path)
    services, _, outcomes = _services(policy=unconfigured)
    harness.attach(services)
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    channel.send.assert_not_awaited()
    assert outcomes.seen == []


async def test_no_prompt_task_starts_when_no_services_are_attached(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    channel = _channel(CHANNEL_ID, members=[])

    await harness.join(_human(JOINER, harness.guild), into=channel)

    channel.send.assert_not_awaited()
    assert harness.pending() == ()


# 4.2 Posting is guarded and kept off the sync path


async def test_a_permission_error_on_posting_does_not_raise_into_the_voice_event(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[], send=_refusal())

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    channel.send.assert_awaited_once()


async def test_a_consenting_members_recording_starts_when_the_prompt_post_fails(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    anna = _human(ANNA, harness.guild, role_ids=[ROLE_ID])
    channel = _channel(CHANNEL_ID, members=[anna], send=_refusal())
    harness.guild.get_channel.return_value = channel

    await harness.join(anna, into=channel)
    await _settled(harness)
    assert harness.service.is_recording is True

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    assert harness.service.is_recording is True
    assert harness.sessions.opened == [1]


async def test_the_voice_handler_does_not_wait_for_a_post_that_never_completes(
    tmp_path: Path,
) -> None:
    """A post that never returns must not hold the handler, or the recording behind it."""
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    never: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    channel = _channel(CHANNEL_ID, members=[], send=AsyncMock(return_value=never))

    await asyncio.wait_for(harness.join(_human(JOINER, harness.guild), into=channel), timeout=5)

    assert len(harness.pending()) == 1
    for task in harness.pending():
        task.cancel()


async def test_a_permission_error_is_logged_with_the_guild_and_channel_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[], send=_refusal())
    caplog.set_level(logging.DEBUG, logger=PROMPT_LOG)

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    warnings = [
        record
        for record in caplog.records
        if record.name == PROMPT_LOG and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert warnings[0].args == ("no_permission", GUILD_ID, CHANNEL_ID, JOINER)


async def test_a_non_permission_http_failure_is_logged_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    failed = AsyncMock(side_effect=discord.HTTPException(_response(500), "boom"))
    channel = _channel(CHANNEL_ID, members=[], send=failed)
    caplog.set_level(logging.DEBUG, logger=PROMPT_LOG)

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    logged = [record.args for record in caplog.records if record.name == PROMPT_LOG]
    assert ("post_failed", GUILD_ID, CHANNEL_ID, JOINER) in logged


# 4.3 Logs carry identifiers and outcome labels only


async def test_the_prompt_logs_carry_only_identifiers_and_outcome_labels(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[], send=_refusal())
    caplog.set_level(logging.DEBUG)

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    records = [record for record in caplog.records if record.name in {PROMPT_LOG, CLIENT_LOG}]
    assert records
    for record in records:
        rendered = record.getMessage()
        assert "://" not in rendered
        assert "state" not in rendered.lower()
        assert CONSENT_STRINGS["description"][:20] not in rendered
        assert "I agree" not in rendered
        assert record.args is None or isinstance(record.args, tuple)


async def test_every_prompt_log_line_is_formatted_lazily(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The message is a constant with `%s` placeholders, never an f-string."""
    harness = Harness(tmp_path)
    harness.attach(_services()[0])
    channel = _channel(CHANNEL_ID, members=[], send=_refusal())
    caplog.set_level(logging.DEBUG, logger=PROMPT_LOG)

    await harness.join(_human(JOINER, harness.guild), into=channel)
    await _settled(harness)

    prompt_records = [record for record in caplog.records if record.name == PROMPT_LOG]
    assert prompt_records
    for record in prompt_records:
        assert isinstance(record.msg, str)
        assert record.msg.count("%s") == len(record.args or ())


# 5.3 The outcome counter: fixed labels, incremented at the matching points


def test_the_outcome_labels_are_the_fixed_set() -> None:
    assert {outcome.value for outcome in PromptOutcome} == {
        "posted",
        "skipped_consented",
        "skipped_already_pinged",
        "no_permission",
        "consented_via_prompt",
        "link_started",
    }


async def test_posting_the_prompt_counts_posted(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    services, _, outcomes = _services()
    harness.attach(services)

    await harness.join(_human(JOINER, harness.guild), into=_channel(CHANNEL_ID, members=[]))
    await _settled(harness)

    assert outcomes.seen == [PromptOutcome.POSTED]


async def test_a_member_who_already_consents_and_links_counts_skipped_consented(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    services, _, outcomes = _services(
        consents=FakeConsents(record=_active()),
        links=FakeLinks(identity=("ext-1", "Anna")),
    )
    harness.attach(services)

    await harness.join(_human(JOINER, harness.guild), into=_channel(CHANNEL_ID, members=[]))
    await _settled(harness)

    assert outcomes.seen == [PromptOutcome.SKIPPED_CONSENTED]


async def test_a_member_already_prompted_in_the_channel_counts_skipped_already_pinged(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    services, prompts, outcomes = _services()
    harness.attach(services)
    prompts.mark(CHANNEL_ID, JOINER)

    await harness.join(_human(JOINER, harness.guild), into=_channel(CHANNEL_ID, members=[]))
    await _settled(harness)

    assert outcomes.seen == [PromptOutcome.SKIPPED_ALREADY_PINGED]


class _GatedPost:
    """Holds the first post open until the test releases it; later posts return at once."""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, **_kwargs: object) -> MagicMock:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return MagicMock()


async def test_two_quick_joins_by_the_same_member_post_one_prompt() -> None:
    services, _, _ = _services()
    guild = _guild(GUILD_ID, _voice_channel(CHANNEL_ID, members=[]))
    member = _human(JOINER, guild)
    post = _GatedPost()
    channel = _channel(CHANNEL_ID, members=[])
    channel.send = post

    first = asyncio.create_task(
        prompt_module.prompt_joining_member(services, member=member, channel=channel)
    )
    await post.entered.wait()
    # The member rejoins while the first prompt is still being posted.
    await prompt_module.prompt_joining_member(services, member=member, channel=channel)
    post.release.set()
    await first

    assert post.calls == 1


async def test_a_refused_post_counts_no_permission(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    services, _, outcomes = _services()
    harness.attach(services)

    await harness.join(
        _human(JOINER, harness.guild), into=_channel(CHANNEL_ID, members=[], send=_refusal())
    )
    await _settled(harness)

    assert outcomes.seen == [PromptOutcome.NO_PERMISSION]


def test_the_counter_is_labelled_by_outcome_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """No person, channel or guild identifier can reach the counter's attributes."""
    added: list[tuple[float, dict[str, object]]] = []

    class FakeInstrument:
        def add(self, amount: float, attributes: dict[str, object]) -> None:
            added.append((amount, attributes))

    monkeypatch.setattr(prompt_module, "CONSENT_PROMPT_TOTAL", FakeInstrument())

    prompt_module.count_consent_prompt(PromptOutcome.POSTED)

    assert added == [(1, {"outcome": "posted"})]
