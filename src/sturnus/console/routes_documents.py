"""The protocols a session produced, and the gate in front of them.

- `GET /api/sessions/{session_id}/documents`
- `GET /api/sessions/{session_id}/documents/{target_id}`

**Why this route exists at all.** `CreatedDocument` carries a URL and the
announcement posts it, so an object-store destination has to produce a URL a
participant can open. The obvious answer -- a presigned S3 URL -- is the
wrong one, and the specification says so in as many words (§3.2): a
presigned URL works for anybody it is forwarded to, keeps working after a
participation ends, and cannot be revoked. This route serves the same bytes
under a rule that is checked on every request instead.

**The rule is the session's own, and it is literally the same call.** Every
handler here begins by asking `SessionReads.session_for` for the session --
the scoped statement `/api/sessions/{id}` and `/api/sessions/{id}/transcript`
are both served from -- and answers 404 when it comes back `None`. Not a
second copy of the participant rule: a protocol is the same meeting the
transcript is, and a second `WHERE` on `session_participant` would be a
second place for the three endpoints to disagree about who may read it. The
directories behind them (`TranscriptReader`, `SessionDocumentDirectory`)
therefore carry no `requested_by` and are unreachable without that line
having already run.

Somebody who was not in the session gets **404**, and so does somebody
asking for a destination that does not exist and somebody asking for an
Outline document. Every refusal is the same answer with the same body: a
distinct one would confirm which sessions the system holds and where a guild
publishes, to somebody with no business knowing either.

**An Outline document is not served here.** Its bytes live in Outline, and
the listing carries its URL so the console can link straight out. Only the
formats whose sink family is the object store have anything for this route
to fetch, and which those are is
`sturnus.application.export_formats`' answer rather than a list repeated
here.
"""

from __future__ import annotations

import logging

from aiohttp import web

from sturnus.application.export_formats import OBJECT_STORE_SINK, format_named
from sturnus.console.ports import DocumentArtefacts, SessionDocumentDirectory
from sturnus.domain.exports import SessionDocument
from sturnus.observability.events import Event, log_event

# Why every reference to `sturnus.console.app` and `routes_read` below is
# imported inside a function rather than at the top of this module: `app`
# imports this one to register its routes, so a module-level import back
# into it would close a cycle that fails on whichever is loaded first. The
# same arrangement `routes_recording` uses, for the same reason.

log = logging.getLogger(__name__)

#: Where `build_api` puts the collaborators. Declared here because they
#: belong to these routes and nothing else reads them.
SESSION_DOCUMENTS: web.AppKey[SessionDocumentDirectory] = web.AppKey("session_documents")
DOCUMENT_ARTEFACTS: web.AppKey[DocumentArtefacts] = web.AppKey("document_artefacts")

_LIST_PATH = "/api/sessions/{session_id}/documents"
#: The same shape `sturnus.infrastructure.documents.sinks.document_path`
#: builds into the URL the worker stores. Two processes have to agree on
#: it: the worker puts it in a link that ends up in a Discord message, and
#: this process has to answer that link months later.
_ARTEFACT_PATH = "/api/sessions/{session_id}/documents/{target_id}"


def register(app: web.Application) -> None:
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_LIST_PATH, require_session(list_documents)),
            web.get(_ARTEFACT_PATH, require_session(read_document)),
        ]
    )


def document_json(document: SessionDocument) -> dict[str, object]:
    """One published protocol, as the console sees it.

    `target_id` is `None` for a document whose destination has since been
    removed, and the row survives that deliberately: the document still
    exists in the other system and the link is what somebody follows when
    they go looking for last quarter's minutes. `readable` says whether
    this process can serve the bytes -- false for an Outline document,
    whose `url` points at Outline and is what the console links to.
    """
    entry = format_named(document.provider)
    return {
        "target_id": document.target_id,
        "provider": document.provider,
        "url": document.url,
        "created_at": document.created_at.isoformat(),
        "readable": _is_readable(document),
        "media_type": entry.media_type if entry is not None else None,
    }


async def list_documents(request: web.Request) -> web.Response:
    """Every protocol this session produced, oldest first.

    The authorisation is the first read and is the session read itself, so
    a session this person was not in is indistinguishable from one that
    does not exist -- which is what both answers must look like from
    outside.

    An empty list for a session that produced none is a real answer -- a
    meeting still being transcribed, or a guild that has configured
    nowhere to publish -- and is not the same as that 404.
    """
    session_id = _session_id(request)
    if not await _may_read(request, session_id):
        raise _not_found()
    found = await request.app[SESSION_DOCUMENTS].documents_of(session_id)
    if found is None:
        raise _not_found()
    return web.json_response(
        {
            # A session id as a string, like every id the console serialises:
            # `sturnus.console.statistics` does the same for every shape.
            "session_id": str(session_id),
            "documents": [document_json(document) for document in found],
        }
    )


