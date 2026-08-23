"""The bot writing down the names `api` is not allowed to ask Discord for.

The decisions are tested in `tests/application/test_directory_mirror.py`;
what is tested here is the adapter around them -- that the gateway is read
for channels, roles and exactly the two role memberships, that what comes
back reaches the store, and that the guild mid-`/setup` is skipped rather
than written empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from unittest.mock import MagicMock

import discord

from sturnus.application.directory_mirror import (
    TEXT,
    VOICE,
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.domain import settings
from sturnus.infrastructure.discord.directory_sync import sync_directory

GUILD_ID = 1
CONSENT_ROLE_ID, ADMIN_ROLE_ID = 41, 42
ANNA, BEN, CARA = 100, 200, 300
T0 = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


class FakeStore:
    def __init__(self) -> None:
        self.channels: list[tuple[int, list[MirroredChannel]]] = []
        self.roles: list[tuple[int, list[MirroredRole]]] = []
        self.members: list[tuple[int, list[MirroredMember]]] = []

    async def replace_channels(
        self, guild_id: int, channels: Sequence[MirroredChannel], _now: datetime
    ) -> None:
        self.channels.append((guild_id, list(channels)))

    async def replace_roles(
        self, guild_id: int, roles: Sequence[MirroredRole], _now: datetime
    ) -> None:
        self.roles.append((guild_id, list(roles)))

    async def replace_members(
        self, guild_id: int, members: Sequence[MirroredMember], _now: datetime
    ) -> None:
        self.members.append((guild_id, list(members)))


class FakeConfig:
    def __init__(self, consent: str | None = None, admin: str | None = None) -> None:
        self._values = {settings.CONSENT_ROLE_ID: consent, settings.ADMIN_ROLE_ID: admin}

    async def get(self, _guild_id: int, key: str) -> str | None:
        return self._values.get(key)


def _member(discord_user_id: int, display_name: str) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = discord_user_id
    member.display_name = display_name
    return member


def _role_object(role_id: int, name: str, position: int, *members: MagicMock) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.position = position
    role.members = list(members)
    return role


def _channel_object(channel_id: int, name: str, position: int) -> MagicMock:
    channel = MagicMock()
    channel.id = channel_id
    channel.name = name
    channel.position = position
    return channel


def _guild(
    *,
    voice: Sequence[MagicMock] = (),
    text: Sequence[MagicMock] = (),
    roles: Sequence[MagicMock] = (),
) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD_ID
    guild.voice_channels = list(voice)
    guild.text_channels = list(text)
    guild.roles = list(roles)
    by_id = {role.id: role for role in roles}
    guild.get_role = MagicMock(side_effect=by_id.get)
    return guild


async def test_the_guilds_voice_and_text_channels_are_both_mirrored() -> None:
    """`voice_channel_id` names a voice channel and the announcement lands
    in a text one, so a console that could name only one kind would still
    be showing a raw snowflake somewhere.
    """
    store = FakeStore()
    guild = _guild(
        voice=[_channel_object(10, "meeting", 1)],
        text=[_channel_object(11, "general", 2)],
    )
    await sync_directory(guild, FakeConfig(), store, T0)
    assert store.channels == [
        (
            GUILD_ID,
            [
                MirroredChannel(channel_id=10, name="meeting", kind=VOICE, position=1),
                MirroredChannel(channel_id=11, name="general", kind=TEXT, position=2),
            ],
        )
    ]


async def test_a_guild_with_no_channels_at_all_is_still_written() -> None:
    """Empty is a fact the mirror must be able to record -- otherwise the
    last channel to be deleted goes on being offered forever.
    """
    store = FakeStore()
    await sync_directory(_guild(), FakeConfig(), store, T0)
    assert store.channels == [(GUILD_ID, [])]


async def test_every_role_of_the_guild_is_mirrored() -> None:
    store = FakeStore()
    guild = _guild(roles=[_role_object(CONSENT_ROLE_ID, "recorded", 3)])
    await sync_directory(guild, FakeConfig(), store, T0)
    assert store.roles == [
        (GUILD_ID, [MirroredRole(role_id=CONSENT_ROLE_ID, name="recorded", position=3)])
    ]


async def test_only_the_holders_of_the_two_naming_roles_are_mirrored() -> None:
    """The bound that keeps this from becoming a copy of Discord's user
    directory: a member of the guild who holds neither role is somebody
    the console never names, and so somebody this table never holds.
    """
    consent = _role_object(CONSENT_ROLE_ID, "recorded", 1, _member(ANNA, "Anna"))
    admin = _role_object(ADMIN_ROLE_ID, "staff", 2, _member(BEN, "Ben"))
    guild = _guild(roles=[consent, admin])
    store = FakeStore()

    await sync_directory(guild, FakeConfig(str(CONSENT_ROLE_ID), str(ADMIN_ROLE_ID)), store, T0)

    assert store.members == [
        (
            GUILD_ID,
            [
                MirroredMember(discord_user_id=ANNA, display_name="Anna"),
                MirroredMember(discord_user_id=BEN, display_name="Ben"),
            ],
        )
    ]


async def test_somebody_in_both_roles_is_written_once() -> None:
    consent = _role_object(CONSENT_ROLE_ID, "recorded", 1, _member(ANNA, "Anna"))
    admin = _role_object(ADMIN_ROLE_ID, "staff", 2, _member(ANNA, "Anna"))
    store = FakeStore()

    await sync_directory(
        _guild(roles=[consent, admin]),
        FakeConfig(str(CONSENT_ROLE_ID), str(ADMIN_ROLE_ID)),
        store,
        T0,
    )

    assert store.members == [
        (GUILD_ID, [MirroredMember(discord_user_id=ANNA, display_name="Anna")])
    ]


async def test_a_guild_that_configured_neither_role_has_no_names_written() -> None:
    """Skip, not clear -- the same distinction `sync_administrators` draws.
    A guild mid-`/setup` has no roster *yet*, and an empty write would make
    that indistinguishable from a roster nobody is on.
    """
    store = FakeStore()
    await sync_directory(_guild(), FakeConfig(), store, T0)
    assert store.members == []


async def test_a_guild_that_configured_neither_role_still_gets_its_channels() -> None:
    """Channels and roles are not gated on configuration: they are what
    `/setup` itself is about to ask an administrator to choose from.
    """
    store = FakeStore()
    guild = _guild(voice=[_channel_object(10, "meeting", 1)])
    await sync_directory(guild, FakeConfig(), store, T0)
    assert store.channels == [
        (GUILD_ID, [MirroredChannel(channel_id=10, name="meeting", kind=VOICE, position=1)])
    ]


async def test_a_configured_role_that_was_deleted_names_nobody() -> None:
    """The clear half: nobody holds a role that does not exist, so the
    people it used to name must stop being named rather than linger.
    """
    store = FakeStore()
    await sync_directory(_guild(), FakeConfig(str(CONSENT_ROLE_ID)), store, T0)
    assert store.members == [(GUILD_ID, [])]


async def test_an_unparseable_role_setting_does_not_stop_the_sweep() -> None:
    """`guild_config` stores text and neither role key is
    integer-validated, so a hand-edited row can hold anything. The
    channels and roles of the guild are unaffected by it.
    """
    store = FakeStore()
    guild = _guild(voice=[_channel_object(10, "meeting", 1)])
    await sync_directory(guild, FakeConfig("nonsense"), store, T0)
    assert store.members == [(GUILD_ID, [])]
    assert len(store.channels) == 1


async def test_one_configured_role_is_enough_to_write_the_names() -> None:
    consent = _role_object(CONSENT_ROLE_ID, "recorded", 1, _member(CARA, "Cara"))
    store = FakeStore()
    await sync_directory(_guild(roles=[consent]), FakeConfig(str(CONSENT_ROLE_ID)), store, T0)
    assert store.members == [
        (GUILD_ID, [MirroredMember(discord_user_id=CARA, display_name="Cara")])
    ]
