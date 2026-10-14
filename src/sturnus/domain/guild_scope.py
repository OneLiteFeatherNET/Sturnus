"""Which guild an installation serves.

One installation serves exactly one guild, named by `STURNUS_GUILD_ID`. This
is the whole boundary: a comparison of two ids, so that "does this
installation serve this guild" is answered in one place and the answer is
not a list that could grow a second entry.

Discord's own private-application setting is the primary protection against
the bot being invited elsewhere; this predicate is the guard that holds if
that setting is wrong.
"""

from __future__ import annotations


def serves_guild(*, configured_guild_id: int, guild_id: int) -> bool:
    """True exactly when `guild_id` is the guild this installation is configured for."""
    return configured_guild_id == guild_id
