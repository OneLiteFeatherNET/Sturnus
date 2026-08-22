"""The settings endpoints: four routes, and one authorisation rule stated four times.

**The rule.** `is_admin_anywhere` decides whether the console *offers* the
settings section; `is_admin(guild_id, ...)` decides whether this guild's
settings may be read or written. Those are different questions and the
narrow one is the only one that authorises anything -- an administrator of
one guild is nobody in another. Every handler below that names a guild
calls `_authorise` before it touches a store, and the console hiding the
section from a non-administrator is a courtesy to the person looking at
the page, never a control: a hidden section is one `curl` away.

**Validation is the store's.** `ConfigStore.set` already refuses a
non-positive-integer for an `INTEGER_KEYS` key. This module calls it and
turns the `ValueError` into a 400; it does not check first. A second,
independently-maintained copy of a validation rule is how the two drift,
and the copy nobody exercises is the one that goes stale. The one check
that *is* here is membership of the key registry, because "there is no
such setting" is a 404 about a URL rather than a 400 about a value -- and
because without it `guild_config` is a table anybody with a session and
one guild could write arbitrary rows into.

**No user input is reflected into a response.** The reasons below are
fixed strings, the same rule the rest of `sturnus.console.app` follows.
That matters more here than it looks: `ConfigStore`'s own `ValueError`
message embeds the value it refused, so passing it through would be an
echo endpoint for anything an administrator can type.

**A write is stored, not necessarily in force.** `/config set` writes and
then reconciles, and reports which of those actually happened, because
"`key` set to `value`" while the running process keeps using the old one
is a lie one layer up from the defect. This process holds no Discord token
and cannot reconcile anything (Spec 13.2), so it cannot do the second half
at all. What it can do is say what still has to happen: every response
carries `takes_effect` and `deferred_while_recording` from
`sturnus.console.settings_view`, and the console renders them.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from sturnus.console import settings_view
from sturnus.console.ports import AdminDirectory, SettingsStore
from sturnus.observability.events import Event, log_event, log_exception

# Why every reference to `sturnus.console.app` below is imported inside a
# function rather than at the top of this one: `app` imports this module
# the ordinary way, so a module-level import back into it would close a
# cycle -- and close it at the worst possible moment, while `app` is still
# defining the very names wanted here. Deferring to call time breaks the
# cycle without inverting the dependency; every deferred import is reached
# from a request or from `build_api`, both long after `app` is loaded.
# The private `_ADMINS` and `_NOW` are `app`'s to own: three route modules
# are being added to that application in parallel, and renaming a constant
# three of them read is a merge conflict bought for a leading underscore.

log = logging.getLogger(__name__)

#: The guild configuration store. Its own key rather than a parameter to
#: `register`, so `build_api` stays a one-line edit -- three agents are
#: adding sections to that function and each extra line is a merge by hand.
SETTINGS_STORE: web.AppKey[SettingsStore] = web.AppKey("settings_store")

#: Refusal reasons. Fixed strings, chosen so the console can key off them
#: without ever needing the offending input echoed back.
_NOT_AN_ADMINISTRATOR = "not an administrator of this guild"
_NO_SUCH_GUILD = "no such guild"
_NO_SUCH_KEY = "no such setting"
_MALFORMED_BODY = "malformed request body"
_VALUE_MUST_BE_A_STRING = "value must be a string"
_VALUE_REFUSED = "the value is not valid for this setting"
_REQUIRED_KEY = "this setting is required and cannot be cleared"

#: What a write did, for the audit line. Two bounded literals rather than
#: the value itself: an operator needs to know that somebody changed
#: `policy_version` on this guild, not what they changed it to.
_SET = "set"
_CLEARED = "cleared"


def register(app: web.Application) -> None:
    """Adds the settings routes to an application that already has a session.

    `require_session` is applied here rather than as a decorator on each
    handler, and it is applied to all four without exception. The
    argument `sturnus.console.app` makes for a decorator over a
    middleware holds either way: the authentication decision is visible
    at the routes it protects, so a route added without it is a route
    that is visibly public rather than silently public.
    """
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get("/api/guilds", require_session(guilds)),
            web.get("/api/guilds/{guild_id}/settings", require_session(read_settings)),
            web.put("/api/guilds/{guild_id}/settings/{key}", require_session(write_setting)),
            web.delete("/api/guilds/{guild_id}/settings/{key}", require_session(clear_setting)),
        ]
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def guilds(request: web.Request) -> web.Response:
    """The guilds the signed-in person administers.

    An empty list rather than a refusal for somebody who administers
    nothing: that is the ordinary state of a participant who signed in to
    look at their own recordings, and answering 403 would make the console
    show an error for it.
    """
    administered = await _admins(request).administered_guilds(_caller(request))
    # Every Discord id as a string: a snowflake exceeds JavaScript's safe
    # integer range, where a JSON number silently loses its last digits
    # and produces an id that looks right and names nothing.
    return web.json_response({"guilds": [{"guild_id": str(guild_id)} for guild_id in administered]})


async def read_settings(request: web.Request) -> web.Response:
    """Every key, with what this guild has it set to and what it defaults to.

    One `snapshot` query rather than one read per key, and driven by the
    registry rather than by the rows that came back -- so a row written
    into `guild_config` by hand under a name nothing reads stays invisible
    instead of appearing as a field the API would refuse to save.
    """
    guild_id = await _authorise(request)
    stored = await request.app[SETTINGS_STORE].snapshot(guild_id)
    return web.json_response(
        {
            "guild_id": str(guild_id),
            "settings": [view.as_json() for view in settings_view.describe_all(stored)],
        }
    )


async def write_setting(request: web.Request) -> web.Response:
    """Sets one value, letting the store decide whether it is a value at all."""
    guild_id = await _authorise(request)
    key = _known_key(request)
    value = await _requested_value(request)
    await _write(request, guild_id, key, value, outcome=_SET)
    return await _one_setting(request, guild_id, key)


async def clear_setting(request: web.Request) -> web.Response:
    """Clears one value so its default comes back.

    Refused for a required key, which has no default to come back to:
    clearing one does not restore anything, it takes the guild out of
    service until somebody sets it again. `/config clear` on this branch
    will do it; a web form will not, and that difference is deliberate.
    A slash command is typed by an administrator reading the reply, a
    button sits next to every field on a page.
    """
    guild_id = await _authorise(request)
    key = _known_key(request)
    if not settings_view.may_clear(key):
        raise _refusal(web.HTTPConflict, _REQUIRED_KEY)
    await _write(request, guild_id, key, None, outcome=_CLEARED)
    return await _one_setting(request, guild_id, key)


# ---------------------------------------------------------------------------
# The guards, in the order they must run
# ---------------------------------------------------------------------------


async def _authorise(request: web.Request) -> int:
    """The guild in the path, once the caller is known to administer it.

    Runs before anything reads the key out of the path, deliberately.
    Answering "no such setting" first would turn these endpoints into an
    oracle for which keys exist, readable by anybody holding a session.
    """
    guild_id = _guild_id(request)
    if not await _admins(request).is_admin(guild_id, _caller(request)):
        raise _refusal(web.HTTPForbidden, _NOT_AN_ADMINISTRATOR)
    return guild_id


def _guild_id(request: web.Request) -> int:
    """The guild id from the path, or 404.

    404 rather than 400 because a guild id that is not a number names no
    guild, and "this URL addresses nothing" is what the browser should be
    told. Nothing here checks that the guild *exists* -- `_authorise`
    does that in the only sense that matters, since a guild the bot does
    not serve has no administrators.
    """
    try:
        return int(request.match_info["guild_id"])
    except ValueError:
        raise _refusal(web.HTTPNotFound, _NO_SUCH_GUILD) from None


def _known_key(request: web.Request) -> str:
    """The setting from the path, if anything in the system reads it."""
    key = request.match_info["key"]
    if not settings_view.is_known(key):
        raise _refusal(web.HTTPNotFound, _NO_SUCH_KEY)
    return key


async def _requested_value(request: web.Request) -> str:
    """The `value` from the request body, as a string and only as a string.

    No coercion: `{"value": 45}` is refused rather than turned into
    `"45"`, because coercing here would be this module quietly holding an
    opinion about what a valid value looks like -- which is the store's
    job. `{"value": null}` is refused for a different reason: clearing a
    setting is what `DELETE` is for, and it has a rule of its own.
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


# ---------------------------------------------------------------------------
# Writing, and saying what it achieved
# ---------------------------------------------------------------------------


async def _write(
    request: web.Request, guild_id: int, key: str, value: str | None, *, outcome: str
) -> None:
    """Delegates the write, and turns the store's refusal into a 400.

    The `except` is the whole point of this function: `ConfigStore.set` is
    the only thing in the system that decides whether a value is
    acceptable, and every rule it enforces is enforced here for free by
    not being restated.
    """
    from sturnus.console.app import _NOW

    discord_user_id = _caller(request)
    try:
        await request.app[SETTINGS_STORE].set(guild_id, key, value, request.app[_NOW]())
    except ValueError as exc:
        # `log_exception` rather than `log.warning("...%s", exc)`: the
        # store's message embeds the value it refused, and a message
        # composed from runtime data is exactly what
        # `sturnus.infrastructure.observability` cannot scrub. The type
        # and the key travel; the value does not.
        log_exception(
            log,
            logging.INFO,
            Event.CONSOLE_SETTING_REJECTED,
            "The console refused a settings write",
            exc,
            guild_id=guild_id,
            config_key=key,
            discord_user_id=discord_user_id,
        )
        raise _refusal(web.HTTPBadRequest, _VALUE_REFUSED) from None
    # The only record that this happened. A slash command at least leaves
    # the administrator holding the reply; a console write leaves nothing
    # behind but this line, so it names who, which guild and which key --
    # and never the value, which for `transcription_prompt` is free text
    # somebody typed.
    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_SETTING_WRITTEN,
        "The console wrote a guild setting",
        guild_id=guild_id,
        config_key=key,
        discord_user_id=discord_user_id,
        outcome=outcome,
    )


async def _one_setting(request: web.Request, guild_id: int, key: str) -> web.Response:
    """Answers a write by reading back what is now stored.

    Re-read rather than assumed, so a `DELETE` reports the default that
    actually came back rather than the one this process believes in.
    """
    stored = await request.app[SETTINGS_STORE].snapshot(guild_id)
    return web.json_response(
        {"guild_id": str(guild_id), "setting": settings_view.describe(key, stored).as_json()}
    )


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
    wrapper fails loudly instead of quietly serving somebody else's guild.
    """
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id
