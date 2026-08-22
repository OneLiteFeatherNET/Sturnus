"""Turning an OAuth callback into a console session, or refusing to.

Separated from the HTTP handlers because the decision here is not an HTTP
decision. It is: *this provider says this identity authenticated -- is
there a person in this system that names, and may they in?* The answer
needs no request object, and testing it should not need a server.

The flow's one load-bearing property lives in `authenticate`: **an Outline
identity is not an identity here until `account_link` says which Discord
user it is.** Every console query is scoped by Discord id, because that is
what `session_participant` names, so a login that skipped the lookup would
mint a session that can be scoped to nobody -- and the handler that later
forgot to check would scope it to everybody.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from sturnus.console.ports import LinkDirectory, OAuthClient, StateStore
from sturnus.infrastructure.documents.outline_oauth import LinkExchangeError
from sturnus.observability.events import Event, log_exception

log = logging.getLogger(__name__)

#: The provider whose links the console reads. A constant rather than a
#: parameter because there is exactly one identity provider for the
#: console, and `account_link` is keyed by provider so a wrong value here
#: would silently find nobody.
PROVIDER = "outline"

#: 32 bytes of URL-safe randomness. The state's only job is to be
#: unguessable for as long as one login takes.
_STATE_BYTES = 32


class NotLinked(Exception):
    """The identity authenticated, and no Discord account is linked to it.

    Distinct from a refused exchange: this person exists and proved it,
    they simply have no `/link` yet. The console says so specifically,
    because "run /link in Discord" is an instruction somebody can act on
    and "access denied" is not.
    """


class ExchangeRefused(Exception):
    """The provider would not turn the code into an identity."""


class UnknownState(Exception):
    """No login this server started corresponds to this callback."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """Who the callback established, in the terms the rest of the console uses."""

    discord_user_id: int
    display_name: str


class ConsoleAuth:
    """Begins and completes the console's sign-in flow."""

    def __init__(self, oauth: OAuthClient, states: StateStore, links: LinkDirectory) -> None:
        self._oauth = oauth
        self._states = states
        self._links = links

    async def begin(self, now: datetime) -> str:
        """Issues a fresh state and returns the URL to send the browser to."""
        state = secrets.token_urlsafe(_STATE_BYTES)
        await self._states.issue(state, now)
        return self._oauth.authorize_url(state)

    async def authenticate(self, code: str, state: str, now: datetime) -> AuthenticatedUser:
        """Completes the flow, or raises the specific reason it could not.

        The state is consumed *before* the code is exchanged. A callback
        that does not correspond to a login this server started is not
        worth an outbound request to the provider, and consuming first is
        also what makes the state single-use against a caller that replays
        the same URL twice in parallel.
        """
        if not await self._states.consume(state, now):
            raise UnknownState("no login corresponds to this callback")

        try:
            identity = await self._oauth.identity_from_code(code)
        except LinkExchangeError as exc:
            # Through `log_exception` rather than `%s`: the exception's own
            # message travels only if `SAFE_MESSAGE_TYPES` vouches for its
            # class, and the type and traceback are rendered from static
            # program text. Worth logging at all because a provider
            # refusing every exchange is otherwise invisible from here.
            log_exception(
                log,
                logging.WARNING,
                Event.CONSOLE_SIGN_IN_REJECTED,
                "The identity provider refused a console sign-in",
                exc,
            )
            raise ExchangeRefused(str(exc)) from exc

        discord_user_id = await self._links.discord_user_for(PROVIDER, identity.external_user_id)
        if discord_user_id is None:
            # Deliberately not logged with the external id: the person is
            # identified, they simply have no link, and recording which
            # Outline account tried to sign in adds nothing an operator
            # would act on.
            raise NotLinked("this identity has no linked Discord account")

        return AuthenticatedUser(
            discord_user_id=discord_user_id, display_name=identity.display_name
        )
