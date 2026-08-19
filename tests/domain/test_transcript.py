from datetime import datetime, timedelta, timezone

from sturnus.domain.transcript import Segment, SpeakerIdentity, build_transcript

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
ANNA = SpeakerIdentity(1, "anna", external_user_id="out-1", external_display_name="Anna Example")
BEN = SpeakerIdentity(2, "ben")


def seg(speaker: SpeakerIdentity, offset: int, length: int, text: str) -> Segment:
    return Segment(
        speaker=speaker,
        start=T0 + timedelta(seconds=offset),
        end=T0 + timedelta(seconds=offset + length),
        text=text,
    )


def build(*segments: Segment):
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
