from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sturnus.infrastructure.linkserver import build_app

# `pytest-aiohttp` is what normally supplies the `aiohttp_client` fixture
# used below, and it is not among this project's test dependencies.
# aiohttp's own bundled plugin (`aiohttp.pytest_plugin`) runs its own event
# loop machinery that conflicts with pytest-asyncio's `asyncio_mode = auto`
# (the two race to own the running loop). This fixture reproduces just the
# `aiohttp_client(app) -> TestClient` shape the tests below need, built
# directly on `aiohttp.test_utils`, so it runs on pytest-asyncio's own loop
# instead of a second, competing one.
AiohttpClientFactory = Callable[
    [web.Application], Awaitable["TestClient[web.Request, web.Application]"]
]


@pytest.fixture
async def aiohttp_client() -> AsyncIterator[AiohttpClientFactory]:
    clients: list[TestClient[web.Request, web.Application]] = []

    async def make(app: web.Application) -> "TestClient[web.Request, web.Application]":
        test_client = TestClient(TestServer(app))
        await test_client.start_server()
        clients.append(test_client)
        return test_client

    yield make
    for test_client in clients:
        await test_client.close()


T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA = 100


class FakeStates:
    def __init__(self, valid: str | None = "good-state") -> None:
        self.valid = valid
        self.consumed: list[str] = []

    async def consume(self, state: str, now: datetime) -> Any:  # noqa: ARG002
        self.consumed.append(state)
        if state != self.valid:
            return None
        from sturnus.application.linking import PendingLink

        self.valid = None  # single use
        return PendingLink(discord_user_id=ANNA, provider="outline")


class FakeOAuth:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def authorize_url(self, state: str) -> str:
        return f"https://outline.example/oauth/authorize?state={state}"

    async def identity_from_code(self, code: str) -> Any:  # noqa: ARG002
        from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity

        if self.fail:
            from sturnus.infrastructure.documents.outline_oauth import LinkExchangeError

            raise LinkExchangeError("nope", status_code=400)
        return ExternalIdentity(external_user_id="9c8b", display_name="Max Example")


class FakeLinks:
    def __init__(self) -> None:
        self.saved: list[tuple[int, str, str, str]] = []

    async def save(self, discord_user_id: int, provider: str, external_id: str, name: str) -> None:
        self.saved.append((discord_user_id, provider, external_id, name))


@pytest.fixture
async def client(aiohttp_client: AiohttpClientFactory) -> TestClient[web.Request, web.Application]:
    return await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=FakeLinks(), now=lambda: T0)
    )


async def test_healthz_is_served(client: TestClient[web.Request, web.Application]) -> None:
    assert (await client.get("/healthz")).status == 200


async def test_a_valid_callback_stores_the_link(aiohttp_client: AiohttpClientFactory) -> None:
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=links, now=lambda: T0)
    )
    response = await c.get("/oauth/callback", params={"code": "c", "state": "good-state"})
    assert response.status == 200
    assert links.saved == [(ANNA, "outline", "9c8b", "Max Example")]


async def test_an_unknown_state_is_refused_and_stores_nothing(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A forged callback must not link anything."""
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=links, now=lambda: T0)
    )
    response = await c.get("/oauth/callback", params={"code": "c", "state": "forged"})
    assert response.status == 400
    assert links.saved == []


async def test_a_replayed_state_is_refused(aiohttp_client: AiohttpClientFactory) -> None:
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=links, now=lambda: T0)
    )
    params = {"code": "c", "state": "good-state"}
    assert (await c.get("/oauth/callback", params=params)).status == 200
    assert (await c.get("/oauth/callback", params=params)).status == 400
    assert len(links.saved) == 1


async def test_a_missing_parameter_is_refused(
    client: TestClient[web.Request, web.Application],
) -> None:
    assert (await client.get("/oauth/callback", params={"state": "good-state"})).status == 400
    assert (await client.get("/oauth/callback", params={"code": "c"})).status == 400


async def test_a_failed_exchange_reports_an_error_and_stores_nothing(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(fail=True), states=FakeStates(), links=links, now=lambda: T0)
    )
    response = await c.get("/oauth/callback", params={"code": "c", "state": "good-state"})
    assert response.status >= 400
    assert links.saved == []


async def test_the_error_page_does_not_echo_the_input(aiohttp_client: AiohttpClientFactory) -> None:
    """Reflecting user input into HTML is how a callback becomes an XSS sink."""
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=FakeLinks(), now=lambda: T0)
    )
    response = await c.get(
        "/oauth/callback", params={"code": "c", "state": "<script>alert(1)</script>"}
    )
    body = await response.text()
    assert "<script>" not in body
