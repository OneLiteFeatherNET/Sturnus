"""The aiohttp application the console's API process serves.

Handlers here are deliberately thin: parse, delegate, serialise. Every
decision worth testing lives in `sturnus.console.auth` or
`sturnus.console.session`, neither of which needs a request object -- so
the interesting tests do not need a server, and the tests that do need one
are about HTTP itself (status codes, cookie flags, redirects).

Two rules hold throughout, by construction rather than by care:

- **No user input is reflected into a response.** Not the state, not the
  code, not a message from the provider. Errors below are fixed strings.
  It is the same rule `sturnus.infrastructure.linkserver` states, and for
  the same reason: the only way to keep a callback endpoint from becoming
  an XSS sink is never to give it one.
- **Every Discord id is serialised as a string.** A snowflake exceeds
  JavaScript's safe integer range, where a JSON number silently loses its
  last digits -- producing an id that looks right and names nobody.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiohttp import web

from sturnus.console import routes_recording, routes_settings, routes_tags
from sturnus.console.audio import AudioDelivery
from sturnus.console.auth import (
    ConsoleAuth,
    ExchangeRefused,
    NotLinked,
    UnknownState,
)
from sturnus.console.ports import (
    AdminDirectory,
    CollectionNames,
    ConsentDirectory,
    GuildNames,
    GuildReports,
    LinkDirectory,
    OAuthClient,
    PersonalConsents,
    PreferenceDirectory,
    ProfileDirectory,
    QueueControl,
    QueueOverview,
    SessionNaming,
    SessionReads,
    SettingsStore,
    StateStore,
    TagWriter,
    TranscriptReader,
)
from sturnus.console.routes_audio import AUDIO_DELIVERY
from sturnus.console.routes_audio import register as register_audio
from sturnus.console.routes_consent import CONSENT_DIRECTORY
from sturnus.console.routes_consent import register as register_consent
from sturnus.console.routes_consent_self import PERSONAL_CONSENTS
from sturnus.console.routes_consent_self import register as register_consent_self
from sturnus.console.routes_directory import COLLECTION_NAMES, GUILD_NAMES
from sturnus.console.routes_directory import register as register_directory
from sturnus.console.routes_me import PREFERENCES, PROFILE_DIRECTORY
from sturnus.console.routes_me import register as register_me
from sturnus.console.routes_queue import QUEUE_CONTROL, QUEUE_OVERVIEW
from sturnus.console.routes_queue import register as register_queue
from sturnus.console.routes_report import GUILD_REPORTS
from sturnus.console.routes_report import register as register_report
from sturnus.console.session import (
    ExpiredSession,
    InvalidSession,
    SessionCookie,
    SignedSession,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
ReadinessCheck = Callable[[], bool]

#: The cookie's name. Prefixed because a browser sends every cookie on the
#: origin to every path on it, and a generic `session` would collide with
#: anything else ever served from this hostname.
SESSION_COOKIE = "sturnus_session"

#: Keys under which collaborators are stored on the application. Constants
#: rather than bare strings so a typo is an import error rather than a
#: `KeyError` at request time.
_AUTH = web.AppKey("auth", ConsoleAuth)
_SESSIONS = web.AppKey("sessions", SessionCookie)
_ADMINS = web.AppKey("admins", AdminDirectory)
# `AppKey`'s second argument is a runtime type, which a `Callable`
# alias is not -- so these two are annotated instead, which gives the
# same lookup typing without asking for a class that does not exist.
_NOW: web.AppKey[Clock] = web.AppKey("now")
_SCHEMA_READY: web.AppKey[ReadinessCheck] = web.AppKey("schema_ready")
_CONSOLE_ORIGIN = web.AppKey("console_origin", str)

#: Per-request storage for the verified session. A typed `RequestKey`
#: rather than a bare string so a handler cannot read it under a
#: slightly different name and get `None` instead of a type error.
_SESSION = web.RequestKey("session", SignedSession)

#: Where a completed sign-in sends the browser. The console's own root,
#: not a URL from the request: an open redirect on a login callback hands
#: an attacker a link that authenticates through this server and lands
#: somewhere else entirely.
_AFTER_LOGIN = "/"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def current_user(request: web.Request) -> SignedSession:
    """The session of the person making this request.

    Only ever called from a handler behind `require_session`, which is
    what guarantees there is one -- so this raises rather than returning
    `None`, and a handler that forgot the decorator fails loudly in
    development instead of quietly serving somebody else's data.
    """
    session = request.get(_SESSION)
    if not isinstance(session, SignedSession):
        raise RuntimeError(
            "current_user() called from a handler that is not behind require_session"
        )
    return session


def require_session(handler: Handler) -> Handler:
    """Refuses a request that carries no valid session, before the handler runs.

    A decorator rather than a middleware so that authentication is visible
    at each route it protects. A middleware with a path allowlist puts the
    security decision somewhere other than the thing it secures, and the
    failure mode of forgetting to add a path to such a list is an endpoint
    that is silently public.
    """

    async def wrapped(request: web.Request) -> web.StreamResponse:
        token = request.cookies.get(SESSION_COOKIE)
        if token is None:
            return _unauthorised()
        try:
            request[_SESSION] = request.app[_SESSIONS].read(token, request.app[_NOW]())
        except ExpiredSession:
            return _unauthorised("session expired")
        except InvalidSession:
            return _unauthorised()
        return await handler(request)

    # Kept so aiohttp's route naming and any future introspection see the
    # handler rather than the wrapper.
    wrapped.__name__ = handler.__name__
    wrapped.__doc__ = handler.__doc__
    return wrapped


def _unauthorised(reason: str = "not signed in") -> web.Response:
    return web.json_response({"error": reason}, status=401)


async def healthz(_request: web.Request) -> web.Response:
    """Liveness. Deliberately independent of the schema: this process does
    not run migrations (the worker owns them, Spec 13.1), and a liveness
    probe that waited for one would restart this process forever during a
    fresh deploy.
    """
    return web.json_response({"status": "ok"})


async def readyz(request: web.Request) -> web.Response:
    if not request.app[_SCHEMA_READY]():
        return web.json_response({"status": "waiting for schema"}, status=503)
    return web.json_response({"status": "ready"})


async def login(request: web.Request) -> web.StreamResponse:
    url = await request.app[_AUTH].begin(request.app[_NOW]())
    raise web.HTTPFound(url)


async def callback(request: web.Request) -> web.StreamResponse:
    """Completes the sign-in and sets the session cookie.

    Each failure gets its own status because each means something
    different to the person in front of the browser: 400 for a callback
    that belongs to no login, 403 for an identity that may not in. The
    body carries a fixed reason string and never anything from the query
    or the provider.
    """
    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return web.json_response({"error": "malformed callback"}, status=400)

    try:
        user = await request.app[_AUTH].authenticate(code, state, request.app[_NOW]())
    except UnknownState:
        return web.json_response({"error": "unknown or expired sign-in attempt"}, status=400)
    except ExchangeRefused:
        return web.json_response({"error": "the identity provider refused"}, status=403)
    except NotLinked:
        # The one error the console renders as an instruction rather than a
        # refusal: this person authenticated and simply has no `/link` yet,
        # which is something they can go and fix.
        return web.json_response({"error": "no linked Discord account"}, status=403)

    # Raised rather than returned: aiohttp deprecated returning an
    # `HTTPException`, and an exception carries headers -- the cookie
    # included -- exactly as a response does.
    redirect = web.HTTPFound(_AFTER_LOGIN)
    _set_session_cookie(request, redirect, user.discord_user_id)
    raise redirect


def _set_session_cookie(
    request: web.Request, response: web.StreamResponse, discord_user_id: int
) -> None:
    """Writes the session cookie with the flags that make it a session.

    `secure` is unconditional: this API is only ever served over TLS
    (Cloudflare Tunnel terminates it), and making the flag conditional on
    something observable at runtime is how it ends up off in the one
    environment that mattered.

    `samesite="Lax"` rather than `Strict` because the OAuth callback is a
    cross-site navigation -- `Strict` would drop the cookie on the very
    hop that sets it.

    No `max_age`: the browser drops it at the end of the session, and the
    authoritative expiry is inside the signed payload where the holder
    cannot extend it (see `sturnus.console.session`).
    """
    token = request.app[_SESSIONS].issue(
        SignedSession(discord_user_id=discord_user_id), now=request.app[_NOW]()
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )


async def logout(_request: web.Request) -> web.Response:
    """Clears the cookie.

    204 whether or not there was a session: signing out of nothing is what
    a stale tab does, and answering 401 would make the console show an
    error for what is in every sense a successful outcome.
    """
    response = web.Response(status=204)
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


def build_api(
    *,
    oauth: OAuthClient,
    states: StateStore,
    links: LinkDirectory,
    admins: AdminDirectory,
    reads: SessionReads,
    config: SettingsStore,
    sessions: SessionCookie,
    now: Clock,
    schema_ready: ReadinessCheck,
    console_origin: str,
    audio: AudioDelivery,
    queue: QueueControl,
    tags: TagWriter,
    queues: QueueOverview,
    consents: ConsentDirectory,
    own_consents: PersonalConsents,
    reports: GuildReports,
    profile: ProfileDirectory,
    prefs: PreferenceDirectory,
    names: GuildNames,
    collections: CollectionNames,
    transcripts: TranscriptReader,
    naming: SessionNaming,
) -> web.Application:
    """Builds the application, with every collaborator injected.

    `now` is a callable rather than a value so a test can pin it, the same
    reason `SystemClock` exists for the bot. `schema_ready` reports
    whether the tables the worker creates have appeared; the caller starts
    this server before waiting for them, so `/healthz` answers from the
    first moment while `/readyz` stays 503.
    """
    # Imported here rather than at module scope: `routes_read` imports
    # `require_session` from this module, so a top-level import in both
    # directions is a cycle that fails on whichever is loaded first.
    from sturnus.console import routes_read

    app = web.Application()
    app[_AUTH] = ConsoleAuth(oauth, states, links)
    app[_SESSIONS] = sessions
    app[_ADMINS] = admins
    app[routes_read.READS] = reads
    app[routes_settings.SETTINGS_STORE] = config
    app[_NOW] = now
    app[_SCHEMA_READY] = schema_ready
    app[_CONSOLE_ORIGIN] = console_origin
    app[AUDIO_DELIVERY] = audio
    app[QUEUE_CONTROL] = queue
    app[routes_tags.TAG_WRITER] = tags
    app[QUEUE_OVERVIEW] = queues
    app[CONSENT_DIRECTORY] = consents
    app[PERSONAL_CONSENTS] = own_consents
    app[GUILD_REPORTS] = reports
    app[PROFILE_DIRECTORY] = profile
    app[PREFERENCES] = prefs
    app[GUILD_NAMES] = names
    app[COLLECTION_NAMES] = collections
    app[routes_recording.TRANSCRIPTS] = transcripts
    app[routes_recording.SESSION_NAMING] = naming
    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/readyz", readyz),
            web.get("/api/auth/login", login),
            web.get("/api/auth/callback", callback),
            web.post("/api/auth/logout", logout),
        ]
    )
    routes_read.register(app)
    register_audio(app)
    register_queue(app)
    register_consent(app)
    register_consent_self(app)
    register_report(app)
    register_me(app)
    register_directory(app)
    routes_settings.register(app)
    routes_tags.register(app)
    routes_recording.register(app)
    return app
