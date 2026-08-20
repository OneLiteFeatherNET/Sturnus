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
