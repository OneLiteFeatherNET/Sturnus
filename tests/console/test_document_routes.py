"""The protocol endpoints, and the rule that makes an object-store URL safe.

This route is the reason `CreatedDocument.url` for an object-store
destination is a console path rather than a presigned S3 URL: a presigned
URL is checked once when it is issued, this is checked on every request. The
tests that matter here are the refusals.

The gate is `SessionReads.session_for`, the same call `/api/sessions/{id}`
and `/api/sessions/{id}/transcript` are served from, so `FakeReads` is what
decides who is in a session here -- not a participant set on the document
double, which would be a second implementation of the rule inside the tests
that are supposed to prove there is only one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiohttp import web

from sturnus.console.statistics import AttendedSession, Participant
from sturnus.domain.exports import SessionDocument
from tests.console.conftest import (
    ANNA,
    BEN,
    AiohttpClientFactory,
    FakeArtefacts,
    FakeReads,
    FakeSessionDocuments,
    build_test_api,
    signed_cookie,
)

SESSION_COOKIE = "sturnus_session"
SESSION = 42
GUILD = 4711
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)

MARKDOWN_KEY = "protocols/42/7.md"
HTML_KEY = "protocols/42/8.html"


def document(
    target_id: int | None, provider: str, document_id: str, url: str = "https://console/x"
) -> SessionDocument:
    return SessionDocument(
        session_id=SESSION,
        guild_id=GUILD,
        target_id=target_id,
        provider=provider,
        document_id=document_id,
        url=url,
        created_at=T0,
    )


def attended() -> AttendedSession:
    """The one session these tests are about, as the reads adapter sees it.

    `FakeReads` does not scope -- scoping is SQL and is tested against the
    real database -- so "Ben was not in it" is expressed by signing Ben in
    against a `FakeReads` that holds no session at all, which is exactly
    what the real `session_for` answers him.
    """
    return AttendedSession(
        id=SESSION,
        channel_id=555,
        channel_name="meeting",
        started_at=T0,
        ended_at=None,
        document_url=None,
        participants=(Participant(ANNA, "anna"),),
        tracks=(),
        title=None,
        description=None,
    )


def api(
    documents: list[SessionDocument] | None = None,
    objects: dict[str, bytes] | None = None,
    reads: FakeReads | None = None,
) -> web.Application:
    """The console with one session Anna may read.

    Who may read it is `FakeReads`' answer, because
    `SessionReads.session_for` is the gate the route actually uses -- the
    same call `/api/sessions/{id}` and the transcript endpoint are served
    from. A participant set on the document double would be a second
    implementation of the rule inside the tests meant to prove there is
    only one.
    """
    return build_test_api(
        reads=reads if reads is not None else FakeReads(sessions=(attended(),)),
        documents=FakeSessionDocuments({SESSION: list(documents or [])}),
        artefacts=FakeArtefacts(objects or {}),
    )


@pytest.fixture
def cookies() -> dict[str, str]:
    return {SESSION_COOKIE: signed_cookie(ANNA)}


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


async def test_a_participant_sees_every_protocol_the_session_produced(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(
        api(
            [
                document(3, "outline", "doc-1", "https://outline.example/doc/1"),
                document(7, "markdown", MARKDOWN_KEY, "https://console/api/..."),
            ]
        )
    )
    response = await client.get(f"/api/sessions/{SESSION}/documents", cookies=cookies)
    assert response.status == 200
    body = await response.json()
    assert body["session_id"] == str(SESSION)
    assert [d["provider"] for d in body["documents"]] == ["outline", "markdown"]


async def test_the_listing_says_which_protocols_this_process_can_serve(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """An Outline document's bytes are in Outline; its `url` is what the
    console links to. A stored one is served from here."""
    client = await aiohttp_client(
        api([document(3, "outline", "doc-1"), document(7, "markdown", MARKDOWN_KEY)])
    )
    body = await (await client.get(f"/api/sessions/{SESSION}/documents", cookies=cookies)).json()
    assert [d["readable"] for d in body["documents"]] == [False, True]


async def test_a_document_whose_destination_was_removed_is_still_listed(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """`session_document.target_id` is `ON DELETE SET NULL`: removing a
    destination is "stop publishing here", not "forget what was
    published"."""
    client = await aiohttp_client(api([document(None, "outline", "doc-1")]))
    body = await (await client.get(f"/api/sessions/{SESSION}/documents", cookies=cookies)).json()
    assert body["documents"][0]["target_id"] is None


