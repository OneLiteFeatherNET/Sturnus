"""What a meeting is called, and what somebody wrote down about it.

A tag and a title look like the same feature and are not, and the
difference is who they belong to. `session_tag` is keyed by its owner:
two people label the same meeting differently and neither sees the
other's words, because a label is a private remark about a conversation
other people were also in (see `sturnus.console.tags`). `session.title`
and `session.description` are one per session and are shared by everybody
who was in it.

That asymmetry is deliberate. **A tag is how one person finds a thing
again; a title is what the meeting was.** "kunde" and "nochmal ansehen"
are notes to self and would be noise -- or worse, an opinion published to
colleagues -- if everyone saw them. "Sprint 34 planning" is not a remark
about the meeting, it is the meeting's name, and a name that four
attendees each had to type separately is four names for one thing. So a
title is written once, by whoever gets there first, and anybody who was
in the room may correct it.

It follows that a participant may overwrite what another participant
wrote, and there is no history. That is the same trade every shared
document makes, and the alternative -- per-person titles -- is just tags
again, spelled longer.

Everything here is pure, for the same reason `sturnus.console.tags` and
`sturnus.console.statistics` are: what a title may be is a rule, and a
rule that can only be exercised through a database is a rule nobody
exercises.

## What is *not* done to the text

Almost nothing, which is the point. A title is prose somebody typed and
not a slug: no lowercasing, no case folding, no punctuation stripping, no
de-duplication against anything. `sturnus.console.tags` normalises hard
because two spellings of one tag filter differently and people report
that as a tag disappearing; nothing about a title is compared to anything,
so there is nothing for a normalisation to protect and everything for it
to spoil.

Three things are done, and each has a reason that survives the "store
what you are given" rule:

- **Trimmed, and empty becomes null.** An empty string and a null would
  be the same fact told two ways -- "nobody has named this" -- and a
  column holding both is a column every reader has to check twice.
- **A title is collapsed to one line.** It is rendered in a heading, in a
  list row and in a browser tab, none of which has a second line, so a
  newline in it is a paste accident rather than a decision. A
  description keeps its own line breaks: it is a paragraph and paragraphs
  have them.
- **Control characters are refused rather than stripped.** Stripping one
  produces text that differs from what was typed in a way nothing on the
  screen can show, which is the same argument
  `sturnus.console.tags.normalise` makes.
"""

from __future__ import annotations

import re
import unicodedata

#: The longest a title may be. A title names a meeting -- "Sprint 34
#: planning", "Kunde OneLiteFeather, Kickoff" -- and is rendered in a
#: heading, a list row and a browser tab, all of which truncate long
#: before this. Two hundred characters is comfortably more than anybody
#: types into a name field and short enough that no layout has to plan
#: for it; a title that needs more than a line is a description.
MAX_TITLE_CHARS = 200

#: The longest a description may be. A few paragraphs: enough for an
#: agenda, the decisions taken and who is doing what, which is what
#: people actually write under a recording. Deliberately far short of the
#: transcript it sits next to -- this field is context for the minutes,
#: not a second copy of them, and an unbounded text column reachable by
#: every participant of every session is a storage decision nobody made.
MAX_DESCRIPTION_CHARS = 4000

#: Any run of whitespace, collapsed to one space in a title. `\s` under
#: `re.UNICODE` (the default for `str` patterns) covers the non-breaking
#: space and the ideographic space too, both of which arrive from a paste
#: and neither of which is visible in an input field.
_WHITESPACE = re.compile(r"\s+")

#: The one control character a description may contain. A newline is what
#: makes prose prose; everything else in Unicode's `C` category -- a NUL,
#: a bidirectional override, a stray form feed -- is refused.
_NEWLINE = "\n"


class InvalidName(ValueError):
    """A title or description that cannot be stored, free of the text itself.

    The reason is a fixed string on purpose, exactly as
    `sturnus.console.tags.InvalidTag`'s is. It travels into an HTTP
    response body, and the rule this API holds throughout is that no user
    input is reflected into one.
    """


def normalise_title(raw: str | None) -> str | None:
    """One line naming a meeting, or `None` for a meeting nobody has named.

    NFC first, so a composed and a decomposed `ü` are stored as one
    string rather than as whichever the keyboard that typed them
    produced; then every run of whitespace becomes one space and the ends
    are trimmed. Case is left exactly as it was typed -- unlike a tag,
    which is lowercased because it is compared, a title is only ever
    displayed.

    Empty after trimming is `None` rather than `""`: clearing the field is
    how somebody un-names a meeting, and the absence of a title has one
    spelling in the database.
    """
    if raw is None:
        return None
    collapsed = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", raw)).strip()
    if not collapsed:
        return None
    # After the collapse, not before it: a tab and a newline are both
    # control characters and both mean "somebody pasted this", which is an
    # ordinary title once the whitespace is one space. What is left at
    # this point is a character that renders as nothing or reverses the
    # text around it.
    _refuse_control_characters(collapsed, "a title", allow_newlines=False)
    if len(collapsed) > MAX_TITLE_CHARS:
        raise InvalidName(f"a title may be at most {MAX_TITLE_CHARS} characters")
    return collapsed


def normalise_description(raw: str | None) -> str | None:
    """What somebody wrote about a meeting, or `None` for nothing written.

    Trimmed at the ends and otherwise stored as it arrived: the line
    breaks between paragraphs are the shape of the text and collapsing
    them would turn an agenda into a run-on sentence. NFC for the same
    reason a title gets it, and for nothing further -- this is prose.

    Carriage returns are dropped rather than kept. A browser submits
    `\\r\\n` from a `<textarea>` on every platform, so keeping them would
    store a line ending that depends on which form posted the text and
    make two identical descriptions different strings.
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFC", raw).replace("\r\n", _NEWLINE).replace("\r", _NEWLINE)
    trimmed = text.strip()
    if not trimmed:
        return None
    _refuse_control_characters(trimmed, "a description", allow_newlines=True)
    if len(trimmed) > MAX_DESCRIPTION_CHARS:
        raise InvalidName(f"a description may be at most {MAX_DESCRIPTION_CHARS} characters")
    return trimmed


def _refuse_control_characters(text: str, subject: str, *, allow_newlines: bool) -> None:
    """Refuses text carrying a character that renders as nothing.

    Refused rather than stripped, for the reason
    `sturnus.console.tags.normalise` gives: a stripped control character
    produces stored text that differs from what was typed in a way
    nothing on the screen can show, so the person who wrote it cannot see
    what happened to it.
    """
    for character in text:
        if allow_newlines and character == _NEWLINE:
            continue
        if unicodedata.category(character)[0] == "C":
            raise InvalidName(f"{subject} may not contain control characters")
