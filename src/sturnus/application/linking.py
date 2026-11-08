"""Application-layer pieces of single-use OAuth state linking.

The state itself is the only thing standing between an attacker and linking
their own external account to someone else's Discord identity, so it is a
security boundary, not bookkeeping -- see `sturnus.infrastructure.db.link_state`
for the repository that issues, consumes, and purges it against the
`oauth_state` table. This module holds only what is genuinely
application-layer: generating the token and the plain data it resolves to.
Neither performs I/O, so neither needs the database this layer may not import.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class LinkedIdentities(Protocol):
    """What `begin_link` reads: the external identity already linked, if any."""

    async def external_identity(
        self, discord_user_id: int, provider: str | None = None
    ) -> tuple[str, str] | None: ...


class StateIssuer(Protocol):
    """What `begin_link` writes: a fresh single-use state for one Discord id."""

    async def issue(
        self, discord_user_id: int, provider: str, now: datetime, ttl: timedelta
    ) -> str: ...


@dataclass(frozen=True)
class LinkStart:
    """Outcome of starting a link: a new state, or the link already held."""

    #: The state to build the authorization URL from, or `None` when the
    #: person is already linked and no state was issued.
    state: str | None
    #: The display name of the existing link, when there is one.
    linked_as: str | None


async def begin_link(
    discord_user_id: int,
    *,
    provider: str,
    identities: LinkedIdentities,
    states: StateIssuer,
    now: datetime,
    ttl: timedelta,
) -> LinkStart:
    """The `/link start` decision, shared with the channel prompt's link button.

    A state is issued only for `discord_user_id`, the person who asked. An
    already linked person gets no state: issuing one would strand it unspent.
    The caller builds the URL and decides how to show it, and it must reach
    the presser alone.
    """
    existing = await identities.external_identity(discord_user_id)
    if existing is not None:
        _, display_name = existing
        return LinkStart(state=None, linked_as=display_name)
    state = await states.issue(discord_user_id, provider, now, ttl)
    return LinkStart(state=state, linked_as=None)


def new_state() -> str:
    """Generates a cryptographically random, unguessable state token.

    Uses `secrets.token_urlsafe`, never `random` -- this token is a
    security boundary, and `random` is not safe against an adversary
    trying to predict it.
    """
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class PendingLink:
    """The Discord identity a successfully consumed state resolves to."""

    discord_user_id: int
    provider: str
