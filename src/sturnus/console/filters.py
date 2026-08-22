"""Which of somebody's recordings they are asking to see.

Pure, and separated from both the handler and the SQL for the same
reason `sturnus.console.paging` is: what a filter means, and what it may
not mean, are decisions worth testing without a server.

## What is searched, and what deliberately is not

**Metadata only: the channel, the date, who was there, and the reader's
own tags.** Not the transcripts, and not the protocols.

That is a decision about other people's speech and not a limitation of
the implementation. Everybody in a recorded session consented to being
recorded *so that a protocol could be written* -- and reading a protocol
is not the same act as being able to search for a phrase and find every
moment somebody said it. A search index over transcripts turns spoken
words into a lookup key: it makes "did anybody ever mention X" a question
the system answers, which is a use of a colleague's voice that nobody in
the room agreed to when they clicked yes.

Everything this module *does* search is already on the page. A session's
channel, its times, the names of everybody who was in it and the labels
this reader wrote are all in the response `/api/sessions` has always
returned to this same person. Filtering by them narrows what somebody can
already see; it does not widen it.

If content search is wanted later, the shape it must have is fixed by
Section 3.3 of the console design and by nothing in this file: the
restriction to sessions the searcher was in belongs *in the statement*,
next to `session_participant`, and never as a filter applied to results
afterwards. It would also want its own line in the policy at
`policy_url`, and by the mechanism in `docs/operations.md` Section 6 that
means a `policy_version` bump and everybody re-consenting -- which is the
point at which it stops being a console feature and becomes a decision
about the product.

## Why refusals rather than empty results

A filter that cannot be applied is a query string somebody wrote wrongly
-- usually by hand, in the address bar. Answering it with an empty list
is indistinguishable from answering it correctly, so the person edits the
dates for a while before working out that the console never understood
them. Every refusal below names the rule and never the value, because the
reason travels into a response body and no user input is reflected into
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sturnus.console.tags import InvalidTag, normalise

#: The longest search text accepted. Not a storage limit -- it is what
#: keeps a `LIKE` pattern from being megabytes long, and a hundred
#: characters is already longer than any channel name, display name or
#: tag it could match.
MAX_QUERY_CHARS = 100

#: How many tags one request may filter by at once. They are combined with
#: AND, so more than a handful describes no recording at all; the ceiling
#: is here to bound the number of `EXISTS` clauses a request can ask the
#: planner for.
MAX_FILTER_TAGS = 10


class InvalidFilter(ValueError):
    """A filter that cannot be applied, with a reason free of the request."""


@dataclass(frozen=True)
class SessionFilter:
    """What a reader is asking to see, once it is known to be answerable.

    Every field is `None` or empty when the corresponding control was left
    alone, so "no filter" is representable and is the default -- rather
    than being spelled as a wildcard somebody has to remember to write.
    """

    #: Free text, matched against the channel name, the display names of
    #: everybody who was in the session, and this reader's own tags.
    #: Never against a transcript; see the module docstring.
    text: str | None
    #: Tags the recording must carry, all of them. AND rather than OR
    #: because a second chip is somebody narrowing a list -- selecting
    #: "retro" and "kunde" and getting *more* rows than "retro" alone is
    #: the opposite of what pressing a second chip looks like.
    tags: tuple[str, ...]
    #: Inclusive bounds, as UTC days. Inclusive because a person picking
    #: "to 21 August" means the whole of the 21st, and a half-open bound
    #: silently drops the day they named.
    since: date | None
    until: date | None
    #: `True` for only the sessions that produced a protocol, `False` for
    #: only those that did not, `None` for both. Three states rather than
    #: a flag, because "sessions with no protocol" is a question people
    #: actually ask -- it is how you find the meeting whose document never
    #: got written.
    protocol: bool | None

    @property
    def is_empty(self) -> bool:
        """Whether this narrows anything at all.

        The page needs it to tell "you have no recordings" from "nothing
        matched what you asked for" -- two sentences that mean very
        different things to somebody who was recorded yesterday.
        """
        return (
            self.text is None
            and not self.tags
            and self.since is None
            and self.until is None
            and self.protocol is None
        )


#: A filter that narrows nothing, which is what a bare list means.
NO_FILTER = SessionFilter(text=None, tags=(), since=None, until=None, protocol=None)


def session_filter(
    *,
    text: str | None,
    tags: list[str],
    since: str | None,
    until: str | None,
    protocol: str | None,
) -> SessionFilter:
    """The filter a query string names, or `InvalidFilter`.

    Takes the already-extracted values rather than a request, so it can be
    exercised without one.
    """
    parsed = SessionFilter(
        text=_text(text),
        tags=_tags(tags),
        since=_day(since, _SINCE_RULE),
        until=_day(until, _UNTIL_RULE),
        protocol=_protocol(protocol),
    )
    if parsed.since is not None and parsed.until is not None and parsed.since > parsed.until:
        # Refused rather than answered with nothing. An empty list is what
        # a correct query for a quiet fortnight also looks like, so
        # somebody who typed the dates the wrong way round would edit them
        # for a while before suspecting the order.
        raise InvalidFilter("the start of the range must not be after its end")
    return parsed


def _text(raw: str | None) -> str | None:
    if raw is None:
        return None
    trimmed = " ".join(raw.split())
    if not trimmed:
        # An empty search box is the absence of a filter, not a filter
        # that matches nothing.
        return None
    if len(trimmed) > MAX_QUERY_CHARS:
        raise InvalidFilter(f"a search may be at most {MAX_QUERY_CHARS} characters")
    return trimmed


def _tags(raw: list[str]) -> tuple[str, ...]:
    """The tags to require, in the one spelling they are stored under.

    Normalised by `sturnus.console.tags.normalise` -- the same function
    the write path uses, not a second copy of it. A filter that lower-cased
    differently from the writer would be a filter that matches nothing and
    looks like it should match everything.

    De-duplicated, because requiring the same tag twice is requiring it
    once and would otherwise count twice against the ceiling.
    """
    wanted: list[str] = []
    for candidate in raw:
        try:
            tag = normalise(candidate)
        except InvalidTag as refusal:
            # Re-raised as this module's refusal so a handler has one
            # exception to catch. The message is `tags`' own fixed
            # sentence and still carries nothing from the request.
            raise InvalidFilter(str(refusal)) from None
        if tag not in wanted:
            wanted.append(tag)
    if len(wanted) > MAX_FILTER_TAGS:
        raise InvalidFilter(f"at most {MAX_FILTER_TAGS} tags may be combined")
    return tuple(wanted)


def _day(raw: str | None, rule: str) -> date | None:
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise InvalidFilter(rule) from None


def _protocol(raw: str | None) -> bool | None:
    if raw is None or not raw.strip():
        return None
    if raw == "with":
        return True
    if raw == "without":
        return False
    raise InvalidFilter(_PROTOCOL_RULE)


_SINCE_RULE = "the start of the range must be a date, as YYYY-MM-DD"
_UNTIL_RULE = "the end of the range must be a date, as YYYY-MM-DD"
_PROTOCOL_RULE = "protocol must be either 'with' or 'without'"


#: The character `LIKE` patterns are escaped with here. Backslash is
#: PostgreSQL's default, but every statement names it explicitly --
#: `standard_conforming_strings` and the driver's own quoting have both
#: changed what a bare backslash means before, and a pattern whose escape
#: character is assumed is a pattern that starts matching the wrong thing
#: after an upgrade.
LIKE_ESCAPE = "\\"


def like_pattern(text: str) -> str:
    """Search text as a `LIKE` pattern that matches it literally, anywhere.

    The wildcards a reader supplies are escaped rather than honoured. A
    search box is a search box: `%` in it means the percent sign somebody
    typed, and honouring it would mean `_` quietly matches every channel
    that exists and `%` matches every recording -- a query language nobody
    asked for and nobody can predict.

    The escape character itself goes first, or escaping the wildcards
    would introduce backslashes this function then fails to escape.
    """
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"
