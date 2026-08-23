"""The bot applying what the console asked for, on its ordinary tick.

`api` holds no Discord token, so the console writes a row saying what
should be true and this makes it true. What is tested here is the adapter
around that: that the newest ask wins and the rest settle without being
applied, that the consent protection reaches every allowed channel through
the same `plan_setup` the slash command uses, that an attempt settles the
intent whichever way it went, and that a failure says something an
administrator can act on.

The decision about *which* intent is applied is tested without a guild in
`tests/domain/test_onboarding.py`; what is decided about the configuration
is tested in `tests/application/test_setup_plan.py`. Neither is repeated
here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import discord
import pytest

from sturnus.domain import settings
from sturnus.domain.onboarding import APPLIED, FAILED, SUPERSEDED, SetupIntent
from sturnus.infrastructure.discord.setup_apply import apply_setup_intents

GUILD_ID = 1
ANNA, BEN = 100, 200
EVERYONE_ID = GUILD_ID
STANDUP, RETRO = 10, 11
STORED_ROLE_ID, NAMED_ROLE_ID, CREATED_ROLE_ID = 41, 42, 43
T0 = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeConfig:
    """`guild_config`, as narrowly as the applier reads and writes it."""

    def __init__(self, **stored: str | None) -> None:
        self.values: dict[str, str | None] = dict(stored)
        self.writes: list[tuple[str, str | None]] = []

    async def get_stored(self, _guild_id: int, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, _guild_id: int, key: str, value: str | None, _now: datetime) -> None:
        self.values[key] = value
        self.writes.append((key, value))


class FakeIntents:
    """The intent table, remembering every settlement in order."""

    def __init__(self, *pending: SetupIntent) -> None:
        self._pending = list(pending)
        #: `(intent_id, outcome, error)`, in the order they were settled.
        self.settled: list[tuple[int, str, str | None]] = []
        #: What `record_outcome` answers -- `False` is the losing tick.
        self.settles = True

    async def pending_for(self, _guild_id: int) -> Sequence[SetupIntent]:
        return tuple(self._pending)

    async def record_outcome(
        self, intent_id: int, *, outcome: str, error: str | None, now: datetime
    ) -> bool:
        assert now == T0
        self.settled.append((intent_id, outcome, error))
        return self.settles

    def outcome_of(self, intent_id: int) -> tuple[str, str | None]:
        (settled,) = [each for each in self.settled if each[0] == intent_id]
        return settled[1], settled[2]


def an_intent(
    intent_id: int = 1,
    *,
    at: datetime = T0,
    by: int = ANNA,
    channel_ids: str | None = str(STANDUP),
    consent_role_name: str | None = None,
) -> SetupIntent:
    return SetupIntent(
        id=intent_id,
        guild_id=GUILD_ID,
        requested_by=by,
        requested_at=at,
        channel_ids=channel_ids,
        consent_role_name=consent_role_name,
        applied_at=None,
        outcome=None,
        error=None,
    )


def a_role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    return role


def forbidden(message: str = "Missing Permissions") -> discord.Forbidden:
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, message)


class Channel:
    """One voice channel's Speak overwrites, as state a test can read back.

    Wrapped in a `MagicMock(spec=discord.VoiceChannel)` rather than being
    one, because the applier asks `isinstance` before it touches a channel
    -- a stored list can name something that is not a voice channel at
    all, and answering that honestly is half of what `resolve` is for.
    """

    def __init__(self, channel_id: int, name: str, **speak: bool | None) -> None:
        self.id = channel_id
        self.name = name
        #: Target role id -> what its `speak` overwrite currently says.
        self.speak: dict[int, bool | None] = {
            int(target): allowed for target, allowed in speak.items()
        }
        self.refuse: discord.DiscordException | None = None
        #: `(target_id, speak, reason)` for every edit that got through.
        self.edits: list[tuple[int, bool | None, str]] = []

    def overwrites_for(self, target: MagicMock) -> discord.PermissionOverwrite:
        overwrite = discord.PermissionOverwrite()
        overwrite.update(speak=self.speak.get(target.id))
        return overwrite

    async def set_permissions(
        self, target: MagicMock, *, overwrite: discord.PermissionOverwrite, reason: str
    ) -> None:
        if self.refuse is not None:
            raise self.refuse
        self.speak[target.id] = overwrite.speak
        self.edits.append((target.id, overwrite.speak, reason))

    def as_discord(self) -> MagicMock:
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = self.id
        channel.name = self.name
        channel.mention = f"<#{self.id}>"
        channel.overwrites_for = self.overwrites_for
        channel.set_permissions = self.set_permissions
        return channel


def a_guild(
    *,
    channels: Sequence[Channel] = (),
    roles: Sequence[MagicMock] = (),
    creates: MagicMock | None = None,
    create_raises: discord.DiscordException | None = None,
) -> MagicMock:
    everyone = a_role(EVERYONE_ID, "@everyone")
    resolved = {channel.id: channel.as_discord() for channel in channels}
    by_id = {role.id: role for role in roles}

    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD_ID
    guild.default_role = everyone
    guild.roles = [everyone, *roles]
    guild.get_channel = resolved.get
    guild.get_role = by_id.get

    async def create_role(*, name: str, reason: str) -> MagicMock:
        del reason
        if create_raises is not None:
            raise create_raises
        created = creates if creates is not None else a_role(CREATED_ROLE_ID, name)
        created.name = name
        guild.roles.append(created)
        return created

    guild.create_role = create_role
    return guild


# ---------------------------------------------------------------------------
# Nothing to do
# ---------------------------------------------------------------------------


async def test_a_guild_nobody_asked_about_is_left_entirely_alone() -> None:
    """Every tick but the handful after somebody presses the button."""
    config = FakeConfig()
    intents = FakeIntents()

    await apply_setup_intents(a_guild(), config, intents, T0)

    assert config.writes == []
    assert intents.settled == []


# ---------------------------------------------------------------------------
# The consent protection
# ---------------------------------------------------------------------------


async def test_the_channels_asked_for_are_added_and_the_overwrites_written() -> None:
    """The whole request, on a guild that has never been configured.

    Deny Speak to `@everyone`, allow it for the consent role, on the
    channel that was asked for -- the primary layer of the consent
    protection (Spec 3.1), applied by the same code `/setup` applies it
    with.
    """
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup])
    config = FakeConfig(**{key: None for key in settings.REQUIRED_KEYS})
    intents = FakeIntents(an_intent(channel_ids=str(STANDUP), consent_role_name="Recorded"))

    await apply_setup_intents(guild, config, intents, T0)

    assert config.values[settings.VOICE_CHANNEL_IDS] == str(STANDUP)
    assert standup.speak[EVERYONE_ID] is False
    assert standup.speak[CREATED_ROLE_ID] is True
    assert intents.outcome_of(1) == (APPLIED, None)


async def test_every_allowed_channel_is_protected_not_only_the_ones_just_named() -> None:
    """A channel already on the list whose `@everyone` may still Speak is a
    hole in the protection whichever request added it."""
    standup, retro = Channel(STANDUP, "Standup"), Channel(RETRO, "Retro")
    guild = a_guild(channels=[standup, retro], roles=[a_role(STORED_ROLE_ID, "Recorded")])
    config = FakeConfig(
        **{
            settings.VOICE_CHANNEL_IDS: str(RETRO),
            settings.CONSENT_ROLE_ID: str(STORED_ROLE_ID),
        }
    )
    intents = FakeIntents(an_intent(channel_ids=str(STANDUP)))

    await apply_setup_intents(guild, config, intents, T0)

    assert config.values[settings.VOICE_CHANNEL_IDS] == f"{STANDUP},{RETRO}"
    for channel in (standup, retro):
        assert channel.speak[EVERYONE_ID] is False
        assert channel.speak[STORED_ROLE_ID] is True


async def test_a_guild_already_configured_correctly_is_not_rewritten() -> None:
    """Asking twice is safe: the plan against a correct guild is empty."""
    standup = Channel(STANDUP, "Standup", **{str(EVERYONE_ID): False, str(STORED_ROLE_ID): True})
    guild = a_guild(channels=[standup], roles=[a_role(STORED_ROLE_ID, "Recorded")])
    config = FakeConfig(
        **{
            settings.VOICE_CHANNEL_IDS: str(STANDUP),
            settings.CONSENT_ROLE_ID: str(STORED_ROLE_ID),
        }
    )
    intents = FakeIntents(an_intent(channel_ids=str(STANDUP)))

    await apply_setup_intents(guild, config, intents, T0)

    assert config.writes == []
    assert standup.edits == []
    assert intents.outcome_of(1) == (APPLIED, None)


async def test_the_audit_reason_names_the_specification_section() -> None:
    """Somebody reading a guild's audit log a year from now gets a why."""
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup])
    intents = FakeIntents(an_intent())

    await apply_setup_intents(guild, FakeConfig(), intents, T0)

    assert all("Spec 3.1" in reason for _target, _speak, reason in standup.edits)


