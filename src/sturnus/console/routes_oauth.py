"""A guild's own sign-in client: five routes, and one thing that never comes back.

**The secret is write-only, and there is nowhere in the read model to put
it.** `GET` on an OAuth configuration answers the slug, the provider, the
base URL, the client id, the redirect URI and `has_secret` -- never the
value, not masked, not truncated, not "the last four characters". That is
why this configuration is not a `guild_config` key: the settings API
renders every value it holds straight back to whoever asks for it, which
is the correct behaviour for a setting and a disclosure for a credential
(§2.2).

**Every refusal here is the same 404.** Not administering the guild, no
such guild, and no client configured for it all answer
`{"error": "no sign-in configuration"}`. That is deliberately stricter
than `routes_settings` next door, which answers 403 for a guild somebody
does not administer, and the difference is the whole point of §2.2's
design: whether a given guild has its own sign-in is exactly the fact the
guild-specific-link arrangement exists to keep undiscoverable. A refusal
that said "you are not an administrator of this guild" would be a
one-request oracle for it.

**No user input is reflected into a response**, the same rule the rest of
`sturnus.console.app` follows. The reasons below are fixed strings, which
matters more here than elsewhere: one of the values these handlers parse
is a client secret, and an endpoint that echoed what it refused would
have echoed one the first time somebody sent a malformed body.

**What is validated here and what is not.** The slug and the two URLs are
decided in `sturnus.domain.oauth_clients`, in pure functions a test
reaches without a server, because the sign-in path has to agree with the
write path about what a slug is -- and two copies of that rule is how the
two come to disagree. This module calls them; it does not restate them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiohttp import web

from sturnus.console.auth import PROVIDER
from sturnus.console.ports import GuildOAuthClients
from sturnus.domain.oauth_clients import (
    GuildOAuthClient,
    SlugUnavailable,
    has_slug_shape,
    is_provider_url,
    is_valid_slug,
)
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: The store of per-guild sign-in clients, under its own key for the
#: reason `SETTINGS_STORE` has one: `build_api` stays a one-line edit
#: while several branches are adding sections to it.
GUILD_OAUTH_CLIENTS: web.AppKey[GuildOAuthClients] = web.AppKey("guild_oauth_clients")

#: The one refusal. Every way of not being allowed to see or change a
#: guild's sign-in configuration produces this and nothing else.
_NO_CONFIGURATION = "no sign-in configuration"
_MALFORMED_BODY = "malformed request body"
_MALFORMED_SLUG = "the sign-in name must be lowercase letters, digits and hyphens"
_SLUG_UNAVAILABLE = "that sign-in name is not available"
_UNSUPPORTED_PROVIDER = "unsupported identity provider"
_MALFORMED_URL = "the base URL and redirect URI must be https addresses"
_MALFORMED_CLIENT_ID = "the client id must be a non-empty string"
_MALFORMED_SECRET = "the client secret must be a non-empty string"

#: What a write did, for the audit line: bounded literals from this
#: file's own source, never the value that was written.
_REGISTERED = "registered"
_SECRET_SET = "secret_set"
_SECRET_CLEARED = "secret_cleared"
_REMOVED = "removed"

#: Bounds on the two free-text fields. Not a claim about what any
#: provider issues -- they are what keeps a `Text` column from being a
#: place to store a megabyte through an authenticated endpoint.
_MAX_CLIENT_ID = 512
_MAX_SECRET = 1024
_MAX_URL = 2048


def register(app: web.Application) -> None:
    """Adds the sign-in configuration routes to an application with a session.

    `require_session` on all five, applied here rather than as a
    decorator, for the reason `routes_settings.register` gives: the
    authentication decision stays visible at the routes it protects, so a
    route added without it is visibly public rather than silently public.

    The secret has routes of its own rather than being a field on the
    registration, mirroring the two-method split `GuildOAuthClientStore`
    already has. It is what makes "save the registration" a request that
    demonstrably cannot carry a credential, and it is what lets an
    administrator re-register a base URL without re-typing a secret they
    may no longer have.
    """
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get("/api/guilds/{guild_id}/oauth-client", require_session(read_client)),
            web.put("/api/guilds/{guild_id}/oauth-client", require_session(write_client)),
            web.delete("/api/guilds/{guild_id}/oauth-client", require_session(remove_client)),
            web.put("/api/guilds/{guild_id}/oauth-client/secret", require_session(write_secret)),
            web.delete("/api/guilds/{guild_id}/oauth-client/secret", require_session(clear_secret)),
        ]
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def read_client(request: web.Request) -> web.Response:
    """This guild's sign-in configuration, without its secret."""
    guild_id = _guild_id(request)
    client = await _clients(request).for_guild(guild_id, requested_by=_caller(request))
    if client is None:
        raise _refusal(web.HTTPNotFound, _NO_CONFIGURATION)
    return _answer(client)


