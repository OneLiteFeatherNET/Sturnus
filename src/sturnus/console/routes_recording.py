"""What one recording is: its transcript, and what it is called.

- `GET /api/sessions/{session_id}/transcript`
- `GET /api/sessions/{session_id}/name`
- `PUT /api/sessions/{session_id}/name`

Three endpoints under one session, all under the same rule: **whoever may
read the session may read and write these.** Not a second copy of that
rule -- literally the same call. Every handler here begins by asking
`SessionReads.session_for` for the session, which is the scoped statement
`/api/sessions/{id}` itself is served from, and answers 404 when it comes
back `None`.

**Why the transcript is not a wider disclosure.** It is already inside
the protocol document the session produced, which is posted to the
channel and linked from the session's own row. Serving it here shows the
same words to the same people through a different door; what it does not
do is make them searchable, which is a separate act with separate
consequences and is refused (see `sturnus.console.filters`).

**A session whose audio is gone still has its transcript**, and that is
the point of the retention window being about the recording rather than
the minutes. So the transcript answers 200 for a session whose audio tab
answers 404, and carries `audio_available: false` so the console can say
which of the two happened rather than rendering an empty tab that looks
broken.

**Why one `name` endpoint for two fields.** A title and a description are
written by one form and are one fact about a meeting -- what it was
called and what it was. Two endpoints would let a form save half of
itself, and there is no interface in which somebody wants to write only
one of them and leave the other at whatever it happened to be.

Two rules from `app.py` hold here unchanged:

- **No user input is reflected into a response.** The refusals below are
  fixed strings, `sturnus.console.naming` included -- an `InvalidName`
  carries a reason and never the text that caused it.
- **Every id is serialised as a string**, which `sturnus.console.
  statistics` does once, for every shape here.
"""

from __future__ import annotations

from json import JSONDecodeError

from aiohttp import web

from sturnus.console.naming import InvalidName, normalise_description, normalise_title
from sturnus.console.ports import SessionNaming, TranscriptReader
from sturnus.console.statistics import (
    SessionName,
    session_name_json,
    transcript_json,
)

# Why every reference to `sturnus.console.app` below is imported inside a
# function rather than at the top of this module: `app` imports this one
# to register its routes, so a module-level import back into it would
# close a cycle that fails on whichever is loaded first. The same
# arrangement `routes_tags` uses, for the same reason.

#: Where `build_api` puts the two collaborators this module needs beyond
#: the reads adapter, which is `routes_read.READS` because "may I see
#: this session" is the same question every other read asks.
TRANSCRIPTS = web.AppKey("transcripts", TranscriptReader)
SESSION_NAMING = web.AppKey("session_naming", SessionNaming)

_TRANSCRIPT_PATH = "/api/sessions/{session_id}/transcript"
_NAME_PATH = "/api/sessions/{session_id}/name"

#: What a rename body must look like. Stated once, so that the several
#: ways of getting it wrong get the one answer that describes the right
#: shape. Both members are optional and null clears the field, which is
#: how a meeting is un-named.
_MALFORMED_BODY = (
    'a JSON object of the form {"title": "...", "description": "..."} is required, '
    "with either member null to clear it"
)


async def transcript_view(request: web.Request) -> web.Response:
    """One session's assembled transcript, or a 404 for one not theirs.

    The authorisation is the first line and is the session read itself:
    `session_for` is scoped by `session_participant` inside the
    statement, so a session this person was not in is indistinguishable
    from one that does not exist -- which is what both answers must look
    like from outside.

    Only after that succeeds is the transcript asked for. `TranscriptReader`
    carries no `requested_by` precisely because it is unreachable without
    this line having already run; see its docstring.
    """
    from sturnus.console.app import current_user
    from sturnus.console.routes_read import READS

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()
    if await request.app[READS].session_for(viewer, session_id) is None:
        return _no_such_session()

    found = await request.app[TRANSCRIPTS].transcript_of(session_id)
    if found is None:
        # Reachable only if the session was deleted between the two
        # reads. The same 404, because the answer is still "there is no
        # such session".
        return _no_such_session()
    return web.json_response(
        transcript_json(found),
        # The transcript is the protected content itself. A shared cache
        # holding it is a copy of a meeting outside every rule this
        # system applies to one.
        headers={"Cache-Control": "private, no-store"},
    )


async def name_view(request: web.Request) -> web.Response:
    """What this meeting is called, for anybody who was in it.

    Served from the same scoped read the session itself is, rather than
    through the write port: there is one statement that decides whether
    somebody may see a session, and a second read of the same two columns
    would be a second place for it to be decided.
    """
    from sturnus.console.app import current_user
    from sturnus.console.routes_read import READS

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()
    found = await request.app[READS].session_for(viewer, session_id)
    if found is None:
        return _no_such_session()
    return web.json_response(
        session_name_json(SessionName(title=found.title, description=found.description))
    )


async def rename(request: web.Request) -> web.Response:
    """Sets what this meeting is called, for everybody who was in it.

    404 for a session that does not exist *and* for one this person was
    not in, decided inside the write rather than here -- see
    `ConsoleSessionNaming`. 400 for text that cannot be stored, with a
    reason that says which rule was broken and never which text broke it.

    Both members are optional in the body and absent means null: a `PUT`
    replaces the name, and a client that sent only a title while leaving
    a description in place would be doing a `PATCH` under a `PUT`'s name.
    The console's form holds both fields and submits both.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    session_id = _session_id(request)
    if session_id is None:
        return _no_such_session()

    try:
        body = await request.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError):
        return _malformed(_MALFORMED_BODY)
    if not isinstance(body, dict):
        return _malformed(_MALFORMED_BODY)
    if not _is_optional_text(body.get("title")) or not _is_optional_text(body.get("description")):
        return _malformed(_MALFORMED_BODY)

    try:
        title = normalise_title(body.get("title"))
        description = normalise_description(body.get("description"))
    except InvalidName as refusal:
        # `str(refusal)` is one of the fixed sentences in
        # `sturnus.console.naming`. It is safe to pass on precisely
        # because that module never puts the offending text into one.
        return _malformed(str(refusal))

    stored = await request.app[SESSION_NAMING].rename(
        session_id, by=viewer, title=title, description=description
    )
    if stored is None:
        return _no_such_session()
    # The stored pair rather than the submitted one: trimming may have
    # changed what was sent, and a client shown its own input back would
    # keep displaying a title the database does not have.
    return web.json_response(session_name_json(stored))


def _is_optional_text(value: object) -> bool:
    """Whether a body member is text or absent.

    `None` and a missing key are both "no title", which is the same fact
    twice and not two different requests. A number or a list is a client
    bug and is refused rather than coerced -- `str(value)` would store
    the repr of somebody's mistake.
    """
    return value is None or isinstance(value, str)


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
    """Adds the recording routes to an application that already has both
    collaborators and `routes_read.READS`."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_TRANSCRIPT_PATH, require_session(transcript_view)),
            web.get(_NAME_PATH, require_session(name_view)),
            web.put(_NAME_PATH, require_session(rename)),
        ]
    )
