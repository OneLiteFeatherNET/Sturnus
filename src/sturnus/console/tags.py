"""What a tag is, before it reaches a database.

A tag is a label one person puts on one meeting they were in. It is
theirs: `session_tag` is keyed by the owner as well as by the session, so
two people can label the same meeting differently and neither overwrites
the other, and nobody reads anybody else's labels. That is a privacy
decision and not an implementation detail -- a label on a meeting is a
remark about a conversation other people were in, and a system that
showed everyone's remarks to everyone would be publishing opinions its
users never agreed to publish. Making tags private is the reversible
choice; making them shared is not.

Everything here is pure, for the same reason `sturnus.console.statistics`
is pure: the rules below decide when two labels are the same label, and a
rule that can only be exercised through a database is a rule nobody
exercises.

**Why normalise at all.** A tag exists to be filtered by. Two spellings
that look identical and filter differently are the failure people report
as "my tag disappeared", and there are three ways to write the same tag
without noticing:

- Case. `Retro` and `retro` are one label to a reader and two to an index.
- Whitespace. A trailing space is invisible in an input field.
- Unicode composition. `ü` is one code point or two depending on which
  keyboard, which operating system and which clipboard produced it, and
  the two render identically in every font.

So each of those is removed before storage, once, here -- rather than at
each of the two places that would otherwise have to remember.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

#: The longest a tag may be, after normalisation. Long enough for a
#: phrase somebody would actually reuse ("sprint planning", "kunde
#: onelitefeather"), short enough that it renders as a chip rather than
#: as a sentence -- a label nobody can read at a glance is a label
#: nobody filters by twice.
MAX_TAG_CHARS = 48

#: How many tags one person may put on one recording. Not a storage
#: limit: twenty chips is already more than a row can show, and a list
#: with no ceiling is one paste away from a session whose row is taller
#: than the screen.
MAX_TAGS_PER_SESSION = 20

#: Any run of whitespace, collapsed to one space. `\s` under `re.UNICODE`
#: (the default for `str` patterns) covers the non-breaking space and the
#: ideographic space too, both of which arrive from a paste and neither of
#: which is visible in an input field.
_WHITESPACE = re.compile(r"\s+")


class InvalidTag(ValueError):
    """A tag that cannot be stored, with a reason free of the tag itself.

    The reason is a fixed string on purpose. It travels into an HTTP
    response body, and the rule this API holds throughout is that no user
    input is reflected into one -- an endpoint that echoes what it was
    given is an XSS sink for whatever renders its errors, however careful
    the client of the day happens to be.
    """


def normalise(raw: str) -> str:
    """One tag, in the single spelling it is stored and compared under.

    NFC first, so that a composed and a decomposed `ü` become the same
    string before anything else looks at them; whitespace collapsed and
    trimmed; then lowercased.

    `lower` rather than `casefold`, deliberately. `casefold` turns `ß`
    into `ss`, which makes `Straße` and `Strasse` the same tag -- correct
    for a caseless *comparison* and wrong for a label somebody typed,
    because the tag they get back is not the word they wrote.

    Raises `InvalidTag` for anything that cannot be a label: empty after
    trimming, longer than `MAX_TAG_CHARS`, or carrying a control
    character.

    **The control-character check runs after the whitespace collapse and
    not before it**, because a tab and a newline are both. Something
    pasted out of a spreadsheet arrives full of them and means one
    ordinary label; a NUL or a bidirectional override does not, and is
    refused rather than stripped -- a stripped one produces a tag that
    differs from what was typed in a way nothing on the screen can show.
    """
    composed = unicodedata.normalize("NFC", raw)
    collapsed = _WHITESPACE.sub(" ", composed).strip().lower()
    if any(unicodedata.category(character)[0] == "C" for character in collapsed):
        raise InvalidTag("a tag may not contain control characters")
    if not collapsed:
        raise InvalidTag("a tag may not be empty")
    if len(collapsed) > MAX_TAG_CHARS:
        raise InvalidTag(f"a tag may be at most {MAX_TAG_CHARS} characters")
    return collapsed


def normalise_all(raw: Iterable[str]) -> tuple[str, ...]:
    """A whole set of tags, normalised, de-duplicated and alphabetical.

    De-duplication happens *after* normalisation, which is the only order
    that works: `Retro` and `retro ` are one tag and arrive as two, and a
    caller that de-duplicated first would store the same label twice and
    then fail on the primary key.

    Sorted rather than kept in the order they were typed, because that is
    the order they will be read back in -- a set has no order and the
    database will not promise one, so a recording's chips would otherwise
    rearrange themselves between the page that saved them and the next
    one that loads them.

    `MAX_TAGS_PER_SESSION` is counted against the de-duplicated result, so
    pasting the same tag thirty times is not a refusal.
    """
    seen: set[str] = set()
    for candidate in raw:
        if not isinstance(candidate, str):
            raise InvalidTag("a tag must be text")
        seen.add(normalise(candidate))
    if len(seen) > MAX_TAGS_PER_SESSION:
        raise InvalidTag(f"a recording may carry at most {MAX_TAGS_PER_SESSION} tags")
    return tuple(sorted(seen))


def tag_counts(pairs: Sequence[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    """A person's tags, most used first and alphabetical within a count.

    Most used first because the filter bar shows the first few and the
    ones somebody reaches for are the ones they have used; alphabetical
    within a count so the tail does not shuffle between two page loads
    for tags that are all used once.
    """
    return tuple(sorted(pairs, key=lambda pair: (-pair[1], pair[0])))
