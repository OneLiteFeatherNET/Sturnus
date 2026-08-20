from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sturnus.application.documents import (
    ChannelRef,
    document_title,
    escape_markdown,
    render_transcript,
)
from sturnus.domain.transcript import SpeakerIdentity, Transcript, TranscriptBlock
from sturnus.infrastructure.templates.markdown import (
    escape_markdown as infrastructure_escape_markdown,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
TEMPLATE = (
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


def render(*blocks: TranscriptBlock) -> str:
    return render_transcript(transcript(*blocks), TEMPLATE, tz=UTC)


def test_a_linked_speaker_is_rendered_as_a_mention() -> None:
    """The id is a real Outline UUID (it has dashes), not the old dash-free
    test fixture that let a broken template pass unnoticed: `escape_markdown`
    backslash-escapes `-`, so an id that comes from Outline's own API --
    not attacker-controlled, unlike the display name beside it -- must
    reach the mention verbatim, or Outline treats it as malformed and never
    resolves it to a notification.

    The two-segment `mention://user/<uuid>` shape is deliberate and load-
    bearing: Outline also accepts a three-segment
    `mention://<node-id>/<type>/<user-id>` form, and older builds accept ONLY
    that one. `docs/verification/outline-mentions.md` records which builds
    parse which, against the version this deployment runs. A build that does
    not parse the form emitted here renders the mention as an ordinary link --
    no chip, no notification, no error.
    """
    out = render(block(LINKED, 0, "hello"))
    assert "@[Max Example](mention://user/c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f)" in out
    assert "\\" not in out.split("mention://user/", 1)[1].split(")", 1)[0]


def test_a_linked_speaker_also_carries_the_discord_link() -> None:
    out = render(block(LINKED, 0, "hello"))
    assert "https://discord.com/users/1234" in out


def test_an_unlinked_speaker_gets_no_mention_but_keeps_the_link() -> None:
    out = render(block(GUEST, 0, "hello"))
    assert "mention://user/" not in out
    assert "https://discord.com/users/9876" in out


def test_the_spoken_text_appears() -> None:
    assert "hello there" in render(block(GUEST, 0, "hello there"))


def test_blocks_appear_in_order() -> None:
    out = render(block(GUEST, 0, "first"), block(LINKED, 60, "second"))
    assert out.index("first") < out.index("second")


def test_the_document_does_not_start_with_a_heading() -> None:
    """Outline keeps the title in its own field (Spec 8.3)."""
    assert not render(block(GUEST, 0, "x")).lstrip().startswith("# ")


def test_a_hostile_display_name_cannot_inject_a_link() -> None:
    hostile = SpeakerIdentity(5, "x](https://evil.example) [y")
    out = render(block(hostile, 0, "text"))
    assert "evil.example" not in out or "\\]" in out


def test_hostile_transcript_text_is_escaped() -> None:
    out = render(block(GUEST, 0, "[click](https://evil.example)"))
    assert "](https://evil.example)" not in out


def test_the_title_carries_date_and_time() -> None:
    title = document_title(transcript(block(GUEST, 0, "x")), tz=UTC)
    assert "2026-08-19" in title


def test_a_participant_list_is_present() -> None:
    out = render(block(LINKED, 0, "a"), block(GUEST, 10, "b"))
    assert "Max Example" in out
    assert "guestuser" in out


def test_the_application_and_infrastructure_escaping_agree() -> None:
    """Pins the two layers to a single escaping rule.

    `sturnus.application.documents.escape_markdown` is used directly by
    `render_transcript` above; `sturnus.infrastructure.templates.markdown`
    re-exports the very same function for the other Markdown-producing
    adapters (e.g. the Jinja2 `md` filter in
    `sturnus.infrastructure.templates.engine`). This test imports both
    entry points and checks they produce identical output for a hostile
    input, so that a future edit which reintroduces a second, independent
    character set -- rather than editing this one shared definition --
    fails here instead of leaving an invisible gap between the two layers.
    """
    hostile = "[click](https://evil.example) *bold* `code` # heading \\ [x] {y} !z |q >r ~s -t +u"
    assert escape_markdown(hostile) == infrastructure_escape_markdown(hostile)
    # And it is not merely a lookalike: the infrastructure re-export must be
    # the exact same function object, not a second, independently maintained
    # copy of the character set that happens to agree today.
    assert infrastructure_escape_markdown is escape_markdown


# ---------------------------------------------------------------------------
# The heading: where the meeting was held, when, and for how long.
# ---------------------------------------------------------------------------

BERLIN = ZoneInfo("Europe/Berlin")
CHANNEL = ChannelRef(752527676903784518, 1166491913877196960, "Meeting-Raum")


def test_times_are_written_in_the_configured_timezone() -> None:
    """A protocol is read by the people who were in the room, so the times in
    it are theirs. UTC would be wrong in a way that does not look wrong --
    any hour reads as a plausible meeting time.
    """
    out = render_transcript(transcript(block(LINKED, 0, "hallo")), TEMPLATE, BERLIN, CHANNEL)
    # T0 is 20:00 UTC, which is 22:00 in Berlin in August (CEST).
    assert "**22:00:00**" in out
    assert "**20:00:00**" not in out


def test_the_heading_links_the_channel_the_meeting_was_held_in() -> None:
    out = render_transcript(transcript(block(LINKED, 0, "hallo")), TEMPLATE, BERLIN, CHANNEL)
    assert "https://discord.com/channels/752527676903784518/1166491913877196960" in out
    assert "Meeting" in out


def test_a_channel_without_a_known_name_still_links() -> None:
    """Sessions recorded before the name was captured have none. The heading
    falls back to the id rather than disappearing: a link that opens the
    right channel beats no channel at all.
    """
    ref = ChannelRef(752527676903784518, 1166491913877196960)
    out = render_transcript(transcript(block(LINKED, 0, "hallo")), TEMPLATE, BERLIN, ref)
    assert "1166491913877196960" in out
    assert ref.url in out


def test_the_date_is_rendered_as_an_outline_date_mention() -> None:
    """Outline turns `mention://date/<YYYY-MM-DD>` into a date chip
    (MentionType.Date, present since well before the deployed version).
    Date only -- the deployed parser accepts no time component, so an ISO
    datetime would degrade to a plain link.
    """
    out = render_transcript(transcript(block(LINKED, 0, "hallo")), TEMPLATE, BERLIN, CHANNEL)
    assert "(mention://date/2026-08-19)" in out
    assert "T22:00" not in out


def test_a_protocol_without_a_channel_still_renders() -> None:
    """`channel` is optional so a caller that cannot resolve it still gets a
    protocol; the heading simply omits the line.
    """
    out = render_transcript(transcript(block(LINKED, 0, "hallo")), TEMPLATE, BERLIN)
    assert "Channel:" not in out
    assert "hallo" in out
    assert "**Date:**" in out


def test_the_attribution_and_the_words_are_separate_paragraphs() -> None:
    """A single newline is a *soft* break, which Markdown renders inline.

    The template writes a blank line between the speaker's name and what
    they said, but `trim_blocks=True` ate the newline that ended the
    attribution line, leaving one where two were written. The published
    protocol therefore read `...(TheMeinerLP | Phillipp) Thank you.` -- the
    name running straight into the sentence.

    Asserted on the exact byte sequence rather than on `in`, because `in`
    passes for both the broken and the fixed rendering.
    """
    out = render(block(GUEST, 3, "Erstens."))

    attribution = f"[guestuser](https://discord.com/users/{GUEST.discord_user_id})"
    assert f"{attribution}\n\nErstens" in out
    assert f"{attribution}\nErstens" not in out


def test_the_channel_and_the_date_are_separate_paragraphs() -> None:
    """The same soft-break defect, in the header.

    Here nothing ate anything -- the template simply wrote one newline
    between two lines that have to be two. `**Channel:** ... **Date:** ...`
    on one line is legible but reads as a single run-on fact.
    """
    out = render_transcript(
        transcript(block(GUEST, 3, "Erstens.")), TEMPLATE, tz=UTC, channel=CHANNEL
    )

    assert f"]({CHANNEL.url})\n\n**Date:**" in out
    assert f"]({CHANNEL.url})\n**Date:**" not in out


def test_the_participants_list_stays_tight() -> None:
    """No blank line between the items.

    A blank line makes it a "loose" list, which Markdown wraps each item in
    a paragraph for and renders with a gap between them. The blank line the
    transcript needs is exactly the one this list must not have -- fixing
    one by hand is how the other breaks.
    """
    out = render(block(LINKED, 3, "Erstens."), block(GUEST, 90, "Zweitens."))

    listing = out.split("## Participants", 1)[1].split("## Transcript", 1)[0]
    items = [line for line in listing.splitlines() if line.startswith("- ")]
    assert len(items) == 2
    # Only the gaps *between* items. The blank line separating the heading
    # from the first item is required and must not be caught here.
    body = listing.strip("\n")
    assert "\n\n- " not in body


def test_a_speaker_is_rendered_the_same_way_in_both_places() -> None:
    """The macro's reason for existing, asserted rather than assumed.

    The two renderings used to be the same conditional written out twice,
    which is two chances to drift -- and a mention that resolves in the
    participant list but not in the transcript is worse than one that fails
    in both, because nobody goes looking for it.
    """
    out = render(block(LINKED, 3, "Erstens."))

    mention = f"@[Max Example](mention://user/{LINKED.external_user_id})"
    assert out.count(mention) == 2
