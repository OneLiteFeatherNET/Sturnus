"""Publishing one protocol to several destinations, and surviving one of them.

Two behaviours carry this file.

**One failing destination must not lose the others.** A guild with Outline
and a second destination whose service is down still gets its Outline
document, and the failure is recorded as a failure rather than as the end of
the publish.

**The retry sweep must retry only what actually failed.** `session_document`
is the record of what succeeded, and a destination already in it is skipped
on the next sweep. Without that, a flaky second destination reprints the
Outline document every five minutes for as long as it stays flaky.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sturnus.application import exporting
from sturnus.application.documents import CreatedDocument
from sturnus.application.export_formats import HTML, MARKDOWN, OUTLINE, format_named
from sturnus.domain.exports import ExportTarget, SessionDocument
from sturnus.domain.transcript import SpeakerIdentity, Transcript, TranscriptBlock

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=2)
SESSION = 77

TEMPLATE = (
    Path(__file__).parent.parent.parent
    / "src/sturnus/infrastructure/documents/outline_template.md.j2"
).read_text(encoding="utf-8")

SPEAKER = SpeakerIdentity(100, "speaker")


def transcript() -> Transcript:
    return Transcript(
        session_started_at=T0,
        session_ended_at=T0 + timedelta(hours=1),
        participants=(SPEAKER,),
        blocks=(TranscriptBlock(speaker=SPEAKER, start=T0, text="hello"),),
    )


def request() -> exporting.RenderRequest:
    return exporting.RenderRequest(
        transcript=transcript(), tz=UTC, channel=None, outline_template=TEMPLATE
    )


def target(
    target_id: int,
    format: str = OUTLINE,
    name: str = "wiki",
    where: str = "col-1",
    enabled: bool = True,
) -> ExportTarget:
    return ExportTarget(
        id=target_id,
        guild_id=1,
        format=format,
        name=name,
        target=where,
        config={},
        has_secret=False,
        enabled=enabled,
        created_at=T0,
        updated_at=T0,
    )


def destination(
    target_id: int | None, format: str = OUTLINE, where: str = "col-1"
) -> exporting.Destination:
    entry = format_named(format)
    assert entry is not None
    return exporting.Destination(
        session_id=SESSION, target_id=target_id, format=entry, target=where, provider=format
    )


class PermanentDocumentError(Exception):
    """Recognised by class name, exactly as the production type is.

    `sturnus.application` may not import
    `sturnus.infrastructure.documents.outline`, so the production code
    matches on `type(exc).__name__`. A local class of the same name is the
    honest way to exercise that -- and it is also what proves the match is
    by name rather than by identity.
    """


class FakeSink:
    def __init__(self, url: str = "https://example/doc", fail: Exception | None = None) -> None:
        self.url = url
        self.fail = fail
        self.created: list[tuple[str, str, str]] = []

    async def create(self, title: str, body: str, where: str) -> CreatedDocument:
        if self.fail is not None:
            raise self.fail
        self.created.append((title, body, where))
        return CreatedDocument(id=f"doc-{len(self.created)}", url=self.url)


class FakeSinks:
    """Resolves a destination to a sink, one per destination it is given."""

    def __init__(self, sinks: dict[int | None, FakeSink] | None = None) -> None:
        self.sinks = sinks or {}
        self.asked: list[exporting.Destination] = []

    def sink_for(self, place: exporting.Destination) -> FakeSink | None:
        self.asked.append(place)
        return self.sinks.get(place.target_id)


class FakeTargets:
    def __init__(self, targets: list[ExportTarget] | None = None) -> None:
        self._targets = targets or []

    async def enabled_for(self, _guild_id: int) -> list[ExportTarget]:
        return [t for t in self._targets if t.enabled]


class FakeRecords:
    def __init__(self, existing: list[SessionDocument] | None = None) -> None:
        self.rows = list(existing or [])
        self.recorded: list[tuple[int, int, str, str, str]] = []

    async def for_session(self, session_id: int) -> list[SessionDocument]:
        return [row for row in self.rows if row.session_id == session_id]

    async def record(
        self,
        session_id: int,
        *,
        target_id: int,
        provider: str,
        document_id: str,
        url: str,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.recorded.append((session_id, target_id, provider, document_id, url))


def ports(
    sinks: FakeSinks | None = None,
    targets: FakeTargets | None = None,
    records: FakeRecords | None = None,
) -> exporting.ExportPorts:
    return exporting.ExportPorts(
        sinks=sinks or FakeSinks(),
        targets=targets or FakeTargets(),
        documents=records or FakeRecords(),
    )


# ---------------------------------------------------------------------------
# Choosing the destinations: pure, and therefore where the rules live
# ---------------------------------------------------------------------------


def test_a_guild_with_no_configured_targets_publishes_where_it_always_did() -> None:
    """The fallback is the point. `document_target` is what every guild
    running today is configured with, there is no migration that moves it
    into the new table, and a release that quietly stopped publishing for
    all of them would be the worst possible outcome of this change."""
    legacy = destination(None, OUTLINE, "col-legacy")
    chosen = exporting.destinations_for(SESSION, [], legacy)
    assert chosen == (legacy,)


def test_a_guild_with_no_targets_and_no_legacy_setting_publishes_nowhere() -> None:
    assert exporting.destinations_for(SESSION, [], None) == ()


def test_a_configured_target_replaces_the_legacy_setting() -> None:
    """Not "as well as". A guild that configured a destination in the table
    said where its protocols go; publishing to the old collection too would
    be a document nobody asked for and nobody knows about."""
    chosen = exporting.destinations_for(SESSION, [target(4)], destination(None))
    assert [place.target_id for place in chosen] == [4]


def test_several_targets_are_ordered_oldest_first() -> None:
    """The order is the order they were configured in, which is what makes
    the primary stable -- see `test_the_primary_...` below."""
    chosen = exporting.destinations_for(
        SESSION, [target(9, name="a"), target(2, name="z"), target(5, name="m")], None
    )
    assert [place.target_id for place in chosen] == [2, 5, 9]


def test_a_target_naming_a_format_this_deployment_cannot_publish_is_ignored() -> None:
    """And ignored *alone*: `guild_export_target.format` is a plain string
    precisely so one unrecognised row does not take the guild's other
    destinations with it."""
    chosen = exporting.destinations_for(
        SESSION, [target(1, format="pdf"), target(2, format=MARKDOWN)], None
    )
    assert [place.target_id for place in chosen] == [2]


def test_a_destination_carries_the_renderer_its_format_names() -> None:
    chosen = exporting.destinations_for(SESSION, [target(1, format=HTML)], None)
    assert chosen[0].format.name == HTML


def test_the_provider_recorded_for_a_configured_target_is_its_format() -> None:
    """`session_document.provider` has to outlive the target it names: once
    a destination is removed `target_id` goes null, and a row that could not
    say what kind of document it points at would be a URL with no context."""
    chosen = exporting.destinations_for(SESSION, [target(1, format=HTML)], None)
    assert chosen[0].provider == HTML


# ---------------------------------------------------------------------------
# Publishing to each of them
# ---------------------------------------------------------------------------


async def test_every_enabled_destination_receives_the_protocol() -> None:
    outline, markdown = FakeSink("https://outline/1"), FakeSink("https://console/2")
    sinks = FakeSinks({1: outline, 2: markdown})
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, MARKDOWN, name="archive", where="protocols")], None
    )
    report = await exporting.publish_session(SESSION, places, request(), ports(sinks), NOW)

    assert len(outline.created) == 1
    assert len(markdown.created) == 1
    assert len(report.published) == 2


async def test_each_destination_gets_the_body_its_own_format_renders() -> None:
    """The whole reason a format is a pair. Two destinations, one
    transcript, two different strings -- one with Outline's mention chips
    in it and one with none."""
    outline, html = FakeSink(), FakeSink()
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, HTML, name="page", where="protocols")], None
    )
    await exporting.publish_session(
        SESSION, places, request(), ports(FakeSinks({1: outline, 2: html})), NOW
    )

    assert "mention://" in outline.created[0][1]
    assert "mention://" not in html.created[0][1]
    assert html.created[0][1].lstrip().lower().startswith("<!doctype html>")


async def test_each_destination_is_addressed_by_its_own_target() -> None:
    first, second = FakeSink(), FakeSink()
    places = exporting.destinations_for(
        SESSION,
        [target(1, OUTLINE, where="col-a"), target(2, OUTLINE, name="b", where="col-b")],
        None,
    )
    await exporting.publish_session(
        SESSION, places, request(), ports(FakeSinks({1: first, 2: second})), NOW
    )

    assert first.created[0][2] == "col-a"
    assert second.created[0][2] == "col-b"


async def test_two_destinations_of_one_format_render_the_body_once() -> None:
    """Rendering is a Jinja pass over every block of a meeting. Doing it
    twice for two destinations that would receive the identical string is
    work nobody asked for, on a worker whose transcription queue is
    waiting on the same process."""
    first, second = FakeSink(), FakeSink()
    places = exporting.destinations_for(
        SESSION,
        [
            target(1, MARKDOWN, name="a", where="one"),
            target(2, MARKDOWN, name="b", where="two"),
        ],
        None,
    )
    await exporting.publish_session(
        SESSION, places, request(), ports(FakeSinks({1: first, 2: second})), NOW
    )
    assert first.created[0][1] == second.created[0][1]


async def test_each_published_destination_is_recorded() -> None:
    records = FakeRecords()
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, MARKDOWN, name="b", where="p")], None
    )
    await exporting.publish_session(
        SESSION,
        places,
        request(),
        ports(FakeSinks({1: FakeSink(), 2: FakeSink()}), records=records),
        NOW,
    )
    assert [(row[1], row[2]) for row in records.recorded] == [(1, OUTLINE), (2, MARKDOWN)]


async def test_the_legacy_destination_is_not_recorded_as_a_session_document() -> None:
    """It has no target row to point at, and `session_document.target_id`
    is part of the unique key that makes a re-export overwrite itself. A
    row with a null there would be appended on every sweep instead. What
    records the legacy destination is `session.document_url`, exactly as it
    always was."""
    records = FakeRecords()
    report = await exporting.publish_session(
        SESSION,
        (destination(None),),
        request(),
        ports(FakeSinks({None: FakeSink()}), records=records),
        NOW,
    )
    assert records.recorded == []
    assert report.primary is not None


# ---------------------------------------------------------------------------
# One failing destination
# ---------------------------------------------------------------------------


async def test_one_failing_destination_does_not_lose_the_others() -> None:
    working = FakeSink("https://outline/1")
    broken = FakeSink(fail=RuntimeError("confluence is down"))
    places = exporting.destinations_for(
        SESSION, [target(1, MARKDOWN, name="a", where="p"), target(2, OUTLINE, name="b")], None
    )
    report = await exporting.publish_session(
        SESSION, places, request(), ports(FakeSinks({1: broken, 2: working})), NOW
    )

    assert len(working.created) == 1
    assert report.failed == 1
    assert len(report.published) == 1


async def test_a_destination_that_failed_is_not_recorded() -> None:
    """The record is what the retry sweep reads to decide what is left to
    do. Recording a failure would mean never trying it again."""
    records = FakeRecords()
    with pytest.raises(exporting.NothingPublished):
        await exporting.publish_session(
            SESSION,
            (destination(1),),
            request(),
            ports(FakeSinks({1: FakeSink(fail=RuntimeError("nope"))}), records=records),
            NOW,
        )
    assert records.recorded == []


async def test_a_publish_where_nothing_reached_anywhere_raises() -> None:
    """So the caller's existing retry path -- `process_one`'s handler and
    the sweep -- sees a failure rather than a quiet no-op. A session that
    published nowhere must stay `closed` and be tried again."""
    with pytest.raises(exporting.NothingPublished):
        await exporting.publish_session(
            SESSION,
            (destination(1),),
            request(),
            ports(FakeSinks({1: FakeSink(fail=RuntimeError("nope"))})),
            NOW,
        )


async def test_a_publish_where_something_reached_somewhere_does_not_raise() -> None:
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, MARKDOWN, name="b", where="p")], None
    )
    report = await exporting.publish_session(
        SESSION,
        places,
        request(),
        ports(FakeSinks({1: FakeSink(), 2: FakeSink(fail=RuntimeError("nope"))})),
        NOW,
    )
    assert report.failed == 1
    assert len(report.published) == 1


async def test_a_permanent_rejection_is_not_a_failure_to_retry() -> None:
    """A deleted collection or a revoked token will not be fixed by trying
    again in five minutes. It is counted apart from a transient failure and
    does not raise, which is the behaviour the single-destination path
    already had."""
    report = await exporting.publish_session(
        SESSION,
        (destination(1),),
        request(),
        ports(FakeSinks({1: FakeSink(fail=PermanentDocumentError())})),
        NOW,
    )
    assert report.rejected == 1
    assert report.failed == 0


async def test_a_destination_whose_sink_this_process_cannot_build_is_a_failure() -> None:
    """An object-store destination on a deployment with no object store
    configured. Not silently skipped: a guild that configured a destination
    and never hears about it again has no way to find out."""
    with pytest.raises(exporting.NothingPublished):
        await exporting.publish_session(
            SESSION, (destination(1, MARKDOWN, "p"),), request(), ports(FakeSinks({})), NOW
        )


async def test_a_failure_beside_a_destination_already_published_does_not_raise() -> None:
    """The session has a protocol -- last sweep wrote it -- and one
    destination is still down. Reporting that as a failed publish would put
    a warning in the log on every sweep for a fault that has already been
    survived."""
    report = await exporting.publish_session(
        SESSION,
        (destination(1), destination(2, MARKDOWN, "p")),
        request(),
        ports(
            FakeSinks({1: FakeSink(), 2: FakeSink(fail=RuntimeError("down"))}),
            records=FakeRecords([recorded(1)]),
        ),
        NOW,
    )
    assert report.skipped == 1
    assert report.failed == 1


# ---------------------------------------------------------------------------
# The retry sweep, and the duplicates it must not make
# ---------------------------------------------------------------------------


def recorded(target_id: int, url: str = "https://outline/1") -> SessionDocument:
    return SessionDocument(
        session_id=SESSION,
        target_id=target_id,
        provider=OUTLINE,
        document_id="doc-1",
        url=url,
        created_at=T0,
    )


async def test_a_destination_already_recorded_is_not_published_again() -> None:
    """The duplicate this whole mechanism exists to prevent: a flaky
    Confluence brings the session back to the sweep every five minutes, and
    without this the Outline document is written again on every one of
    them."""
    outline, broken = FakeSink(), FakeSink(fail=RuntimeError("still down"))
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, MARKDOWN, name="b", where="p")], None
    )
    report = await exporting.publish_session(
        SESSION,
        places,
        request(),
        ports(FakeSinks({1: outline, 2: broken}), records=FakeRecords([recorded(1)])),
        NOW,
    )
    assert outline.created == []
    assert report.skipped == 1
    assert report.failed == 1


async def test_only_the_destination_that_failed_is_retried() -> None:
    second = FakeSink("https://console/2")
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, MARKDOWN, name="b", where="p")], None
    )
    report = await exporting.publish_session(
        SESSION,
        places,
        request(),
        ports(FakeSinks({1: FakeSink(), 2: second}), records=FakeRecords([recorded(1)])),
        NOW,
    )
    assert len(second.created) == 1
    assert len(report.published) == 1


async def test_a_sweep_that_had_nothing_left_to_do_does_not_raise() -> None:
    """Every destination already recorded is a session that is finished,
    not a session that failed everywhere."""
    report = await exporting.publish_session(
        SESSION,
        (destination(1),),
        request(),
        ports(FakeSinks({1: FakeSink()}), records=FakeRecords([recorded(1)])),
        NOW,
    )
    assert report.skipped == 1
    assert report.failed == 0


# ---------------------------------------------------------------------------
# The primary
# ---------------------------------------------------------------------------


async def test_the_primary_is_the_destination_the_guild_configured_first() -> None:
    """`session.document_url` is what the announcement posts, so exactly
    one destination has to be it. The oldest enabled target: it does not
    move when somebody renames a destination, and it is the one a guild
    that has only ever had one destination has always had."""
    first, second = FakeSink("https://outline/first"), FakeSink("https://console/second")
    places = exporting.destinations_for(
        SESSION,
        [target(9, MARKDOWN, name="added later", where="p"), target(2, OUTLINE, name="wiki")],
        None,
    )
    report = await exporting.publish_session(
        SESSION, places, request(), ports(FakeSinks({2: first, 9: second})), NOW
    )
    assert report.primary is not None
    assert report.primary.destination.target_id == 2
    assert report.primary.document.url == "https://outline/first"


async def test_a_failed_primary_leaves_no_primary_to_announce() -> None:
    """So `session.document_url` is not written, the session stays
    undocumented, and the sweep brings it back -- with the destination that
    already worked skipped."""
    places = exporting.destinations_for(
        SESSION, [target(1, OUTLINE), target(2, MARKDOWN, name="b", where="p")], None
    )
    report = await exporting.publish_session(
        SESSION,
        places,
        request(),
        ports(FakeSinks({1: FakeSink(fail=RuntimeError("down")), 2: FakeSink()})),
        NOW,
    )
    assert report.primary is None
    assert len(report.published) == 1


async def test_a_primary_already_recorded_is_still_reported_as_the_primary() -> None:
    """The narrow window where the document was created and recorded but
    stamping the session failed. Without this the sweep would skip the
    primary for ever and never write `document_url`, so the announcement
    would never go out for a session that has a perfectly good document."""
    report = await exporting.publish_session(
        SESSION,
        (destination(1),),
        request(),
        ports(FakeSinks({1: FakeSink()}), records=FakeRecords([recorded(1, "https://outline/x")])),
        NOW,
    )
    assert report.primary is not None
    assert report.primary.document.url == "https://outline/x"


async def test_publishing_nowhere_at_all_is_not_an_error() -> None:
    """A guild that has configured nothing. There is no document to make
    and nothing failed; raising would fill the log with a sweep's worth of
    warnings about guilds that never asked for a protocol."""
    report = await exporting.publish_session(SESSION, (), request(), ports(), NOW)
    assert report.primary is None
    assert report.failed == 0
