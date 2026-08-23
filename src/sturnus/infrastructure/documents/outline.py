"""The Outline document adapter (Spec 8.4).

**The API shape here is unverified.** No running Outline instance was
reachable while this module was written, so the endpoint path, request
field names, and the location of the URL in the response are all
assumptions built from Outline's published API documentation, not
confirmed against a live server. See `docs/verification/outline-api.md`
for exactly what was assumed and what must be checked before this ships.

Because a wrong guess here is likely, not just possible, every place where
the guess could be wrong is deliberately kept to one line each:

- `_CREATE_DOCUMENT_PATH` is the only place the endpoint path is spelled.
- `_build_payload` is the only place request field names are spelled.
- `_extract_created_document` is the only place the response shape is
  read.

Fixing any one of those after real verification should not touch the rest
of this file.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import SpanKind

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.application.documents import CreatedDocument, DocumentSink
from sturnus.infrastructure.telemetry import set_span_fields, span
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Assumed endpoint for document creation (Outline's documented
#: `documents.create` RPC-style API). UNVERIFIED -- see
#: docs/verification/outline-api.md.
_CREATE_DOCUMENT_PATH = "/api/documents.create"

#: Assumed endpoint for the collection list. UNVERIFIED in exactly the
#: same way, and kept to one line for exactly the same reason.
_LIST_COLLECTIONS_PATH = "/api/collections.list"

#: How many collections one page asks for. Outline's list endpoints are
#: `offset`/`limit` paginated, and this number is a trade between round
#: trips and response size on a sweep that runs about once an hour --
#: neither side of which is under pressure. It is deliberately not a
#: setting: nothing an operator knows would tell them what to set it to.
_COLLECTION_PAGE_LIMIT = 100

#: A stop the protocol does not provide. Pagination ends when a page comes
#: back shorter than it was asked for, which is correct against a server
#: that behaves; against one that does not -- a proxy that keeps returning
#: a full page, a misread response shape -- the loop below would never
#: finish, inside a worker whose transcription queue is waiting on the same
#: process. This bounds it at a hundred thousand collections, which is far
#: past any real instance and far short of forever.
_MAX_COLLECTION_PAGES = 1000

#: Status codes that mean "this will never succeed as-is": an invalid
#: token, a token without access, or a target collection that does not
#: exist. Retrying these forever would leave a queue that never drains.
_PERMANENT_STATUS_CODES = frozenset({401, 403, 404})


class PermanentDocumentError(Exception):
    """Raised when Outline rejects the request in a way retrying cannot fix.

    Deliberately carries no response body or request payload in its
    message: the caller (the job queue) may log this exception, and the
    transcript body must never reach a log line (Spec constraint).
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Outline permanently rejected the request (HTTP {status_code})")
        self.status_code = status_code


def _build_payload(*, title: str, body: str, collection_id: str) -> dict[str, Any]:
    """Builds the `documents.create` request body.

    UNVERIFIED field names, assumed from Outline's public API docs:
    `title`, `text` (Markdown), `collectionId`, and `publish` (true to
    make the document visible immediately rather than leaving it a
    private draft only the creating token's user can see).
    """
    return {
        "title": title,
        "text": body,
        "collectionId": collection_id,
        "publish": True,
    }


def _extract_collections(payload: dict[str, Any]) -> list[MirroredCollection]:
    """Reads one page of `collections.list` into `(id, name)` pairs.

    UNVERIFIED shape, assumed from Outline's public API docs, and the only
    place in this file the list response is read -- the same containment
    `_extract_created_document` gets, for the same reason.

    Nothing but `id` and `name` is taken. A collection also carries a
    description, an icon, a colour and a permission, and none of them is
    anything the console needs to turn a pasted UUID back into the words
    the administrator saw when they copied it.
    """
    return [
        MirroredCollection(collection_id=entry["id"], name=entry["name"])
        for entry in payload["data"]
    ]


def _extract_created_document(payload: dict[str, Any], base_url: str) -> CreatedDocument:
    """Reads `id` and `url` out of a successful response.

    UNVERIFIED shape, assumed from Outline's public API docs: the created
    document sits under a top-level `data` object, with `id` and a `url`
    that is a path relative to the Outline instance rather than a full
    URL -- so it is resolved against `base_url` before being handed back.
    A bot posting a relative path into Discord would produce a broken
    link rather than a working one, so this resolution happens exactly
    once here rather than being left to callers to remember.
    """
    data = payload["data"]
    document_id = data["id"]
    url = data["url"]
    absolute_url = url if url.startswith(("http://", "https://")) else f"{base_url}{url}"
    return CreatedDocument(id=document_id, url=absolute_url)


