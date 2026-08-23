"""What a title and a description may be, before either reaches a column.

Pure, like the rules for a tag, and separated from both the handler and
the SQL for the same reason: what a title may be is a decision worth
testing without a server.

The contrast with `tests/console/test_tags.py` is the point of this file.
A tag is normalised hard -- lowercased, whitespace collapsed, unicode
composed -- because two spellings of one tag filter differently and
people report that as a tag disappearing. A title is compared to nothing,
so almost nothing is done to it: the tests below are mostly assertions
that what somebody typed is what comes back.
"""

from __future__ import annotations

import pytest

from sturnus.console.naming import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    InvalidName,
    normalise_description,
    normalise_title,
)

# ---------------------------------------------------------------------------
# A title is prose, not a slug
# ---------------------------------------------------------------------------


def test_a_title_is_stored_exactly_as_it_was_typed() -> None:
    """Case, punctuation and spacing all survive.

    A tag would come back `sprint 34 planning: kunde olf`, and that is
    right for something people filter by and wrong for something people
    read.
    """
    assert normalise_title("Sprint 34 Planning: Kunde OLF") == "Sprint 34 Planning: Kunde OLF"


def test_the_case_of_a_title_is_never_folded() -> None:
    assert normalise_title("RETRO") == "RETRO"


def test_a_title_is_trimmed_at_the_ends() -> None:
    """A trailing space is invisible in an input field."""
    assert normalise_title("  weekly retro  ") == "weekly retro"


def test_a_title_becomes_one_line() -> None:
    """It is rendered in a heading, a list row and a browser tab, and none
    of those has a second line -- so a newline in it is a paste accident
    rather than a decision."""
    assert normalise_title("weekly\nretro") == "weekly retro"


def test_a_run_of_whitespace_inside_a_title_becomes_one_space() -> None:
    assert normalise_title("weekly \t  retro") == "weekly retro"


def test_two_spellings_of_the_same_letter_are_stored_as_one() -> None:
    """NFC, so a composed and a decomposed `ü` are one string.

    Not for comparison -- nothing compares titles -- but so that a title
    is stored the same way whichever keyboard, operating system or
    clipboard produced it, and does not change under an edit that only
    retyped one letter.
    """
    assert normalise_title("Kündigung") == normalise_title("Kündigung")


def test_an_empty_title_is_no_title_at_all() -> None:
    """Clearing the field is how a meeting is un-named, and the absence of
    a title has one spelling in the database."""
    assert normalise_title("") is None
    assert normalise_title("   ") is None


def test_no_title_stays_no_title() -> None:
    assert normalise_title(None) is None


def test_a_title_may_be_exactly_as_long_as_the_limit() -> None:
    assert normalise_title("a" * MAX_TITLE_CHARS) == "a" * MAX_TITLE_CHARS


def test_a_title_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(InvalidName, match="title"):
        normalise_title("a" * (MAX_TITLE_CHARS + 1))


def test_the_refusal_never_repeats_the_title_it_refused() -> None:
    """The reason travels into a response body, and no user input is
    reflected into one."""
    with pytest.raises(InvalidName) as refusal:
        normalise_title("‮" + "x" * 10)
    assert "x" * 10 not in str(refusal.value)


def test_a_title_carrying_a_character_that_renders_as_nothing_is_refused() -> None:
    """Refused rather than stripped: a stripped one produces a stored
    title that differs from what was typed in a way nothing on the screen
    can show."""
    with pytest.raises(InvalidName, match="control characters"):
        normalise_title("weekly\x00retro")


def test_a_title_carrying_a_bidirectional_override_is_refused() -> None:
    with pytest.raises(InvalidName, match="control characters"):
        normalise_title("weekly ‮retro")


# ---------------------------------------------------------------------------
# A description is a paragraph, and keeps its shape
# ---------------------------------------------------------------------------


def test_a_description_keeps_its_line_breaks() -> None:
    """Collapsing them would turn an agenda into a run-on sentence."""
    assert normalise_description("Agenda:\n\n- one\n- two") == "Agenda:\n\n- one\n- two"


def test_a_description_is_trimmed_at_the_ends_only() -> None:
    assert normalise_description("\n  one\n\ntwo  \n") == "one\n\ntwo"


def test_the_line_endings_a_browser_submits_are_stored_as_one_kind() -> None:
    """A `<textarea>` posts `\\r\\n` on every platform, so keeping them
    would make two identical descriptions different strings depending on
    which form posted them."""
    assert normalise_description("one\r\ntwo\rthree") == "one\ntwo\nthree"


def test_an_empty_description_is_no_description_at_all() -> None:
    assert normalise_description("   \n  ") is None
    assert normalise_description(None) is None


def test_a_description_may_be_exactly_as_long_as_the_limit() -> None:
    text = "a" * MAX_DESCRIPTION_CHARS
    assert normalise_description(text) == text


def test_a_description_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(InvalidName, match="description"):
        normalise_description("a" * (MAX_DESCRIPTION_CHARS + 1))


def test_a_description_carrying_a_character_that_renders_as_nothing_is_refused() -> None:
    with pytest.raises(InvalidName, match="control characters"):
        normalise_description("what we\x07decided")


def test_a_description_is_far_shorter_than_the_transcript_beside_it() -> None:
    """A bound stated as a relationship rather than as a number.

    This field is context for the minutes and not a second copy of them,
    and a text column reachable by every participant of every session
    with no ceiling at all is a storage decision nobody made.
    """
    assert MAX_DESCRIPTION_CHARS > MAX_TITLE_CHARS
    assert MAX_DESCRIPTION_CHARS <= 10_000
