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
from datetime import date, datetime
from typing import Protocol

from sturnus.console.statistics import AttendedSession
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
    """Whether somebody administers any guild the bot serves."""

    async def is_admin_anywhere(self, discord_user_id: int) -> bool: ...


class SessionReads(Protocol):
    """Everything the console reads, already narrowed to one Discord user.

    Every method takes `discord_user_id` first, and that is the whole
    point of the shape: there is no method here that can be called
    without naming whose data is being asked for, so a handler cannot
    accidentally ask a wider question than it is entitled to. The
    narrowing is done by the statement itself in
    `sturnus.console.queries` -- not by a filter afterwards, which is a
    filter somebody can forget.
    """

    async def sessions_for(self, discord_user_id: int) -> Sequence[AttendedSession]: ...

    #: `None` for a session that does not exist *and* for one this person
    #: was not in. The handler answers 404 to both, deliberately.
    async def session_for(
        self, discord_user_id: int, session_id: int
    ) -> AttendedSession | None: ...

    async def sessions_in_year(
        self, discord_user_id: int, year: int
    ) -> Sequence[AttendedSession]: ...

    async def sessions_on_day(
        self, discord_user_id: int, day: date
    ) -> Sequence[AttendedSession]: ...

    #: This person's own transcripts, encoded as the column stores them.
    #: Their own and never the session's: the dashboard's word count says
    #: how much *they* said, and the transcript is the protected content.
    async def transcripts_of(self, discord_user_id: int) -> Sequence[str]: ...