async def write_client(request: web.Request) -> web.Response:
    """Registers or replaces the registration, leaving any secret alone.

    A replacement rather than a patch, because a guild has one client and
    the fields describe one OAuth application: an administrator moving to
    a different Outline instance changes the base URL and the client id
    together, and a partial write is how a client id ends up pointing at
    the wrong host.
    """
    guild_id = _guild_id(request)
    registration = _registration(await _body(request))

    try:
        client = await _clients(request).save(
            guild_id,
            requested_by=_caller(request),
            now=_now(request),
            slug=registration.slug,
            provider=registration.provider,
            base_url=registration.base_url,
            client_id=registration.client_id,
            redirect_uri=registration.redirect_uri,
        )
    except SlugUnavailable:
        raise _refusal(web.HTTPConflict, _SLUG_UNAVAILABLE) from None
    if client is None:
        raise _refusal(web.HTTPNotFound, _NO_CONFIGURATION)
    _audit(request, guild_id, outcome=_REGISTERED)
    return _answer(client)


async def remove_client(request: web.Request) -> web.Response:
    """Removes the registration and frees its slug.

    204 rather than a body: there is nothing left to read back. A guild
    that had none answers 404, so "already gone" and "removed" are not
    the same reply to an administrator who clicked twice.
    """
    guild_id = _guild_id(request)
    if not await _clients(request).delete(guild_id, requested_by=_caller(request)):
        raise _refusal(web.HTTPNotFound, _NO_CONFIGURATION)
    _audit(request, guild_id, outcome=_REMOVED)
    return web.Response(status=204)


async def write_secret(request: web.Request) -> web.Response:
    """Stores the client secret, and answers with what can be read back.

    Which is the registration with `has_secret` true, and nothing else.
    The response to storing a secret is the one response most likely to
    be built by echoing what came in, so it is built by re-reading the
    row instead.
    """
    guild_id = _guild_id(request)
    body = await _body(request)
    secret = body.get("client_secret")
    if not isinstance(secret, str) or not secret or len(secret) > _MAX_SECRET:
        raise _refusal(web.HTTPBadRequest, _MALFORMED_SECRET)

    client = await _clients(request).set_secret(
        guild_id, secret, requested_by=_caller(request), now=_now(request)
    )
    if client is None:
        raise _refusal(web.HTTPNotFound, _NO_CONFIGURATION)
    _audit(request, guild_id, outcome=_SECRET_SET)
    return _answer(client)


async def clear_secret(request: web.Request) -> web.Response:
    """Forgets the client secret, leaving the registration in place.

    Half a registration is a real state and the interface has to be able
    to reach it: an administrator whose secret has leaked wants it gone
    now, and re-registering the whole client to achieve that would free
    the slug in between. Sign-in through this guild's link stops working
    immediately, answering exactly as an unknown slug does.
    """
    guild_id = _guild_id(request)
    client = await _clients(request).set_secret(
        guild_id, None, requested_by=_caller(request), now=_now(request)
    )
    if client is None:
        raise _refusal(web.HTTPNotFound, _NO_CONFIGURATION)
    _audit(request, guild_id, outcome=_SECRET_CLEARED)
    return _answer(client)


# ---------------------------------------------------------------------------
# Reading the request
# ---------------------------------------------------------------------------


def _guild_id(request: web.Request) -> int:
    """The guild from the path, or the same 404 everything else answers.

    A guild id that is not a number names no guild, so it gets the
    refusal every other unanswerable request here gets rather than a 400
    that would distinguish it.
    """
    try:
        return int(request.match_info["guild_id"])
    except ValueError:
        raise _refusal(web.HTTPNotFound, _NO_CONFIGURATION) from None


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY) from None
    if not isinstance(body, dict):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY)
    return body


@dataclass(frozen=True)
class _Registration:
    """The four fields a registration is, once they have been believed.

    A value rather than a `dict`, so that the handler that hands them to
    the store cannot pass one of them under the wrong keyword and so that
    the type checker sees the same four names the port declares.
    """

    slug: str
    provider: str
    base_url: str
    client_id: str
    redirect_uri: str | None