async def test_a_session_that_published_nothing_lists_nothing(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """An empty list is a real answer -- a meeting still being transcribed
    -- and is not the same as the 404 somebody outside the session gets."""
    client = await aiohttp_client(api([]))
    response = await client.get(f"/api/sessions/{SESSION}/documents", cookies=cookies)
    assert response.status == 200
    assert (await response.json())["documents"] == []


async def test_somebody_outside_the_session_cannot_list_its_protocols(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(api([document(7, "markdown", MARKDOWN_KEY)], reads=FakeReads()))
    response = await client.get(
        f"/api/sessions/{SESSION}/documents", cookies={SESSION_COOKIE: signed_cookie(BEN)}
    )
    assert response.status == 404
    assert await response.json() == {"error": "no such document"}


# ---------------------------------------------------------------------------
# The artefact
# ---------------------------------------------------------------------------


async def test_a_participant_reads_the_stored_protocol(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(
        api([document(7, "markdown", MARKDOWN_KEY)], {MARKDOWN_KEY: b"# Minutes\n"})
    )
    response = await client.get(f"/api/sessions/{SESSION}/documents/7", cookies=cookies)
    assert response.status == 200
    assert await response.text() == "# Minutes\n"
    assert response.headers["Content-Type"].startswith("text/markdown")


async def test_the_html_protocol_is_served_as_html(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(
        api([document(8, "html", HTML_KEY)], {HTML_KEY: b"<!doctype html><p>hi</p>"})
    )
    response = await client.get(f"/api/sessions/{SESSION}/documents/8", cookies=cookies)
    assert response.headers["Content-Type"].startswith("text/html")


async def test_a_protocol_is_never_cached_by_a_shared_cache(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The meeting written down, on its way to one named reader. A shared
    cache holding a copy would hand it to the next person through the same
    proxy -- exactly the audience the check exists to exclude."""
    client = await aiohttp_client(
        api([document(7, "markdown", MARKDOWN_KEY)], {MARKDOWN_KEY: b"x"})
    )
    response = await client.get(f"/api/sessions/{SESSION}/documents/7", cookies=cookies)
    assert response.headers["Cache-Control"] == "private, no-store"


async def test_the_html_protocol_is_served_under_a_policy_that_lets_it_do_nothing(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """It is a page served from the console's own origin. The template
    fetches nothing and runs nothing, and this says so to the browser too."""
    client = await aiohttp_client(api([document(8, "html", HTML_KEY)], {HTML_KEY: b"<p>hi</p>"}))
    response = await client.get(f"/api/sessions/{SESSION}/documents/8", cookies=cookies)
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "sandbox" in policy
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_somebody_outside_the_session_cannot_read_its_protocol(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The rule this route exists to enforce. A presigned S3 URL would
    have answered this request."""
    client = await aiohttp_client(
        api(
            [document(7, "markdown", MARKDOWN_KEY)],
            {MARKDOWN_KEY: b"secret minutes"},
            reads=FakeReads(),
        )
    )
    response = await client.get(
        f"/api/sessions/{SESSION}/documents/7", cookies={SESSION_COOKIE: signed_cookie(BEN)}
    )
    assert response.status == 404
    assert "secret minutes" not in await response.text()


async def test_an_outline_document_is_not_served_from_here(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """Its bytes live in Outline. The listing carries its URL so the
    console can link straight out."""
    client = await aiohttp_client(api([document(3, "outline", "doc-1")]))
    response = await client.get(f"/api/sessions/{SESSION}/documents/3", cookies=cookies)
    assert response.status == 404


async def test_a_destination_this_session_never_reached_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    client = await aiohttp_client(api([document(7, "markdown", MARKDOWN_KEY)]))
    response = await client.get(f"/api/sessions/{SESSION}/documents/99", cookies=cookies)
    assert response.status == 404


async def test_a_row_whose_object_is_gone_is_a_404_and_not_a_500(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The row outlived its object. Nothing is broken -- the same reading
    the audio route gives a recording the retention sweep erased."""
    client = await aiohttp_client(api([document(7, "markdown", MARKDOWN_KEY)], {}))
    response = await client.get(f"/api/sessions/{SESSION}/documents/7", cookies=cookies)
    assert response.status == 404


async def test_a_document_of_a_format_this_deployment_cannot_read_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """A row written by a future release that knows `pdf`, read by one
    that does not. It must not be served as some other media type, and it
    must not be a 500."""
    client = await aiohttp_client(api([document(9, "pdf", "protocols/42/9.pdf")]))
    response = await client.get(f"/api/sessions/{SESSION}/documents/9", cookies=cookies)
    assert response.status == 404


@pytest.mark.parametrize("path", ["/api/sessions/abc/documents", "/api/sessions/42/documents/xyz"])
async def test_a_path_segment_that_is_not_a_number_is_a_404(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str], path: str
) -> None:
    client = await aiohttp_client(api([]))
    assert (await client.get(path, cookies=cookies)).status == 404


@pytest.mark.parametrize("path", ["/api/sessions/42/documents", "/api/sessions/42/documents/7"])
async def test_neither_route_answers_without_a_session(
    aiohttp_client: AiohttpClientFactory, path: str
) -> None:
    client = await aiohttp_client(api([document(7, "markdown", MARKDOWN_KEY)]))
    assert (await client.get(path)).status == 401


# ---------------------------------------------------------------------------
# The seal, from this side of it
# ---------------------------------------------------------------------------


async def test_the_artefact_is_asked_for_under_the_guild_the_row_names(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """A stored protocol is sealed under a key bound to its guild, and the
    binding has to come from the row rather than from the object -- an
    envelope carrying the guild it was filed under would authenticate just
    as happily after being moved onto another guild's key.

    So the guild the route supplies is load-bearing, and supplying the
    wrong one fails in a way indistinguishable from a missing object. This
    is the only place that can be checked."""
    artefacts = FakeArtefacts({MARKDOWN_KEY: b"# Minutes\n"})
    client = await aiohttp_client(
        build_test_api(
            reads=FakeReads(sessions=(attended(),)),
            documents=FakeSessionDocuments({SESSION: [document(7, "markdown", MARKDOWN_KEY)]}),
            artefacts=artefacts,
        )
    )
    response = await client.get(f"/api/sessions/{SESSION}/documents/7", cookies=cookies)
    assert response.status == 200
    assert artefacts.asked == [(MARKDOWN_KEY, GUILD)]


async def test_an_artefact_that_does_not_open_is_a_404_and_not_a_500(
    aiohttp_client: AiohttpClientFactory, cookies: dict[str, str]
) -> None:
    """The object is there and does not open: a wrong master key, an
    envelope sealed under a guild this row does not name, a body edited in
    the bucket. There is nothing here to serve, so the reader gets the
    same refusal every other reason gets -- and the log gets a different
    `reason`, because "the sweep took it" and "it failed to authenticate"
    are different mornings for whoever is on call."""
    client = await aiohttp_client(
        build_test_api(
            reads=FakeReads(sessions=(attended(),)),
            documents=FakeSessionDocuments({SESSION: [document(7, "markdown", MARKDOWN_KEY)]}),
            artefacts=FakeArtefacts(
                {MARKDOWN_KEY: b"# Minutes\n"}, unreadable=frozenset({MARKDOWN_KEY})
            ),
        )
    )
    response = await client.get(f"/api/sessions/{SESSION}/documents/7", cookies=cookies)
    assert response.status == 404
    assert "Minutes" not in await response.text()
