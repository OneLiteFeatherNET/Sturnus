"""Setting a guild up from the console, by asking the bot to do it.

- `POST /api/guilds/{guild_id}/setup` -- ask
- `GET  /api/guilds/{guild_id}/setup` -- what came of it
- `GET  /api/invite` -- the link that puts the bot in a server

**Why an ask rather than a write.** Every step of setting a guild up that
matters needs a Discord token: creating the consent role, denying `Speak`
to `@everyone` and allowing it for that role, registering the command
tree. This process holds no Discord token and must not be given one
(Spec 13.2) -- it already holds S3 and the master key, so it can decrypt
every recording ever made, and that is not a process to also hand the
ability to act as the bot. So it writes down what should be true, and the
bot's existing ten-second reconcile tick makes it true and writes back
what happened. The mirrors run backwards.

That is also why this endpoint does not write `guild_config` directly even
though it could. A guild whose `voice_channel_ids` names a room where
`@everyone` may still speak looks configured and is not; the two halves
are one act, and only the bot can perform either half of it.

**Poll, do not wait.** `POST` answers immediately with the same payload
`GET` answers, because there is nothing to wait for: the request is a row,
and the bot reaches it within a tick. The console polls `GET` until
`request.status` stops being `pending`. `pending` for more than a few
seconds means the bot is not in this guild yet, which `bot.seen_at`
already says outright.

**404, never 403.** A guild this person does not administer answers
exactly as a guild that does not exist. Writing a setup request is an act
on somebody else's server, and a 403 would confirm to somebody just
established as having no business with that guild that it exists.

**Nothing a caller typed is reflected back in a refusal.** Every reason
below is a fixed string, the same rule the rest of `sturnus.console`
follows. The one thing that is echoed is the stored intent -- which the
same person's own guild wrote, and which the console has to render.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from sturnus.console.ports import GuildSetup, GuildSetupState
from sturnus.domain import settings
from sturnus.domain.onboarding import (
    INVITE_PERMISSIONS,
    INVITE_SCOPES,
    SetupIntent,
    invite_url,
)
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Where the collaborators are found. Their own keys rather than
#: parameters to `register`, so `build_api` stays a two-line edit --
#: several agents are adding sections to that function and each extra
#: line is a merge by hand.
GUILD_SETUP: web.AppKey[GuildSetup] = web.AppKey("guild_setup")

#: This deployment's Discord application id, or `None` if the operator has
#: not configured one. Not a credential: an application id is public by
#: design and appears in every invite link ever clicked.
DISCORD_CLIENT_ID: web.AppKey[str | None] = web.AppKey("discord_client_id")

_SETUP_PATH = "/api/guilds/{guild_id}/setup"
_INVITE_PATH = "/api/invite"

#: Discord's own limit on a role name. Checked here rather than left to
#: the gateway because the alternative is a request that is accepted, sits
#: pending for a tick, and comes back failed for a reason the person could
#: have been told while they were still typing.
_MAX_ROLE_NAME = 100

#: The one refusal covering every reason there is to refuse a guild. See
#: the module docstring on why "there is none" and "not yours" are one
#: answer.
_NO_SUCH_GUILD = "no such guild"

_MALFORMED_BODY = "malformed request body"
_CHANNELS_MUST_BE_STRINGS = "channel_ids must be a list of snowflake strings"
_CHANNELS_REFUSED = "the channel list is not valid"
_ROLE_NAME_MUST_BE_A_STRING = "consent_role_name must be a string"
_ROLE_NAME_REFUSED = "the consent role name is not valid"

#: What a setup request has asked for is a guild's configuration and who
#: asked for it. Nothing between this and the browser has any business
#: keeping a copy.
_PRIVATE = {"Cache-Control": "private, no-store"}

#: `applied_at` is null and no outcome has been written: the bot has not
#: reached this request yet. Not one of `sturnus.domain.onboarding`'s
#: outcomes, because it is the absence of one -- the row is not settled.
PENDING = "pending"


def register(app: web.Application) -> None:
    """Adds the onboarding routes to an application that already has a session.

    `require_session` is applied here rather than as a decorator on each
    handler, and to all three without exception -- including the invite
    link, whose URL is public. Public is not the same as unauthenticated:
    an endpoint of this API that answered without a session would be the
    only one, and "visibly public" is a property worth keeping for the
    routes that genuinely are.
    """
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_SETUP_PATH, require_session(read_setup)),
            web.post(_SETUP_PATH, require_session(request_setup)),
            web.get(_INVITE_PATH, require_session(bot_invite)),
        ]
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def read_setup(request: web.Request) -> web.Response:
    """Where this guild's setup has got to, and whether the bot is even there."""
    guild_id = _guild_id(request)
    state = await request.app[GUILD_SETUP].state(guild_id, requested_by=_caller(request))
    if state is None:
        raise _refusal(web.HTTPNotFound, _NO_SUCH_GUILD)
    return _state_response(guild_id, state)


