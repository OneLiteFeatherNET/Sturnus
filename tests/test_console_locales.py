"""One registry of which languages the console speaks, checked across two.

`sturnus.domain.preferences` holds the server-side answer: `locale` is a
stored per-person preference with a closed set of legal values, because a
value naming no message catalogue is not a preference but a broken page.
The console holds the other answer as the files in `console/i18n/locales/`
-- and nothing in either language references the other.

Two registries of the same fact drift in a way that is silent in both
directions. Adding a third catalogue to the console and not here means the
API refuses to store the preference that the language switcher offers, so
the switch appears to work and forgets on reload. Adding it here and not
to the console means a person can store a language nothing can render, and
the console falls back to English with nothing anywhere saying why. Both
are one-line omissions, neither fails anything, and the failure surfaces
as a bug report from whoever speaks the third language.

**This check goes live with PR #115** (`feat(console): let the console
speak the language its guild meets in`), which is what creates
`console/i18n/locales/`. On a branch where that directory does not exist
yet, these tests skip: the skip means the two branches have not met, not
that the check is optional. Once #115 lands, a locale added to one side
and not the other fails here.

The precedent for reaching across the language boundary from a Python
test is `tests/test_component_credentials.py`, which checks a Helm chart
decision, and `console/test/palette.spec.ts`, which reads a stylesheet off
disk to check contrast. This is the same shape, pointed the other way.
"""

from pathlib import Path

import pytest

from sturnus.domain.preferences import ALLOWED_VALUES, DEFAULTS, LOCALE

#: `tests/` sits directly under the repository root, next to `console/`.
LOCALE_FILES = Path(__file__).resolve().parent.parent / "console" / "i18n" / "locales"

MISSING = (
    f"{LOCALE_FILES} does not exist on this branch. It arrives with PR #115, "
    "which gives the console its message catalogues; until that lands there is "
    "no second registry to disagree with. This skip means the two branches have "
    "not met yet -- it does not mean the check is optional."
)


def _shipped_locales() -> set[str]:
    """The languages the console actually has strings for.

    Read from the filenames rather than from `console/nuxt.config.ts`,
    because a catalogue that exists is the fact that matters: a locale
    listed in the config with no file behind it renders nothing, and a
    file the config forgot is a language nobody can select. The filename
    is the one place both of those show up.
    """
    return {
        path.stem
        for path in LOCALE_FILES.iterdir()
        if path.is_file() and not path.name.startswith(".")
    }


@pytest.mark.skipif(not LOCALE_FILES.is_dir(), reason=MISSING)
def test_the_console_and_the_api_agree_on_which_languages_exist() -> None:
    """The whole point of the file. Either side gaining a language the
    other has never heard of fails here, in whichever direction it
    happened.
    """
    assert _shipped_locales() == set(ALLOWED_VALUES[LOCALE]), (
        "console/i18n/locales/ and sturnus.domain.preferences.ALLOWED_VALUES[LOCALE] "
        "disagree about which languages the console speaks. Adding a language means "
        "adding it to both: the catalogue renders the page, the registry decides "
        "whether the preference may be stored at all."
    )


@pytest.mark.skipif(not LOCALE_FILES.is_dir(), reason=MISSING)
def test_the_default_locale_is_one_the_console_can_render() -> None:
    """Everybody who never opens the settings page gets this one, so it
    is the single value that must never be storable-but-unrenderable.
    """
    assert DEFAULTS[LOCALE] in _shipped_locales()