def _registration(body: dict[str, Any]) -> _Registration:
    """The four fields a registration is, each refused on its own terms.

    The slug's two refusals are separate because they are different
    answers to the administrator: a slug that is not spelled like a slug
    is a mistake in what they typed (400), and one that is spelled
    correctly but is not theirs to have is a name that is taken (409).
    A reserved name and a name another guild holds are one refusal, so
    that which of the two it was cannot be read off the reply.
    """
    slug = body.get("slug")
    if not isinstance(slug, str) or not has_slug_shape(slug):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_SLUG)
    if not is_valid_slug(slug):
        raise _refusal(web.HTTPConflict, _SLUG_UNAVAILABLE)

    provider = body.get("provider", PROVIDER)
    if provider != PROVIDER:
        # Not a 400 about a value this deployment might one day accept:
        # a registration against a provider nothing here can exchange
        # with is a client that resolves to nothing at sign-in time, and
        # storing it would produce a guild whose link is permanently and
        # silently broken.
        raise _refusal(web.HTTPBadRequest, _UNSUPPORTED_PROVIDER)

    base_url = body.get("base_url")
    if not isinstance(base_url, str) or len(base_url) > _MAX_URL or not is_provider_url(base_url):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_URL)

    redirect_uri = body.get("redirect_uri")
    if redirect_uri is not None and (
        not isinstance(redirect_uri, str)
        or len(redirect_uri) > _MAX_URL
        or not is_provider_url(redirect_uri)
    ):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_URL)

    client_id = body.get("client_id")
    if not isinstance(client_id, str) or not client_id or len(client_id) > _MAX_CLIENT_ID:
        raise _refusal(web.HTTPBadRequest, _MALFORMED_CLIENT_ID)

    return _Registration(
        slug=slug,
        provider=provider,
        base_url=base_url,
        client_id=client_id,
        redirect_uri=redirect_uri,
    )


# ---------------------------------------------------------------------------
# Answering, and recording that it happened
# ---------------------------------------------------------------------------


def _answer(client: GuildOAuthClient) -> web.Response:
    """The read model as JSON. There is no branch here that could add a secret."""
    return web.json_response(
        {
            # A snowflake as a string: a JSON number silently loses its
            # last digits in JavaScript, producing an id that looks right
            # and names nobody.
            "guild_id": str(client.guild_id),
            "oauth_client": {
                "slug": client.slug,
                "provider": client.provider,
                "base_url": client.base_url,
                "client_id": client.client_id,
                # Present and null rather than absent for a guild using
                # the deployment's own callback: a console that could not
                # tell "the default" from "an API that does not send this
                # field" would have to guess, and both guesses are wrong
                # somewhere.
                "redirect_uri": client.redirect_uri,
                #: Whether a secret is stored. All that is left of it.
                "has_secret": client.has_secret,
                "created_at": client.created_at.isoformat(),
                "updated_at": client.updated_at.isoformat(),
            },
        }
    )


def _audit(request: web.Request, guild_id: int, *, outcome: str) -> None:
    """The only record that a guild's sign-in credential changed.

    WARNING, a level above the settings writes next door, because this is
    the credential that decides who gets a session at all: whoever
    controls the identity provider behind a slug controls who this
    console believes is signing in.

    Who, which guild, and which of the four acts it was. **Neither half
    of the credential.** The secret is obvious; the client id is left off
    because it is one half of a pair, and a retained, Grafana-readable
    log is not the place to narrow the other half's blast radius by one
    guess.
    """
    log_event(
        log,
        logging.WARNING,
        Event.CONSOLE_OAUTH_CLIENT_CHANGED,
        "An administrator changed a guild's sign-in client",
        guild_id=guild_id,
        requested_by=_caller(request),
        outcome=outcome,
    )


def _refusal(exception: type[web.HTTPException], reason: str) -> web.HTTPException:
    """A refusal with a JSON body, raised rather than returned.

    aiohttp deprecated returning an `HTTPException` from a handler, and
    raising lets the guards above read as a straight line instead of
    threading an optional response back through every caller.
    """
    return exception(text=json.dumps({"error": reason}), content_type="application/json")


def _clients(request: web.Request) -> GuildOAuthClients:
    return request.app[GUILD_OAUTH_CLIENTS]


def _now(request: web.Request) -> datetime:
    """The one clock this application was built with."""
    from sturnus.console.app import _NOW

    return request.app[_NOW]()


def _caller(request: web.Request) -> int:
    """The Discord id of the person making this request.

    Only ever reached from behind `require_session`; `current_user`
    raises rather than returning `None` if that is ever untrue, so a
    route registered without the wrapper fails loudly instead of quietly
    writing somebody else's guild.
    """
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id
