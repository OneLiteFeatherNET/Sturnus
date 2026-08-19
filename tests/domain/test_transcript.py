from datetime import UTC, datetime, timedelta

from sturnus.domain.transcript import Segment, SpeakerIdentity, Transcript, build_transcript

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA = SpeakerIdentity(1, "anna", external_user_id="out-1", external_display_name="Anna Example")
BEN = SpeakerIdentity(2, "ben")


def seg(speaker: SpeakerIdentity, offset: int, length: int, text: str) -> Segment:
    return Segment(
        speaker=speaker,
        start=T0 + timedelta(seconds=offset),
        end=T0 + timedelta(seconds=offset + length),
        text=text,
    )


def build(*segments: Segment) -> Transcript:
    return build_transcript(list(segments), T0, T0 + timedelta(hours=1))


def test_blocks_are_ordered_by_time_across_speakers() -> None:
    t = build(seg(BEN, 30, 3, "second"), seg(ANNA, 0, 2, "first"))
    assert [b.text for b in t.blocks] == ["first", "second"]


def test_consecutive_segments_of_same_speaker_merge() -> None:
    t = build(seg(ANNA, 0, 2, "first half"), seg(ANNA, 3, 2, "second half"))
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "first half second half"
    assert t.blocks[0].start == T0


def test_long_pause_splits_a_block() -> None:
    t = build(seg(ANNA, 0, 2, "before"), seg(ANNA, 300, 2, "after"))
    assert [b.text for b in t.blocks] == ["before", "after"]


def test_other_speaker_interrupts_a_block() -> None:
    t = build(seg(ANNA, 0, 2, "one"), seg(BEN, 3, 1, "interjection"), seg(ANNA, 5, 2, "three"))
    assert [b.text for b in t.blocks] == ["one", "interjection", "three"]


def test_participants_are_unique_and_ordered_by_first_appearance() -> None:
    t = build(seg(BEN, 0, 1, "b"), seg(ANNA, 5, 1, "a"), seg(BEN, 9, 1, "b again"))
    assert t.participants == (BEN, ANNA)


def test_empty_and_whitespace_segments_are_dropped() -> None:
    t = build(seg(ANNA, 0, 1, "   "), seg(ANNA, 60, 1, "real"))
    assert [b.text for b in t.blocks] == ["real"]


def test_no_segments_yields_empty_transcript() -> None:
    t = build()
    assert t.blocks == ()
    assert t.participants == ()


def test_transcript_carries_session_bounds() -> None:
    t = build(seg(ANNA, 0, 1, "x"))
    assert t.session_started_at == T0
    assert t.session_ended_at == T0 + timedelta(hours=1)


def test_model_carries_no_markup() -> None:
    t = build(seg(ANNA, 0, 1, "plain text"))
    assert t.blocks[0].text == "plain text"
    assert t.blocks[0].speaker.external_display_name == "Anna Example"


def test_nested_segment_does_not_break_merge() -> None:
    """Short segment entirely inside a longer one should not break the run."""
    t = build(
        seg(ANNA, 0, 20, "long"),  # T0 to T0+20
        seg(ANNA, 2, 1, "short"),  # T0+2 to T0+3 (nested inside "long")
        seg(ANNA, 20, 1, "after"),  # T0+20 to T0+21 (gap 0 from "long")
    )
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "long short after"


def test_partial_overlap_continues_merge() -> None:
    """Segment starting before first ends, extending past it."""
    t = build(
        seg(ANNA, 0, 5, "first"),  # T0 to T0+5
        seg(ANNA, 3, 5, "second"),  # T0+3 to T0+8 (overlaps, extends past)
        seg(ANNA, 8, 1, "third"),  # T0+8 to T0+9 (gap 0 from "second")
    )
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "first second third"


def test_participants_dedup_by_discord_id_prefer_external_fields() -> None:
    """Dedup by discord_user_id; prefer variant with external fields."""
    anna_no_external = SpeakerIdentity(1, "anna")
    anna_with_external = SpeakerIdentity(
        1, "anna", external_user_id="out-1", external_display_name="Anna Example"
    )

    t = build(
        seg(anna_no_external, 0, 1, "first"),
        seg(anna_with_external, 5, 1, "second"),
    )
    assert len(t.participants) == 1
    assert t.participants[0].discord_user_id == 1
    assert t.participants[0].external_user_id == "out-1"
    assert t.participants[0].external_display_name == "Anna Example"
