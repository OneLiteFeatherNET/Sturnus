"""A guild's own OAuth client, and the slug that selects it.

`GET /api/auth/login` takes no parameters and reads no cookie -- there is
no session yet, that is what login is for -- so it cannot look up a
guild's client from an identity it does not have. `/g/{slug}/sign-in`
carries the guild in the URL instead, which is why `slug` is unique
across the deployment rather than per guild: it is a public path segment
and has to name exactly one guild. Two guilds behind `/g/acme/sign-in`
would send one of them through the other's identity provider.

The alternative -- a public page listing every guild Sturnus serves --
was rejected because it discloses which organisations use the service to
anyone, signed in or not. An administrator distributes their own link.

**The secret is not here.** `GET` on an OAuth configuration returns the
client id, the base URL, the redirect URI and whether a secret is set --
never the secret, not even masked-but-recoverable. So the read model
carries `has_secret` and has nowhere to put the value, which is the same
construction `ExportTarget` uses and for the same reason: the settings
API renders what it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GuildOAuthClient:
    """One guild's console sign-in client, as anything outside may see it.

    One per guild, keyed by the guild: a guild with two clients would
    make "which one does this state select" a question the callback
    cannot answer, since the state is what selects the client for the
    code exchange.

    `redirect_uri` is nullable and means "the one this deployment is
    configured with". A guild that runs its own identity provider against
    the same console does not need a different callback, and requiring
    one would make every registration carry a value nobody varied.

    **This is the console sign-in flow only.** `api` holds the master key
    and `link` does not -- the chart's `_helpers.tpl` actively prevents
    adding it -- so the Discord account-link flow stays on the
    environment-configured client. Saying that out loud is what stops
    somebody "fixing the asymmetry" later by handing `link` the master
    key, which is the one change this architecture exists to prevent.
    """

    guild_id: int
    slug: str
    provider: str
    base_url: str
    client_id: str
    redirect_uri: str | None
    #: Whether a client secret is stored, never the secret itself.
    has_secret: bool
    created_at: datetime
    updated_at: datetime
