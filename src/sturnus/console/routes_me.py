"""Who the person in front of the console is, and how they want it to look.

- `GET    /api/me`
- `GET    /api/me/preferences`
- `PUT    /api/me/preferences/{key}`
- `DELETE /api/me/preferences/{key}`

**No endpoint here takes a user id in the path, and none ever should.**
The session decides whose identity and whose preferences these are. A path
parameter would turn every one of these into an authorisation question --
somebody would have to decide, per route, whether one person may read
another's theme, and the answer would have to be re-decided the next time
a route was added. There is no reason to have that question. `/api/me` is
the whole scope, and the cookie is the whole rule.

**Why `/api/me` grows a name and not an avatar.** The name is already in
this database: `account_link.display_name` is the Outline display name,
written when the person linked their account, and until now the console
threw it away immediately after using the link to find a Discord id -- so
the profile menu could render a snowflake and a boolean and could not
greet anybody. An avatar would have to come from Discord. This process
holds no Discord token and must not be given one (Spec 13.2): it already
holds S3 and the master key, so it can decrypt every recording ever made.
Mirroring every linked person's avatar into this database to decorate a
menu is not a trade worth making for a picture, so the console renders
initials from the name instead. That is settled; it does not need
re-deriving.

`display_name` is present-and-null rather than absent when there is no
link row, so a client never has to tell "this person has no name" from
"this API predates the field" -- it would guess, and it would guess wrong
on one of them.

**Validation is the store's.** `PreferenceStore.set` refuses a key nobody
reads and a value outside `sturnus.domain.preferences.ALLOWED_VALUES`;
this module calls it and turns the `ValueError` into a 400. It does not
check first. A second copy of a validation rule is how the two drift, and
`is_allowed` is called once in this system -- on the write path, where the
read path can never be reached with a value it cannot use.

That is why an unknown key is **400 and not the settings endpoints' 404**.
There the key check is the API's own and has to run before the guild is
even looked at, so that the endpoint is not an oracle for which settings
exist. Here there is nothing to hide -- the keys are this person's own --
and both refusals come from the same `ValueError`, so restating the
registry in the handler to tell them apart would be exactly the second
copy this arrangement exists to avoid.

**No user input is reflected into a response.** `PreferenceStore`'s own
message embeds the value it refused, so the reasons below are fixed
strings -- the same rule the rest of `sturnus.console.app` follows.

**Nothing here is logged.** A person choosing a dark console is not an
administrative act on somebody else, so there is no audit line to write;
and the one interesting value in these responses is `display_name`, which
is in `sturnus.observability.fields.DENIED_NAMES` and must never reach a
log record at all.
"""

from __future__ import annotations

import json

from aiohttp import web

from sturnus.console.ports import AdminDirectory, PreferenceDirectory, ProfileDirectory

# Every reference to `sturnus.console.app` below is imported inside a
# function rather than at module scope: `app` imports this module the
# ordinary way, so a module-level import back into it would close a cycle
# while `app` is still defining the very names wanted here. The same trade
# `routes_settings` and `routes_consent` make, for the same reason.

#: Where the collaborators are found. Their own keys rather than
#: parameters to `register`, so `build_api` stays a two-line edit --
#: several agents are adding sections to that function and each extra
#: line is a merge by hand.
PROFILE_DIRECTORY: web.AppKey[ProfileDirectory] = web.AppKey("profile_directory")
PREFERENCES: web.AppKey[PreferenceDirectory] = web.AppKey("preferences")

_ME_PATH = "/api/me"
_PREFERENCES_PATH = "/api/me/preferences"
_PREFERENCE_PATH = "/api/me/preferences/{key}"

#: Refusal reasons. Fixed strings, chosen so the console can key off them
#: without ever needing the offending input echoed back.
_MALFORMED_BODY = "malformed request body"
_VALUE_MUST_BE_A_STRING = "value must be a string"
_VALUE_REFUSED = "no such preference, or not a value it accepts"

#: Everything here is about one identifiable person and goes stale the
#: moment they change it. Nothing in between this and the browser has any
#: business keeping a copy.
_PRIVATE = {"Cache-Control": "private, no-store"}


