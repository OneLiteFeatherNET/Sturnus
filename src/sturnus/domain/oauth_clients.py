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

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlparse

#: Short enough to be typed and read back over a chat message, long enough
#: to name an organisation. The lower bound is also what keeps a slug from
#: landing on the one- and two-letter path segments a deployment is most
#: likely to want for itself.
MIN_SLUG_LENGTH: Final = 3
MAX_SLUG_LENGTH: Final = 32

#: Lowercase, hyphen-separated words, **beginning with a letter**.
#:
#: The leading letter is the rule that costs the most and buys the most: a
#: Discord snowflake is digits, and `/g/1289374650912837465/sign-in` and a
#: guild id in a path are the same string to whoever is reading the link.
#: Requiring a letter first makes a slug and an id unconfusable rather
#: than merely unlikely to be confused.
#:
#: One case, so `/g/Acme/sign-in` and `/g/acme/sign-in` cannot be two
#: links that look identical in a sans-serif font and select different
#: credentials. Refused rather than lowercased: a slug quietly rewritten
#: on the way into the table is a slug the administrator does not
#: recognise in the link they were told to hand out.
_SHAPE: Final = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", re.ASCII)

#: Names this deployment serves, or may serve, itself.
#:
#: The console publishes a guild's link at `/g/{slug}/sign-in` and the API
#: reads the same word out of `?guild=`, so a slug is a path segment in
#: everything but name. Reserving these costs a guild one candidate and
#: buys the certainty that a route added later cannot be shadowed by a
#: name a guild already claimed -- a collision nobody can resolve
#: afterwards, because the loser is somebody's published sign-in link.
#:
#: Every entry is a name the shape rules would otherwise allow;
#: `tests/domain/test_oauth_slugs.py` pins that, so an entry that stops
#: doing any work is visible rather than decorative.
RESERVED_SLUGS: Final[frozenset[str]] = frozenset(
    {
        "api",
        "assets",
        "auth",
        "callback",
        "console",
        "guild",
        "guilds",
        "healthz",
        "login",
        "logout",
        "readyz",
        "session",
        "sessions",
        "sign-in",
        "sign-out",
        "static",
        "sturnus",
        "well-known",
    }
)


def has_slug_shape(slug: str) -> bool:
    """Whether this is spelled like a slug, reservations aside.

    Separate from `is_valid_slug` because the two refusals are different
    answers to an administrator registering one: a slug that is not
    spelled like a slug is a mistake in what they typed, and a slug that
    is spelled correctly but is not theirs to have is a name that is
    taken. The read path never needs the distinction -- an unusable slug
    resolves to nothing however it came to be unusable -- so it calls
    `is_valid_slug` and asks no further.
    """
    return MIN_SLUG_LENGTH <= len(slug) <= MAX_SLUG_LENGTH and _SHAPE.fullmatch(slug) is not None


def is_valid_slug(slug: str) -> bool:
    """Whether this may be the word in a guild's sign-in link.

    The one decision about what a slug is, in a pure function with no
    database behind it, because it is reached from two directions that
    must agree: the administrator registering a slug, and the sign-in
    endpoint resolving one. A login that looked up a slug the write path
    would have refused is a query that can only ever miss -- and a login
    that resolved one the write path allowed is the bug this function
    exists to make impossible.

    Nothing here normalises. Trimming whitespace or lowercasing would
    make `Acme `, `acme` and ` ACME` the same registration, and the link
    an administrator distributes carries whichever of them they typed.
    """
    return has_slug_shape(slug) and slug not in RESERVED_SLUGS


class SlugUnavailable(Exception):
    """The slug asked for is not this guild's to have.

    One exception for "another guild already holds it" and for "this
    deployment reserves it", because they are one answer to the person
    asking: pick a different name. Splitting them would also hand a
    caller a way to tell a claimed slug from a free one by which refusal
    came back, and the claimed ones are precisely what §2.2 does not want
    enumerable.
    """


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


def is_provider_url(value: str) -> bool:
    """Whether this may be a guild's identity-provider base URL or callback.

    Held to more than "parses": the base URL is where a browser is sent
    to authorise, and the redirect URI is where it comes back, so both
    are addresses an administrator of one guild chooses and other people
    follow.

    - **`https` only.** The authorization code and, for the base URL, the
      whole consent step travel over it. A guild that could register
      `http://` would be a guild whose members' codes cross the network
      in the clear.
    - **No userinfo.** `https://console.example@evil.example/` is a valid
      URL naming `evil.example`, and reads to a human as the first host.
      This is the one form where refusing to parse is the difference
      between what an administrator reviewing the value sees and what a
      browser does.
    - **No query and no fragment.** `authorize_url` builds its own query
      string; a base URL carrying one would produce two, and a fragment
      never reaches a server at all.

    A path is allowed: an Outline behind `https://wiki.example/outline`
    is an ordinary deployment, and `OutlineOAuth` appends to whatever it
    is given.
    """
    if value != value.strip() or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and "@" not in parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )
