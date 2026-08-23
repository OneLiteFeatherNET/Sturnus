"""What a person may see and change about their own consent.

- `GET  /api/me/consents`
- `PUT  /api/me/consents/{guild_id}/scope`
- `POST /api/me/consents/{guild_id}/revoke`

**Why this is a separate module from `routes_consent`.** That one is an
administrator acting on somebody else and every handler in it carries a
subject taken from the URL. Every handler here acts on the person in the
signed cookie and **no path in this file names a user at all** -- there is
no parameter to substitute, so there is no version of these endpoints that
acts on a third party. The two capabilities are genuinely different and
splitting them at the module boundary is what keeps them from drifting
into one handler with a flag.

**404 is not the shape here.** `routes_consent` answers "no such guild"
for a guild the caller does not administer, because the existence of a
consent roster is itself something to withhold. Nothing is withheld from
somebody asking about themselves: a guild they have no record in produces
a named refusal, and that refusal tells them exactly one fact they already
knew.

**The three things this cannot do, all said out loud rather than left to
be discovered:**

* It cannot remove the Discord role. `api` holds no Discord token and
  never will (Spec 13.2) -- it can decrypt every recording ever made, and
  a process with that reach is not one to also give the ability to act as
  the bot. So a withdrawal here stops the recording within the consent
  cache's five seconds and leaves a role in Discord that no longer means
  anything. `role_stays` on the answer is how the interface knows to say
  so; the sentence itself is the console's (`ROLE_STAYS_NOTE`), because
  bounded literals cross this boundary and prose does not.
* It cannot delete anything already recorded. Withdrawing consent is a
  decision about the future; `/audio purge` is the separate, deliberate
  act that erases the past.
* It cannot widen a scope in a guild that does not offer video consent.
  Software cannot read the policy document, so it must not pretend to
  have checked it -- see `sturnus.domain.settings.VIDEO_CONSENT_OFFERED`.

**Widening inserts, narrowing does not.** `consent` is an append-only
history. Agreeing to something you had not agreed to is a grant and gets
a row carrying the guild's current `policy_version`; withdrawing part of
what you gave modifies the grant you gave. The rule lives in
`ConsolePersonalConsents`, not here -- this file parses, delegates and
serialises.
"""

from __future__ import annotations

import logging

from aiohttp import web

from sturnus.console.ports import OwnConsent, PersonalConsents, RevocationOutcome, ScopeOutcome
from sturnus.domain.consent import ConsentScope
from sturnus.observability.events import Event, log_event

# Imported inside each function that needs it: `app` imports this module
# the ordinary way, so a module-level import back into it would close a
# cycle while `app` is still defining the names wanted here. The same
# trade `routes_consent` and `routes_settings` make.

log = logging.getLogger(__name__)

#: Where the collaborator is found. Its own key rather than a parameter to
#: `register`, so `build_api` stays a one-line edit.
PERSONAL_CONSENTS: web.AppKey[PersonalConsents] = web.AppKey("personal_consents")

_LIST_PATH = "/api/me/consents"
_SCOPE_PATH = "/api/me/consents/{guild_id}/scope"
_REVOKE_PATH = "/api/me/consents/{guild_id}/revoke"

#: A guild id that is not a number names no guild. 400 rather than 404,
#: because unlike the administrator's routes there is nothing here whose
#: existence is worth concealing -- the caller is asking about their own
#: records, and "that is not a guild id" is the whole of the answer.
_NOT_A_GUILD = "not a guild id"
#: A body that is not an object with a `scope` string in it.
_BAD_SCOPE_BODY = 'body must be {"scope": "audio"|"audio_video"}'


