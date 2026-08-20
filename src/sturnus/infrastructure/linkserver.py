"""The link service: the OAuth account-link callback (Spec 8.4, Spec 13.2).

This is the only publicly reachable process in the system. It holds the
OAuth client secret and a database connection, and nothing else -- no
Discord token, no S3 credentials, no master key. That separation is the
reason it is a deployment of its own, and nothing here may erode it: this
module talks to no store but the two ports it is handed, and imports
nothing that would pull in a wider blast radius.

Three routes only: `/healthz` and `/readyz` for Kubernetes, and
`/oauth/callback`, which validates both parameters are present, consumes
the state, exchanges the code, saves the mapping, and returns a small
self-contained confirmation page telling the person to return to Discord.

Two properties hold everywhere in this module, by construction rather than
by care:

- **No user input is ever reflected into a response.** Not the state, not
  the code, not an error message from Outline. The confirmation and error
  pages below are fixed strings with no interpolation slot for any of
  them -- the only way to keep a callback endpoint from becoming an XSS
  sink is to never give it one.
- **Nothing sensitive is logged.** Log lines here name a Discord user id, a
  provider, and an external user id -- never the state, the code, or a
  token. `OutlineOAuth` already applies the same rule to the exchange
  itself; this module does not undo that by logging around it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from aiohttp import web

from sturnus.application.linking import PendingLink
from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity, LinkExchangeError

log = logging.getLogger(__name__)

_CONFIRMATION_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Account linked</title>
<style>
  body { font-family: sans-serif; text-align: center; padding: 4rem 1rem; }
  h1 { color: #2e7d32; }
</style>
</head>
<body>
<h1>Your account is linked</h1>
<p>You can close this tab and return to Discord.</p>
</body>
</html>
"""

_ERROR_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Link failed</title>
<style>
  body { font-family: sans-serif; text-align: center; padding: 4rem 1rem; }
  h1 { color: #c62828; }
</style>
</head>
<body>
<h1>Something went wrong</h1>
<p>This link could not be completed. Please return to Discord and run
<code>/link</code> again.</p>
</body>
</html>
"""


class OAuthClient(Protocol):
    """What the callback route needs from an OAuth client (see `OutlineOAuth`)."""

    async def identity_from_code(self, code: str) -> ExternalIdentity: ...


class StateStore(Protocol):
    """What the callback route needs from a state store (see `LinkStateStore`)."""

    async def consume(self, state: str, now: datetime) -> PendingLink | None: ...


class LinkRepository(Protocol):
    """What the callback route needs from an account-link repository.

    See `sturnus.infrastructure.db.repositories.AccountLinkRepository.save`.
    """

    async def save(
        self, discord_user_id: int, provider: str, external_user_id: str, display_name: str
    ) -> None: ...


def build_app(
    *,
    oauth: OAuthClient,
    states: StateStore,
    links: LinkRepository,
    now: Callable[[], datetime],
    schema_ready: Callable[[], bool],
) -> web.Application:
    """Builds the aiohttp application the link deployment serves.

    `now` is injected rather than read from the wall clock directly so a
    test can pin it -- the same reason `SystemClock` exists for the bot.

    `schema_ready` reports whether the database tables the worker creates
    have appeared yet. The caller starts this server *before* waiting for
    them, so `/healthz` answers from the first moment while `/readyz` stays
    503 until the wait finishes -- see `sturnus.entrypoints.link`.
    """

    async def healthz(_request: web.Request) -> web.Response:
        # Liveness only: the process answers HTTP. No dependency is
        # checked here -- a slow database must not make Kubernetes kill an
        # otherwise-fine process.
        return web.json_response({"status": "ok"})

    async def readyz(_request: web.Request) -> web.Response:
        # Beyond the schema, this process holds no connection of its own:
        # `states` and `links` are backed by the same database the callback
        # route exercises on every real request, so there is nothing further
        # to probe that `/healthz` does not already cover.
        if not schema_ready():
            return web.json_response({"status": "waiting for database schema"}, status=503)
        return web.json_response({"status": "ready"})

    async def oauth_callback(request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            log.warning("Rejected an OAuth callback missing a required parameter")
            return web.Response(text=_ERROR_PAGE, content_type="text/html", status=400)

        pending = await states.consume(state, now())
        if pending is None:
            # Covers both a forged state and a replayed one -- see
            # `LinkStateStore.consume`, which deliberately makes the two
            # indistinguishable to the caller.
            log.warning("Rejected an OAuth callback with an unknown, expired or reused state")
            return web.Response(text=_ERROR_PAGE, content_type="text/html", status=400)

        try:
            identity = await oauth.identity_from_code(code)
        except LinkExchangeError:
            log.warning(
                "Outline refused the account link attempt for discord_user_id=%s",
                pending.discord_user_id,
            )
            return web.Response(text=_ERROR_PAGE, content_type="text/html", status=502)

        await links.save(
            pending.discord_user_id,
            pending.provider,
            identity.external_user_id,
            identity.display_name,
        )
        log.info(
            "Linked discord_user_id=%s to %s account external_user_id=%s",
            pending.discord_user_id,
            pending.provider,
            identity.external_user_id,
        )
        return web.Response(text=_CONFIRMATION_PAGE, content_type="text/html", status=200)

    app = web.Application()
    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/readyz", readyz),
            web.get("/oauth/callback", oauth_callback),
        ]
    )
    return app
