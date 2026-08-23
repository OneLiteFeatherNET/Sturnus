"""What a person is allowed to set about themselves in the console.

The registry is pure data plus one predicate, and everything worth
testing about it is a boundary: an unknown key, a value outside the set,
and the defaults themselves -- which are what a person who never touched
the settings actually gets.
"""

from __future__ import annotations

from sturnus.domain import preferences


def test_a_person_who_set_nothing_follows_their_operating_system() -> None:
    """`system`, not `light`. The console already honours
    `prefers-color-scheme`; a stored `light` default would override the
    operating system for everybody who never asked for it.
    """
    assert preferences.DEFAULTS[preferences.THEME] == "system"


def test_a_person_who_set_nothing_reads_english() -> None:
    assert preferences.DEFAULTS[preferences.LOCALE] == "en"


def test_every_key_has_a_default() -> None:
    """A key with no default has no answer for somebody who never set it,
    and the read path would have to invent one at the call site.
    """
    assert set(preferences.DEFAULTS) == preferences.KNOWN_KEYS


def test_every_default_is_itself_an_allowed_value() -> None:
    """A default outside its own allowed set would be a value `set` refuses
    to store and the read path hands out anyway.
    """
    for key, value in preferences.DEFAULTS.items():
        assert preferences.is_allowed(key, value)


def test_a_theme_the_console_cannot_render_is_not_allowed() -> None:
    assert preferences.is_allowed(preferences.THEME, "midnight") is False


def test_a_language_nothing_is_translated_into_is_not_allowed() -> None:
    assert preferences.is_allowed(preferences.LOCALE, "fr") is False


def test_a_key_nobody_registered_is_not_allowed_whatever_its_value() -> None:
    """Both halves of the pair are checked, so a typo in the key cannot
    smuggle a value past the value check.
    """
    assert preferences.is_allowed("colour_scheme", "dark") is False


def test_the_offered_values_are_exactly_the_ones_the_console_implements() -> None:
    assert preferences.ALLOWED_VALUES[preferences.THEME] == frozenset({"system", "light", "dark"})
    assert preferences.ALLOWED_VALUES[preferences.LOCALE] == frozenset({"en", "de"})
