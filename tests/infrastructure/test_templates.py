import pytest
from jinja2.exceptions import SecurityError

from sturnus.infrastructure.templates.engine import render
from sturnus.infrastructure.templates.markdown import escape_markdown


def test_a_plain_template_renders() -> None:
    assert render("Hello {{ name }}", name="world") == "Hello world"


_LEAK_MARKERS = ("__", "<class", "object at 0x")
"""Substrings that would betray an internal Python object reaching output.

Deliberately loose (a plain "__" alone catches any dunder name, module path,
or private attribute) so a leak cannot slip through by using a spelling this
list did not anticipate.
"""


@pytest.mark.parametrize(
    "source",
    [
        "{{ ''.__class__ }}",
        "{{ ''.__class__.__mro__ }}",
        "{{ [].__class__.__base__.__subclasses__() }}",
        "{{ ().__class__.__bases__[0].__subclasses__() }}",
        "{{ config.__class__.__init__.__globals__ }}",
    ],
)
def test_sandbox_escapes_are_refused(source: str) -> None:
    """Templates become admin-settable in a later phase.

    An unguarded environment would then be a shell in the bot's pod for any
    guild administrator, so the sandbox goes in before the door opens.

    The property that matters is that nothing about the object's internals
    (its class, its module, its attributes) ever reaches rendered output —
    not that the sandbox raises. Jinja's Undefined can absorb an expression
    silently (its __str__ renders as an empty string) instead of raising,
    and that is just as safe as a SecurityError: either outcome is accepted,
    but a render that slips through unraised is still checked for a leak.
    """
    try:
        rendered = render(source)
    except SecurityError:
        return
    for marker in _LEAK_MARKERS:
        assert marker not in rendered, (
            f"{source!r} rendered {rendered!r}, which leaks {marker!r}"
        )


def test_display_names_cannot_inject_a_link() -> None:
    """Discord display names are attacker-controlled input."""
    rendered = escape_markdown("[click here](https://evil.example)")
    assert "](" not in rendered


def test_escaping_neutralises_emphasis_and_code() -> None:
    for hostile in ["*bold*", "_italic_", "`code`", "# heading"]:
        assert escape_markdown(hostile) != hostile


def test_escaping_leaves_ordinary_text_alone() -> None:
    assert escape_markdown("Anna Example") == "Anna Example"


def test_escaping_survives_a_round_of_rendering() -> None:
    """The filter must be reachable from a template, not only from Python."""
    out = render("{{ name | md }}", name="a]b(c)")
    assert "](" not in out


def test_a_speaker_name_cannot_break_out_of_a_mention() -> None:
    """The exact shape used by the Outline adapter.

    `_SPECIAL` in markdown.py escapes every `\\`, `[`, `]`, `(` and `)` a
    name contains, backslash-prefixing each one. That guarantees a hostile
    name can never contribute an unescaped `]` immediately followed by an
    unescaped `(`: whichever of the two the name supplies arrives with a
    backslash sitting directly in front of it, which breaks the contiguity
    a live mention construct depends on. So the assertion does not count
    occurrences of the substring "mention://user/" — inert copies of that
    text sitting escaped inside the name are harmless and would fail a
    substring count for the wrong reason. It counts *active* mention
    constructs instead: contiguous, unescaped "](mention://user/"
    sequences, which is the exact junction between the closing bracket and
    the opening paren that a real `@[label](mention://user/id)` needs. A
    hostile name can inflate the substring count but cannot assemble that
    live sequence, so this assertion would fail if it ever could.
    """
    out = render(
        "@[{{ name | md }}](mention://user/{{ uid }})",
        name="x](mention://user/other) [y",
        uid="real-id",
    )
    assert out.count("](mention://user/") == 1
