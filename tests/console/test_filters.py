"""What a filter means, and what it refuses to mean.

Pure, so the rules can be exercised without a server. Two groups matter
most: the refusals, because each of them is a query string somebody typed
into an address bar and an empty list would be indistinguishable from a
correct answer; and the escaping, because a search box that quietly
accepts wildcards is a query language nobody asked for.
"""

from __future__ import annotations

from datetime import date

import pytest

from sturnus.console.filters import (
    MAX_FILTER_TAGS,
    MAX_QUERY_CHARS,
    NO_FILTER,
    InvalidFilter,
    SessionFilter,
    like_pattern,
    session_filter,
)


def asked(
    *,
    text: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    protocol: str | None = None,
) -> SessionFilter:
    return session_filter(text=text, tags=tags or [], since=since, until=until, protocol=protocol)


# ---------------------------------------------------------------------------
# A list with no filter on it
# ---------------------------------------------------------------------------


def test_a_request_that_asks_for_nothing_narrows_nothing() -> None:
    assert asked() == NO_FILTER


def test_a_filter_that_narrows_nothing_says_so() -> None:
    """The page needs it to tell "you have no recordings" from "nothing
    matched", which mean very different things to somebody who was
    recorded yesterday."""
    assert asked().is_empty


def test_an_empty_search_box_is_not_a_filter() -> None:
    assert asked(text="   ").is_empty


def test_a_search_somebody_typed_is_a_filter() -> None:
    assert not asked(text="retro").is_empty


# ---------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------


def test_search_text_is_trimmed_and_collapsed() -> None:
    assert asked(text="  weekly   retro ").text == "weekly retro"


def test_search_text_keeps_its_case() -> None:
    """Matching is case-insensitive in the statement, so folding here
    would only make the box show something other than what was typed."""
    assert asked(text="Retro").text == "Retro"


def test_a_search_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(InvalidFilter):
        asked(text="x" * (MAX_QUERY_CHARS + 1))


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_a_tag_filter_is_spelled_the_way_the_tag_was_stored() -> None:
    """The same `normalise` the write path uses, not a second copy. A
    filter that lower-cased differently from the writer would match
    nothing and look like it should match everything."""
    assert asked(tags=["  Retro "]).tags == ("retro",)


def test_asking_for_the_same_tag_twice_asks_for_it_once() -> None:
    assert asked(tags=["retro", "Retro"]).tags == ("retro",)


def test_several_tags_are_all_required() -> None:
    """AND rather than OR: a second chip is somebody narrowing a list,
    and getting more rows from it than from the first alone is the
    opposite of what pressing it looks like."""
    assert asked(tags=["retro", "kunde"]).tags == ("retro", "kunde")


def test_more_tags_than_may_be_combined_are_refused() -> None:
    with pytest.raises(InvalidFilter):
        asked(tags=[f"tag-{index}" for index in range(MAX_FILTER_TAGS + 1)])


def test_a_tag_that_could_never_have_been_stored_is_refused() -> None:
    with pytest.raises(InvalidFilter):
        asked(tags=["   "])


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_a_range_is_a_pair_of_days() -> None:
    window = asked(since="2026-08-01", until="2026-08-21")
    assert (window.since, window.until) == (date(2026, 8, 1), date(2026, 8, 21))


def test_half_a_range_is_a_range() -> None:
    """ "Everything since March" is a question people ask."""
    assert asked(since="2026-03-01").until is None


def test_a_date_that_is_not_one_is_refused() -> None:
    with pytest.raises(InvalidFilter):
        asked(since="last tuesday")


def test_a_range_that_ends_before_it_begins_is_refused() -> None:
    """Answered with nothing, it would be indistinguishable from a
    correct query for a quiet fortnight -- so somebody who typed the
    dates the wrong way round would edit them for a while before
    suspecting the order."""
    with pytest.raises(InvalidFilter):
        asked(since="2026-08-21", until="2026-08-01")


def test_a_range_of_one_day_is_allowed() -> None:
    assert asked(since="2026-08-21", until="2026-08-21").since == date(2026, 8, 21)


# ---------------------------------------------------------------------------
# Whether a protocol was written
# ---------------------------------------------------------------------------


def test_asking_for_recordings_with_a_protocol() -> None:
    assert asked(protocol="with").protocol is True


def test_asking_for_recordings_without_one() -> None:
    """How you find the meeting whose document never got written."""
    assert asked(protocol="without").protocol is False


def test_asking_for_neither_asks_for_both() -> None:
    assert asked(protocol=None).protocol is None


def test_a_protocol_filter_that_is_neither_is_refused() -> None:
    with pytest.raises(InvalidFilter):
        asked(protocol="maybe")


# ---------------------------------------------------------------------------
# Refusals carry a rule and never a value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [
        {"text": "<script>alert(1)</script>" + "x" * MAX_QUERY_CHARS},
        {"since": "<script>alert(1)</script>"},
        {"until": "<script>alert(1)</script>"},
        {"protocol": "<script>alert(1)</script>"},
    ],
)
def test_a_refusal_never_repeats_what_was_asked_for(attempt: dict[str, str]) -> None:
    with pytest.raises(InvalidFilter) as refused:
        asked(**attempt)  # type: ignore[arg-type]
    assert "script" not in str(refused.value)


# ---------------------------------------------------------------------------
# Search text as a pattern
# ---------------------------------------------------------------------------


def test_a_plain_search_matches_anywhere_in_a_value() -> None:
    """So that "retro" finds "weekly retro"."""
    assert like_pattern("retro") == "%retro%"


def test_a_percent_sign_means_the_character_somebody_typed() -> None:
    """Honouring it would make a search for `%` match every recording --
    a query language nobody asked for and nobody can predict."""
    assert like_pattern("100%") == "%100\\%%"


def test_an_underscore_is_not_a_single_character_wildcard() -> None:
    assert like_pattern("a_b") == "%a\\_b%"


def test_the_escape_character_is_escaped_first() -> None:
    """Escaping the wildcards first would introduce backslashes this
    function then fails to escape, and the pattern would run off the end
    of what the reader typed."""
    assert like_pattern("a\\%b") == "%a\\\\\\%b%"