def register(app: web.Application) -> None:
    """Adds the personal consent routes to an application that has its directory."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_LIST_PATH, require_session(list_own_consents)),
            web.put(_SCOPE_PATH, require_session(set_own_scope)),
            web.post(_REVOKE_PATH, require_session(revoke_own_consent)),
        ]
    )


async def list_own_consents(request: web.Request) -> web.Response:
    """Every guild the signed-in person holds a consent record in."""
    consents = await request.app[PERSONAL_CONSENTS].for_person(_caller(request))
    return web.json_response(
        {"consents": [_consent_json(consent) for consent in consents]},
        # It names which guilds a particular person agreed to be recorded
        # in, and it goes stale the moment anybody runs `/consent revoke`.
        headers={"Cache-Control": "private, no-store"},
    )


async def set_own_scope(request: web.Request) -> web.Response:
    """Narrows or widens what this person's consent covers.

    **409 for a refusal, 400 only for a request that cannot be read.** A
    widening in a guild that does not offer video consent is a well-formed
    request about a real person which the current state refuses -- which
    is exactly the distinction a client needs in order to choose between
    "fix your request" and "this is not available here". A scope this code
    cannot name is the other kind and gets a 400.
    """
    viewer = _caller(request)
    guild_id = _guild_id(request)
    if guild_id is None:
        return _bad_request(_NOT_A_GUILD)

    scope = await _requested_scope(request)
    if scope is None:
        return _bad_request(_BAD_SCOPE_BODY)

    outcome = await request.app[PERSONAL_CONSENTS].set_scope(viewer, guild_id, scope)
    if outcome.refusal is not None:
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_CONSENT_SCOPE_REFUSED,
            "Refused a consent scope change asked for from the console",
            guild_id=guild_id,
            discord_user_id=viewer,
            scope=scope,
            reason=outcome.refusal,
        )
        return web.json_response(_scope_json(outcome), status=409)

    if outcome.changed:
        # INFO rather than WARNING, and the asymmetry with
        # `console.consent_revoked` is the point: this is a person acting
        # on their own consent, which is the ordinary case this whole
        # feature exists for. The heavy line is the one where somebody
        # acts on somebody else.
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_CONSENT_SCOPE_CHANGED,
            "A person changed what their recording consent covers",
            guild_id=guild_id,
            discord_user_id=viewer,
            scope=outcome.scope,
        )
    return web.json_response(_scope_json(outcome))


async def revoke_own_consent(request: web.Request) -> web.Response:
    """This person withdrawing their own consent, effective immediately."""
    viewer = _caller(request)
    guild_id = _guild_id(request)
    if guild_id is None:
        return _bad_request(_NOT_A_GUILD)

    outcome = await request.app[PERSONAL_CONSENTS].revoke_own(viewer, guild_id)
    if not outcome.revoked:
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_CONSENT_REVOKE_REFUSED,
            "Refused a consent revocation asked for from the console",
            guild_id=guild_id,
            discord_user_id=viewer,
            requested_by=viewer,
            reason=outcome.refusal,
        )
        return web.json_response(_revocation_json(outcome), status=409)

    # `requested_by` equals `discord_user_id` here, and it is written
    # anyway. The audit query for "who withdrew whose consent" is one
    # query over `console.consent_self_revoked` and
    # `console.consent_revoked` together, and a line missing the field
    # would drop out of it rather than answering "themselves".
    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_CONSENT_SELF_REVOKED,
        "A person withdrew their own recording consent from the console",
        guild_id=guild_id,
        discord_user_id=viewer,
        requested_by=viewer,
    )
    return web.json_response(_revocation_json(outcome))


# ---------------------------------------------------------------------------
# Reading the request
# ---------------------------------------------------------------------------


def _guild_id(request: web.Request) -> int | None:
    """The guild from the path. `None` for a segment that is not a number."""
    try:
        return int(request.match_info["guild_id"])
    except ValueError:
        return None


async def _requested_scope(request: web.Request) -> str | None:
    """The `scope` out of the body, or `None` for anything unreadable.

    Whether the string names a scope this system knows is not decided
    here: `ConsolePersonalConsents.set_scope` owns the vocabulary, and a
    second copy of it in a handler is a second copy that goes stale. This
    only establishes that there is a string to hand over.
    """
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    scope = body.get("scope")
    if not isinstance(scope, str):
        return None
    # The vocabulary is the domain's enum, read here rather than
    # restated: a scope this system cannot name is a malformed request
    # (400) and not a state that refuses one (409), and only this side of
    # the call knows the difference. `ConsolePersonalConsents.set_scope`
    # refuses an unknown value as well, which is not duplication but the
    # ordinary rule that a collaborator does not trust its caller.
    return scope if scope in tuple(ConsentScope) else None


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


def _consent_json(consent: OwnConsent) -> dict[str, object]:
    return {
        # A Discord snowflake exceeds JavaScript's safe integer range,
        # where a JSON number silently loses its last digits and produces
        # an id that looks right and names nobody.
        "guild_id": str(consent.guild_id),
        # Why it stands where it does, as one of four literals. `active`
        # is sent beside it rather than derived from it, because
        # `scheduled` and `active` are both "you are being recorded" and
        # a client working that out for itself would be a second reading
        # of `sturnus.domain.consent.is_consent_active`.
        "state": consent.state,
        "active": consent.active,
        "scope": consent.scope,
        "policy_version": consent.policy_version,
        "guild_policy_version": consent.guild_policy_version,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": None if consent.revoked_at is None else consent.revoked_at.isoformat(),
        # Whether this guild offers the video scope at all. False means
        # the control is absent from the interface, not disabled: no
        # administrator has asserted that the policy document names
        # video, and a choice the API will refuse is worse than no choice.
        "video_consent_offered": consent.video_consent_offered,
    }


def _scope_json(outcome: ScopeOutcome) -> dict[str, object]:
    return {
        "scope": outcome.scope,
        "changed": outcome.changed,
        "refusal": outcome.refusal,
        "policy_version": outcome.policy_version,
    }


def _revocation_json(outcome: RevocationOutcome) -> dict[str, object]:
    return {
        "revoked": outcome.revoked,
        "refusal": outcome.refusal,
        "effective_at": (
            None if outcome.effective_at is None else outcome.effective_at.isoformat()
        ),
        "recordings_from_effective_at": outcome.recordings_from_effective_at,
        # Always true from this process, and sent as a field anyway rather
        # than assumed by the client. It is what `api` cannot do, and the
        # day some other process serves this route the answer changes
        # without every console needing to be redeployed.
        "role_stays": True,
    }


def _bad_request(error: str) -> web.Response:
    return web.json_response({"error": error}, status=400)
