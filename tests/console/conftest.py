"""Shared fixtures and doubles for the console's API tests.

The `aiohttp_client` fixture is the same one `tests/infrastructure/
test_linkserver.py` builds for the same reason: `pytest-aiohttp` is not a
dependency of this project, and aiohttp's own bundled plugin runs an event
loop that races pytest-asyncio's. This reproduces the shape those tests
need on pytest-asyncio's loop instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity, LinkExchangeError

AiohttpClientFactory = Callable[
    [web.Application], Awaitable["TestClient[web.Request, web.Application]"]
]

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
SECRET = "s" * 32
ANNA, BEN = 100, 200
#: The guild a test means when it names no guild at all.
GUILD = 4711
ANNA_OUTLINE = "c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f"


@pytest.fixture
async def aiohttp_client() -> AsyncIterator[AiohttpClientFactory]:
    clients: list[TestClient[web.Request, web.Application]] = []

    async def make(app: web.Application) -> TestClient[web.Request, web.Application]:
        test_client = TestClient(TestServer(app))
        await test_client.start_server()
        clients.append(test_client)
        return test_client

    yield make
    for test_client in clients:
        await test_client.close()


class FakeOAuth:
    """Stands in for `OutlineOAuth` without a live Outline."""

    def __init__(self, identity: ExternalIdentity | None = None, fail: bool = False) -> None:
        self.identity = identity or ExternalIdentity(ANNA_OUTLINE, "Anna Example")
        self.fail = fail
        self.authorize_calls: list[str] = []

    def authorize_url(self, state: str) -> str:
        self.authorize_calls.append(state)
        return f"https://outline.example/oauth/authorize?state={state}"

    async def identity_from_code(self, code: str) -> ExternalIdentity:
        if self.fail:
            raise LinkExchangeError("refused", status_code=400)
        del code
        return self.identity


class FakeStates:
    """The single-use OAuth state store, in memory."""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self._valid: set[str] = set()

    async def issue(self, state: str, now: datetime) -> None:
        del now
        self.issued.append(state)
        self._valid.add(state)

    async def consume(self, state: str, now: datetime) -> bool:
        del now
        if state not in self._valid:
            return False
        self._valid.discard(state)
        return True


class FakeLinks:
    """`account_link`, reversed: Outline identity to Discord user."""

    def __init__(self, mapping: dict[str, int] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {ANNA_OUTLINE: ANNA}

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None:
        del provider
        return self.mapping.get(external_user_id)


class FakeAdmins:
    """The mirrored administrator membership, in memory.

    One source of truth -- the per-guild mapping -- because that is what
    `admin_member` is. `admins` is shorthand for "administers the one
    guild the test did not bother to name", and every question is derived
    from the mapping rather than tracked beside it: a double that can
    answer "yes" to `is_admin_anywhere` and "no" to every `is_admin` would
    prove the opposite of what a test using it claims.

    The endpoints themselves are tested against the real `AdminMemberStore`
    on the real database (`tests/console/test_settings_routes.py`), since
    a per-guild authorisation rule is not worth proving against a
    dictionary.
    """

    def __init__(
        self,
        admins: set[int] | None = None,
        by_guild: dict[int, set[int]] | None = None,
    ) -> None:
        self.by_guild: dict[int, set[int]] = {
            guild_id: set(members) for guild_id, members in (by_guild or {}).items()
        }
        if admins:
            self.by_guild.setdefault(GUILD, set()).update(admins)

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        return any(discord_user_id in members for members in self.by_guild.values())

    async def administered_guilds(self, discord_user_id: int) -> Sequence[int]:
        return tuple(
            sorted(
                guild_id
                for guild_id, members in self.by_guild.items()
                if discord_user_id in members
            )
        )

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool:
        return discord_user_id in self.by_guild.get(guild_id, set())


def now_at(moment: datetime = T0) -> Callable[[], datetime]:
    return lambda: moment


class UnusedSettings:
    """A `SettingsStore` that refuses to be used.

    `build_api` needs one to build an application at all, and the tests
    that only care about signing in have no business owning a
    configuration store. Raising rather than returning something plausible
    keeps that honest: if one of those tests ever does reach a settings
    endpoint, it fails here instead of quietly passing against a double
    whose validation is nobody's.
    """

    async def snapshot(self, guild_id: int) -> dict[str, str]:
        del guild_id
        raise AssertionError("this test built an application without a settings store")

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None:
        del guild_id, key, value, now
        raise AssertionError("this test built an application without a settings store")
