"""The format registry: a format is a renderer and a sink, not just a sink.

The defect this file exists to prevent is one string away. `render_transcript`
emits Outline-flavoured Markdown -- `mention://` chips, and every value passed
through `escape_markdown` -- and a sink that is not Outline handed that string
publishes the chip syntax as literal text and the backslashes with it. So the
tests below assert on both halves of each entry: what the renderer produced,
and which sink family the entry is paired with.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sturnus.application import export_formats
from sturnus.application.documents import ChannelRef
from sturnus.domain.transcript import SpeakerIdentity, Transcript, TranscriptBlock

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)

OUTLINE_TEMPLATE = (
    Path(__file__).parent.parent.parent
    / "src/sturnus/infrastructure/documents/outline_template.md.j2"
).read_text(encoding="utf-8")

LINKED = SpeakerIdentity(
    1234,
    "maxm",
    external_user_id="c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f",
    external_display_name="Max Example",
)
GUEST = SpeakerIdentity(9876, "guestuser")


def transcript(*blocks: TranscriptBlock) -> Transcript:
    speakers: list[SpeakerIdentity] = []
    for b in blocks:
        if b.speaker not in speakers:
            speakers.append(b.speaker)
    return Transcript(
        session_started_at=T0,
        session_ended_at=T0 + timedelta(hours=1),
        participants=tuple(speakers),
        blocks=blocks,
    )


def block(speaker: SpeakerIdentity, offset: int, text: str) -> TranscriptBlock:
    return TranscriptBlock(speaker=speaker, start=T0 + timedelta(seconds=offset), text=text)


def request(
    *blocks: TranscriptBlock,
    channel: ChannelRef | None = None,
    tz: object = UTC,
) -> export_formats.RenderRequest:
    return export_formats.RenderRequest(
        transcript=transcript(*blocks),
        tz=tz,  # type: ignore[arg-type]
        channel=channel,
        outline_template=OUTLINE_TEMPLATE,
    )


def render(name: str, *blocks: TranscriptBlock, **kw: object) -> str:
    found = export_formats.format_named(name)
    assert found is not None
    return found.render(request(*blocks, **kw))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_every_implemented_format_is_reachable_by_name() -> None:
    """The three formats this deployment can actually publish."""
    assert set(export_formats.supported_formats()) == {"outline", "markdown", "html"}


def test_a_format_this_deployment_does_not_implement_is_simply_absent() -> None:
    """`pdf` and `confluence` are specified and deliberately not built.

    Absent rather than present-and-broken: a target configured for a format
    nothing can render must be refused where an administrator can read the
    refusal, not accepted and then silently skipped every time a meeting ends.
    """
    assert export_formats.format_named("pdf") is None
    assert export_formats.format_named("confluence") is None


def test_an_unknown_format_is_none_rather_than_an_error() -> None:
    """A row naming a format this code does not know must not take the
    guild's other destinations down with it -- see `ExportTarget.format`."""
    assert export_formats.format_named("smoke-signal") is None


def test_the_registry_is_keyed_by_the_name_each_entry_carries() -> None:
    """A registry whose key and whose entry disagree is a registry that
    dispatches on one and reports the other."""
    for name, entry in export_formats.FORMATS.items():
        assert entry.name == name


def test_every_entry_names_a_sink_family_and_a_media_type() -> None:
    for entry in export_formats.FORMATS.values():
        assert entry.sink in {export_formats.OUTLINE_SINK, export_formats.OBJECT_STORE_SINK}
        assert entry.media_type
        assert entry.file_extension


# ---------------------------------------------------------------------------
# The catalogue: what exists, and what this build can run
# ---------------------------------------------------------------------------


def test_the_catalogue_names_everything_the_specification_names() -> None:
    """Five formats, not three. `supported_formats` answers "what may be
    configured"; the catalogue answers "what exists", which is the question
    an interface has to render and the larger of the two."""
    assert {entry.name for entry in export_formats.catalogue()} == {
        "outline",
        "markdown",
        "html",
        "pdf",
        "confluence",
    }


def test_a_format_this_build_runs_is_available_and_names_its_sink() -> None:
    available = {entry.name: entry for entry in export_formats.catalogue() if entry.available}
    assert set(available) == set(export_formats.supported_formats())
    for name, entry in available.items():
        assert entry.sink == export_formats.FORMATS[name].sink


