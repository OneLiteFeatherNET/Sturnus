"""The object-store sink, and the resolver that decides which sink runs.

Two load-bearing assertions in this file. The first is the one about the
URL: a presigned S3 URL would satisfy `CreatedDocument` and would be
wrong -- it works for anybody it is forwarded to, it keeps working after a
participation ends, and nothing can revoke it. The URL a protocol's link
carries has to point back at the console, where the rule is checked on
every request.

The second is that **what lands in the bucket is not the protocol**. A
Markdown export is every word every participant said; access control is
the console route in front of it, encryption is a property of the object,
and the two answer different questions about a bucket somebody has a copy
of. The sink is given a store whose only write method seals, so there is
no plaintext spelling of this act left to test for.
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
from sturnus.domain.errors import UnreadableArtefact
from sturnus.infrastructure.crypto import KeyWrapper, is_sealed_artefact
from sturnus.infrastructure.documents.artefacts import SealedArtefacts
from sturnus.infrastructure.documents.sinks import DocumentSinks, ObjectStoreSink
from sturnus.infrastructure.objectstore import S3DocumentStore

BUCKET = "sturnus-audio"
T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD = 4711
MASTER = b"0" * 32


def sealed_store() -> SealedArtefacts:
    return SealedArtefacts(
        S3DocumentStore(endpoint=None, bucket=BUCKET, access_key="ak", secret_key="sk"),
        KeyWrapper(master_key=MASTER, key_id="k1"),
    )


@pytest.fixture
def store() -> Iterator[SealedArtefacts]:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield sealed_store()


def raw(key: str) -> bytes:
    """What is actually in the bucket, read without going through the seal."""
    stored = boto3.client("s3", region_name="us-east-1").get_object(Bucket=BUCKET, Key=key)
    body: bytes = stored["Body"].read()
    return body


def destination(
    format: str = MARKDOWN, target_id: int | None = 7, where: str = "protocols"
) -> Destination:
    entry = format_named(format)
    assert entry is not None
    return Destination(
        session_id=42,
        target_id=target_id,
        format=entry,
        target=where,
        provider=format,
        guild_id=GUILD,
    )


def at(store: SealedArtefacts, target_id: int) -> ObjectStoreSink:
    """The Markdown sink for one of a guild's several destinations."""
    entry = format_named(MARKDOWN)
    assert entry is not None
    return ObjectStoreSink(
        store,
        console_origin="https://sturnus.example",
        session_id=42,
        target_id=target_id,
        guild_id=GUILD,
        file_extension=entry.file_extension,
    )


def sink(store: SealedArtefacts, format: str = MARKDOWN) -> ObjectStoreSink:
    entry = format_named(format)
    assert entry is not None
    return ObjectStoreSink(
        store,
        console_origin="https://sturnus.example",
        session_id=42,
        target_id=7,
        guild_id=GUILD,
        file_extension=entry.file_extension,
    )


# ---------------------------------------------------------------------------
# What the sink writes, and what it hands back
# ---------------------------------------------------------------------------


async def test_the_rendered_protocol_reaches_the_object_store(store: SealedArtefacts) -> None:
    await sink(store).create("A meeting", "# Minutes\n", "protocols")
    assert await store.get("protocols/42/7.md", guild_id=GUILD) == b"# Minutes\n"


async def test_what_lands_in_the_bucket_is_not_the_protocol(store: SealedArtefacts) -> None:
    """The gap this closes. Every other object in this bucket -- the
    recordings, the stored spectrograms -- is ciphertext, and a Markdown
    export is the most sensitive of the lot: every word every participant
    said, in one object."""
    await sink(store).create("A meeting", "Anna said something\n", "protocols")
    stored = raw("protocols/42/7.md")
    assert b"Anna said something" not in stored
    assert is_sealed_artefact(stored)


async def test_the_object_does_not_claim_to_be_a_document(store: SealedArtefacts) -> None:
    """The media type describes the protocol and no longer describes the
    object. A bucket listing that calls a sealed envelope `text/html`
    invites the next reader to treat it as text; the real media type
    reaches the browser from the format registry, which is where it always
    came from."""
    await sink(store, HTML).create("A meeting", "<p>hi</p>", "protocols")
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket=BUCKET, Key="protocols/42/7.html"
    )
    assert stored["ContentType"] == "application/octet-stream"


async def test_the_document_id_is_the_object_key(store: SealedArtefacts) -> None:
    """`session_document.document_id` is what the console route reads back
    to find the bytes, so it has to be a real identifier in the store that
    holds them rather than a second, invented one."""
    created = await sink(store).create("A meeting", "body", "protocols")
    assert created.id == "protocols/42/7.md"


async def test_the_url_points_at_the_console_and_not_at_the_object_store(
    store: SealedArtefacts,
) -> None:
    """The whole argument of §3.2. A presigned S3 URL outlives the access
    rules that issued it and cannot be revoked; this one is answered by a
    route that re-checks participation on every request."""
    created = await sink(store).create("A meeting", "body", "protocols")
    assert created.url == "https://sturnus.example/api/sessions/42/documents/7"
    assert "X-Amz-Signature" not in created.url
    assert BUCKET not in created.url


