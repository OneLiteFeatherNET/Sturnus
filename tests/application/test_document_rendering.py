from datetime import UTC, datetime, timedelta
from pathlib import Path

from sturnus.application.documents import document_title, escape_markdown, render_transcript
from sturnus.domain.transcript import SpeakerIdentity, Transcript, TranscriptBlock
from sturnus.infrastructure.templates.markdown import (
    escape_markdown as infrastructure_escape_markdown,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
TEMPLATE = (
    Path(__file__).parent.parent.parent
    / "src/sturnus/infrastructure/documents/outline_template.md.j2"
).read_text(encoding="utf-8")

LINKED = SpeakerIdentity(1234, "maxm", external_user_id="9c8b", external_display_name="Max Example")
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
    out = render(block(LINKED, 0, "hello"))
    assert "@[Max Example](mention://user/9c8b)" in out


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
