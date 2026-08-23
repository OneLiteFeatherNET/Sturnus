"""The object-store sink, and the resolver that decides which sink runs.

The load-bearing assertion in this file is the one about the URL. A
presigned S3 URL would satisfy `CreatedDocument` and would be wrong: it
works for anybody it is forwarded to, it keeps working after a participation
ends, and nothing can revoke it. The URL a protocol's link carries has to
point back at the console, where the rule is checked on every request.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

from sturnus.application.documents import CreatedDocument, DocumentSink
from sturnus.application.export_formats import HTML, MARKDOWN, OUTLINE, format_named
from sturnus.application.exporting import Destination
from sturnus.infrastructure.documents.sinks import DocumentSinks, ObjectStoreSink
from sturnus.infrastructure.objectstore import S3DocumentStore

BUCKET = "sturnus-audio"
T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[S3DocumentStore]:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3DocumentStore(endpoint=None, bucket=BUCKET, access_key="ak", secret_key="sk")


def destination(
    format: str = MARKDOWN, target_id: int | None = 7, where: str = "protocols"
) -> Destination:
    entry = format_named(format)
    assert entry is not None
    return Destination(
        session_id=42, target_id=target_id, format=entry, target=where, provider=format
    )


def sink(store: S3DocumentStore, format: str = MARKDOWN) -> ObjectStoreSink:
    entry = format_named(format)
    assert entry is not None
    return ObjectStoreSink(
        store,
        console_origin="https://sturnus.example",
        session_id=42,
        target_id=7,
        media_type=entry.media_type,
        file_extension=entry.file_extension,
    )


# ---------------------------------------------------------------------------
# What the sink writes, and what it hands back
# ---------------------------------------------------------------------------


async def test_the_rendered_protocol_reaches_the_object_store(store: S3DocumentStore) -> None:
    await sink(store).create("A meeting", "# Minutes\n", "protocols")
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket=BUCKET, Key="protocols/42/7.md"
    )
    assert stored["Body"].read() == b"# Minutes\n"


async def test_the_object_is_stored_under_its_own_media_type(store: S3DocumentStore) -> None:
    """A browser handed `text/html` renders the protocol; handed
    `binary/octet-stream` it offers to download it."""
    await sink(store, HTML).create("A meeting", "<p>hi</p>", "protocols")
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket=BUCKET, Key="protocols/42/7.html"
    )
    assert stored["ContentType"] == "text/html; charset=utf-8"


async def test_the_document_id_is_the_object_key(store: S3DocumentStore) -> None:
    """`session_document.document_id` is what the console route reads back
    to find the bytes, so it has to be a real identifier in the store that
    holds them rather than a second, invented one."""
    created = await sink(store).create("A meeting", "body", "protocols")
    assert created.id == "protocols/42/7.md"


async def test_the_url_points_at_the_console_and_not_at_the_object_store(
    store: S3DocumentStore,
) -> None:
    """The whole argument of §3.2. A presigned S3 URL outlives the access
    rules that issued it and cannot be revoked; this one is answered by a
    route that re-checks participation on every request."""
    created = await sink(store).create("A meeting", "body", "protocols")
    assert created.url == "https://sturnus.example/api/sessions/42/documents/7"
    assert "X-Amz-Signature" not in created.url
    assert BUCKET not in created.url


async def test_two_destinations_of_one_format_get_two_objects(store: S3DocumentStore) -> None:
    """A guild publishing Markdown to two prefixes wants two artefacts.
    One key for both would have the second overwrite the first."""
    entry = format_named(MARKDOWN)
    assert entry is not None
    for target_id, prefix in ((7, "team"), (9, "archive")):
        await ObjectStoreSink(
            store,
            console_origin="https://sturnus.example",
            session_id=42,
            target_id=target_id,
            media_type=entry.media_type,
            file_extension=entry.file_extension,
        ).create("A meeting", f"body {target_id}", prefix)
    assert await store.get("team/42/7.md") == b"body 7"
    assert await store.get("archive/42/9.md") == b"body 9"


async def test_a_re_export_replaces_the_artefact_at_the_same_address(
    store: S3DocumentStore,
) -> None:
    """`SessionDocumentStore.record` upserts on `(session_id, target_id)`,
    so a second artefact at a second key would leave a row pointing at one
    of two objects with nothing saying which is current."""
    await sink(store).create("A meeting", "first", "protocols")
    created = await sink(store).create("A meeting", "second", "protocols")
    assert await store.get(created.id) == b"second"


async def test_reading_an_artefact_that_is_not_there_is_a_key_error(
    store: S3DocumentStore,
) -> None:
    with pytest.raises(KeyError):
        await store.get("protocols/1/1.md")


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class FakeOutline(DocumentSink):
    async def create(self, title: str, body: str, target: str) -> CreatedDocument:
        raise AssertionError(f"not called: {title} {body} {target}")


def test_an_outline_destination_resolves_to_the_outline_sink() -> None:
    outline = FakeOutline()
    sinks = DocumentSinks(outline=outline)
    assert sinks.sink_for(destination(OUTLINE, 3, "col-1")) is outline


def test_an_object_store_destination_resolves_to_an_object_store_sink(
    store: S3DocumentStore,
) -> None:
    sinks = DocumentSinks(objects=store, console_origin="https://sturnus.example")
    assert isinstance(sinks.sink_for(destination(MARKDOWN)), ObjectStoreSink)


def test_the_resolver_branches_on_the_family_and_not_on_the_format(
    store: S3DocumentStore,
) -> None:
    """`markdown` and `html` are two formats and one family, which is what
    makes `pdf` an entry in the registry rather than a change here."""
    sinks = DocumentSinks(objects=store, console_origin="https://sturnus.example")
    assert isinstance(sinks.sink_for(destination(MARKDOWN)), ObjectStoreSink)
    assert isinstance(sinks.sink_for(destination(HTML)), ObjectStoreSink)


def test_a_destination_this_process_cannot_serve_resolves_to_nothing() -> None:
    """A deployment with no object store configured. `None` rather than a
    raise: one unbuildable destination must not take a guild's working
    Outline document down with it."""
    assert DocumentSinks(outline=FakeOutline()).sink_for(destination(MARKDOWN)) is None


def test_a_deployment_with_no_outline_sink_serves_no_outline_destination() -> None:
    assert DocumentSinks().sink_for(destination(OUTLINE, 3, "col-1")) is None


async def test_the_console_origins_trailing_slash_does_not_double_up(
    store: S3DocumentStore,
) -> None:
    """`STURNUS_CONSOLE_ORIGIN` is a Helm value somebody types, and a
    doubled slash in a link posted to Discord is a broken link."""
    sinks = DocumentSinks(objects=store, console_origin="https://sturnus.example/")
    resolved = sinks.sink_for(destination(MARKDOWN))
    assert resolved is not None
    created = await resolved.create("A meeting", "body", "protocols")
    assert created.url == "https://sturnus.example/api/sessions/42/documents/7"