def test_an_unbuilt_format_is_reported_as_unavailable_with_no_sink() -> None:
    """`sink` is `None` rather than a guess.

    A sink family is what decides whether a `target` is an address in
    Outline or a key prefix in an object store, and nothing here has
    decided what would carry a PDF. Naming one would be this module
    inventing an answer and every reader downstream believing it.
    """
    unbuilt = {entry.name: entry for entry in export_formats.catalogue() if not entry.available}
    assert set(unbuilt) == set(export_formats.UNBUILT)
    for entry in unbuilt.values():
        assert entry.sink is None


def test_no_name_is_both_buildable_and_unbuilt() -> None:
    """The two halves of the catalogue are what a name is moved *between*
    when a format is finally built. A name left in both would be reported
    twice, and a console would render it as choosable and as refused at
    once."""
    assert set(export_formats.UNBUILT).isdisjoint(export_formats.FORMATS)


def test_the_catalogue_offers_what_a_guild_already_publishes_to_first() -> None:
    """Order is part of the answer: a reader offered a list reads the first
    row as the ordinary one, and `outline` is what every guild published to
    before any of the others existed."""
    assert [entry.name for entry in export_formats.catalogue()][:3] == list(
        export_formats.supported_formats()
    )


def test_the_catalogue_carries_no_renderer_and_no_media_type() -> None:
    """It is the answer to "what may I configure?", and a caller asking
    that can use none of it. `media_type` and the renderer are read inside
    this process, from `ExportFormat`, by the sink and by the route that
    serves an artefact back."""
    entry = export_formats.catalogue()[0]
    assert not hasattr(entry, "render")
    assert not hasattr(entry, "media_type")
    assert not hasattr(entry, "target_pattern")


# ---------------------------------------------------------------------------
# outline: today's behaviour, unchanged
# ---------------------------------------------------------------------------


def test_the_outline_format_renders_through_the_packaged_template() -> None:
    """Byte-for-byte what the worker produced before the registry existed."""
    from sturnus.application.documents import render_transcript

    blocks = (block(LINKED, 0, "hello"), block(GUEST, 30, "hi"))
    expected = render_transcript(transcript(*blocks), OUTLINE_TEMPLATE, UTC, None)
    assert render("outline", *blocks) == expected


def test_the_outline_format_still_emits_mention_chips() -> None:
    body = render("outline", block(LINKED, 0, "hello"))
    assert f"mention://user/{LINKED.external_user_id}" in body


def test_the_outline_format_goes_to_the_outline_sink() -> None:
    entry = export_formats.format_named("outline")
    assert entry is not None
    assert entry.sink == export_formats.OUTLINE_SINK


# ---------------------------------------------------------------------------
# markdown: plain CommonMark, and no Outline syntax anywhere in it
# ---------------------------------------------------------------------------


def test_plain_markdown_names_a_linked_speaker_without_a_mention_chip() -> None:
    """`mention://` is Outline's own syntax. Anywhere else it is five
    characters of protocol scheme rendered as literal text next to
    somebody's name."""
    body = render("markdown", block(LINKED, 0, "hello"))
    assert "mention://" not in body
    assert "Max Example" in body


def test_plain_markdown_falls_back_to_the_discord_name_for_an_unlinked_speaker() -> None:
    body = render("markdown", block(GUEST, 0, "hello"))
    assert "guestuser" in body


def test_plain_markdown_still_escapes_a_hostile_display_name() -> None:
    """Plain does not mean unescaped. A display name is attacker-controlled
    text on its way into a Markdown document, exactly as it is for Outline."""
    hostile = SpeakerIdentity(5, "[click here](https://evil.example)")
    body = render("markdown", block(hostile, 0, "hello"))
    assert "[click here](https://evil.example)" not in body
    assert "\\[click here\\]" in body


def test_plain_markdown_escapes_hostile_transcript_text() -> None:
    body = render("markdown", block(GUEST, 0, "![img](https://evil.example/x.png)"))
    assert "![img](https://evil.example/x.png)" not in body


def test_plain_markdown_carries_the_transcript_and_the_participants() -> None:
    body = render("markdown", block(LINKED, 0, "the first thing"), block(GUEST, 30, "the second"))
    assert "the first thing" in body
    assert "the second" in body
    assert body.index("the first thing") < body.index("the second")