async def read_document(request: web.Request) -> web.StreamResponse:
    """One stored protocol's bytes, under the session's own rule.

    Answered whole rather than streamed, and with no `Range` handling: a
    protocol is tens of kilobytes of text, so there is nothing to page
    through -- unlike the audio route beside it, where the size of a
    recording is the whole problem.
    """
    session_id = _session_id(request)
    try:
        target_id = int(request.match_info["target_id"])
    except ValueError:
        raise _not_found() from None

    caller = _caller(request)
    # Authorisation first, and only then the lookup -- the order
    # `routes_audio._resolve` argues for: a stranger who asks for a
    # destination that does not exist must learn no more than one who asks
    # for a destination that does. Both leave `document` as `None` and
    # both take the one refusal below, which is why there is no branch
    # here that a log line or a status code could tell apart.
    document = None
    if await _may_read(request, session_id):
        document = await request.app[SESSION_DOCUMENTS].document_of(session_id, target_id)
    if document is None or not _is_readable(document):
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_DOCUMENT_REFUSED,
            "Refused a session's protocol to somebody outside the session it belongs to",
            session_id=session_id,
            target_id=target_id,
            requested_by=caller,
            reason="no_such_document",
        )
        raise _not_found()

    entry = format_named(document.provider)
    # `_is_readable` already established the format is known and stored
    # here; asserting keeps the type honest rather than widening the
    # media type to `str | None` for a case that cannot happen.
    assert entry is not None
    try:
        body = await request.app[DOCUMENT_ARTEFACTS].get(document.document_id)
    except KeyError:
        # The row outlived its object. Nothing is broken, so this is a 404
        # and not a 500 -- the same reading the audio route gives a
        # recording the retention sweep erased.
        log_event(
            log,
            logging.WARNING,
            Event.CONSOLE_DOCUMENT_REFUSED,
            "A protocol's object is no longer in the store",
            session_id=session_id,
            target_id=target_id,
            requested_by=caller,
            reason="artefact_erased",
        )
        raise _not_found() from None

    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_DOCUMENT_SERVED,
        "Served a session's protocol to a participant of it",
        session_id=session_id,
        target_id=target_id,
        requested_by=caller,
        format=document.provider,
        bytes=len(body),
    )
    return web.Response(
        body=body,
        content_type=entry.media_type.split(";")[0],
        charset="utf-8",
        headers={
            # The meeting written down, decrypted, on its way to one named
            # reader. A shared cache holding a copy would hand it to the
            # next person through the same proxy -- exactly the audience
            # the check above exists to exclude.
            "Cache-Control": "private, no-store",
            # The document is served from the console's own origin and its
            # body is HTML for one of the formats, so it is a page this
            # origin will execute. It renders nothing but the transcript
            # and fetches nothing (see `export_formats.HTML_TEMPLATE`), and
            # this says so to the browser as well as to a reader of the
            # template: no script, no frame, no subresource, nothing to
            # send anywhere.
            "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; sandbox"),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _is_readable(document: SessionDocument) -> bool:
    """Whether this process holds the bytes of this document at all.

    True only for the formats whose sink family is the object store, which
    is the registry's answer and not a list repeated here -- so `pdf`
    becomes readable the day it is built, with no change in this file. An
    Outline document's bytes are in Outline.
    """
    entry = format_named(document.provider)
    return entry is not None and entry.sink == OBJECT_STORE_SINK


async def _may_read(request: web.Request, session_id: int) -> bool:
    """Whether this request may see this session at all.

    One line, in one place, and it is `SessionReads.session_for` -- the
    same call `/api/sessions/{id}` is served from. See the module
    docstring for why it is that call rather than a rule of this route's
    own.
    """
    from sturnus.console.routes_read import READS

    return await request.app[READS].session_for(_caller(request), session_id) is not None


def _session_id(request: web.Request) -> int:
    try:
        return int(request.match_info["session_id"])
    except ValueError:
        raise _not_found() from None


def _caller(request: web.Request) -> int:
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id


def _not_found() -> web.HTTPException:
    """The one refusal these endpoints have, for every reason they refuse."""
    return web.HTTPNotFound(text='{"error": "no such document"}', content_type="application/json")