class OutlineSink(DocumentSink):
    """Creates protocol documents in an Outline collection over HTTP.

    `transport` exists purely so tests can substitute `httpx.MockTransport`
    -- production code never passes it and gets a real network transport.

    Deliberately holds no collection id: `base_url` and `api_token` are one
    Outline instance's connection details, true for every guild the worker
    serves, but the collection a given document belongs to is `document_target`
    (Spec 11) -- per-guild configuration this adapter cannot know at
    construction time. `create`'s `target` parameter carries it instead; see
    `sturnus.application.documents.DocumentSink`'s docstring for why.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._transport = transport

    async def list_collections(self) -> list[MirroredCollection]:
        """Every collection this token can see, as `(id, name)` pairs.

        Exists so the console can show a name where `document_target`
        currently shows a UUID. `api` cannot make this call -- it holds no
        Outline token, by the console design's Section 2.1 -- so `worker`
        makes it on a slow sweep and writes the answer where `api` reads.

        Paginated because Outline's list endpoints are, and read to the
        end because a half-read list is worse than no list: an
        administrator offered the first hundred collections has no way to
        tell that the one they want was on page two. The loop stops on a
        page shorter than it asked for, with `_MAX_COLLECTION_PAGES` as a
        floor under a server that never gives one.

        Failures are classified exactly as `create` classifies them, and
        for the same reason: 401, 403 and 404 mean this token will never
        list anything, and a sweep retrying them every hour forever would
        produce an hourly log line and never a collection. Nothing is
        logged here -- the caller
        (`sturnus.application.collection_mirror.sweep_outline_collections`)
        is what decides that a failed sweep leaves the previous mirror
        standing, and it says so once rather than twice.
        """
        collections: list[MirroredCollection] = []
        with span(
            "document.list_collections",
            SpanKind.CLIENT,
            http_method="POST",
            url_path=_LIST_COLLECTIONS_PATH,
            server_address=urlsplit(self._base_url).hostname or "",
        ) as active:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._api_token}"},
            ) as client:
                for page in range(_MAX_COLLECTION_PAGES):
                    response = await client.post(
                        _LIST_COLLECTIONS_PATH,
                        json={
                            "offset": page * _COLLECTION_PAGE_LIMIT,
                            "limit": _COLLECTION_PAGE_LIMIT,
                        },
                    )
                    set_span_fields(active, http_status=response.status_code)
                    if response.status_code in _PERMANENT_STATUS_CODES:
                        set_span_fields(active, permanent=True)
                        raise PermanentDocumentError(response.status_code)
                    response.raise_for_status()

                    found = _extract_collections(response.json())
                    collections.extend(found)
                    if len(found) < _COLLECTION_PAGE_LIMIT:
                        break

            set_span_fields(active, count=len(collections))
        return collections

    async def create(self, title: str, body: str, target: str) -> CreatedDocument:
        payload = _build_payload(title=title, body=body, collection_id=target)
        # The highest-value span per line in this adapter. This file's own
        # docstring says the endpoint path, the field names and the response
        # shape are UNVERIFIED guesses against a live Outline -- this span is
        # what confirms or refutes them in production.
        #
        # Sizes only. `title` is derived from the transcript and `body` *is*
        # the transcript; neither goes anywhere near an attribute. Note also
        # what is absent: `url.full` would carry the Authorization header's
        # host and any query string, which is exactly the reason no
        # `opentelemetry-instrumentation-httpx` is installed.
        with span(
            "document.create",
            SpanKind.CLIENT,
            http_method="POST",
            url_path=_CREATE_DOCUMENT_PATH,
            server_address=urlsplit(self._base_url).hostname or "",
            collection_id=target,
            title_chars=len(title),
            body_bytes=len(body.encode("utf-8")),
        ) as active:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._api_token}"},
            ) as client:
                response = await client.post(_CREATE_DOCUMENT_PATH, json=payload)

            set_span_fields(active, http_status=response.status_code)

            if response.status_code in _PERMANENT_STATUS_CODES:
                # `permanent` is the operationally decisive bit: 401/403/404
                # means "this will never succeed, stop retrying", while a
                # 5xx is swept up again every 300s by
                # `retry_pending_documents`. From outside, those two look
                # identical today. `collection_id` earns its place for the
                # same reason -- a 404 is un-diagnosable without it, because
                # "is the configured document_target real?" is the whole
                # question.
                set_span_fields(active, permanent=True)
                log_event(
                    log,
                    logging.WARNING,
                    Event.SESSION_DOCUMENT_REJECTED,
                    "Outline permanently rejected document creation",
                    http_status=response.status_code,
                    collection_id=target,
                )
                raise PermanentDocumentError(response.status_code)

            response.raise_for_status()

            created = _extract_created_document(response.json(), self._base_url)
            set_span_fields(active, document_id=created.id)
            log_event(
                log,
                logging.DEBUG,
                Event.SESSION_DOCUMENT_CREATED,
                "Created an Outline document",
                document_id=created.id,
                collection_id=target,
                title_chars=len(title),
                body_bytes=len(body.encode("utf-8")),
            )
            return created
