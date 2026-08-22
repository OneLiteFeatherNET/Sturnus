"""Who has consented in a guild, and an administrator's power to end it.

- `GET  /api/guilds/{guild_id}/consents`
- `POST /api/guilds/{guild_id}/consents/{discord_user_id}/revoke`

**Why this exists.** Until now the only way a consent could end was the
person ending it themselves with `/consent revoke`, or an administrator
bumping `policy_version`, which ends everybody's at once. Neither answers
the case this is for: somebody left the team, or asked in a channel rather
than in a slash command, or is no longer somebody this guild should be
recording. The alternative an administrator reaches for otherwise is
removing the Discord role by hand -- which stops the recording and leaves
`revoked_at` NULL, so `/consent status` still reports consent active and
re-adding the role silently resumes recording a person who never
re-consented.

**What a revocation from here is, exactly.** It stamps `revoked_at` on the
stored consent record. It does not remove the Discord role, because this
process holds no Discord token and never will (Spec 13.2). That is enough
to stop the recording -- the stored record is checked on every frame
through a five second cache, and it is the layer that exists precisely
because the role can be bypassed by anyone with administrator permissions
in Discord. It is not enough to make Discord *look* right, and the console
says so next to the button rather than letting somebody infer it.

**It is not a delete.** Nothing already recorded is touched. That is a
separate decision with a separate command (`/audio purge`), and folding
the two together would mean an administrator who wanted to stop recording
somebody tomorrow had also erased a meeting their team read last week.
Every row in the listing carries how many recordings of that person the
guild still holds, so the distinction is on the screen rather than in a
document nobody opens.

**404, never 403.** A guild this person does not administer answers
exactly as a guild that does not exist. The list is a list of people who
consented to being recorded, together with when and under which policy;
a 403 would confirm that such a list exists here, to somebody just
established as having no business with it.

**The audit line is the whole audit.** `consent.revoked_at` records that a
revocation happened and never who performed it. So
`Event.CONSOLE_CONSENT_REVOKED` is emitted at WARNING with `requested_by`
alongside `discord_user_id`, and it is the only place the pair is ever
written down.
"""

from __future__ import annotations

import logging

from aiohttp import web

from sturnus.console.ports import ConsentDirectory, ConsentHolder, RevocationOutcome
from sturnus.observability.events import Event, log_event

# Every reference to `sturnus.console.app` below is imported inside a
# function rather than at module scope: `app` imports this module the
# ordinary way, so a module-level import back into it would close a cycle
# while `app` is still defining the very names wanted here. The same trade
# `routes_settings` makes, for the same reason.

log = logging.getLogger(__name__)

#: Where the collaborator is found. Its own key rather than a parameter to
#: `register`, so `build_api` stays a one-line edit -- several agents are
#: adding sections to that function and each extra line is a merge by hand.
CONSENT_DIRECTORY: web.AppKey[ConsentDirectory] = web.AppKey("consent_directory")

_LIST_PATH = "/api/guilds/{guild_id}/consents"
_REVOKE_PATH = "/api/guilds/{guild_id}/consents/{discord_user_id}/revoke"

#: The one refusal, for every reason there is to refuse. See the module
#: docstring on why "no such guild" and "not yours" are one answer.
_NO_SUCH_GUILD = "no such guild"