async def test_two_destinations_of_one_format_get_two_objects(store: SealedArtefacts) -> None:
    """A guild publishing Markdown to two prefixes wants two artefacts.
    One key for both would have the second overwrite the first."""
    for target_id, prefix in ((7, "team"), (9, "archive")):
        await at(store, target_id).create("A meeting", f"body {target_id}", prefix)
    assert await store.get("team/42/7.md", guild_id=GUILD) == b"body 7"
    assert await store.get("archive/42/9.md", guild_id=GUILD) == b"body 9"


async def test_two_artefacts_never_share_a_key(store: SealedArtefacts) -> None:
    """A data key per artefact, so no two objects are sealed under one and
    no nonce is ever reused. Two identical bodies at two addresses are two
    different envelopes."""
    for target_id, prefix in ((7, "team"), (9, "archive")):
        await at(store, target_id).create("A meeting", "the same body", prefix)
    assert raw("team/42/7.md") != raw("archive/42/9.md")


async def test_a_re_export_replaces_the_artefact_at_the_same_address(
    store: SealedArtefacts,
) -> None:
    """`SessionDocumentStore.record` upserts on `(session_id, target_id)`,
    so a second artefact at a second key would leave a row pointing at one
    of two objects with nothing saying which is current.

    It is also how a protocol written in clear before this format existed
    becomes a sealed one: the object at that address is replaced, in
    place."""
    await sink(store).create("A meeting", "first", "protocols")
    created = await sink(store).create("A meeting", "second", "protocols")
    assert await store.get(created.id, guild_id=GUILD) == b"second"


async def test_reading_an_artefact_that_is_not_there_is_a_key_error(
    store: SealedArtefacts,
) -> None:
    with pytest.raises(KeyError):
        await store.get("protocols/1/1.md", guild_id=GUILD)


# ---------------------------------------------------------------------------
# The seal, and what it refuses
# ---------------------------------------------------------------------------


async def test_an_artefact_does_not_open_for_another_guild(store: SealedArtefacts) -> None:
    """What the associated data buys. Somebody who can write to the bucket
    but cannot decrypt copies one guild's artefact onto another guild's
    key; the second guild's reader gets a refusal rather than the first
    guild's meeting."""
    created = await sink(store).create("A meeting", "# Minutes\n", "protocols")
    with pytest.raises(UnreadableArtefact):
        await store.get(created.id, guild_id=GUILD + 1)


async def test_an_artefact_does_not_open_under_another_master_key(
    store: SealedArtefacts,
) -> None:
    created = await sink(store).create("A meeting", "# Minutes\n", "protocols")
    other = SealedArtefacts(
        S3DocumentStore(endpoint=None, bucket=BUCKET, access_key="ak", secret_key="sk"),
        KeyWrapper(master_key=b"1" * 32, key_id="k1"),
    )
    with pytest.raises(UnreadableArtefact):
        await other.get(created.id, guild_id=GUILD)


async def test_an_edited_object_does_not_open(store: SealedArtefacts) -> None:
    """A protocol is served from the console's own origin and one of its
    formats is HTML, so a body somebody can edit in the bucket is a page
    somebody can edit into that origin. It is authenticated, not merely
    encrypted."""
    created = await sink(store, HTML).create("A meeting", "<p>hi</p>", "protocols")
    tampered = bytearray(raw(created.id))
    tampered[-1] ^= 0xFF
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key=created.id, Body=bytes(tampered)
    )
    with pytest.raises(UnreadableArtefact):
        await store.get(created.id, guild_id=GUILD)


async def test_a_protocol_stored_before_the_seal_existed_is_still_served(
    store: SealedArtefacts,
) -> None:
    """The release that introduced export targets wrote these in clear.
    Nothing sweeps them, and refusing them would turn every link already
    handed to a participant into a 404 over bytes that are already in the
    bucket. They are served, and logged on every read, until a re-export
    replaces one."""
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key="protocols/42/7.md", Body=b"# Minutes\n"
    )
    assert await store.get("protocols/42/7.md", guild_id=GUILD) == b"# Minutes\n"


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
    store: SealedArtefacts,
) -> None:
    sinks = DocumentSinks(objects=store, console_origin="https://sturnus.example")
    assert isinstance(sinks.sink_for(destination(MARKDOWN)), ObjectStoreSink)


def test_the_resolver_branches_on_the_family_and_not_on_the_format(
    store: SealedArtefacts,
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
    store: SealedArtefacts,
) -> None:
    """`STURNUS_CONSOLE_ORIGIN` is a Helm value somebody types, and a
    doubled slash in a link posted to Discord is a broken link."""
    sinks = DocumentSinks(objects=store, console_origin="https://sturnus.example/")
    resolved = sinks.sink_for(destination(MARKDOWN))
    assert resolved is not None
    created = await resolved.create("A meeting", "body", "protocols")
    assert created.url == "https://sturnus.example/api/sessions/42/documents/7"
