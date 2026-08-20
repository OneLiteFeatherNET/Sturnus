"""Tests for the Outline document adapter.

The API shape this adapter targets is **unverified** (see
`docs/verification/outline-api.md`) -- no running Outline instance was
reachable while this task was implemented. These tests pin the adapter's
own contract (absolute URLs, no secrets or content in logs, retryable
versus permanent failures), which must hold regardless of what the real
API turns out to look like, using `httpx.MockTransport` so nothing here
touches a network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from sturnus.infrastructure.documents.outline import OutlineSink, PermanentDocumentError

BASE = "https://outline.example"
COLLECTION = "col-1"


def sink(handler: httpx.MockTransport) -> OutlineSink:
    return OutlineSink(
        base_url=BASE,
        api_token="secret-token",
        transport=handler,
    )


async def test_the_target_reaches_the_request_body_per_call() -> None:
    """`target` is a parameter of `create`, not fixed at construction (Spec 11
    `document_target`): the same `OutlineSink` must be usable for two
    different guilds' collections without being reconstructed between calls.
    """
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["collectionId"])
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    instance = sink(httpx.MockTransport(handle))
    await instance.create("T", "B", "guild-a-collection")
    await instance.create("T", "B", "guild-b-collection")
    assert seen == ["guild-a-collection", "guild-b-collection"]


async def test_a_created_document_returns_its_id_and_url() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "doc-1", "url": "/doc/protocol-abc"}})

    created = await sink(httpx.MockTransport(handle)).create("Title", "Body", COLLECTION)
    assert created.id == "doc-1"
    assert created.url.endswith("/doc/protocol-abc")


async def test_the_url_is_absolute() -> None:
    """Outline returns a relative path; the bot posts this into Discord."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    created = await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)
    assert created.url.startswith(BASE)


async def test_an_already_absolute_url_is_returned_unchanged() -> None:
    """If the API ever returns a full URL, it must not be mangled into one."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"id": "d", "url": "https://outline.example/doc/y"}}
        )

    created = await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)
    assert created.url == "https://outline.example/doc/y"


async def test_the_token_is_sent_and_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    with caplog.at_level("DEBUG"):
        await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)

    assert "secret-token" in seen.get("authorization", "")
    assert "secret-token" not in caplog.text


async def test_the_body_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Transcript content must never reach a log line."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    with caplog.at_level("DEBUG"):
        await sink(httpx.MockTransport(handle)).create("T", "CONFIDENTIAL-SPEECH", COLLECTION)

    assert "CONFIDENTIAL-SPEECH" not in caplog.text


async def test_a_server_error_is_retryable() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)
    assert not isinstance(excinfo.value, PermanentDocumentError)


async def test_a_rejected_token_is_permanent() -> None:
    """Retrying an unauthorised call forever would never drain the queue."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(PermanentDocumentError):
        await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)


async def test_a_missing_collection_is_permanent() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with pytest.raises(PermanentDocumentError):
        await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)


async def test_a_forbidden_token_is_permanent() -> None:
    """A token that is valid but lacks access to the collection also never heals."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    with pytest.raises(PermanentDocumentError):
        await sink(httpx.MockTransport(handle)).create("T", "B", COLLECTION)


async def test_a_permanent_failure_does_not_leak_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even on the error path -- where it might be tempting to log for
    diagnostics -- the transcript content must not appear."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with caplog.at_level("DEBUG"), pytest.raises(PermanentDocumentError):
        await sink(httpx.MockTransport(handle)).create("T", "CONFIDENTIAL-SPEECH", COLLECTION)

    assert "CONFIDENTIAL-SPEECH" not in caplog.text
    assert "secret-token" not in caplog.text
