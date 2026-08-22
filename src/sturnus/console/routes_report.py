"""What a guild's recording adds up to.

- `GET /api/guilds/{guild_id}/report`

**Why this exists.** An administrator configuring Sturnus has no way to
tell whether it is working out. How often does this guild actually meet,
how long do its meetings run, how many of them produced a protocol, is the
transcription measuring anything at all. Every one of those is answerable
from rows the system already writes, and none of them is answerable from
anywhere in the product today.

**What this endpoint is not, and why that is deliberate.** It reports on a
*guild*: how much was recorded, over which months, how big the meetings
were, how many distinct people the guild has recorded. It does not name a
person and it does not rank anybody.

That is a boundary rather than a gap. A per-person readout of meeting
attendance and speaking time — "who was in the most sessions" — is a
different artifact from a usage report: in Germany and the EU it is a
means of monitoring performance and conduct, which is a matter for a works
council rather than something a console adds because the columns happen to
be there. The rows exist. Turning them into a ranking of colleagues is a
decision somebody has to take on purpose, and this endpoint is built so
that taking it is a visible act rather than a field that appeared in a
payload.

**404, never 403**, for a guild this person does not administer — the same
answer as for a guild that does not exist. The report says when a guild
meets and how often, which is a description of a team's working week.

**No decision is taken here.** The shaping is
`sturnus.console.reporting`, which is pure and tested without a database;
this module is the shape of one HTTP response and nothing else.
"""

from __future__ import annotations

from aiohttp import web

from sturnus.console.ports import GuildReports
from sturnus.console.reporting import guild_report

#: Where the collaborator is found. Its own key rather than a parameter to
#: `register`, so `build_api` stays a one-line edit -- several agents are
#: adding sections to that function and each extra line is a merge by hand.
GUILD_REPORTS: web.AppKey[GuildReports] = web.AppKey("guild_reports")

_REPORT_PATH = "/api/guilds/{guild_id}/report"


def register(app: web.Application) -> None:
    """Adds the report route to an application that already has its reports."""
    from sturnus.console.app import require_session

    app.add_routes([web.get(_REPORT_PATH, require_session(guild_report_view))])


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


def _no_such_guild() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such guild" and "you do not administer that guild" are
    deliberately indistinguishable; see the module docstring.
    """
    return web.json_response({"error": "no such guild"}, status=404)
