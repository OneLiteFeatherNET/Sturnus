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

    # The continuation check must key on discord_user_id, not full identity
    # equality, or a mid-session identity variant would split one speaker
    # into several blocks (Spec 8.3: consecutive segments from the same
    # speaker merge). The merged block must also carry the richer identity,
    # never a poorer one than any of its segments had.
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "first second"
    assert t.blocks[0].speaker.external_user_id == "out-1"
    assert t.blocks[0].speaker.external_display_name == "Anna Example"


def test_merge_survives_identity_variants_interleaved() -> None:
    """No-external, with-external, no-external in sequence must still merge to one block."""
    anna_no_external = SpeakerIdentity(1, "anna")
    anna_with_external = SpeakerIdentity(
        1, "anna", external_user_id="out-1", external_display_name="Anna Example"
    )

    t = build(
        seg(anna_no_external, 0, 1, "a"),
        seg(anna_with_external, 3, 1, "b"),
        seg(anna_no_external, 6, 1, "c"),
    )
    assert len(t.participants) == 1
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "a b c"
    # Even though the last segment reverted to the poorer variant, the block
    # must still carry the richer identity seen earlier in the run.
    assert t.blocks[0].speaker.external_user_id == "out-1"


def test_merge_continues_across_a_display_name_change() -> None:
    """A display-name change alone must not split the block: only the ID anchors identity."""
    anna_old_name = SpeakerIdentity(1, "anna")
    anna_new_name = SpeakerIdentity(1, "anna-renamed")

    t = build(
        seg(anna_old_name, 0, 1, "before rename"),
        seg(anna_new_name, 3, 1, "after rename"),
    )
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "before rename after rename"
    assert t.blocks[0].speaker.discord_user_id == 1


CHRIS = SpeakerIdentity(3, "chris")


def test_a_recorded_speaker_with_no_usable_text_is_still_an_attendee() -> None:
    """Being in the room and saying something quotable are different facts.

    `participants` is rendered as the protocol's attendee list *and* as its
    headline count, so leaving a recorded speaker out of it is not a gap in
    the transcript -- it is the document stating that someone was not at the
    meeting. Which is a claim the transcription pipeline is in no position to
    make: a track reaches this point only after the speaker was in the
    channel, consented, was recorded, was gated and was decoded, and the last
    of those steps drops a whole 30-second window at a time on a judgement the
    model itself calls a probability.

    So the two are separated. `blocks` stays exactly as it was -- a speaker
    with nothing to quote is quoted nowhere -- while `participants` reports
    everyone the caller says was recorded.
    """
    t = build_transcript([seg(ANNA, 0, 1, "hallo")], T0, T0 + timedelta(hours=1), recorded=[BEN])

    assert t.participants == (ANNA, BEN)
    assert [b.text for b in t.blocks] == ["hallo"]


def test_a_recorded_speaker_who_also_spoke_is_listed_once() -> None:
    """The roster and the segments overlap on almost every real session."""
    t = build_transcript(
        [seg(ANNA, 0, 1, "hallo")], T0, T0 + timedelta(hours=1), recorded=[ANNA, BEN]
    )

    assert t.participants == (ANNA, BEN)


def test_a_speaking_participant_keeps_the_richer_identity_over_the_roster() -> None:
    """The roster must not overwrite an external link the segments carried.

    `assemble` builds both from the same `SpeakerIdentity`, so this cannot
    diverge today -- but a roster that replaced the identity would silently
    strip the Outline mention off a linked account, which is the one thing
    these identities exist to carry.
    """
    plain_anna = SpeakerIdentity(1, "anna")
    t = build_transcript(
        [seg(ANNA, 0, 1, "hallo")], T0, T0 + timedelta(hours=1), recorded=[plain_anna]
    )

    assert t.participants == (ANNA,)
    assert t.participants[0].external_user_id == "out-1"


def test_the_roster_orders_silent_attendees_after_the_speakers() -> None:
    """Speakers first in the order they spoke, then whoever did not.

    Chronological order is the only order the speaking half has, and a silent
    attendee has no time to be placed at. Appending them keeps the top of the
    list reading the way it did before the roster existed, and keeps the
    rendering deterministic rather than dependent on how the repository
    happened to return the jobs.
    """
    t = build_transcript(
        [seg(BEN, 30, 1, "ben"), seg(ANNA, 0, 1, "anna")],
        T0,
        T0 + timedelta(hours=1),
        recorded=[CHRIS, BEN, ANNA],
    )

    assert t.participants == (ANNA, BEN, CHRIS)


def test_a_session_where_nobody_produced_text_still_names_who_was_recorded() -> None:
    """The production failure, with the invented lines correctly removed.

    Two recordings came back holding nothing but `" Untertitelung des ZDF,
    2020"`. With that text now dropped the protocol is empty, which is
    truthful; a protocol that additionally reported zero participants for a
    call two people were recorded on would not be.
    """
    t = build_transcript([], T0, T0 + timedelta(hours=1), recorded=[ANNA, BEN])

    assert t.blocks == ()
    assert t.participants == (ANNA, BEN)
