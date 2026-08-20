"""Tests for the Outline OAuth client that establishes a participant's identity.

The API shape this client targets is **researched but not live-verified**
(see `docs/verification/outline-oauth.md`) -- no running Outline instance
with a registerable OAuth application was reachable while this task was
implemented. Public source (Outline's OAuth-provider PR, its OpenAPI spec,
and its hosting docs) grounds the shapes used here, but nothing below was
exercised against a real server. These tests pin the client's own contract
-- state carried, secret never in the browser URL, a clear error on a
rejected code, and nothing sensitive reaching a log line -- which must hold
regardless of what the real API turns out to look like, using
`httpx.MockTransport` so nothing here touches a network.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from sturnus.infrastructure.documents.outline_oauth import (
    ExternalIdentity,
    LinkExchangeError,
    OutlineOAuth,
)

BASE = "https://outline.example"
REDIRECT = "https://sturnus.example/oauth/callback"


def client(transport: httpx.MockTransport | None = None) -> OutlineOAuth:
    return OutlineOAuth(
        base_url=BASE,
        client_id="cid",
        client_secret="csecret",
        redirect_uri=REDIRECT,
        transport=transport,
    )


def test_the_authorize_url_carries_the_state() -> None:
    query = parse_qs(urlparse(client().authorize_url("state-123")).query)
    assert query["state"] == ["state-123"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["client_id"] == ["cid"]
    assert query["response_type"] == ["code"]


def test_the_authorize_url_never_carries_the_secret() -> None:
    """It goes to the user's browser."""
    assert "csecret" not in client().authorize_url("s")


def test_the_authorize_url_asks_for_the_narrowest_scope() -> None:
    """Only `read` is asked for -- Outline has no narrower identity-only scope.

    See docs/verification/outline-oauth.md for why `read` is the least
    that reads one's own identity.
    """
    query = parse_qs(urlparse(client().authorize_url("s")).query)
    assert query["scope"] == ["read"]


async def test_a_code_resolves_to_an_identity() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer"})
        assert request.url.path.endswith("/api/auth.info")
        assert request.headers["Authorization"] == "Bearer at"
        return httpx.Response(200, json={"data": {"user": {"id": "9c8b", "name": "Max Example"}}})

    identity = await client(httpx.MockTransport(handle)).identity_from_code("code-1")
    assert identity == ExternalIdentity(external_user_id="9c8b", display_name="Max Example")


async def test_a_rejected_code_raises_a_link_error() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(LinkExchangeError):
        await client(httpx.MockTransport(handle)).identity_from_code("stale")


async def test_a_rejected_identity_lookup_raises_a_link_error() -> None:
    """A token that exchanges fine but is refused by the identity call is still a link failure."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer"})
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(LinkExchangeError):
        await client(httpx.MockTransport(handle)).identity_from_code("code-1")


async def test_no_secret_or_token_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "super-secret-token"})
        return httpx.Response(200, json={"data": {"user": {"id": "x", "name": "n"}}})

    with caplog.at_level("DEBUG"):
        await client(httpx.MockTransport(handle)).identity_from_code("code-1")

    assert "super-secret-token" not in caplog.text
    assert "csecret" not in caplog.text
    assert "code-1" not in caplog.text


async def test_no_secret_reaches_the_log_on_a_rejected_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure path must be just as careful as the success path."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with caplog.at_level("DEBUG"), pytest.raises(LinkExchangeError):
        await client(httpx.MockTransport(handle)).identity_from_code("stale-code")

    assert "csecret" not in caplog.text
    assert "stale-code" not in caplog.text
