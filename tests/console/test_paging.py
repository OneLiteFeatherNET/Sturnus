"""What a request may ask for, and what it may not.

Pure, so the rules can be exercised without a server. The interesting
half is the refusals: every one of them is a query string somebody could
type into the address bar, and the reason it comes back with must name
the rule without repeating the value.
"""

from __future__ import annotations

import pytest

from sturnus.console.paging import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidPage,
    page_request,
)


def test_a_request_that_names_no_window_gets_the_first_page() -> None:
    """What a plain link to the list should mean."""
    assert page_request(None, None) == page_request(str(DEFAULT_PAGE_SIZE), "0")


def test_the_first_page_starts_at_the_beginning() -> None:
    window = page_request(None, None)
    assert (window.limit, window.offset) == (DEFAULT_PAGE_SIZE, 0)


def test_a_window_somebody_named_is_the_window_they_get() -> None:
    window = page_request("5", "10")
    assert (window.limit, window.offset) == (5, 10)


def test_the_largest_allowed_page_is_allowed() -> None:
    assert page_request(str(MAX_PAGE_SIZE), None).limit == MAX_PAGE_SIZE


def test_a_page_larger_than_the_ceiling_is_refused_rather_than_trimmed() -> None:
    """Silently answering with a hundred tells neither a buggy client nor
    somebody pulling their whole history that a rule exists."""
    with pytest.raises(InvalidPage):
        page_request(str(MAX_PAGE_SIZE + 1), None)


def test_a_page_of_nothing_is_refused() -> None:
    with pytest.raises(InvalidPage):
        page_request("0", None)


def test_a_negative_page_is_refused() -> None:
    with pytest.raises(InvalidPage):
        page_request("-1", None)


def test_a_negative_offset_is_refused() -> None:
    with pytest.raises(InvalidPage):
        page_request(None, "-1")


def test_an_offset_past_the_end_is_not_a_refusal() -> None:
    """It is what a bookmark to page five looks like after a retention
    sweep, and the honest answer is an empty page with the real total --
    not a 400 that says the bookmark was malformed."""
    assert page_request(None, "10000").offset == 10000


@pytest.mark.parametrize("nonsense", ["", "twenty", "1.5", "0x20", "1e3"])
def test_a_window_that_is_not_a_number_is_refused(nonsense: str) -> None:
    with pytest.raises(InvalidPage):
        page_request(nonsense, None)


def test_a_refusal_never_repeats_what_was_asked_for() -> None:
    """The reason travels into a response body, and no user input is
    reflected into one."""
    with pytest.raises(InvalidPage) as refused:
        page_request("<script>alert(1)</script>", None)
    assert "script" not in str(refused.value)


def test_a_number_written_in_another_script_is_still_a_number() -> None:
    """`int` accepts every decimal digit Unicode has, not only the ASCII
    ones -- so `?limit=\u0662\u0660` is twenty. Pinned rather than
    prevented: it is the same number, and refusing it would be this API
    deciding which alphabets may address it."""
    assert page_request("\u0662\u0660", None).limit == 20
