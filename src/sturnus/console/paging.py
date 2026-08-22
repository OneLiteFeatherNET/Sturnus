"""How much of a list one request may ask for.

Pure, and separated from the handler for the same reason
`sturnus.console.statistics` is: what a page is, and what a page may not
be, are decisions worth testing without a server.

**Why an offset and not a cursor.** A keyset cursor -- "everything before
this instant and this id" -- is the stable answer: it cannot skip a row
or show one twice when something is inserted while somebody is reading.
It also cannot say how many recordings there are, and cannot go to a page
somebody names. Both of those are what this particular list is for: a
person hunting for a meeting from March pages backwards through their own
history, and the first thing they want to know is how far back it goes.

What makes the offset defensible here is the size of the thing being
paged. The statement is already scoped to one person's attendance (see
`sturnus.console.queries`) -- tens to hundreds of sessions, not millions
-- so the rows an `OFFSET` walks past are bounded by how many meetings one
human has been in. And the drift it admits is bounded too: a session
inserted at the top between two page loads shifts everything down by one,
so a row can appear twice or be missed once. Sessions are inserted when a
meeting starts, which is minutes to days apart, not the write rate a feed
has.

If either of those stops being true -- a person with tens of thousands of
sessions, or an import that writes history in bulk -- this is the module
to change, and the shape of `SessionPage` is deliberately one a cursor
could also fill.

**Why a refusal rather than a clamp.** `?limit=5000` is either a client
bug or somebody trying to pull their whole history in one response, and
silently answering with 100 tells neither of them anything. A refusal
names the rule; a clamp hides it and then gets discovered as "the console
ignores my limit".
"""

from __future__ import annotations

from dataclasses import dataclass

#: What a page holds when nothing asks for a different size. Twenty rows
#: is about a screen and a half of the recordings list, which is enough to
#: scan without being enough to make the page slow to render.
DEFAULT_PAGE_SIZE = 20

#: The largest page anybody may ask for. Not a database limit: it is what
#: keeps one request from serialising a person's entire history --
#: every session with every participant and every track inline -- into a
#: single response body.
MAX_PAGE_SIZE = 100


class InvalidPage(ValueError):
    """A window that cannot be served, with a reason free of the request.

    The reason travels into an HTTP response body, and no user input is
    reflected into one -- so it names the rule and never the value that
    broke it.
    """


@dataclass(frozen=True)
class PageRequest:
    """The window a caller asked for, once it is known to be servable."""

    limit: int
    offset: int


def page_request(limit: str | None, offset: str | None) -> PageRequest:
    """The window named by a query string, or `InvalidPage`.

    Both parameters are optional: a request that names neither gets the
    first page, which is what a link to `/api/sessions` should mean.

    Parsed with `int` rather than by pattern, so `+5` and surrounding
    whitespace are accepted the way every other integer in this API is.
    `0x20` is not an integer to `int` and is refused, which is correct: a
    query string that needs a base is a query string somebody generated
    wrongly.
    """
    return PageRequest(limit=_limit(limit), offset=_offset(offset))


def _limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_PAGE_SIZE
    try:
        limit = int(raw)
    except ValueError:
        raise InvalidPage(_LIMIT_RULE) from None
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise InvalidPage(_LIMIT_RULE)
    return limit


def _offset(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        offset = int(raw)
    except ValueError:
        raise InvalidPage(_OFFSET_RULE) from None
    if offset < 0:
        raise InvalidPage(_OFFSET_RULE)
    return offset


_LIMIT_RULE = f"limit must be a whole number between 1 and {MAX_PAGE_SIZE}"
_OFFSET_RULE = "offset must be a whole number of zero or more"
