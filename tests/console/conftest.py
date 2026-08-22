"""Shared fixtures and doubles for the console's API tests.

The `aiohttp_client` fixture is the same one `tests/infrastructure/
test_linkserver.py` builds for the same reason: `pytest-aiohttp` is not a
dependency of this project, and aiohttp's own bundled plugin runs an event
loop that races pytest-asyncio's. This reproduces the shape those tests
need on pytest-asyncio's loop instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, date, datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sturnus.console.statistics import AttendedSession
from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity, LinkExchangeError

AiohttpClientFactory = Callable[
    [web.Application], Awaitable["TestClient[web.Request, web.Application]"]
]

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
SECRET = "s" * 32
ANNA, BEN = 100, 200
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
    def __init__(self, admins: set[int] | None = None) -> None:
        self.admins = admins or set()

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        return discord_user_id in self.admins


def now_at(moment: datetime = T0) -> Callable[[], datetime]:
    return lambda: moment


class FakeReads:
    """The console's reads, in memory.

    Note what this does *not* do: it does not scope. Scoping is a property
    of the SQL and is tested against the real database in
    `tests/console/test_queries.py` -- a double that filtered in Python
    would only ever prove that the double filters. What the route tests
    use it for is the other half: that each handler asks for the
    signed-in user and nobody else, and what it does with the answer.
    """

    def __init__(
        self,
        sessions: Sequence[AttendedSession] = (),
        transcripts: Sequence[str] = (),
    ) -> None:
        self.sessions = tuple(sessions)
        self.transcripts = tuple(transcripts)
        #: Every Discord id this was asked about, in order. The route
        #: tests assert on it, because "the handler passed the session's
        #: own user id through" is the thing that cannot be checked from
        #: the response body.
        self.asked_for: list[int] = []
        self.years: list[int] = []
        self.days: list[date] = []

    async def sessions_for(self, discord_user_id: int) -> Sequence[AttendedSession]:
        self.asked_for.append(discord_user_id)
        return self.sessions

    async def session_for(self, discord_user_id: int, session_id: int) -> AttendedSession | None:
        self.asked_for.append(discord_user_id)
        return next((s for s in self.sessions if s.id == session_id), None)

    async def sessions_in_year(self, discord_user_id: int, year: int) -> Sequence[AttendedSession]:
        self.asked_for.append(discord_user_id)
        self.years.append(year)
        return self.sessions

    async def sessions_on_day(self, discord_user_id: int, day: date) -> Sequence[AttendedSession]:
        self.asked_for.append(discord_user_id)
        self.days.append(day)
        return self.sessions

    async def transcripts_of(self, discord_user_id: int) -> Sequence[str]:
        self.asked_for.append(discord_user_id)
        return self.transcripts
