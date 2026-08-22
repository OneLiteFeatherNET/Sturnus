"""Labelling a recording, and reading back the labels one person uses.

- `GET /api/tags`
- `PUT /api/sessions/{session_id}/tags`

**Why tags exist.** A console that lists every meeting somebody was in,
newest first, answers "what happened last Tuesday" and nothing else. The
question people actually arrive with is "where is the meeting where we
decided the thing", and neither a date nor a channel name answers it. A
tag is the one piece of information about a meeting that only a person
who was in it can supply.

**Whose tags these are.** One person's, always. `session_tag` is keyed by
its owner, every read names them, and there is no endpoint here that
returns a label somebody else wrote. That is a decision rather than a
consequence: a label is a remark about a conversation other people were
also in, and a shared tag list would publish those remarks to everybody
in the meeting -- a new disclosure between participants, needing rules
about deletion and about what may be written, that private labels simply
do not need. It is also the reversible direction. Private tags can be
made shared later by deciding to; tags people have already read cannot be
made private again.

**Why `PUT` of a whole set rather than `POST` and `DELETE` per tag.** A
tag editor is a set of chips somebody adds to and removes from, and the
question it asks the server is "these are my labels now". Expressed as
one idempotent write, a double-submitted form is harmless and a lost
response can simply be retried; expressed as a stream of adds and
deletes, the same two events leave a set that depends on which arrived
first.

Two rules from `app.py` hold here unchanged:

- **No user input is reflected into a response.** The refusals below are
  fixed strings, `sturnus.console.tags` included -- an `InvalidTag`
  carries a reason and never the tag that caused it. An endpoint that
  echoes what it was handed is an XSS sink for whatever renders its
  errors.
- **Every id is serialised as a string**, which here means the tag
  endpoints never put one in a body at all.
"""

from __future__ import annotations

from json import JSONDecodeError

from aiohttp import web

from sturnus.console.ports import TagWriter
from sturnus.console.statistics import tags_json
from sturnus.console.tags import InvalidTag, normalise_all

# Why every reference to `sturnus.console.app` below is imported inside a
# function rather than at the top of this module: `app` imports this one
# to register its routes, so a module-level import back into it would
# close a cycle that fails on whichever is loaded first. `_NOW` and
# `current_user` stay `app`'s to own.

#: Where `build_api` puts the write side. The read side is
#: `routes_read.READS`, because "which labels do I use" is a read scoped
#: to the signed-in person like every other read the console makes.
TAG_WRITER = web.AppKey("tag_writer", TagWriter)

_TAGS_PATH = "/api/tags"
_SESSION_TAGS_PATH = "/api/sessions/{session_id}/tags"

#: What a body must look like. Stated once, so that the three ways of
#: getting it wrong get the one answer that describes the right shape.
_MALFORMED_BODY = 'a JSON object of the form {"tags": ["...", "..."]} is required'


async def tags_view(request: web.Request) -> web.Response:
    """Every label the signed-in person uses, most used first."""
    from sturnus.console.app import current_user
    from sturnus.console.routes_read import READS

    viewer = current_user(request).discord_user_id
    uses = await request.app[READS].tags_of(viewer)
    return web.json_response(
        {"tags": tags_json(uses)},
        # How somebody labels their meetings is as much theirs as the
        # meetings are, and it changes the moment they edit one.
        headers={"Cache-Control": "private, no-store"},
    )


async def replace_tags(request: web.Request) -> web.Response:
    """Sets the signed-in person's labels on one recording to exactly this set.

    404 for a session that does not exist *and* for one this person was
    not in, decided inside the write rather than here -- see
    `ConsoleTagWriter`. 400 for a body that is not a set of storable
    labels, with a reason that says which rule was broken and never
    which tag broke it.
    """
    from sturnus.console.app import _NOW, current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    try:
        body = await request.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError):
        return _malformed(_MALFORMED_BODY)
    if not isinstance(body, dict) or not isinstance(body.get("tags"), list):
        return _malformed(_MALFORMED_BODY)

    try:
        wanted = normalise_all(body["tags"])
    except InvalidTag as refusal:
        # `str(refusal)` is one of the fixed sentences in
        # `sturnus.console.tags`. It is safe to pass on precisely because
        # that module never puts the offending tag into one.
        return _malformed(str(refusal))

    stored = await request.app[TAG_WRITER].replace(
        session_id, owner=viewer, tags=wanted, now=request.app[_NOW]()
    )
    if stored is None:
        return _no_such_session()
    # The stored set rather than the submitted one: normalisation may have
    # merged two of the chips somebody typed, and a client shown its own
    # input back would keep displaying a tag the database does not have.
    return web.json_response({"tags": list(stored)})


def _session_id(request: web.Request) -> int | None:
    try:
        return int(request.match_info["session_id"])
    except ValueError:
        # A path segment that is not a number names nothing, which is the
        # same answer as naming something that does not exist.
        return None


def _malformed(reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=400)


def _no_such_session() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such session" and "you were not in it" are deliberately
    indistinguishable; see the module docstring of `routes_audio`.
    """
    return web.json_response({"error": "no such session"}, status=404)


def register(app: web.Application) -> None:
    """Adds the tag routes to an application that already has its writer."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_TAGS_PATH, require_session(tags_view)),
            web.put(_SESSION_TAGS_PATH, require_session(replace_tags)),
        ]
    )
