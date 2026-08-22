"""What a guild's recording adds up to, and who took part in it.

- `GET /api/guilds/{guild_id}/report`
- `GET /api/guilds/{guild_id}/report/participation`

**Why this exists.** An administrator configuring Sturnus has no way to
tell whether it is working out. How often does this guild actually meet,
how long do its meetings run, how many of them produced a protocol, is the
transcription measuring anything at all. Every one of those is answerable
from rows the system already writes, and none of them is answerable from
anywhere in the product today.

**Two endpoints, because they are two decisions.**

The first reports on a *guild*: how much was recorded, over which months,
how big the meetings were, how many distinct people the guild has
recorded. It names nobody, and `sturnus.console.reporting` is built so
that it cannot: it is handed counts rather than people.

The second is an attendance ranking — named individuals, ordered by how
many meetings they were in. That is a different artifact from a usage
report. In Germany and the EU a per-person readout of attendance and
speaking time is a means of monitoring performance and conduct, subject to
co-determination (BetrVG §87(1)(6)) whether or not anybody intended it as
one. So it is a separate path, a separate collaborator and a separate
module, and reading it emits an audit line — see `participation_view` and
`sturnus.console.participation`. A deployment that should not offer it
does not have to unpick the first one to stop.

**404, never 403**, for a guild this person does not administer — the same
answer as for a guild that does not exist. The report says when a guild
meets and how often, which is a description of a team's working week.

**No arithmetic is done here.** The shaping is
`sturnus.console.reporting` and `sturnus.console.participation`, both
pure and tested without a database; this module is the shape of two HTTP
responses and one log line.
"""

from __future__ import annotations

import logging

from aiohttp import web

from sturnus.console.participation import participation
from sturnus.console.ports import GuildReports, ParticipationReports
from sturnus.console.reporting import guild_report
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Where the collaborator is found. Its own key rather than a parameter to
#: `register`, so `build_api` stays a one-line edit -- several agents are
#: adding sections to that function and each extra line is a merge by hand.
GUILD_REPORTS: web.AppKey[GuildReports] = web.AppKey("guild_reports")

#: The attendance ranking's collaborator. A second key rather than one
#: object answering both, because the two are different decisions -- see
#: `participation_view` below and `sturnus.console.participation`.
PARTICIPATION_REPORTS: web.AppKey[ParticipationReports] = web.AppKey("participation_reports")

_REPORT_PATH = "/api/guilds/{guild_id}/report"
_PARTICIPATION_PATH = "/api/guilds/{guild_id}/report/participation"


def register(app: web.Application) -> None:
    """Adds the report routes to an application that already has its reports."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_REPORT_PATH, require_session(guild_report_view)),
            web.get(_PARTICIPATION_PATH, require_session(participation_view)),
        ]
    )


async def guild_report_view(request: web.Request) -> web.Response:
    """One guild's recording, in aggregate."""
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        # A path segment that is not a number names no guild, which is the
        # same answer as naming one that does not exist.
        return _no_such_guild()

    recording = await request.app[GUILD_REPORTS].recording_of(guild_id, requested_by=viewer)
    if recording is None:
        return _no_such_guild()

    return web.json_response(
        guild_report(
            recording.sessions,
            guild_id=guild_id,
            distinct_participants=recording.distinct_participants,
            zone=recording.zone,
            zone_name=recording.zone_name,
        ),
        # It describes when a team meets and how often. Nothing in between
        # this and the browser has any business keeping a copy.
        headers={"Cache-Control": "private, no-store"},
    )


async def participation_view(request: web.Request) -> web.Response:
    """Who took part in the most of this guild's meetings.

    **The one endpoint in this console that names other people and ranks
    them**, and the reasons that is a decision rather than a feature are
    in `sturnus.console.participation`. Two things follow from it here.

    It is a *separate route with a separate collaborator*, so a
    deployment that should not offer it is one revert away rather than an
    audit of a shared response shape.

    And reading it is logged, which no other read in this console is. That
    asymmetry is deliberate: `console.track_served` records one person
    playing another's voice back, and this records one person reading an
    ordered list of their colleagues' attendance. Both are uses of other
    people's data that leave no other trace, and "who looked, and when" is
    the first question anybody reviewing the arrangement will ask. The
    line names the guild and the reader and never who was in the list --
    the list is the point, and copying it into a retained log store would
    be making a second copy of exactly the thing under discussion.
    """
    from sturnus.console.app import current_user

    viewer = current_user(request).discord_user_id
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        return _no_such_guild()

    found = await request.app[PARTICIPATION_REPORTS].attendance_in(guild_id, requested_by=viewer)
    if found is None:
        # No line here. Nothing was disclosed and nobody was authorised,
        # and logging refusals by guild id would let anybody with a
        # session fill the audit trail with guilds of their choosing.
        return _no_such_guild()

    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_PARTICIPATION_READ,
        "Somebody read a guild's meeting attendance ranking",
        guild_id=guild_id,
        requested_by=viewer,
        # How many people were in the answer, which is the one thing about
        # the list that is safe to retain and is enough to tell an empty
        # guild from a whole team.
        participants=len(found.people),
    )
    return web.json_response(
        participation(found.people, guild_id=guild_id, sessions=found.sessions),
        # It is an ordered list of named colleagues. Nothing in between
        # this and the browser has any business keeping a copy.
        headers={"Cache-Control": "private, no-store"},
    )


def _no_such_guild() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such guild" and "you do not administer that guild" are
    deliberately indistinguishable; see the module docstring.
    """
    return web.json_response({"error": "no such guild"}, status=404)
