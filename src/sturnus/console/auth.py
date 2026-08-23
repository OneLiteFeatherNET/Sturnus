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

**Which client is a per-sign-in question, not a per-process one** (§2.2).
This class held one `OAuthClient` for the life of the process, which is
what made "sign in against *this guild's* identity provider"
unrepresentable. It now holds a `SignInClients` and asks it twice, once at
each end of the round trip, from the only thing available there:

    begin(guild="acme")  --> for_slug("acme")  --> (guild id, client)
             |                                          |
             |                     the guild id goes into the state
             v                                          |
    authenticate(code, state) <-- for_guild(state.guild_id) <-- client

The asymmetry is forced and worth naming: `GET /api/auth/login` takes no
parameters and reads no cookie -- there is no session yet, that is what
login is for -- so before the redirect the slug in the URL is the only
thing that can name a guild, and after it the state is. **The state is
what selects the client for the code exchange**, so a callback cannot be
steered onto a different guild's credential by anything the caller sends.

Sign-in without a guild is unchanged and stays the ordinary case: a
deployment that configures no per-guild client behaves exactly as it did
in v0.15.0, against the environment-configured client.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from sturnus.console.ports import LinkDirectory, SignInClients, StateStore
from sturnus.infrastructure.documents.outline_oauth import LinkExchangeError
from sturnus.observability.events import Event, log_event, log_exception

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


class UnknownSignIn(Exception):
    """No sign-in can be run against what was asked for.

    One exception for four situations that must stay indistinguishable
    from outside: the slug names no guild, it names a guild whose client
    was never given a secret, it names one registered against a provider
    this deployment cannot exchange with, and it names one whose secret
    this process cannot unwrap because the master key was rotated without
    it. An attacker walking a list of names must not be able to tell
    "there is no such organisation here" from "there is one, and its
    sign-in is half-configured" -- that disclosure is the thing §2.2
    chose guild-specific links to avoid.

    It is also what `authenticate` raises when the registration a state
    named has gone away mid-login, which is the same statement about the
    same guild made half a round trip later.
    """


@dataclass(frozen=True)
class AuthenticatedUser:
    """Who the callback established, in the terms the rest of the console uses."""

    discord_user_id: int
    display_name: str


class ConsoleAuth:
    """Begins and completes the console's sign-in flow."""

    def __init__(self, clients: SignInClients, states: StateStore, links: LinkDirectory) -> None:
        self._clients = clients
        self._states = states
        self._links = links

    async def begin(self, now: datetime, *, guild: str | None = None) -> str:
        """Issues a fresh state and returns the URL to send the browser to.

        `guild` is the slug out of the sign-in link, or `None` for the
        deployment's own sign-in. The guild is resolved *before* the state
        is issued, so a slug that names nothing leaves no row behind --
        and so an unusable slug cannot be told from an unknown one by
        watching what the server did.

        The resolved guild id goes into the state, which is the whole
        mechanism: it is the only thing that survives to the callback,
        where there is still no session to read a guild from.
        """
        chosen = None if guild is None else await self._clients.for_slug(guild)
        if guild is not None and chosen is None:
            raise UnknownSignIn("this sign-in link resolves to nothing")

        if chosen is None:
            client = await self._clients.for_guild(None)
            guild_id = None
            if client is None:  # pragma: no cover - the environment client always resolves
                raise UnknownSignIn("this deployment has no sign-in client")
        else:
            client, guild_id = chosen.client, chosen.guild_id

        state = secrets.token_urlsafe(_STATE_BYTES)
        await self._states.issue(state, now, guild_id)
        return client.authorize_url(state)

    async def authenticate(self, code: str, state: str, now: datetime) -> AuthenticatedUser:
        """Completes the flow, or raises the specific reason it could not.

        The state is consumed *before* the code is exchanged. A callback
        that does not correspond to a login this server started is not
        worth an outbound request to the provider, and consuming first is
        also what makes the state single-use against a caller that replays
        the same URL twice in parallel.

        **The consumed state is what selects the client**, and the query
        string contributes nothing but the code. A callback that could
        name its own guild would be a callback that could ask this
        process to spend one guild's client secret on a code issued by
        another guild's provider.
        """
        consumed = await self._states.consume(state, now)
        if consumed is None:
            raise UnknownState("no login corresponds to this callback")

        oauth = await self._clients.for_guild(consumed.guild_id)
        if oauth is None:
            # Reachable without anybody misbehaving: an administrator
            # deleting their guild's registration, or clearing its
            # secret, while somebody is at the provider's consent screen.
            # Logged because a guild whose every sign-in ends here is an
            # operator's problem and is otherwise invisible; the guild id
            # is registered and the client id is deliberately not.
            log_event(
                log,
                logging.WARNING,
                Event.CONSOLE_SIGN_IN_REJECTED,
                "A sign-in came back for a guild that no longer has a usable client",
                guild_id=consumed.guild_id,
                reason="client_unresolvable",
            )
            raise UnknownSignIn("the client this sign-in began against is gone")

        try:
            identity = await oauth.identity_from_code(code)
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