def register(app: web.Application) -> None:
    """Adds the personal routes to an application that already has a session."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_ME_PATH, require_session(me)),
            web.get(_PREFERENCES_PATH, require_session(read_preferences)),
            web.put(_PREFERENCE_PATH, require_session(write_preference)),
            web.delete(_PREFERENCE_PATH, require_session(clear_preference)),
        ]
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def me(request: web.Request) -> web.Response:
    """Who the caller is, and whether the settings section applies to them.

    `is_admin` is a rendering hint and never a control. The administrative
    endpoints each decide for themselves -- a hidden section is a courtesy
    to the person looking at the page, not a boundary, and it is one
    `curl` away from being ignored.
    """
    caller = _caller(request)
    return web.json_response(
        {
            "discord_user_id": str(caller),
            "display_name": await request.app[PROFILE_DIRECTORY].display_name_for(caller),
            "is_admin": await _admins(request).is_admin_anywhere(caller),
        },
        headers=_PRIVATE,
    )


async def read_preferences(request: web.Request) -> web.Response:
    """Every preference in force for the caller, defaults included.

    The effective values rather than the stored ones, so the console never
    has to know `DEFAULTS`: a client falling back itself would be a second
    copy of that table, in a language that cannot import the first one and
    with nothing to make the two agree.
    """
    return await _effective(request)


async def write_preference(request: web.Request) -> web.Response:
    """Sets one preference for the caller."""
    key = request.match_info["key"]
    await _store(request, key, await _requested_value(request))
    return await _effective(request)


async def clear_preference(request: web.Request) -> web.Response:
    """Restores one preference to its default for the caller.

    A removal, not a stored default: an absent row means "never
    expressed", which is what lets a later change to `DEFAULTS` reach
    everybody who never disagreed with it.

    Clearing something nobody set is a success. It is what a reset button
    does on a fresh account, and answering an error for it would make the
    console show a failure for an outcome that is exactly what was asked
    for.
    """
    await _store(request, request.match_info["key"], None)
    return await _effective(request)


# ---------------------------------------------------------------------------
# Reading the request, writing the answer
# ---------------------------------------------------------------------------


async def _requested_value(request: web.Request) -> str:
    """The `value` from the request body, as a string and only as a string.

    No coercion: `{"value": 3}` is refused rather than turned into `"3"`,
    because coercing here would be this module quietly holding an opinion
    about what a legal value looks like -- which is
    `sturnus.domain.preferences`' job. `{"value": null}` is refused for a
    different reason: restoring a default is what `DELETE` is for.
    """
    try:
        body = await request.json()
    except ValueError:
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY) from None
    if not isinstance(body, dict):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY)
    value = body.get("value")
    if not isinstance(value, str):
        raise _refusal(web.HTTPBadRequest, _VALUE_MUST_BE_A_STRING)
    return value


async def _store(request: web.Request, key: str, value: str | None) -> None:
    """Delegates the write, and turns the store's refusal into a 400.

    The `except` is the whole point of this function: `PreferenceStore.set`
    is the only thing in the system that decides whether a preference may
    be stored, and every rule it enforces is enforced here for free by not
    being restated.

    The exception itself is dropped rather than logged. Its message embeds
    the value it refused, which is unbounded text somebody typed, and
    there is nothing an operator would do with the news that a browser
    sent a theme this build does not have.
    """
    from sturnus.console.app import _NOW

    try:
        await request.app[PREFERENCES].set(_caller(request), key, value, request.app[_NOW]())
    except ValueError:
        raise _refusal(web.HTTPBadRequest, _VALUE_REFUSED) from None


async def _effective(request: web.Request) -> web.Response:
    """The caller's preferences as they now stand.

    Every answer is a re-read rather than an assumption, so a write
    reports what the store actually holds and a clear reports the default
    that actually came back -- not the one this process believes in.
    """
    stored = await request.app[PREFERENCES].snapshot(_caller(request))
    return web.json_response({"preferences": stored}, headers=_PRIVATE)


def _refusal(exception: type[web.HTTPException], reason: str) -> web.HTTPException:
    """A refusal with a JSON body, built rather than returned as a response.

    aiohttp deprecated returning an `HTTPException` from a handler, and
    raising lets the guards above read as a straight line instead of
    threading an optional response back through every caller.
    """
    return exception(text=json.dumps({"error": reason}), content_type="application/json")


def _admins(request: web.Request) -> AdminDirectory:
    """The mirrored administrator membership this application was built with."""
    from sturnus.console.app import _ADMINS

    return request.app[_ADMINS]


def _caller(request: web.Request) -> int:
    """The Discord id of the person making this request.

    Only ever reached from behind `require_session`, which is what
    guarantees there is one -- `current_user` raises rather than returning
    `None` if that is ever untrue, so a route registered without the
    wrapper fails loudly instead of quietly acting for somebody else.

    This is also the whole authorisation model of this module: it is the
    only place a Discord id enters, and it comes out of a signed cookie.
    """
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id
