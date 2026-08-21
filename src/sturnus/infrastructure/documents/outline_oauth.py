"""The Outline OAuth client that establishes a participant's identity (Spec 8.4).

**The API shape here is researched, not live-verified.** No running Outline
instance with a registerable OAuth application was reachable while this
module was written. Every endpoint path, parameter name, and response field
below is grounded in Outline's public OAuth-provider documentation, its
merged implementation PR, and its published OpenAPI spec -- not confirmed
by an actual token exchange against a live server. See
`docs/verification/outline-oauth.md` for exactly what each assumption rests
on and what must be confirmed before this ships.

Spec 8.4's whole simplification is that Sturnus never stores an Outline
access or refresh token: `identity_from_code` exchanges the authorization
code, reads the identity it names, and lets the token go out of scope when
the coroutine returns. `OutlineOAuth` itself holds no per-flow state either
-- `authorize_url` and `identity_from_code` share nothing between calls,
which is also why this client does not attempt PKCE: Outline's `/oauth/
authorize` accepts a `code_challenge`, but using it would require carrying
a `code_verifier` from the browser redirect through to the callback, and
the state that survives that round trip (`sturnus.infrastructure.db.
link_state.LinkStateStore`, `sturnus.application.linking.PendingLink`) has
no field for one. Adding PKCE here without that plumbing would mean caching
the verifier in this object's own memory, which breaks the moment Sturnus
runs more than one process or restarts between authorize and callback --
worse than not having PKCE at all. See the verification doc for the
follow-up this implies.

Because a wrong guess here is expensive in a specific way -- a wrong user id
field does not fail loudly, it produces a mention that silently resolves to
nobody -- every place a guess could be wrong is deliberately kept to one
line each:

- `_AUTHORIZE_PATH` and `_TOKEN_PATH` are the only places the OAuth
  endpoint paths are spelled.
- `_IDENTITY_PATH` is the only place the identity endpoint path is spelled.
- `_extract_identity` is the only place the identity response shape,
  including which field is the user id, is read.
- `SCOPE` is the only place the requested scope is spelled.

Fixing any one of those after real verification should not touch the rest
of this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Assumed authorization endpoint (Outline's built-in OAuth 2.0 provider,
#: distinct from the OIDC login endpoints used to sign into Outline
#: itself). UNVERIFIED against a live instance -- see
#: docs/verification/outline-oauth.md.
_AUTHORIZE_PATH = "/oauth/authorize"

#: Assumed token endpoint, same provider. UNVERIFIED.
_TOKEN_PATH = "/oauth/token"

#: Assumed identity endpoint: Outline's `auth.info` RPC call, the same one
#: personal API tokens use to "check that a token is still valid and load
#: the IDs for the current user and workspace". Outline's OAuth routes
#: expose no dedicated `/oauth/userinfo`-style endpoint of their own, so an
#: OAuth access token is assumed to work as a bearer credential against the
#: ordinary API exactly like a personal API token does. UNVERIFIED.
_IDENTITY_PATH = "/api/auth.info"

#: The narrowest scope assumed to exist that can still read one's own
#: identity. Outline's scope model (per its `Scope` enum: `read`, `write`,
#: `create`) is not granular per-resource, so `read` -- while it also grants
#: read access to documents and collections this bot has no use for -- is
#: the least broad option available, not a scope reserved for identity
#: alone. See docs/verification/outline-oauth.md.
SCOPE = "read"


class LinkExchangeError(Exception):
    """Raised when Outline refuses to turn an authorization code into an identity.

    Deliberately carries no response body, code, or token in its message:
    a caller may log this exception, and none of those three may reach a
    log line.
    """

    def __init__(self, reason: str, *, status_code: int) -> None:
        super().__init__(f"Outline rejected the account link attempt: {reason}")
        self.status_code = status_code


@dataclass(frozen=True)
class ExternalIdentity:
    """The Outline identity a successfully exchanged authorization code names.

    `external_user_id` becomes `account_link.external_user_id` -- it is
    what mention rendering resolves back to an `@[..](mention://user/..)`
    reference, so it must be Outline's own opaque user id, not a display
    name or email that could collide or change.
    """

    external_user_id: str
    display_name: str


def _extract_identity(payload: dict[str, Any]) -> ExternalIdentity:
    """Reads the user id and display name out of a successful `auth.info` response.

    UNVERIFIED shape, assumed from Outline's published OpenAPI spec: the
    response wraps the payload under a top-level `data` object (as the rest
    of Outline's RPC-style API does), and the current user sits nested
    under `data.user` alongside a sibling `data.team` this client ignores.
    `data.user.id` is assumed to be Outline's internal, immutable user id
    (a UUID) -- this is the field that ends up in `account_link.
    external_user_id`, so a wrong guess here does not raise; it produces a
    mention that silently resolves to nobody.
    """
    user = payload["data"]["user"]
    return ExternalIdentity(external_user_id=str(user["id"]), display_name=str(user["name"]))


class OutlineOAuth:
    """Exchanges an OAuth authorization code for an Outline identity, once.

    Holds no per-flow state and stores no token: `identity_from_code`
    exchanges the code, reads the identity, and returns -- the access
    token it briefly holds goes out of scope with the coroutine frame.
    `transport` exists purely so tests can substitute `httpx.MockTransport`
    -- production code never passes it and gets a real network transport.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._transport = transport

    def authorize_url(self, state: str) -> str:
        """Builds the URL to send the user's browser to.

        Carries `client_id`, `redirect_uri`, `scope`, and `state` -- never
        `client_secret`, which this URL must not carry because it is handed
        to the user's browser, not kept between this bot and Outline.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "state": state,
            }
        )
        return f"{self._base_url}{_AUTHORIZE_PATH}?{query}"

    async def identity_from_code(self, code: str) -> ExternalIdentity:
        """Exchanges `code` for an access token, reads the identity it names, discards the token.

        Raises `LinkExchangeError` if Outline refuses either the code
        exchange or the subsequent identity lookup -- callers should treat
        both as "this link attempt failed", not distinguish them.
        """
        async with httpx.AsyncClient(transport=self._transport) as http:
            # No `code`, no `client_secret`, no request body: an
            # authorization code is a bearer credential for one exchange and
            # the secret is one for every exchange.
            log_event(
                log,
                logging.DEBUG,
                Event.LINK_CALLBACK_REJECTED,
                "Exchanging an Outline authorization code for an access token",
                reason="exchange_started",
            )
            token_response = await http.post(
                f"{self._base_url}{_TOKEN_PATH}",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if token_response.status_code != httpx.codes.OK:
                # The status code, never the response body: Outline's error
                # bodies are not documented here and have not been read, so
                # they are exactly the class of content that needs reading
                # before it is waved through.
                log_event(
                    log,
                    logging.WARNING,
                    Event.LINK_EXCHANGE_FAILED,
                    "Outline rejected the authorization code exchange",
                    http_status=token_response.status_code,
                    reason="code_exchange",
                )
                raise LinkExchangeError(
                    "the authorization code was refused", status_code=token_response.status_code
                )

            access_token = token_response.json()["access_token"]

            log_event(
                log,
                logging.DEBUG,
                Event.LINK_ESTABLISHED,
                "Fetching the linked identity from Outline",
                reason="identity_lookup_started",
            )
            identity_response = await http.post(
                f"{self._base_url}{_IDENTITY_PATH}",
                headers={"Authorization": f"Bearer {access_token}"},
                json={},
            )

        if identity_response.status_code != httpx.codes.OK:
            log_event(
                log,
                logging.WARNING,
                Event.LINK_EXCHANGE_FAILED,
                "Outline rejected the identity lookup",
                http_status=identity_response.status_code,
                reason="identity_lookup",
            )
            raise LinkExchangeError(
                "the identity lookup was refused", status_code=identity_response.status_code
            )

        identity = _extract_identity(identity_response.json())
        # `identity.display_name` came back on the same response and is
        # deliberately not logged -- see `fields.DENIED_NAMES`.
        log_event(
            log,
            logging.INFO,
            Event.LINK_ESTABLISHED,
            "Resolved the external identity",
            external_user_id=identity.external_user_id,
            provider="outline",
        )
        return identity
