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

from sturnus.application.documents import CreatedDocument, DocumentSink
from sturnus.infrastructure.telemetry import set_span_fields, span
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Assumed endpoint for document creation (Outline's documented
#: `documents.create` RPC-style API). UNVERIFIED -- see
#: docs/verification/outline-api.md.
_CREATE_DOCUMENT_PATH = "/api/documents.create"

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
