"""What a person may set about themselves in the console.

The deliberate mirror of `sturnus.domain.settings`, one layer down: that
module is what an administrator decides for a guild, this one is what a
person decides for themselves. Same shape -- keys, defaults, a closed set
of legal values -- because the storage underneath is the same shape too
(`user_preference` is `guild_config` keyed by person instead of guild),
and two registries that look alike are two registries nobody has to hold
two mental models for.

**Why `system` is the theme default rather than `light`.** The console
already renders correctly against `prefers-color-scheme`, so a person who
never opens the settings page gets whatever their operating system asked
for. Storing `light` as the default would take that away: everybody on a
dark desktop would be handed a light console on their first visit, by a
preference they never expressed, and the only way back would be to go and
set the value the system had already been telling us. `system` is
therefore not "no answer yet" -- it is the answer, and it says *ask the
device*. `light` and `dark` exist for the person who wants the console to
disagree with their desktop, which is a real wish and a rarer one.

`locale` defaults to `en` because that is the language this repository is
written in and the only one every string exists in; `de` is offered
because that is what this deployment's guilds actually meet in (compare
`settings.DEFAULTS[TRANSCRIPTION_LANGUAGE]`).

Pure data and one predicate, in `domain`, so that the store that writes
these values and the endpoint that will eventually offer them cannot
disagree about which keys exist or which values are legal.
"""

from __future__ import annotations

THEME = "theme"
LOCALE = "locale"

#: What a person who has never set anything gets. Every known key appears
#: here: a key with no default would leave the read path to invent one at
#: whichever call site reached it first.
DEFAULTS: dict[str, str] = {
    THEME: "system",
    LOCALE: "en",
}

#: The closed set of values each key accepts. Closed rather than free
#: text because both of these end up selecting a code path -- a stylesheet
#: and a message catalogue -- and a value naming neither is not a
#: preference, it is a broken page.
ALLOWED_VALUES: dict[str, frozenset[str]] = {
    THEME: frozenset({"system", "light", "dark"}),
    LOCALE: frozenset({"en", "de"}),
}

KNOWN_KEYS: frozenset[str] = frozenset(DEFAULTS)


def is_allowed(key: str, value: str) -> bool:
    """Whether this pair may be stored.

    Checks the key as well as the value, so a typo in the key is refused
    rather than silently accepted as a setting nobody ever reads -- the
    same failure `ConfigStore.set` guards against for `guild_config`.
    """
    return value in ALLOWED_VALUES.get(key, frozenset())