async def request_setup(request: web.Request) -> web.Response:
    """Asks the bot to configure this guild, and answers with the guild's state.

    The same payload `GET` answers, deliberately. Under the rule that the
    newest ask wins, "what did I just ask for" and "what will this guild
    be configured from" are the same question -- and if somebody else
    asked in between, the honest answer to both is theirs.
    """
    from sturnus.console.app import _NOW

    guild_id = _guild_id(request)
    discord_user_id = _caller(request)
    channel_ids, consent_role_name = await _requested_setup(request)

    state = await request.app[GUILD_SETUP].request(
        guild_id,
        requested_by=discord_user_id,
        channel_ids=channel_ids,
        consent_role_name=consent_role_name,
        now=request.app[_NOW](),
    )
    if state is None:
        raise _refusal(web.HTTPNotFound, _NO_SUCH_GUILD)

    # The only record that a person asked, as opposed to that the bot
    # acted: the two are separated by a tick, and by however long a guild
    # takes to invite the bot. Names who and which guild, never the
    # channel list -- ids are already visible to anybody in the server,
    # but the role name is free text somebody typed.
    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_SETUP_REQUESTED,
        "The console asked the bot to set a guild up",
        guild_id=guild_id,
        discord_user_id=discord_user_id,
    )
    # 202, not 201: nothing exists yet that this URL now addresses. The
    # bot has been asked, and the body says how to find out whether it
    # did anything.
    return _state_response(guild_id, state, status=202)


async def bot_invite(request: web.Request) -> web.Response:
    """The link that puts this deployment's bot into a server.

    The one onboarding step that is genuinely web-doable, and the step
    every other one waits on: until the bot is in the guild it mirrors
    nothing, so nobody administers it as far as this API is concerned and
    every other route here answers 404 for it.

    Answers with `url: null` rather than a refusal when no application id
    is configured. A console that could not tell "this deployment has not
    been given its client id" from "an API that does not serve this" would
    have to guess, and both guesses are wrong somewhere.
    """
    client_id = request.app[DISCORD_CLIENT_ID]
    return web.json_response(
        {
            "client_id": client_id,
            "url": None if client_id is None else invite_url(client_id),
            # Sent even when there is no link, because they are what the
            # page tells somebody to tick if they build the link by hand
            # in Discord's own URL generator instead.
            "permissions": INVITE_PERMISSIONS,
            "scopes": list(INVITE_SCOPES),
        },
        headers=_PRIVATE,
    )


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def _guild_id(request: web.Request) -> int:
    """The guild in the path, or 404.

    A path segment that is not a number names no guild, which is the same
    answer as naming one that does not exist -- and the same answer as
    naming one this person does not administer.
    """
    try:
        return int(request.match_info["guild_id"])
    except ValueError:
        raise _refusal(web.HTTPNotFound, _NO_SUCH_GUILD) from None


async def _requested_setup(request: web.Request) -> tuple[str, str | None]:
    """The body, as the stored spelling of a channel list and a role name.

    Rendered into `guild_config`'s own format here rather than kept as a
    list, because that is what an intent stores: applying it is then a
    write of the value rather than a second serialisation nobody would
    keep in step with the first.

    Every rule about what a channel list may contain is
    `settings.parse_channel_ids`', reached by round-tripping what was
    rendered. A duplicate, an empty list and a non-integer are all refused
    there, with reasons argued there -- and a second copy of those rules
    here is how the two drift apart, with the copy nobody exercises going
    stale first.
    """
    try:
        body = await request.json()
    except ValueError:
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY) from None
    if not isinstance(body, dict):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY)

    channels = body.get("channel_ids")
    if not isinstance(channels, list) or not all(isinstance(each, str) for each in channels):
        raise _refusal(web.HTTPBadRequest, _CHANNELS_MUST_BE_STRINGS)
    # Snowflakes as strings, always: one exceeds JavaScript's safe integer
    # range, where a JSON number silently loses its last digits and
    # produces an id that looks right and names nothing. A client that
    # sent numbers would be a client that has already lost them.
    rendered = ",".join(channels)
    try:
        settings.parse_channel_ids(rendered)
    except settings.InvalidChannelList:
        raise _refusal(web.HTTPBadRequest, _CHANNELS_REFUSED) from None

    return rendered, _requested_role_name(body)


