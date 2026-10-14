"""Where a guild publishes: six routes, one authorisation rule, and no secret out.

- `GET    /api/export-formats`
- `GET    /api/guilds/{guild_id}/export-targets`
- `POST   /api/guilds/{guild_id}/export-targets`
- `PUT    /api/guilds/{guild_id}/export-targets/{target_id}`
- `DELETE /api/guilds/{guild_id}/export-targets/{target_id}`
- `PUT    /api/guilds/{guild_id}/export-targets/{target_id}/secret`

**The credential never comes back, and the shape of this module is what
makes that true rather than a promise.** `ExportTarget` -- the read model
every response here is built from -- has nowhere to put a secret; it carries
`has_secret` and nothing else. `ExportTargetStore.secret_for` exists and is
not on `sturnus.console.ports.ExportTargets`, so nothing reachable from a
handler in this file can call it. Not masked, not truncated, not "the last
four characters": a masked-but-recoverable value is a value, and this is
precisely why export destinations are not `guild_config` keys -- that API
renders every value it holds straight back to whichever administrator asks.

The secret is therefore write-only, on a route of its own. That is not
tidiness: the edit form cannot render the token, so it cannot re-submit it
either, and a `PUT` on the target that also wrote the credential would clear
it every time somebody renamed a destination.

**404, not 403, for everything.** Unlike `sturnus.console.routes_settings`,
which answers 403 to somebody who does not administer a guild, every refusal
here is the same 404 with the same body -- the reasoning
`sturnus.console.routes_audio` gives. A 403 would confirm that a guild
exists and that it has a destination with a given id, to somebody the
system has just decided has no business knowing either. "You do not
administer this guild", "there is no such guild" and "there is no such
destination" are deliberately indistinguishable.

**Which formats may be configured is the registry's answer, not this
module's.** `sturnus.application.export_formats.supported_formats` is the
one list, so a format added there becomes configurable here with no change
in this file -- and one that is specified but not built (`pdf`,
`confluence`) is refused where an administrator can read the refusal rather
than accepted and silently skipped after every meeting.

**`GET /api/export-formats` is that same answer said before the refusal
instead of after it.** It is the one route here that is not about a guild,
and it is here rather than in a module of its own because it reads the
registry these five routes enforce: a caller cannot be told what
`_requested_target` will accept by any list except the one it accepts from.
It carries the unbuilt names too, marked unavailable -- see
`export_formats.catalogue`. A reader that was told only the buildable
three would have to invent the difference between *not offered* and *not
built*, and inventing it means hard-coding a list this deployment has no
way to correct.

**No audit line, and nothing per-caller in the answer.** Every other route
in this file either writes or reads a guild's own configuration; this one
reads a compiled-in constant that is identical for every administrator of
every guild on this deployment, so there is nothing an access record could
establish and no guild id to scope one to.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from sturnus.application.export_formats import (
    CatalogueEntry,
    catalogue,
    format_named,
    supported_formats,
)
from sturnus.console.ports import ExportTargets
from sturnus.domain.exports import ExportTarget
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Where `build_api` puts the store. Its own key rather than a parameter to
#: `register`, matching `routes_settings.SETTINGS_STORE`.
EXPORT_TARGETS: web.AppKey[ExportTargets] = web.AppKey("export_targets")

#: The one route here that names no guild. Every format on it is the same
#: for every guild on this deployment, which is why it is not under one.
_FORMATS_PATH = "/api/export-formats"

_PATH = "/api/guilds/{guild_id}/export-targets"
_TARGET_PATH = "/api/guilds/{guild_id}/export-targets/{target_id}"
_SECRET_PATH = "/api/guilds/{guild_id}/export-targets/{target_id}/secret"

#: The one refusal these routes have, for every reason they can refuse.
#: See the module docstring.
_NOT_FOUND = "no such export target"
_MALFORMED_BODY = "malformed request body"
_UNKNOWN_FORMAT = "this deployment cannot publish that format"
_BAD_NAME = "name must be a non-empty string"
_BAD_TARGET = "target is not something this format can address"
_BAD_CONFIG = "config must be an object"
_BAD_ENABLED = "enabled must be true or false"
_BAD_SECRET = "secret must be a string, or null to clear it"
_DUPLICATE_NAME = "a destination of this guild already has that name"

#: What a write did, for the audit line. Bounded literals, and never the
#: value: a destination's `target` is an address in somebody else's system
#: and its `name` is free text an administrator typed.
_CREATED = "created"
_UPDATED = "updated"
_DELETED = "deleted"
_SECRET_SET = "secret_set"
_SECRET_CLEARED = "secret_cleared"


def register(app: web.Application) -> None:
    """Adds the export-target routes, each behind a session.

    `require_session` applied here rather than as a decorator, for the
    reason `sturnus.console.routes_settings.register` gives: a route added
    without it is visibly public rather than silently public.
    """
    from sturnus.console.app import require_session

    app.add_routes(
        [
            # Behind a session like everything else, though it holds no
            # secret and names no guild. `routes_setup.register` makes the
            # argument for the invite link and it is the same one: public
            # is not the same as unauthenticated, and an endpoint of this
            # API that answered without a session would be the only one.
            web.get(_FORMATS_PATH, require_session(list_formats)),
            web.get(_PATH, require_session(list_targets)),
            web.post(_PATH, require_session(create_target)),
            web.put(_TARGET_PATH, require_session(update_target)),
            web.delete(_TARGET_PATH, require_session(delete_target)),
            # A path of its own, because writing the credential is a
            # different act under a different rule from writing the
            # destination -- and because a `PUT` on the target that also
            # took the secret would clear it on every rename.
            web.put(_SECRET_PATH, require_session(write_secret)),
        ]
    )


def format_json(entry: CatalogueEntry) -> dict[str, Any]:
    """One format, as a caller deciding what to configure may be told it.

    Three fields, and the case for stopping at three is that a fourth
    would have to be invented here. `media_type` and `file_extension` are
    on `ExportFormat` and are read by the sink that stores the bytes and
    by the route that serves them back -- neither of them a caller. A
    label is a word in a language this process has no catalogue for. And
    `target_pattern` is a Python regular expression: handing one to a
    caller to compile is handing over a dialect, not a rule, and the rule
    is enforced here whatever the caller believes.

    `sink` is `null` for an unbuilt format rather than absent, so that "no
    sink has been decided for this" and "this response forgot a field" are
    different things on the wire.
    """
    return {"name": entry.name, "available": entry.available, "sink": entry.sink}


def target_json(target: ExportTarget) -> dict[str, Any]:
    """One destination, as the console sees it.

    Built from the read model, which is what guarantees the credential is
    not in it: there is no field here that was dropped, because there was
    never a field to drop. `has_secret` is the whole of what a caller
    learns about it.

    `guild_id` is a string and `id` is a number, and the difference is not
    an inconsistency: a Discord snowflake exceeds JavaScript's safe integer
    range, where a JSON number silently loses its last digits, while this
    row's own primary key is a `SERIAL` that will not reach 2^53.
    """
    return {
        "id": target.id,
        "guild_id": str(target.guild_id),
        "format": target.format,
        "name": target.name,
        "target": target.target,
        "config": dict(target.config),
        "has_secret": target.has_secret,
        "enabled": target.enabled,
        "created_at": target.created_at.isoformat(),
        "updated_at": target.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def list_formats(_request: web.Request) -> web.Response:
    """Every format this deployment knows of, and which of them it can run.

    The unbuilt ones are in the answer, marked `available: false`. Leaving
    them out would make this endpoint say exactly what a 400's `supported`
    list already said, and the reason that list was not enough is that a
    reader who has to distinguish "not offered here" from "not built here"
    can only do it by keeping a list of its own -- which is a second
    registry, and a second registry is one that goes stale the day `pdf`
    is built and nothing fails.

    A list rather than a mapping, because the order is part of the answer:
    `outline` is what every guild published to before the others existed,
    and JSON object order is a thing implementations are entitled to lose.
    """
    return web.json_response({"formats": [format_json(entry) for entry in catalogue()]})


async def list_targets(request: web.Request) -> web.Response:
    """Every destination of this guild, enabled or not.

    Disabled ones included, because switching a destination off is not the
    same as forgetting how it was configured -- and a settings page that
    hid them would leave an administrator no way to switch one back on.
    """
    guild_id = await _authorise(request)
    targets = await request.app[EXPORT_TARGETS].all_for(guild_id)
    return web.json_response(
        {"guild_id": str(guild_id), "targets": [target_json(t) for t in targets]}
    )


async def create_target(request: web.Request) -> web.Response:
    """Adds a destination, and refuses a name this guild already uses.

    The store would upsert on `(guild_id, name)` -- which is right for a
    `PUT` and wrong for a `POST`: a create that silently replaced an
    existing destination would let a typo redirect a guild's protocols
    with nothing said. The name check is this module's, deliberately, and
    it is the only rule here the store does not already enforce.
    """
    guild_id = await _authorise(request)
    body = await _body(request)
    fields = _requested_target(body)
    existing = await request.app[EXPORT_TARGETS].all_for(guild_id)
    if any(t.name == fields["name"] for t in existing):
        raise _refusal(web.HTTPConflict, _DUPLICATE_NAME)
    return await _write(request, guild_id, fields, outcome=_CREATED)


async def update_target(request: web.Request) -> web.Response:
    """Replaces a destination's configuration, keeping its name.

    The name is read from the stored row rather than from the body, so
    this addresses the same row the URL names whatever the client sends.
    Renaming is a create plus a delete, and it is that on purpose: a name
    is how an administrator refers to a destination, and changing one
    silently under an id is how "publish to Wiki" stops meaning what the
    person who set it up thought it meant.

    The credential is untouched. See the module docstring.
    """
    guild_id = await _authorise(request)
    target_id = _target_id(request)
    stored = await request.app[EXPORT_TARGETS].get(guild_id, target_id)
    if stored is None:
        raise _refusal(web.HTTPNotFound, _NOT_FOUND)
    fields = _requested_target(await _body(request))
    fields["name"] = stored.name
    return await _write(request, guild_id, fields, outcome=_UPDATED)


async def delete_target(request: web.Request) -> web.Response:
    """Removes a destination. What it published survives.

    `session_document.target_id` is `ON DELETE SET NULL`: the documents
    still exist in the other system, and the links to them are what
    somebody follows when they go looking for last quarter's minutes.
    """
    guild_id = await _authorise(request)
    target_id = _target_id(request)
    if not await request.app[EXPORT_TARGETS].delete(guild_id, target_id):
        raise _refusal(web.HTTPNotFound, _NOT_FOUND)
    _audit(request, guild_id, target_id, _DELETED)
    return web.Response(status=204)


async def write_secret(request: web.Request) -> web.Response:
    """Stores this destination's credential, or clears it with `null`.

    Answers with the destination, which now says `has_secret: true` -- and
    that is the only thing about the credential any response ever says.
    """
    from sturnus.console.app import _NOW

    guild_id = await _authorise(request)
    target_id = _target_id(request)
    body = await _body(request)
    if "secret" not in body:
        raise _refusal(web.HTTPBadRequest, _BAD_SECRET)
    secret = body["secret"]
    if secret is not None and (not isinstance(secret, str) or not secret):
        raise _refusal(web.HTTPBadRequest, _BAD_SECRET)
    store = request.app[EXPORT_TARGETS]
    if not await store.set_secret(guild_id, target_id, secret, request.app[_NOW]()):
        raise _refusal(web.HTTPNotFound, _NOT_FOUND)
    _audit(
        request,
        guild_id,
        target_id,
        _SECRET_CLEARED if secret is None else _SECRET_SET,
    )
    return await _one(request, guild_id, target_id)


# ---------------------------------------------------------------------------
# The guards, in the order they must run
# ---------------------------------------------------------------------------


async def _authorise(request: web.Request) -> int:
    """The guild in the path, once the caller is known to administer it.

    Runs before anything reads a target id or a body, deliberately -- the
    same order `routes_settings._authorise` establishes and for the same
    reason: answering "no such destination" first would turn these
    endpoints into an oracle for which destinations exist, readable by
    anybody holding a session.
    """
    from sturnus.console.app import _ADMINS, current_user

    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        raise _refusal(web.HTTPNotFound, _NOT_FOUND) from None
    caller = current_user(request).discord_user_id
    if not await request.app[_ADMINS].is_admin(guild_id, caller):
        # INFO and not WARNING: somebody following a stale link to a guild
        # they left is the ordinary cause, and the response tells them
        # nothing either way.
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_EXPORT_TARGET_REFUSED,
            "Refused a guild's export configuration to somebody who does not administer it",
            guild_id=guild_id,
            discord_user_id=caller,
            reason="not_an_administrator",
        )
        raise _refusal(web.HTTPNotFound, _NOT_FOUND)
    return guild_id


def _target_id(request: web.Request) -> int:
    """The destination id from the path, or the same 404 as everything else.

    A path segment that is not a number names nothing, and saying so is
    the same answer as naming something that does not exist.
    """
    try:
        return int(request.match_info["target_id"])
    except ValueError:
        raise _refusal(web.HTTPNotFound, _NOT_FOUND) from None


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY) from None
    if not isinstance(body, dict):
        raise _refusal(web.HTTPBadRequest, _MALFORMED_BODY)
    return body


def _requested_target(body: dict[str, Any]) -> dict[str, Any]:
    """The destination a request describes, or the refusal it earns.

    Every check here is about a value this API alone can decide: the store
    holds no opinion on which formats exist, and the format is what decides
    whether a `target` is addressable at all. Nothing from the body reaches
    a response -- the reasons are fixed strings, the same rule the rest of
    the console follows.
    """
    format_name = body.get("format")
    entry = format_named(format_name) if isinstance(format_name, str) else None
    if entry is None:
        # The one refusal that carries something beyond a reason, and it is
        # this module's own registry rather than anything from the request:
        # an administrator who typed `pdf` is told what they may type
        # instead rather than left to guess.
        raise _refusal(web.HTTPBadRequest, _UNKNOWN_FORMAT, supported=list(supported_formats()))

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _refusal(web.HTTPBadRequest, _BAD_NAME)

    target = body.get("target")
    if not isinstance(target, str) or not entry.accepts_target(target):
        # One reason for "not a string", "empty" and "not addressable by
        # this format". The distinction is not one a caller needs and the
        # third depends on the format, which the message must not restate.
        raise _refusal(web.HTTPBadRequest, _BAD_TARGET)

    config = body.get("config", {})
    if not isinstance(config, dict):
        raise _refusal(web.HTTPBadRequest, _BAD_CONFIG)

    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _refusal(web.HTTPBadRequest, _BAD_ENABLED)

    return {
        "format": entry.name,
        "name": name.strip(),
        "target": target,
        "config": config,
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Writing, and saying what it achieved
# ---------------------------------------------------------------------------


async def _write(
    request: web.Request, guild_id: int, fields: dict[str, Any], *, outcome: str
) -> web.Response:
    from sturnus.console.app import _NOW

    target_id = await request.app[EXPORT_TARGETS].save(
        guild_id,
        format=fields["format"],
        name=fields["name"],
        target=fields["target"],
        config=fields["config"],
        enabled=fields["enabled"],
        now=request.app[_NOW](),
    )
    _audit(request, guild_id, target_id, outcome)
    return await _one(request, guild_id, target_id, status=201 if outcome == _CREATED else 200)


async def _one(
    request: web.Request, guild_id: int, target_id: int, status: int = 200
) -> web.Response:
    """Answers a write by reading back what is now stored.

    Re-read rather than assumed, so the response carries the store's
    `created_at`/`updated_at` and its `has_secret` rather than this
    handler's belief about them.
    """
    stored = await request.app[EXPORT_TARGETS].get(guild_id, target_id)
    if stored is None:
        raise _refusal(web.HTTPNotFound, _NOT_FOUND)
    return web.json_response(target_json(stored), status=status)


def _audit(request: web.Request, guild_id: int, target_id: int, outcome: str) -> None:
    """The only record that somebody changed where a guild publishes.

    Names who, which guild, which destination and what kind of change --
    and never the destination's name, its address or its format. A
    `target` is an address in somebody else's system and a `name` is free
    text; `target_id` is what an operator joins back to the row for the
    rest, with the access to do so.
    """
    from sturnus.console.app import current_user

    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_EXPORT_TARGET_WRITTEN,
        "The console changed a guild's export configuration",
        guild_id=guild_id,
        target_id=target_id,
        discord_user_id=current_user(request).discord_user_id,
        outcome=outcome,
    )


def _refusal(exception: type[web.HTTPException], reason: str, **extra: Any) -> web.HTTPException:
    """A refusal with a JSON body, built rather than returned as a response.

    aiohttp deprecated returning an `HTTPException` from a handler, and
    raising lets the guards above read as a straight line instead of
    threading an optional response back through every caller.
    """
    return exception(text=json.dumps({"error": reason, **extra}), content_type="application/json")
