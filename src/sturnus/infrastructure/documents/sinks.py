"""Which adapter carries a destination's bytes, and the object-store one.

`sturnus.application.export_formats` says a format is a renderer paired with
a *sink family*; this is where a family becomes an object. It is one branch
per family and never one per format, which is the whole point: `pdf` is an
object-store artefact exactly as `markdown` and `html` are, so adding it is
an entry in the registry and no change here at all. A fifth family --
Confluence over HTTP -- would be one more `elif` and one more constructor
argument, and nothing else in the system would move.

**Why the object-store sink hands back a console URL.** `CreatedDocument`
carries a URL and the announcement posts it, so an object-store artefact
needs a URL somebody can open. A presigned S3 URL would be the easy answer
and is the wrong one: it outlives the access rules that issued it, works for
anybody it is forwarded to, and cannot be revoked when a participation ends.
The URL here points at `sturnus.console.routes_documents`, which checks that
the person asking was in the session on every request -- the same rule the
session's own metadata is served under.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sturnus.application.documents import CreatedDocument, DocumentSink
from sturnus.application.export_formats import OBJECT_STORE_SINK, OUTLINE_SINK
from sturnus.application.exporting import Destination, document_key
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)


class DocumentObjectStore(Protocol):
    """Where a rendered protocol is written. `SealedArtefacts`, structurally.

    **One method, and its name is the decision.** A Markdown export is
    every word every participant said, in one object, in the bucket that
    otherwise holds nothing but ciphertext; a port offering a plain `put`
    beside this one would be a port somebody eventually writes a protocol
    through in clear. There is no such method, so there is nothing to
    choose between.

    `guild_id` is here because sealing binds the artefact's key to the
    guild and the purpose -- see
    `sturnus.infrastructure.documents.artefacts.SealedArtefacts` and
    `sturnus.infrastructure.crypto.seal_artefact`.
    """

    async def put_sealed(self, key: str, body: bytes, *, guild_id: int) -> None: ...


def document_path(session_id: int, target_id: int) -> str:
    """The console path that serves one session's artefact at one destination.

    Written once, here, and read by `sturnus.console.routes_documents`,
    which registers the same shape. Two processes have to agree on it --
    the worker puts it in a URL that ends up in a Discord message, and the
    API has to answer that URL months later.
    """
    return f"/api/sessions/{session_id}/documents/{target_id}"


class ObjectStoreSink(DocumentSink):
    """Stores one session's rendered protocol as an object, once.

    Built per destination rather than shared, because an object needs a key
    and a key needs to know which session and which target it belongs to --
    which is why `sturnus.application.exporting.SinkRegistry.sink_for` takes
    the whole `Destination` rather than a format name. It holds a reference
    to the store and four values; constructing one per publish costs
    nothing.

    `target` is the destination's own prefix, already checked against
    `ExportFormat.accepts_target` before the row could be stored -- so it
    cannot climb out of itself, cannot begin with `/` and cannot be empty.
    """

    def __init__(
        self,
        store: DocumentObjectStore,
        *,
        console_origin: str,
        session_id: int,
        target_id: int,
        guild_id: int,
        file_extension: str,
    ) -> None:
        self._store = store
        self._console_origin = console_origin.rstrip("/")
        self._session_id = session_id
        self._target_id = target_id
        self._guild_id = guild_id
        self._file_extension = file_extension

    async def create(self, title: str, body: str, target: str) -> CreatedDocument:
        """Writes the artefact and returns the console URL that serves it.

        The document's `id` is its object key, which is what
        `session_document.document_id` then holds and what the console
        route reads back to find the bytes. That is a real identifier of
        the artefact in the system that stores it, exactly as an Outline
        document id is -- not a second, invented one that would need a
        column nobody has.

        `title` is not written anywhere here: it is already inside the
        rendered body (the HTML template puts it in `<title>` and in the
        heading), and an object store has nowhere else to put it. The
        parameter stays because `DocumentSink` is one port and a sink that
        quietly took a different shape would not be interchangeable with
        the others.

        The artefact is **sealed**, and the port says so in its one
        method's name. This object is the most sensitive thing in the
        bucket -- a recording is one speaker, a protocol is every word
        every participant said -- and until now it was the only thing in
        it that was not ciphertext. What the media type described is
        still true of the document and no longer true of the object, so
        it is not written here at all: the console reads it back from the
        format registry, which is where it always came from.
        """
        key = document_key(target, self._session_id, self._target_id, self._file_extension)
        payload = body.encode("utf-8")
        await self._store.put_sealed(key, payload, guild_id=self._guild_id)
        # DEBUG and sizes only, matching `OutlineSink.create`: `title` is
        # derived from the transcript and `body` *is* the transcript. The
        # key is not logged either -- `s3_key` is in `DENIED_NAMES`, and an
        # object key embeds the ids that are already on this line.
        log_event(
            log,
            logging.DEBUG,
            Event.SESSION_DOCUMENT_CREATED,
            "Stored a rendered protocol in the object store",
            session_id=self._session_id,
            target_id=self._target_id,
            title_chars=len(title),
            body_bytes=len(payload),
        )
        return CreatedDocument(
            id=key,
            url=f"{self._console_origin}{document_path(self._session_id, self._target_id)}",
        )


class DocumentSinks:
    """Resolves a destination to the adapter that can carry it.

    Both collaborators are optional and `None` is a real answer, not a
    failure: a deployment with no object store configured cannot serve an
    object-store destination, and the caller
    (`sturnus.application.exporting.publish_session`) counts that as one
    destination failing rather than as the end of the publish. Raising here
    would take a guild's working Outline document down with its
    misconfigured Markdown one.
    """

    def __init__(
        self,
        *,
        outline: DocumentSink | None = None,
        objects: DocumentObjectStore | None = None,
        console_origin: str = "",
    ) -> None:
        self._outline = outline
        self._objects = objects
        self._console_origin = console_origin

    def sink_for(self, destination: Destination) -> DocumentSink | None:
        family = destination.format.sink
        if family == OUTLINE_SINK:
            return self._outline
        if family == OBJECT_STORE_SINK:
            if self._objects is None or destination.target_id is None:
                # `target_id is None` is the legacy `document_target`
                # destination, which is always Outline -- so this is
                # unreachable rather than merely unlikely. It is checked
                # because an object needs a key and a key needs a target
                # id: the alternative is a `None` in the middle of an
                # object key, discovered in a bucket listing.
                return None
            return ObjectStoreSink(
                self._objects,
                console_origin=self._console_origin,
                session_id=destination.session_id,
                target_id=destination.target_id,
                guild_id=destination.guild_id,
                file_extension=destination.format.file_extension,
            )
        return None