def _requested_role_name(body: dict[str, object]) -> str | None:
    """The consent role's name, if the request named one.

    Absent and `null` both mean "do not name one", which leaves whatever
    role the guild already has -- omitting it must never be the
    destructive path (Spec 10.1). A blank string is refused rather than
    treated as absent: a role called nothing is not what anybody meant,
    and Discord would refuse it a tick later anyway.
    """
    name = body.get("consent_role_name")
    if name is None:
        return None
    if not isinstance(name, str):
        raise _refusal(web.HTTPBadRequest, _ROLE_NAME_MUST_BE_A_STRING)
    trimmed = name.strip()
    if not trimmed or len(trimmed) > _MAX_ROLE_NAME:
        raise _refusal(web.HTTPBadRequest, _ROLE_NAME_REFUSED)
    return trimmed


# ---------------------------------------------------------------------------
# Writing the response
# ---------------------------------------------------------------------------


def _state_response(guild_id: int, state: GuildSetupState, *, status: int = 200) -> web.Response:
    return web.json_response(
        {
            # Every Discord id as a string: a snowflake exceeds
            # JavaScript's safe integer range.
            "guild_id": str(guild_id),
            "bot": {
                # The field the channel picker depends on. An empty
                # channel list means "this server has no voice channels"
                # only when this is true; while it is false it means
                # nobody has looked yet.
                "has_arrived": state.seen_at is not None,
                # Present and null rather than absent, so a client never
                # has to tell "the bot has never swept this guild" from
                # "an API that does not send this".
                "seen_at": None if state.seen_at is None else state.seen_at.isoformat(),
            },
            "request": None if state.intent is None else _intent_json(state.intent),
        },
        status=status,
        headers=_PRIVATE,
    )


def _intent_json(intent: SetupIntent) -> dict[str, object]:
    return {
        # A row id rather than a snowflake, and a string anyway: every id
        # this API sends is one, and a client that had to remember which
        # kind each field was would eventually get one wrong.
        "id": str(intent.id),
        "status": _status_of(intent),
        "requested_by": str(intent.requested_by),
        "requested_at": intent.requested_at.isoformat(),
        # The list as the console offered it, back in the spelling it sent
        # -- so a page can tick the same boxes again without parsing a
        # stored format. Empty only for a row written by hand: the API
        # refuses a request that names no channel.
        "channel_ids": _channel_ids_json(intent.channel_ids),
        "consent_role_name": intent.consent_role_name,
        # When the bot finished with it, however it finished. Null while
        # the status is `pending`, and the two are the same fact said
        # twice on purpose: a client keys off the status, and a person
        # reading the payload gets a time.
        "settled_at": None if intent.applied_at is None else intent.applied_at.isoformat(),
        # Null unless the status is `failed`. Free text the bot composed
        # for a person to act on -- which channel, which permission, what
        # to do about it -- so it is rendered rather than keyed off.
        "error": intent.error,
    }


def _status_of(intent: SetupIntent) -> str:
    """`pending`, or whatever the bot wrote when it settled the row.

    Not narrowed to the outcomes this build knows. `outcome` is text
    rather than a database enum precisely so a value this code has never
    seen is a row a reader can ignore instead of a write that fails inside
    a reconcile tick, and an endpoint that refused to render one would
    give that property back.
    """
    if intent.is_pending or intent.outcome is None:
        return PENDING
    return intent.outcome


def _channel_ids_json(stored: str | None) -> list[str]:
    """The stored list, as the strings it was sent as.

    Unparseable is rendered as empty rather than raising. Only a
    hand-written row can be unparseable -- this API round-trips every list
    through `parse_channel_ids` before storing it -- and a page that 500s
    is a worse answer than one that shows a failed request whose `error`
    already says the list could not be read.
    """
    if stored is None:
        return []
    try:
        return [str(channel_id) for channel_id in settings.parse_channel_ids(stored)]
    except settings.InvalidChannelList:
        return []


def _refusal(exception: type[web.HTTPException], reason: str) -> web.HTTPException:
    """A refusal with a JSON body, built rather than returned as a response.

    aiohttp deprecated returning an `HTTPException` from a handler, and
    raising lets the guards above read as a straight line instead of
    threading an optional response back through every caller.
    """
    return exception(text=json.dumps({"error": reason}), content_type="application/json")


def _caller(request: web.Request) -> int:
    """The Discord id of the person making this request.

    Only ever reached from behind `require_session`, which is what
    guarantees there is one -- `current_user` raises rather than returning
    `None` if that is ever untrue, so a route registered without the
    wrapper fails loudly instead of quietly acting on somebody else's
    server.
    """
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id
