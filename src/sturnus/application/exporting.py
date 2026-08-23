"""Publishing one session's protocol to every destination its guild has.

`session.document_url` used to be the whole story: one collection, one
document, one URL. A guild may now enable several destinations, and the two
consequences of that are what this module is.

**One failing destination must not lose the others.** Publishing walks the
list and survives each entry's own failure, the same shape
`sturnus.application.publishing.announce_ready_sessions` already uses for a
sweep over sessions: an unreachable Confluence must not cost a guild the
Outline document it would otherwise have had.

**The retry sweep must retry only what actually failed.** That is what
`session_document` is for. A destination already recorded there is skipped,
so a destination that keeps failing brings the session back to the sweep
every five minutes without republishing the ones that worked. Without it a
flaky second destination produces a hundred duplicate Outline documents in a
morning -- each of them a real document in somebody's wiki, and none of them
removable by anything in this system.

**Which destination is primary, and why it matters.** `session.document_url`
is what the announcement posts and what everything already reading a session
reads, so exactly one destination has to be it. It is the **oldest enabled
target** -- the lowest `guild_export_target.id`, the first one the guild
configured. Not the first by name, which would move when somebody renamed a
destination; not "the Outline one", which assumes a shape a guild is free
not to have. A guild that has only ever had one destination has always had
that one, so nothing moves for anybody who has not asked for a second.

**The legacy destination.** A guild with no rows in `guild_export_target`
publishes exactly where it published before this module existed: the
collection in `document_target`, through the Outline sink, recorded on the
session row and nowhere else. There is no migration that moves those guilds
into the table, and a release that quietly stopped publishing for all of
them would be the worst possible outcome of this change. That destination
carries no `target_id`, which is also why it is not written to
`session_document`: `(session_id, target_id)` is the unique key that makes a
re-export overwrite itself, and a null there appends instead of conflicting.
What records it is `session.document_url`, and what deduplicates it is the
sweep's own "closed and not documented" condition, exactly as before.

Dependency-rule note: this module lives in `sturnus.application` and
therefore names its collaborators as local `Protocol`s rather than importing
the adapters (`sturnus.infrastructure.db.export_targets.ExportTargetStore`,
`...session_documents.SessionDocumentStore`, the sinks), which
`tests/test_architecture.py` forbids. `sturnus.entrypoints.worker` wires the
concrete ones in.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sturnus.application.documents import CreatedDocument, DocumentSink, document_title
from sturnus.application.export_formats import ExportFormat, RenderRequest, format_named
from sturnus.domain.exports import ExportTarget, SessionDocument
from sturnus.observability.events import Event, log_event, log_exception

log = logging.getLogger(__name__)

__all__ = [
    "Destination",
    "ExportPorts",
    "NothingPublished",
    "PublishReport",
    "PublishedDocument",
    "RenderRequest",
    "destinations_for",
    "document_key",
    "publish_session",
]


class NothingPublished(Exception):
    """Every destination this session has failed, so it published nowhere.

    Raised so the caller's existing retry path sees a failure rather than a
    quiet no-op: `sturnus.application.worker` logs it and leaves the session
    `closed`, which is precisely what the retry sweep looks for.

    Carries no message composed from runtime data. The caller logs it
    through `log_exception`, which keeps the type and drops the string, and
    a message naming the destination would be the one place a target's
    address reached a log line.
    """


@dataclass(frozen=True, slots=True)
class Destination:
    """One place one session's protocol is being written to.

    `target_id` is `None` for the legacy destination derived from
    `document_target` -- see the module docstring. Everything else about
    the two is the same, which is the point: there is one publishing path,
    not a special case for the guilds that have not configured a table row.

    `provider` is what `session_document.provider` and
    `session.document_provider` record. It is the format name for a
    configured target and the guild's `document_provider` setting for the
    legacy one, so nothing about what an existing guild writes to that
    column changes.
    """

    session_id: int
    target_id: int | None
    format: ExportFormat
    target: str
    provider: str


@dataclass(frozen=True, slots=True)
class PublishedDocument:
    """A destination, and the document that actually reached it."""

    destination: Destination
    document: CreatedDocument


@dataclass(frozen=True, slots=True)
class PublishReport:
    """What one publish achieved, in the terms the caller has to act on.

    `primary` is what `session.document_url` is stamped from, and it is
    `None` whenever the primary destination did not produce a document in
    this run -- which is what leaves the session undocumented and therefore
    back in front of the retry sweep.
    """

    published: tuple[PublishedDocument, ...]
    primary: PublishedDocument | None
    #: Destinations that failed in a way another attempt may fix.
    failed: int
    #: Destinations that were rejected permanently -- a deleted collection,
    #: a revoked token. Counted apart because no sweep will fix them and a
    #: caller must not retry on their account.
    rejected: int
    #: Destinations already recorded in `session_document`, and therefore
    #: not published again.
    skipped: int


class ExportTargetReader(Protocol):
    """Where a guild's enabled destinations are read from.

    Matches `sturnus.infrastructure.db.export_targets.ExportTargetStore`
    structurally. Only the enabled ones: switching a destination off is an
    administrator saying "stop publishing here", and a publish that read
    the disabled rows too would be ignoring them.
    """

    async def enabled_for(self, guild_id: int) -> Sequence[ExportTarget]: ...


class SessionDocumentRecorder(Protocol):
    """Where what a session published is recorded and read back.

    `for_session` is read *before* publishing, not after: it is what says
    which destinations are already done. Matches
    `sturnus.infrastructure.db.session_documents.SessionDocumentStore`.
    """

    async def for_session(self, session_id: int) -> Sequence[SessionDocument]: ...

    async def record(
        self,
        session_id: int,
        *,
        target_id: int,
        provider: str,
        document_id: str,
        url: str,
        now: datetime,
    ) -> None: ...


class SinkRegistry(Protocol):
    """Resolves a destination to the sink that can carry it.

    Takes the whole `Destination` rather than a format name because a sink
    is not always a singleton: an object-store sink writes one object per
    session per target, so it has to be built knowing which. The
    infrastructure implementation
    (`sturnus.infrastructure.documents.sinks.DocumentSinks`) branches on the
    *sink family* the format names, never on the format itself -- which is
    what lets a fourth format be added to the registry with no change here
    at all.

    `None` for a destination this process cannot serve: an object-store
    format on a deployment with no object store configured. Not an
    exception, because one unbuildable destination must not stop the
    others; the caller counts it as a failure and says so.
    """

    def sink_for(self, destination: Destination) -> DocumentSink | None: ...


@dataclass(frozen=True, slots=True)
class ExportPorts:
    """The three collaborators publishing needs, in one parameter.

    Bundled rather than passed separately because they always travel
    together and `sturnus.application.worker.process_one` already carries
    more collaborators than a reader can hold in their head. Nothing here
    is optional: a deployment that cannot read targets is a deployment that
    publishes nowhere, and finding that out through an `AttributeError` at
    the end of a meeting is not an improvement on finding it out at
    startup.
    """

    sinks: SinkRegistry
    targets: ExportTargetReader
    documents: SessionDocumentRecorder


def document_key(prefix: str, session_id: int, target_id: int, extension: str) -> str:
    """Where an object-store destination puts one session's artefact.

    The shape of `sturnus.application.recording.audio_key` and here for the
    same reason: a key format is a decision two processes have to agree on
    -- the worker writes it and the console reads it back -- and a decision
    two processes share belongs where neither of them owns it.

    Keyed on the *target* rather than on the format, because two
    destinations of the same format are two artefacts: a guild publishing
    Markdown to two prefixes wants two objects, and one key for both would
    have the second overwrite the first.

    `prefix` is the target's own `target` column, already checked against
    `ExportFormat.accepts_target` before a target could be stored -- so it
    cannot contain `..`, cannot begin with `/`, and cannot be empty.
    """
    return f"{prefix}/{session_id}/{target_id}.{extension}"


def destinations_for(
    session_id: int,
    targets: Sequence[ExportTarget],
    fallback: Destination | None,
) -> tuple[Destination, ...]:
    """Which destinations this session's protocol goes to, in order.

    Pure, and deliberately: every rule about *where* a protocol goes is
    decided here, where a test reaches it without a database, a sink or a
    transcript.

    The rules, in the order they apply:

    1. A target naming a format this deployment cannot publish is dropped,
       and dropped alone. `guild_export_target.format` is a plain string
       precisely so that one unrecognised row is a row a reader ignores
       rather than a failed read that takes the guild's other destinations
       with it (see `sturnus.domain.exports.ExportTarget`).
    2. What is left is ordered by id, oldest first -- so the primary is the
       destination the guild configured first, and it does not move when
       somebody renames one.
    3. A guild with nothing left publishes to `fallback`, the legacy
       `document_target` destination, or nowhere if it has none.

    The fallback *replaces* rather than joins: a guild that configured a
    destination said where its protocols go, and also writing to the old
    collection would be a document nobody asked for and nobody knows about.
    """
    chosen: list[Destination] = []
    for target in sorted(targets, key=lambda t: t.id):
        entry = format_named(target.format)
        if entry is None:
            # INFO, not WARNING: on a deployment that has not built `pdf`
            # yet this is a configured intention rather than a fault, and
            # it repeats on every publish for as long as the row exists.
            # `target_id` and not the target's name, which is free text an
            # administrator typed.
            log_event(
                log,
                logging.INFO,
                Event.SESSION_EXPORT_SKIPPED,
                "A destination names a format this deployment cannot publish",
                session_id=session_id,
                target_id=target.id,
                reason="unknown_format",
            )
            continue
        chosen.append(
            Destination(
                session_id=session_id,
                target_id=target.id,
                format=entry,
                target=target.target,
                provider=entry.name,
            )
        )
    if chosen:
        return tuple(chosen)
    return () if fallback is None else (fallback,)


async def publish_session(
    session_id: int,
    destinations: Sequence[Destination],
    request: RenderRequest,
    exports: ExportPorts,
    now: datetime,
) -> PublishReport:
    """Writes this session's protocol to each destination, and says what happened.

    Renders once per *format*, not once per destination: a guild with two
    Markdown destinations receives the identical string at both, and a
    second Jinja pass over every block of a meeting is work nobody asked
    for on a worker whose transcription queue is waiting on the same
    process.

    Survives each destination's own failure, and records only the ones that
    succeeded. Raises `NothingPublished` when this session reached
    **nowhere at all** -- something failed, nothing was published and
    nothing was already published on an earlier attempt -- so the caller's
    existing retry path sees a failure. A skip is what keeps that narrow:
    a session whose Outline document was written last sweep and whose
    second destination is still down has a protocol, and reporting that as
    a failed publish would fill the log with a fault that has already been
    survived. A permanent rejection does not raise either, for the reason
    it never did: no attempt will change it.
    """
    already = await _already_published(session_id, destinations, exports)
    bodies: dict[str, str] = {}
    title = document_title(request.transcript, request.tz)

    published: list[PublishedDocument] = []
    primary: PublishedDocument | None = None
    failed = rejected = skipped = 0

    for index, place in enumerate(destinations):
        done = already.get(place.target_id) if place.target_id is not None else None
        if done is not None:
            skipped += 1
            if index == 0:
                # The narrow window where the document was created and
                # recorded but stamping the session failed. Reporting it as
                # the primary anyway is what lets `document_url` be written
                # on the next sweep; without it the primary is skipped for
                # ever and the announcement never goes out for a session
                # that has a perfectly good document.
                primary = PublishedDocument(place, CreatedDocument(done.document_id, done.url))
            continue

        sink = exports.sinks.sink_for(place)
        if sink is None:
            failed += 1
            log_event(
                log,
                logging.ERROR,
                Event.SESSION_EXPORT_FAILED,
                "No sink is configured for this destination's format in this process",
                session_id=session_id,
                target_id=place.target_id,
                format=place.format.name,
                reason="no_sink",
            )
            continue

        body = bodies.get(place.format.name)
        if body is None:
            body = bodies[place.format.name] = place.format.render(request)

        try:
            created = await sink.create(title, body, place.target)
        except Exception as exc:
            if type(exc).__name__ == "PermanentDocumentError":
                # Recognised by class name, not by `except
                # PermanentDocumentError`: that type lives in
                # `sturnus.infrastructure.documents.outline`, which this
                # module must never import. ERROR rather than WARNING for
                # the reason it always was -- a permanent rejection is the
                # end of the road for this destination, and no sweep will
                # fix it.
                rejected += 1
                log_event(
                    log,
                    logging.ERROR,
                    Event.SESSION_DOCUMENT_REJECTED,
                    "A destination permanently rejected creation; no retry will succeed",
                    session_id=session_id,
                    target_id=place.target_id,
                    format=place.format.name,
                )
                continue
            failed += 1
            # Never `%s` on `exc`: the failure comes out of a Jinja render
            # or an httpx request, either of which can carry template
            # context or request content in its message -- and this one
            # would carry a destination's address with it.
            log_exception(
                log,
                logging.WARNING,
                Event.SESSION_EXPORT_FAILED,
                "One destination failed; the others are unaffected and the sweep will retry",
                exc,
                session_id=session_id,
                target_id=place.target_id,
                format=place.format.name,
            )
            continue

        reached = PublishedDocument(place, created)
        published.append(reached)
        if index == 0:
            primary = reached
        if place.target_id is not None:
            await exports.documents.record(
                session_id,
                target_id=place.target_id,
                provider=place.provider,
                document_id=created.id,
                url=created.url,
                now=now,
            )
        log_event(
            log,
            logging.INFO,
            Event.SESSION_DOCUMENT_CREATED,
            "Published the session protocol to a destination",
            session_id=session_id,
            target_id=place.target_id,
            format=place.format.name,
            provider=place.provider,
            document_id=created.id,
            body_bytes=len(body.encode("utf-8")),
        )

    if failed and not published and not skipped:
        raise NothingPublished
    return PublishReport(
        published=tuple(published),
        primary=primary,
        failed=failed,
        rejected=rejected,
        skipped=skipped,
    )


async def _already_published(
    session_id: int, destinations: Sequence[Destination], exports: ExportPorts
) -> dict[int, SessionDocument]:
    """What this session has already reached, keyed by target.

    Not read at all when no destination carries a `target_id` -- the legacy
    single-destination path never writes to `session_document`, so it has
    no reason to read it either, and a guild that configured nothing should
    not cost a query per publish for a table it has no rows in.
    """
    if not any(place.target_id is not None for place in destinations):
        return {}
    return {
        row.target_id: row
        for row in await exports.documents.for_session(session_id)
        if row.target_id is not None
    }
