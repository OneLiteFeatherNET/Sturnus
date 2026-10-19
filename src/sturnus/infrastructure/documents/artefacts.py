"""The stored protocol, sealed on the way in and opened on the way out.

One class, and both processes hold it: the worker writes an artefact
through it (`sturnus.infrastructure.documents.sinks.DocumentObjectStore`)
and the API reads one back through it
(`sturnus.console.ports.DocumentArtefacts`). Two classes would be two
places the envelope's associated data is spelled, and an artefact sealed
under a context the reader does not reproduce is an artefact nobody can
open -- a failure that surfaces months later, on somebody's link to last
quarter's minutes, and not at the moment the mistake was made.

**Why this exists at all.** A Markdown export is every word every
participant said, in one object. Every other object in that bucket -- the
recordings, and the stored spectrograms beside them -- is ciphertext; this
was the only one that was not. Access control in front of it
(`sturnus.console.routes_documents`) is a rule this process enforces;
encryption is a property of the bytes, and the two answer different
questions about a bucket somebody has a copy of.

**The key an artefact is sealed under, and why it is not the recording's.**
A recording is sealed under a per-session data key wrapped into
`transcription_job`, and the retention sweep ends that recording's life
after `audio_retention_days`. An export artefact is not audio: it belongs
to the protocol, which deliberately outlives the recording -- a transcript
answers `200` with `audio_available: false` rather than `404` precisely
because the retention window governs the recording and not the record of
the meeting. Sealing an artefact under a session data key would therefore
tie a document meant to last to a key whose whole purpose is to stop
lasting, and the failure would be silent: every stored Markdown and HTML
export unreadable on the day the window closes, noticed by whoever next
opened an old document.

Two further facts make the session key wrong here even setting the
lifetime aside. A data key is per *job*, which is per *speaker*; a
session's protocol merges every speaker, so there is no one session key
to choose and picking a participant's would bind a whole meeting's
document to one person's row. And the retention sweep deletes objects,
which means the correct hardening of that sweep -- clearing
`wrapped_data_key` when the audio goes -- would take the protocols with
it.

So an artefact carries a data key of its own, generated per write and
wrapped under the master key with `secret_context(PURPOSE, guild_id)`:
bound to the guild and to the purpose, so an object relocated onto
another guild's key fails to authenticate rather than handing that guild
somebody else's meeting; and bound to nothing whose deletion is somebody's
scheduled job. `tests/application/test_retention.py` pins the lifetime,
so that re-binding these objects to the audio's fails a test rather than
a link.
"""

from __future__ import annotations

import logging
from typing import Protocol

from cryptography.exceptions import InvalidTag

from sturnus.domain.errors import UnreadableArtefact
from sturnus.infrastructure.crypto import (
    KeyWrapper,
    is_sealed_artefact,
    open_artefact,
    seal_artefact,
    secret_context,
)
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

__all__ = ["PURPOSE", "DocumentBytes", "SealedArtefacts"]

#: What this artefact's wrapped key is bound to, beside the guild. A
#: literal from this source, and distinct from `export-target` -- one
#: guild holds both a destination's credential and that destination's
#: artefacts, and binding to the guild alone would leave the two
#: interchangeable within it. See
#: `sturnus.infrastructure.crypto.secret_context`.
PURPOSE = "export-artefact"

#: What the object is stored as. Not the format's own media type: these
#: bytes are an envelope, not a document, and a bucket listing that calls
#: them `text/markdown` invites the next reader to treat them as text. The
#: real media type is the format registry's answer and reaches the browser
#: from there (`sturnus.application.export_formats.ExportFormat`).
_SEALED_MEDIA_TYPE = "application/octet-stream"


class DocumentBytes(Protocol):
    """Whole small objects in the bucket. `S3DocumentStore`, structurally.

    Structural rather than the concrete class, for the reason every other
    collaborator in this codebase is: what this module is *about* is the
    envelope, and a test of the envelope should not need a bucket to
    exercise it. `KeyError` for an object that is not there.
    """

    async def put(self, key: str, body: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...


class SealedArtefacts:
    """Seals a rendered protocol into the object store, and opens it back.

    Holds the process's `KeyWrapper` -- the same one the export targets'
    credentials are wrapped with, handed in rather than built from the
    master key again, so a rotation is threaded through one decode per
    process rather than one per collaborator.
    """

    def __init__(self, store: DocumentBytes, keys: KeyWrapper) -> None:
        self._store = store
        self._keys = keys

    async def put_sealed(self, key: str, body: bytes, *, guild_id: int) -> None:
        """Seals `body` under a data key of its own and writes the object.

        There is no unsealed spelling of this method, on this class or on
        the port it satisfies: a protocol written in clear is the thing
        this module exists to make unavailable rather than discouraged.

        Nothing is logged here. `ObjectStoreSink.create` already writes
        one line per stored artefact, with the session and the target on
        it; a second line for the same act, from the layer underneath,
        would be the same event told twice with less context.
        """
        sealed = seal_artefact(body, self._keys, secret_context(PURPOSE, guild_id))
        await self._store.put(key, sealed, _SEALED_MEDIA_TYPE)

    async def get(self, key: str, *, guild_id: int) -> bytes:
        """The protocol's bytes, or a refusal.

        `KeyError` when the object is not there, which is
        `S3DocumentStore.get`'s own answer and an ordinary one -- a row
        can outlive its object. `UnreadableArtefact` when the object is
        there and does not open, which is not ordinary, and which the
        caller reports as the finding it is rather than as a tidy-up.

        **An object stored before this format existed is served as it
        is.** Those artefacts are plaintext Markdown and HTML written by
        the release that introduced export targets, nothing sweeps them,
        and refusing them would turn every link already handed to a
        participant into a 404 in the name of protecting a document that
        link is the only way to reach. They are logged at WARNING on
        every read, so the corpus is visible while it drains: a re-export
        of the session overwrites the object at the same address, sealed.
        """
        stored = await self._store.get(key)
        if not is_sealed_artefact(stored):
            log_event(
                log,
                logging.WARNING,
                Event.SESSION_EXPORT_UNSEALED,
                "Served a protocol stored before export artefacts were sealed",
                guild_id=guild_id,
                bytes=len(stored),
            )
            return stored
        try:
            return open_artefact(stored, self._keys, secret_context(PURPOSE, guild_id))
        except (InvalidTag, ValueError) as exc:
            # Raised rather than logged here. The line worth writing has
            # the session and the target on it, and this class knows
            # neither; `sturnus.console.routes_documents.read_document`
            # does, and logs it there beside the refusal it turns into.
            raise UnreadableArtefact("a stored protocol did not open") from exc