def register(app: web.Application) -> None:
    """Adds the consent routes to an application that already has its directory."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_LIST_PATH, require_session(list_consents)),
            web.post(_REVOKE_PATH, require_session(revoke_consent)),
        ]
    )


async def list_consents(request: web.Request) -> web.Response:
    """Everyone this guild holds a consent record for."""
    viewer = _caller(request)
    guild_id = _guild_id(request)
    if guild_id is None:
        return _no_such_guild()

    holders = await request.app[CONSENT_DIRECTORY].holders(guild_id, requested_by=viewer)
    if holders is None:
        return _no_such_guild()
    return web.json_response(
        {
            "guild_id": str(guild_id),
            "consents": [_holder_json(holder) for holder in holders],
        },
        # It names who agreed to be recorded in a particular guild, and it
        # goes stale the moment anybody runs `/consent grant`.
        headers={"Cache-Control": "private, no-store"},
    )


async def revoke_consent(request: web.Request) -> web.Response:
    """Withdraws one person's consent on their behalf.

    A revocation that changes nothing is **409, not 400**: the request is
    well formed and the person is real, and what is wrong is the state
    they are already in -- which is the distinction a client needs to
    decide between "fix your request" and "somebody got there first". The
    reason travels with it, because a button that fails without saying why
    is a bug report waiting to be filed.
    """
    viewer = _caller(request)
    guild_id = _guild_id(request)
    subject = _subject(request)
    if guild_id is None or subject is None:
        return _no_such_guild()

    outcome = await request.app[CONSENT_DIRECTORY].revoke(guild_id, subject, requested_by=viewer)
    if outcome is None:
        return _no_such_guild()

    if not outcome.revoked:
        # INFO, not WARNING: two administrators reaching for the same name
        # is this feature working. The interesting line is the one below.
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_CONSENT_REVOKE_REFUSED,
            "Refused a consent revocation asked for from the console",
            guild_id=guild_id,
            discord_user_id=subject,
            requested_by=viewer,
            reason=outcome.refusal,
        )
        return web.json_response(_outcome_json(outcome), status=409)

    # The audit line, and the only one there will ever be:
    # `consent.revoked_at` records that a revocation happened and never
    # who performed it. WARNING because this is a third party acting on
    # somebody else's consent, which is a heavier act than any other the
    # console offers.
    log_event(
        log,
        logging.WARNING,
        Event.CONSOLE_CONSENT_REVOKED,
        "An administrator withdrew a person's recording consent from the console",
        guild_id=guild_id,
        discord_user_id=subject,
        requested_by=viewer,
    )
    return web.json_response(_outcome_json(outcome))


# ---------------------------------------------------------------------------
# Reading the request
# ---------------------------------------------------------------------------


def _guild_id(request: web.Request) -> int | None:
    """The guild from the path. `None` for a segment that is not a number.

    A path segment that is not a number names no guild, which is the same
    answer as naming one that does not exist -- and the same answer as
    naming one this person does not administer. All three are
    `_no_such_guild`.
    """
    try:
        return int(request.match_info["guild_id"])
    except ValueError:
        return None


def _subject(request: web.Request) -> int | None:
    """The person whose consent is being withdrawn."""
    try:
        return int(request.match_info["discord_user_id"])
    except ValueError:
        return None


def _caller(request: web.Request) -> int:
    """The Discord id of the person making this request.

    Only ever reached from behind `require_session`, which is what
    guarantees there is one -- `current_user` raises rather than returning
    `None` if that is ever untrue, so a route registered without the
    wrapper fails loudly instead of quietly acting for somebody else.
    """
    from sturnus.console.app import current_user

    return current_user(request).discord_user_id


# ---------------------------------------------------------------------------
# Writing the response
# ---------------------------------------------------------------------------


def _holder_json(holder: ConsentHolder) -> dict[str, object]:
    return {
        # A Discord snowflake exceeds JavaScript's safe integer range,
        # where a JSON number silently loses its last digits and produces
        # an id that looks right and names nobody.
        "discord_user_id": str(holder.discord_user_id),
        "display_name": holder.display_name,
        "policy_version": holder.policy_version,
        "granted_at": holder.granted_at.isoformat(),
        "revoked_at": None if holder.revoked_at is None else holder.revoked_at.isoformat(),
        # Sent as its own field rather than left to the client to derive
        # from the two above it. Whether a grant is still in force also
        # depends on the guild's current `policy_version`, and a console
        # that worked it out for itself would be a second implementation
        # of `sturnus.domain.consent.is_consent_active` -- one that would
        # agree with the recorder right up until one of them changed.
        "active": holder.active,
        # What revoking will *not* do, as a number. An administrator not
        # shown this would reasonably assume withdrawing consent erases
        # what was recorded under it.
        "recordings_with_audio": holder.recordings_with_audio,
    }


def _outcome_json(outcome: RevocationOutcome) -> dict[str, object]:
    return {"revoked": outcome.revoked, "refusal": outcome.refusal}


def _no_such_guild() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such guild" and "you do not administer that guild" are
    deliberately indistinguishable; see the module docstring.
    """
    return web.json_response({"error": _NO_SUCH_GUILD}, status=404)