# ---------------------------------------------------------------------------
# The consent role
# ---------------------------------------------------------------------------


async def test_a_role_the_console_named_is_reused_rather_than_duplicated() -> None:
    """Asking twice for "Recorded" must not leave two roles called Recorded.

    The second one would grant recording consent to a role nobody was told
    about, and the overwrites would name whichever the code picked.
    """
    existing = a_role(NAMED_ROLE_ID, "Recorded")
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup], roles=[existing])
    config = FakeConfig()
    intents = FakeIntents(an_intent(consent_role_name="Recorded"))

    await apply_setup_intents(guild, config, intents, T0)

    assert config.values[settings.CONSENT_ROLE_ID] == str(NAMED_ROLE_ID)
    assert standup.speak[NAMED_ROLE_ID] is True


async def test_a_role_the_console_named_and_the_guild_lacks_is_created_under_that_name() -> None:
    """The name is the request: the role does not exist yet."""
    standup = Channel(STANDUP, "Standup")
    created = a_role(CREATED_ROLE_ID, "placeholder")
    guild = a_guild(channels=[standup], creates=created)
    config = FakeConfig()
    intents = FakeIntents(an_intent(consent_role_name="Being recorded"))

    await apply_setup_intents(guild, config, intents, T0)

    assert created.name == "Being recorded"
    assert config.values[settings.CONSENT_ROLE_ID] == str(CREATED_ROLE_ID)


