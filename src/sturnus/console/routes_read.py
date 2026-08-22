"""The console's read endpoints: dashboard, recordings, calendar.

Thin by design, like every other handler in this package. Each one does
three things -- take the Discord id out of the session, ask
`SessionReads` for what that person was in, hand the rows to
`sturnus.console.statistics` -- and none of them makes an authorisation
decision, because there is none left to make: `SessionReads` cannot be
asked a question that is not already scoped (see
`sturnus.console.queries`).

Two rules from `app.py` hold here unchanged and are worth restating,
because this is where the data actually is:

- **No user input is reflected into a response.** The refusals below are
  fixed strings. A year and a date arrive from a query string, and
  echoing either back is how an endpoint becomes an XSS sink for whatever
  renders its errors.
- **Every Discord id and every session id is serialised as a string.**
  `statistics` does it, once, for every shape.

`register` adds the routes and nothing else; the collaborator is put on
the application by `build_api`, which is the only place that knows which
adapter this process was given.
"""

from __future__ import annotations

from datetime import date

from aiohttp import web

from sturnus.console.app import current_user, require_session
from sturnus.console.filters import InvalidFilter, session_filter
from sturnus.console.paging import InvalidPage, page_request
from sturnus.console.ports import SessionReads
from sturnus.console.statistics import (
    calendar_year,
    dashboard,
    day_timeline,
    session_json,
    session_page_json,
    year_bounds,
)

#: Where `build_api` puts the reads adapter. Public, unlike the keys in
#: `app.py`, because `build_api` is in another module and this is the one
#: name it needs from here besides `register`.
READS = web.AppKey("reads", SessionReads)


def _malformed(reason: str) -> web.Response:
    """400 with a fixed reason. Never carries anything from the request."""
    return web.json_response({"error": reason}, status=400)


def _not_found() -> web.Response:
    """404, and the same 404 for "no such session" and "not yours".

    Two different answers would let somebody walk the id space and learn
    which sessions the system holds, which is itself something they were
    never part of.
    """
    return web.json_response({"error": "no such session"}, status=404)


@require_session
async def dashboard_view(request: web.Request) -> web.Response:
    """Everything this person has accumulated, across every session.

    Two reads rather than one: the sessions carry the measurements, and
    the transcripts are fetched separately because they are the protected
    content and nothing else on this page needs them. Keeping them apart
    means the session endpoints never load a transcript at all.
    """
    viewer = current_user(request).discord_user_id
    reads = request.app[READS]
    return web.json_response(
        dashboard(
            await reads.sessions_for(viewer),
            viewer=viewer,
            transcripts=await reads.transcripts_of(viewer),
        )
    )


@require_session
async def sessions_view(request: web.Request) -> web.Response:
    """One page of this person's recordings, and how many there are.

    Paged rather than whole. A person who has been in three hundred
    meetings was previously served three hundred sessions with every
    participant, every tag and every track inline, in one body, on every
    visit to the list -- and the page then rendered an article for each.

    A window that falls past the end is an empty page and not a refusal:
    it is what a bookmark to page five looks like after a retention sweep,
    and the total travelling with it is what lets the console say so
    rather than claim the person has no recordings.
    """
    viewer = current_user(request).discord_user_id
    try:
        window = page_request(request.query.get("limit"), request.query.get("offset"))
        # `getall` rather than `get`: `?tag=retro&tag=kunde` is how a set
        # of chips is written, and reading only the first would silently
        # answer a narrower question than the one on the screen.
        matching = session_filter(
            text=request.query.get("q"),
            tags=request.query.getall("tag", []),
            since=request.query.get("from"),
            until=request.query.get("to"),
            protocol=request.query.get("protocol"),
        )
    except (InvalidPage, InvalidFilter) as refusal:
        # `str(refusal)` is a fixed sentence from `sturnus.console.paging`
        # or `sturnus.console.filters` and never the value that broke the
        # rule.
        return _malformed(str(refusal))
    page = await request.app[READS].sessions_page(
        viewer, limit=window.limit, offset=window.offset, matching=matching
    )
    return web.json_response(session_page_json(page, viewer))


@require_session
async def session_view(request: web.Request) -> web.Response:
    """One session, or a 404 for one this person was not in.

    An id that is not a number gets the same 404 rather than a 400: it
    names no session either, and a distinct answer only tells the caller
    which ids are well formed.
    """
    viewer = current_user(request).discord_user_id
    try:
        session_id = int(request.match_info["session_id"])
    except ValueError:
        return _not_found()
    found = await request.app[READS].session_for(viewer, session_id)
    if found is None:
        return _not_found()
    return web.json_response(session_json(found, viewer))


@require_session
async def calendar_view(request: web.Request) -> web.Response:
    """One entry per day of a year that had recordings.

    The year is required and never defaulted to the current one: somebody
    whose meetings were all last year would get an empty heatmap with
    nothing on the page saying which year they are looking at.

    `year_bounds` is called here rather than only inside the query
    because it is what rejects a year that parses as an integer and is
    not a date -- `datetime(0, 1, 1)` raises, and a 500 is not an answer
    to a malformed query string.
    """
    try:
        year = int(request.query["year"])
        year_bounds(year)
    except (KeyError, ValueError):
        return _malformed("a year is required, as four digits")
    viewer = current_user(request).discord_user_id
    found = await request.app[READS].sessions_in_year(viewer, year)
    return web.json_response({"year": year, "days": calendar_year(found)})


@require_session
async def calendar_day_view(request: web.Request) -> web.Response:
    """One day's sessions, in the order they happened."""
    try:
        day = date.fromisoformat(request.match_info["day"])
    except ValueError:
        return _malformed("a date is required, as YYYY-MM-DD")
    viewer = current_user(request).discord_user_id
    found = await request.app[READS].sessions_on_day(viewer, day)
    return web.json_response({"date": day.isoformat(), "sessions": day_timeline(found)})


def register(app: web.Application) -> None:
    """Adds the read routes. `app[READS]` must already be set."""
    app.add_routes(
        [
            web.get("/api/dashboard", dashboard_view),
            web.get("/api/sessions", sessions_view),
            web.get("/api/sessions/{session_id}", session_view),
            web.get("/api/calendar", calendar_view),
            web.get("/api/calendar/{day}", calendar_day_view),
        ]
    )