def test_plain_markdown_goes_to_the_object_store() -> None:
    entry = export_formats.format_named("markdown")
    assert entry is not None
    assert entry.sink == export_formats.OBJECT_STORE_SINK
    assert entry.file_extension == "md"


# ---------------------------------------------------------------------------
# html: HTML escaping, and never the Markdown escaper
# ---------------------------------------------------------------------------


def test_html_escapes_a_hostile_display_name_as_html() -> None:
    """The whole reason a format is a pair. `escape_markdown` would answer
    a `<script>` in a display name with a backslash in front of nothing --
    every character of the tag survives, and the browser runs it."""
    hostile = SpeakerIdentity(5, "<script>alert(1)</script>")
    body = render("html", block(hostile, 0, "hello"))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_html_escapes_hostile_transcript_text_as_html() -> None:
    body = render("html", block(GUEST, 0, "<img src=x onerror=alert(1)>"))
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img" in body


def test_html_does_not_carry_markdown_backslash_escapes() -> None:
    """`escape_markdown` escapes `.` and `-` among others, so a sentence
    that went through it arrives full of backslashes -- visible ones, in
    HTML, where nothing ever removes them again."""
    body = render("html", block(GUEST, 0, "one. two-three!"))
    assert "one. two-three!" in body
    assert "\\." not in body


def test_html_is_a_whole_document_a_browser_can_open() -> None:
    """It is served from an object store on its own, not embedded in a
    page somebody else wrote."""
    body = render("html", block(GUEST, 0, "hello"))
    assert body.lstrip().lower().startswith("<!doctype html>")
    assert "<title>" in body
    assert 'charset="utf-8"' in body or "charset=utf-8" in body


def test_html_carries_no_mention_syntax() -> None:
    body = render("html", block(LINKED, 0, "hello"))
    assert "mention://" not in body


def test_html_goes_to_the_object_store_as_text_html() -> None:
    entry = export_formats.format_named("html")
    assert entry is not None
    assert entry.sink == export_formats.OBJECT_STORE_SINK
    assert entry.media_type == "text/html; charset=utf-8"
    assert entry.file_extension == "html"


# ---------------------------------------------------------------------------
# What every renderer owes its reader, whichever one it is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["outline", "markdown", "html"])
def test_every_format_localises_times_to_the_configured_timezone(name: str) -> None:
    """20:00 UTC is 22:00 in Berlin, and a protocol read in Berlin that
    says 20:00 is wrong in a way that does not look wrong."""
    body = render(name, block(GUEST, 0, "hello"), tz=ZoneInfo("Europe/Berlin"))
    assert "22:00" in body


@pytest.mark.parametrize("name", ["outline", "markdown", "html"])
def test_every_format_renders_without_a_channel(name: str) -> None:
    """A session recorded before the channel name was captured still gets
    a protocol; the heading is simply absent."""
    assert render(name, block(GUEST, 0, "hello"), channel=None)


@pytest.mark.parametrize("name", ["outline", "markdown", "html"])
def test_every_format_names_the_channel_when_there_is_one(name: str) -> None:
    """A channel name with no metacharacter in it, so this asserts on the
    heading being present rather than on how each format escapes -- which
    is what the escaping tests above are for, one per format."""
    body = render(name, block(GUEST, 0, "hello"), channel=ChannelRef(1, 4711, "meetingraum"))
    assert "meetingraum" in body
    assert "https://discord.com/channels/1/4711" in body


# ---------------------------------------------------------------------------
# What a target string may say, per format
# ---------------------------------------------------------------------------


def test_an_object_store_target_may_not_climb_out_of_its_prefix() -> None:
    """The target becomes part of an object key. `..` in one is not a
    traversal in S3, but it is a key nobody meant to write and one no
    listing groups where the administrator expects it."""
    entry = export_formats.format_named("markdown")
    assert entry is not None
    assert entry.accepts_target("protocols")
    assert entry.accepts_target("team/protocols")
    assert not entry.accepts_target("../secrets")
    assert not entry.accepts_target("/absolute")
    assert not entry.accepts_target("")


def test_an_outline_target_is_a_collection_id() -> None:
    entry = export_formats.format_named("outline")
    assert entry is not None
    assert entry.accepts_target("c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f")
    assert not entry.accepts_target("")
    assert not entry.accepts_target("a collection with spaces")