async def test_naming_a_role_replaces_a_stored_one_rather_than_keeping_it() -> None:
    """An administrator who typed a name meant that role, not the old one."""
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup], roles=[a_role(STORED_ROLE_ID, "Old")])
    config = FakeConfig(**{settings.CONSENT_ROLE_ID: str(STORED_ROLE_ID)})
    intents = FakeIntents(an_intent(consent_role_name="New"))

    await apply_setup_intents(guild, config, intents, T0)

    assert config.values[settings.CONSENT_ROLE_ID] == str(CREATED_ROLE_ID)


async def test_naming_no_role_keeps_the_one_the_guild_already_has() -> None:
    """Omitting the name must never be the destructive path (Spec 10.1).

    A request that quietly swapped a working consent role for a fresh,
    empty one would revoke everybody's consent as a side effect of
    somebody ticking a channel.
    """
    stored = a_role(STORED_ROLE_ID, "Recorded")
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup], roles=[stored])
    config = FakeConfig(**{settings.CONSENT_ROLE_ID: str(STORED_ROLE_ID)})
    intents = FakeIntents(an_intent(consent_role_name=None))

    await apply_setup_intents(guild, config, intents, T0)

    assert config.values[settings.CONSENT_ROLE_ID] == str(STORED_ROLE_ID)
    assert standup.speak[STORED_ROLE_ID] is True


async def test_a_stored_role_deleted_out_from_under_the_id_is_replaced() -> None:
    """A stale id grants nobody anything; writing overwrites for it would
    look like the protection was applied when it was not."""
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup])
    config = FakeConfig(**{settings.CONSENT_ROLE_ID: str(STORED_ROLE_ID)})
    intents = FakeIntents(an_intent())

    await apply_setup_intents(guild, config, intents, T0)

    assert config.values[settings.CONSENT_ROLE_ID] == str(CREATED_ROLE_ID)
    assert standup.speak[CREATED_ROLE_ID] is True


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------


async def test_two_requests_leave_one_application_and_one_superseded_row() -> None:
    """The rule: two administrators asking are not a queue of two jobs.

    Applying both in sequence would finish on the older list, which is the
    correction being overwritten by the mistake it corrected.
    """
    standup, retro = Channel(STANDUP, "Standup"), Channel(RETRO, "Retro")
    guild = a_guild(channels=[standup, retro])
    config = FakeConfig()
    intents = FakeIntents(
        an_intent(1, at=T0, by=ANNA, channel_ids=str(STANDUP)),
        an_intent(2, at=T0 + timedelta(seconds=30), by=BEN, channel_ids=str(RETRO)),
    )

    await apply_setup_intents(guild, config, intents, T0)

    assert intents.outcome_of(1) == (SUPERSEDED, None)
    assert intents.outcome_of(2)[0] == APPLIED
    assert config.values[settings.VOICE_CHANNEL_IDS] == str(RETRO)


async def test_a_superseded_request_is_never_acted_on() -> None:
    """It settles, and the guild never sees what it asked for."""
    standup, retro = Channel(STANDUP, "Standup"), Channel(RETRO, "Retro")
    guild = a_guild(channels=[standup, retro])
    intents = FakeIntents(
        an_intent(1, at=T0, channel_ids=str(STANDUP)),
        an_intent(2, at=T0 + timedelta(seconds=30), channel_ids=str(RETRO)),
    )

    await apply_setup_intents(guild, FakeConfig(), intents, T0)

    assert standup.edits == []
    assert retro.edits != []


# ---------------------------------------------------------------------------
# Settling, exactly once
# ---------------------------------------------------------------------------


