"""What the console's API needs from the world, as narrow protocols.

Each of these is satisfied by an adapter wired in by
`sturnus.entrypoints.api`, and by a fake in the tests. They are declared
here rather than imported from the concrete classes so this package
depends on shapes rather than on `sturnus.infrastructure` -- the same rule
`sturnus.application` follows, for the same reason: a console module that
imports an adapter is a console module that cannot be tested without one.

They are also narrow on purpose. `LinkDirectory` exposes one method, not
the whole of `AccountLinkRepository`, because one method is what the login
flow uses -- and a protocol that offers more than its consumer needs is an
invitation for the next handler to reach for something it should not have.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity


class OAuthClient(Protocol):
    """The identity provider the console authenticates against."""

    def authorize_url(self, state: str) -> str: ...

    async def identity_from_code(self, code: str) -> ExternalIdentity: ...


class StateStore(Protocol):
    """Single-use OAuth states, tying a callback to a login this server began."""

    async def issue(self, state: str, now: datetime) -> None: ...

    #: `False` for a state that was never issued, has already been used, or
    #: has expired -- the caller treats all three identically, because from
    #: the outside they are the same event: this is not a callback for a
    #: login we started.
    async def consume(self, state: str, now: datetime) -> bool: ...


class LinkDirectory(Protocol):
    """The bridge from an external identity to the Discord user it belongs to.

    This is the whole authorisation model: every console query is scoped by
    Discord id, because that is what `session_participant` names, and the
    only bridge to one is a link the person made themselves with `/link`.
    """

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None: ...


class AdminDirectory(Protocol):
    """Who administers what, as far as the console is allowed to know.

    Three questions rather than one, because the console asks three
    genuinely different ones and answering the narrow one with the wide
    one is the failure this protocol exists to make hard to write:

    * `is_admin_anywhere` decides whether the settings section is offered
      at all. It is a rendering hint and never a control.
    * `administered_guilds` is what the guild picker lists.
    * `is_admin` is the only one that authorises anything. Every settings
      read and every settings write goes through it, per guild, because
      an administrator of one guild is nobody in another.

    Read from `admin_member`, which `bot` mirrors on its sweep. The API
    process has no gateway to ask Discord directly, deliberately: a
    process that can decrypt every recording ever made is not one to also
    hand the ability to act as the bot (Spec 13.2).
    """

    async def is_admin_anywhere(self, discord_user_id: int) -> bool: ...

    async def administered_guilds(self, discord_user_id: int) -> Sequence[int]: ...

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool: ...


class SettingsStore(Protocol):
    """Per-guild runtime configuration, read whole and written one key at a time.

    Narrow to two methods on purpose. In particular there is no
    `get`/`get_stored` here: the listing endpoint reads a guild's whole
    configuration in one query rather than one per key, and a protocol
    that offered the per-key read would be an invitation for the next
    handler to loop over `KNOWN_KEYS` doing seventeen round trips.

    **`set` is where value validation lives, and it must stay there.** It
    refuses an unknown key and refuses a non-positive-integer for an
    integer key, and the API's job is to turn that `ValueError` into a
    400 -- never to check the same thing first. Two copies of a
    validation rule is how the two drift.
    """

    async def snapshot(self, guild_id: int) -> dict[str, str]: ...

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None: ...
