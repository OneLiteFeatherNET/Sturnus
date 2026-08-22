"""When two labels are the same label, and when one is no label at all.

Every rule here exists to prevent the same failure: a person types a tag,
filters by it, and gets nothing back — because what they stored and what
they searched for differ by something no font can show. So the tests are
mostly about spellings that must collapse into one, and about the two
that must not.
"""

from __future__ import annotations

import pytest

from sturnus.console.tags import (
    MAX_TAG_CHARS,
    MAX_TAGS_PER_SESSION,
    InvalidTag,
    normalise,
    normalise_all,
    tag_counts,
)


def test_case_does_not_make_a_second_tag() -> None:
    assert normalise("Retro") == normalise("retro") == "retro"


def test_surrounding_space_does_not_make_a_second_tag() -> None:
    assert normalise("  retro ") == "retro"


def test_inner_whitespace_collapses_to_one_space() -> None:
    assert normalise("sprint\t\tplanning") == "sprint planning"


def test_a_non_breaking_space_is_whitespace_like_any_other() -> None:
    """It arrives from a paste and is invisible in an input field."""
    assert normalise("sprint\u00a0planning") == "sprint planning"


def test_a_decomposed_umlaut_is_the_same_tag_as_a_composed_one() -> None:
    """`ü` is one code point or two depending on the keyboard that made it.

    Both render identically, so storing them as two tags produces a
    filter that matches half the recordings and looks like it should
    match all of them.
    """
    composed = normalise("f\u00fchrung")
    decomposed = normalise("fu\u0308hrung")
    assert composed == decomposed


def test_the_sharp_s_is_not_folded_into_ss() -> None:
    """`casefold` would make `Straße` and `Strasse` one tag.

    Right for a caseless comparison and wrong for a label somebody typed:
    the tag they get back would not be the word they wrote.
    """
    assert normalise("Straße") == "straße"


def test_a_tag_of_only_space_is_no_tag() -> None:
    with pytest.raises(InvalidTag):
        normalise("   ")


def test_a_tag_may_not_carry_a_control_character() -> None:
    """Refused rather than stripped: a stripped one differs from what was
    typed in a way nothing on the screen can show."""
    with pytest.raises(InvalidTag):
        normalise("re\x00tro")


def test_a_bidirectional_override_is_not_a_label() -> None:
    """It is invisible and it reverses everything rendered after it."""
    with pytest.raises(InvalidTag):
        normalise("re\u202etro")


def test_a_tab_is_whitespace_rather_than_a_forbidden_control_character() -> None:
    """A label pasted out of a spreadsheet arrives full of them and means
    one ordinary tag, so the collapse runs before the refusal."""
    assert normalise("sprint\nplanning") == "sprint planning"


def test_a_tag_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(InvalidTag):
        normalise("x" * (MAX_TAG_CHARS + 1))


def test_a_tag_exactly_at_the_limit_is_kept() -> None:
    assert normalise("x" * MAX_TAG_CHARS) == "x" * MAX_TAG_CHARS


def test_the_length_limit_applies_after_normalisation() -> None:
    """A label padded past the limit with spaces is not too long."""
    assert normalise(" " + "x" * MAX_TAG_CHARS + " ") == "x" * MAX_TAG_CHARS


def test_a_refusal_never_carries_the_tag_that_caused_it() -> None:
    """The reason travels into an HTTP response body.

    No user input is reflected into one, which is a rule this API keeps
    by never putting any there in the first place.
    """
    with pytest.raises(InvalidTag) as refused:
        normalise("x" * (MAX_TAG_CHARS + 1))
    assert "x" * 10 not in str(refused.value)


def test_two_spellings_of_one_tag_are_stored_once() -> None:
    assert normalise_all(["Retro", "retro "]) == ("retro",)


def test_tags_come_back_alphabetical_however_they_were_typed() -> None:
    """The order a read returns them in, so chips do not rearrange
    themselves between the page that saved them and the next one."""
    assert normalise_all(["retro", "abschluss"]) == ("abschluss", "retro")


def test_a_set_over_the_ceiling_is_refused() -> None:
    with pytest.raises(InvalidTag):
        normalise_all([f"tag-{index}" for index in range(MAX_TAGS_PER_SESSION + 1)])


def test_the_ceiling_counts_distinct_tags_not_submitted_ones() -> None:
    """Pasting the same label thirty times is not a refusal."""
    assert normalise_all(["retro"] * (MAX_TAGS_PER_SESSION + 10)) == ("retro",)


def test_something_that_is_not_text_is_not_a_tag() -> None:
    with pytest.raises(InvalidTag):
        normalise_all(["retro", 7])  # type: ignore[list-item]


def test_no_tags_at_all_is_an_empty_set_rather_than_a_refusal() -> None:
    """Removing the last chip is how somebody clears a recording's labels."""
    assert normalise_all([]) == ()


def test_the_tags_used_most_come_first() -> None:
    assert tag_counts([("retro", 1), ("kunde", 9)]) == (("kunde", 9), ("retro", 1))


def test_tags_used_equally_often_are_alphabetical() -> None:
    """So that the tail of the list does not shuffle between page loads."""
    assert tag_counts([("retro", 1), ("abschluss", 1)]) == (("abschluss", 1), ("retro", 1))