async def test_a_missing_permission_settles_the_intent_rather_than_retrying() -> None:
    """The retry bound is one attempt, and it is the whole design.

    The tick runs six times a minute forever. An intent left pending after
    failing would retry a permission error against Discord's rate limiter
    just as often -- so the attempt settles it, the reason is recorded,
    and an administrator who has fixed the permission asks again.
    """
    standup = Channel(STANDUP, "Standup")
    standup.refuse = forbidden()
    guild = a_guild(channels=[standup])
    intents = FakeIntents(an_intent())

    await apply_setup_intents(guild, FakeConfig(), intents, T0)

    outcome, error = intents.outcome_of(1)
    assert outcome == FAILED
    assert error is not None
    assert "Manage Permissions" in error


async def test_a_role_that_could_not_be_created_says_what_to_do_about_it() -> None:
    """Role position is the one thing no permission bitmask expresses.

    The bot's own role has to sit above the consent role or Discord
    refuses the edit, which is why the message says so rather than
    quoting a 403.
    """
    guild = a_guild(channels=[Channel(STANDUP, "Standup")], create_raises=forbidden())
    intents = FakeIntents(an_intent(consent_role_name="Recorded"))

    await apply_setup_intents(guild, FakeConfig(), intents, T0)

    outcome, error = intents.outcome_of(1)
    assert outcome == FAILED
    assert error is not None
    assert "Manage Roles" in error
    assert "above" in error


async def test_what_could_be_configured_is_still_written_when_discord_refuses() -> None:
    """A failure is "not everything happened", not "nothing happened".

    Refusing to store what the bot did determine because a permission edit
    failed would leave the guild worse off than before the request.
    """
    standup = Channel(STANDUP, "Standup")
    standup.refuse = forbidden()
    guild = a_guild(channels=[standup])
    config = FakeConfig()

    await apply_setup_intents(guild, config, FakeIntents(an_intent()), T0)

    assert config.values[settings.VOICE_CHANNEL_IDS] == str(STANDUP)


async def test_a_channel_the_request_named_and_the_bot_cannot_see_is_not_added() -> None:
    """The console picks from a mirror, and a mirror can be behind Discord.

    Adding the room anyway would put it on the allowed list with nobody's
    Speak overwrites written -- a hole in the consent protection that
    looks exactly like a configured guild.
    """
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup])
    config = FakeConfig()
    intents = FakeIntents(an_intent(channel_ids=f"{STANDUP},{RETRO}"))

    await apply_setup_intents(guild, config, intents, T0)

    outcome, error = intents.outcome_of(1)
    assert outcome == FAILED
    assert error is not None
    assert str(RETRO) in error
    assert config.values[settings.VOICE_CHANNEL_IDS] == str(STANDUP)
    assert standup.speak[EVERYONE_ID] is False


async def test_a_stored_channel_that_has_since_vanished_does_not_fail_the_request() -> None:
    """It is not this request's doing.

    Failing every future setup over a channel somebody deleted last week
    would leave the guild unable to change anything until an administrator
    hand-edited `voice_channel_ids`.
    """
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup])
    config = FakeConfig(**{settings.VOICE_CHANNEL_IDS: f"{STANDUP},{RETRO}"})
    intents = FakeIntents(an_intent(channel_ids=str(STANDUP)))

    await apply_setup_intents(guild, config, intents, T0)

    assert intents.outcome_of(1) == (APPLIED, None)
    assert standup.speak[EVERYONE_ID] is False


async def test_a_channel_list_nothing_can_read_fails_without_touching_the_guild() -> None:
    """Only reachable through a hand-written row -- the API validates
    before it writes -- and there is nothing the bot can do with it."""
    standup = Channel(STANDUP, "Standup")
    guild = a_guild(channels=[standup])
    intents = FakeIntents(an_intent(channel_ids="not-a-snowflake"))

    await apply_setup_intents(guild, FakeConfig(), intents, T0)

    assert intents.outcome_of(1)[0] == FAILED
    assert standup.edits == []


async def test_the_tick_that_loses_the_race_says_nothing() -> None:
    """`record_outcome` is conditional on the intent still being unapplied.

    Two ticks racing on one guild produce one application and one honest
    `False`; the loser must not log a second answer for the same request.
    """
    guild = a_guild(channels=[Channel(STANDUP, "Standup")])
    intents = FakeIntents(an_intent())
    intents.settles = False

    # No assertion beyond "this returns rather than raising": what the
    # loser must do is nothing at all.
    await apply_setup_intents(guild, FakeConfig(), intents, T0)

    assert [outcome for _id, outcome, _error in intents.settled] == [APPLIED]


@pytest.mark.parametrize("outcome", [APPLIED, FAILED, SUPERSEDED])
def test_every_outcome_this_module_writes_is_one_the_table_accepts(outcome: str) -> None:
    """A guard on the one thing a database enum would have caught."""
    from sturnus.domain.onboarding import OUTCOMES

    assert outcome in OUTCOMES
