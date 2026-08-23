"""Who has consented in a guild, and an administrator's power to end it.

- `GET  /api/guilds/{guild_id}/consents`
- `POST /api/guilds/{guild_id}/consents/revoke`
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

**`revoked_at` is an instant an administrator may choose.** Absent means
now, which is what this endpoint always did, so no client breaks by not
sending it. A future instant is a scheduled withdrawal -- "from the end of
the month" -- and takes effect on its own, within the consent cache's five
seconds of the moment it names. A past instant is a correction: somebody
left in March and nobody wrote it down until June. The only value refused
is one before `granted_at`, which would claim a grant ended before it
began.

**It is not a delete, and a back-dated revocation is not a delete
either.** Nothing already recorded is touched. That is a separate decision
with a separate command (`/audio purge`), and folding the two together
would mean an administrator who wanted to stop recording somebody tomorrow
had also erased a meeting their team read last week -- or, with a
back-dated instant, three months of them at once. Every row in the listing
carries how many recordings of that person the guild still holds, and the
revocation answers with how many fall on or after the chosen instant, so
the distinction is on the screen rather than in a document nobody opens.

**404, never 403.** A guild this person does not administer answers
exactly as a guild that does not exist. The list is a list of people who
consented to being recorded, together with when and under which policy;
a 403 would confirm that such a list exists here, to somebody just
established as having no business with it.

**The audit line is the whole audit.** `consent.revoked_at` records that a
revocation happened and never who performed it. So
`Event.CONSOLE_CONSENT_REVOKED` is emitted at WARNING with `requested_by`
alongside `discord_user_id`, and it is the only place the pair is ever
written down. It also carries `effective_at_given`, because an
administrator back-dating a revocation is a different act from clicking
"withdraw" and `revoked_at` alone cannot tell them apart -- by the time
anybody reads the row, a chosen instant and a defaulted one look
identical.

**The roster is served a page at a time**, with `limit`, `offset` and a
`total`, which is the convention `GET /api/sessions` established and
`sturnus.console.paging` enforces. It is not a second pagination shape:
one API with two ways of asking for a window is two things for every
client to learn and one of them to get wrong. Paging also moved the
*order* out of the browser and into SQL -- see
`ConsoleConsentDirectory.holders` for the key, the tiebreak that makes it
total, and why `active` is deliberately not part of it.

**Several people may be withdrawn in one request, and partial success is
the ordinary case.** Some names have no consent on record, some were
withdrawn while the page was open, some are fine. `POST .../consents/
revoke` therefore answers 200 with one outcome per person in the order
they were named: the status describes the request and the body describes
each person, because one status code for a mixed outcome is a status
code lying to somebody. The refusals are the same bounded literals the
single endpoint uses (`no_consent_on_record`, `already_revoked`,
`effective_before_grant`), so the console writes one sentence per
refusal rather than two vocabularies of them.

**A batch is bounded and audited per person.** At most
`MAX_REVOCATIONS_PER_REQUEST` names, because a request naming ten
thousand people is a denial of service with a valid session; and one
audit line per person, because a line saying "9 people" cannot answer
"was this person's consent withdrawn, and by whom" without somebody
first knowing which batch they were in.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiohttp import web

from sturnus.console.paging import MAX_PAGE_SIZE, InvalidPage, page_request
from sturnus.console.ports import (
    ConsentDirectory,
    ConsentHolder,
    PersonRevocation,
    RevocationOutcome,
)
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
#: No collision with `_REVOKE_PATH`: that one has a segment between
#: `consents` and `revoke` and this one does not, so no id can be
#: mistaken for the literal.
_BULK_REVOKE_PATH = "/api/guilds/{guild_id}/consents/revoke"

#: The one refusal, for every reason there is to refuse. See the module
#: docstring on why "no such guild" and "not yours" are one answer.
_NO_SUCH_GUILD = "no such guild"

#: An `effective_at` that is not an ISO-8601 instant, or is one with no
#: offset. 400 rather than 409: a string that names no moment is a
#: malformed request, not a state that refuses one. Naive is refused for
#: the reason the domain refuses it (`sturnus.domain._time.require_aware`)
#: -- "2026-08-20T10:00:00" is a different instant in every guild that
#: reads it, and a revocation is not a thing to be approximately dated.
_BAD_EFFECTIVE_AT = "effective_at must be an ISO-8601 instant with a UTC offset"

#: The most people one request may name. **Not an arbitrary round
#: number: it is `paging.MAX_PAGE_SIZE`**, the largest listing this API
#: will serve in one response -- so the biggest batch is exactly one page
#: of the roster it is withdrawn from. An interface can therefore never
#: build a request it could not have shown, and the two bounds cannot
#: drift apart into a console offering "select all" over a page the write
#: endpoint would refuse.
#:
#: Bounded at all because a request naming ten thousand people is a
#: denial of service with a valid session: each name is a read and a
#: write of its own (see `ConsoleConsentDirectory.revoke_many`), so the
#: work one request may ask for has to be capped somewhere -- and a cap
#: that is refused out loud is a cap somebody can build against.
MAX_REVOCATIONS_PER_REQUEST = MAX_PAGE_SIZE

#: A batch that names nobody. Refused rather than answered with an empty
#: success: a request naming nobody is a client that built its body
#: wrongly, and "nothing happened, as you asked" would hide that until
#: somebody noticed a roster that never changes.
_BAD_BATCH = 'body must be {"discord_user_ids": ["...", ...]} naming at least one person'
#: An entry that is not a snowflake written as a string. See `_subjects`
#: for why a JSON number is refused rather than coerced.
_BAD_SUBJECT = "every discord_user_ids entry must be a Discord id written as a string"
#: Over the bound. Names the rule and the number, never the count the
#: request actually carried -- no user input is reflected into a response.
_BATCH_TOO_LARGE = f"discord_user_ids must name at most {MAX_REVOCATIONS_PER_REQUEST} people"
#: The same person twice. See `_subjects`.
_REPEATED_SUBJECT = "discord_user_ids must not name the same person twice"


def register(app: web.Application) -> None:
    """Adds the consent routes to an application that already has its directory."""
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_LIST_PATH, require_session(list_consents)),
            web.post(_BULK_REVOKE_PATH, require_session(revoke_consents)),
            web.post(_REVOKE_PATH, require_session(revoke_consent)),
        ]
    )


async def list_consents(request: web.Request) -> web.Response:
    """One page of everyone this guild holds a consent record for.

    Paged rather than whole, and paged with `sturnus.console.paging`
    rather than with a second convention: a guild with four hundred
    participants used to send four hundred records to draw the first ten,
    and the console then sorted them in the browser. The window and the
    total travel together exactly as they do for `GET /api/sessions`.

    A window past the end is an empty page and not a refusal. It is what
    a bookmark to page five looks like once people have left, and the
    total travelling beside it is what lets the console say so rather
    than claim the guild has nobody.
    """
    viewer = _caller(request)
    guild_id = _guild_id(request)
    if guild_id is None:
        return _no_such_guild()

    try:
        window = page_request(request.query.get("limit"), request.query.get("offset"))
    except InvalidPage as refusal:
        # `str(refusal)` is a fixed sentence from `sturnus.console.paging`
        # and never the value that broke the rule.
        return web.json_response({"error": str(refusal)}, status=400)

    page = await request.app[CONSENT_DIRECTORY].holders(
        guild_id, requested_by=viewer, limit=window.limit, offset=window.offset
    )
    if page is None:
        return _no_such_guild()
    return web.json_response(
        {
            "guild_id": str(guild_id),
            "consents": [_holder_json(holder) for holder in page.holders],
            # How many people this guild holds consent for, not how many
            # are on this page. A list that cannot say how much it is not
            # showing is a list people page to the end of to find out.
            "total": page.total,
            "limit": page.limit,
            "offset": page.offset,
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

    try:
        effective_at = await _effective_at(request)
    except ValueError:
        return web.json_response({"error": _BAD_EFFECTIVE_AT}, status=400)

    outcome = await request.app[CONSENT_DIRECTORY].revoke(
        guild_id, subject, requested_by=viewer, effective_at=effective_at
    )
    if outcome is None:
        return _no_such_guild()

    _log_revocation(guild_id, subject, viewer, outcome, effective_at_given=effective_at is not None)
    if not outcome.revoked:
        return web.json_response(_outcome_json(outcome), status=409)
    return web.json_response(_outcome_json(outcome))


async def revoke_consents(request: web.Request) -> web.Response:
    """Withdraws several people's consent in one request.

    **200 for a mixed outcome, and the body says what happened to each.**
    The single-person endpoint answers 409 with a named refusal for a
    person who has no consent on record or whose consent is already
    withdrawn, and both of those are ordinary here: an administrator
    ticks nine boxes off a roster they opened five minutes ago, and by
    the time they press the button one of the nine has been withdrawn by
    a colleague. One status code cannot describe that -- 409 would claim
    nothing happened while eight withdrawals did, and 200 alone would
    claim everything did.

    So the status describes the *request* -- it was well formed, the
    caller was entitled to make it, and every person named was decided --
    and the body describes each person. There is one entry per name, in
    the order they were named, so the console never has to match answers
    back to requests.

    **Why not 207 Multi-Status.** It is a WebDAV status, and more to the
    point the console's `useApi` strips the body off every response it
    treats as a failure -- `ApiError` keeps the status and the path and
    nothing else, deliberately, so an in-cluster hostname can never reach
    a hydration payload. Any status outside 2xx would therefore destroy
    the per-person outcomes this endpoint exists to deliver, and an
    administrator would be told "something was refused" with no way to
    learn which name.
    """
    viewer = _caller(request)
    guild_id = _guild_id(request)
    if guild_id is None:
        return _no_such_guild()

    try:
        body = await _body(request)
        subjects = _subjects(body)
        effective_at = _instant(body)
    except ValueError as refusal:
        # Every message reachable here is a fixed sentence from this
        # module. No user input is reflected into a response body.
        return web.json_response({"error": str(refusal)}, status=400)

    done = await request.app[CONSENT_DIRECTORY].revoke_many(
        guild_id, subjects, requested_by=viewer, effective_at=effective_at
    )
    if done is None:
        return _no_such_guild()

    for person in done:
        _log_revocation(
            guild_id,
            person.discord_user_id,
            viewer,
            person.outcome,
            effective_at_given=effective_at is not None,
        )
    revoked = sum(1 for person in done if person.outcome.revoked)
    # Beside the per-person lines and never instead of them. Nine lines
    # from a batch and nine lines from nine clicks are identical, so this
    # is the only place the fact that they were one decision survives. It
    # carries no name: the names are on the lines above it.
    log_event(
        log,
        logging.WARNING,
        Event.CONSOLE_CONSENT_BULK_REVOKED,
        "An administrator withdrew several people's recording consent at once",
        guild_id=guild_id,
        requested_by=viewer,
        count=len(done),
        revoked=revoked,
        refused=len(done) - revoked,
    )
    return web.json_response(
        {
            "guild_id": str(guild_id),
            "requested": len(done),
            "revoked": revoked,
            "refused": len(done) - revoked,
            "outcomes": [_person_json(person) for person in done],
        }
    )


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


async def _effective_at(request: web.Request) -> datetime | None:
    """The instant the consent should stop, out of an optional body.

    `None` for no body, an empty body, or a body without the key -- which
    means now, and is exactly what this endpoint did before the field
    existed. Not sending it is therefore not a client that needs
    updating, which is the whole reason the field is optional rather
    than required with a documented sentinel.

    Raises `ValueError` for a string that is not an ISO-8601 instant and
    for one carrying no offset. Whether the instant is *allowed* -- not
    before `granted_at` -- is the directory's decision, because only it
    has the grant to compare against; this establishes only that a moment
    was named.
    """
    return _instant(await _body(request))


async def _body(request: web.Request) -> dict[str, object]:
    """The request body as an object, or `{}` for a request without one.

    Read once and passed to each field reader rather than read per field:
    the bulk endpoint needs two things out of one body, and
    `request.json()` twice is a second parse of the same bytes.

    An absent body is an empty object rather than a refusal, because
    `effective_at` is optional and a revocation that sends nothing at all
    is the shape every client used before the field existed.
    """
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except Exception as exc:
        raise ValueError("body is not JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("body is not an object")
    return body


def _instant(body: dict[str, object]) -> datetime | None:
    """`effective_at` out of a body, or `None` for one that does not name it."""
    raw = body.get("effective_at")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(_BAD_EFFECTIVE_AT)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(_BAD_EFFECTIVE_AT) from exc
    if parsed.tzinfo is None:
        raise ValueError(_BAD_EFFECTIVE_AT)
    # Normalised on the way in, so what is compared against `granted_at`,
    # what is stored and what is echoed back are all one instant in one
    # zone. Every other time in this system is UTC; a `+02:00` that
    # survived to the column would be the only one that was not.
    return parsed.astimezone(UTC)


def _subjects(body: dict[str, object]) -> tuple[int, ...]:
    """The people a batch names, in the order it named them.

    **Every id is a string, as every snowflake in this API is.** A number
    is refused rather than coerced: a snowflake exceeds JavaScript's safe
    integer range, so an id that survived a `JSON.parse` as a number has
    already lost its last digits -- and withdrawing the consent of
    whoever the rounded id happens to name is a worse answer than
    refusing the request.

    **A repeated name is refused rather than de-duplicated.** The answer
    is one outcome per name in the order they were named, so collapsing
    two entries into one would hand the caller a shorter list than it
    sent, which it could only reconcile by matching on id -- exactly the
    work this shape exists to spare it.
    """
    named = body.get("discord_user_ids")
    if not isinstance(named, list) or not named:
        raise ValueError(_BAD_BATCH)
    if len(named) > MAX_REVOCATIONS_PER_REQUEST:
        raise ValueError(_BATCH_TOO_LARGE)
    subjects: list[int] = []
    for entry in named:
        # `str` first, then `int`: `isinstance(True, int)` is `True`, and
        # a bare `int` check would also admit a JSON number.
        if not isinstance(entry, str):
            raise ValueError(_BAD_SUBJECT)
        try:
            subjects.append(int(entry))
        except ValueError:
            raise ValueError(_BAD_SUBJECT) from None
    if len(set(subjects)) != len(subjects):
        raise ValueError(_REPEATED_SUBJECT)
    return tuple(subjects)


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
        # What this person's grant covers. Every row says `audio` until a
        # guild turns `video_consent_offered` on, and it is on the roster
        # anyway: a setting an administrator can switch on with no readout
        # of who then used it is a setting nobody can audit.
        "scope": holder.scope,
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
    return {
        "revoked": outcome.revoked,
        "refusal": outcome.refusal,
        # What was actually stored, echoed rather than left to the client
        # to assume. A request that named no instant got `now`, and a
        # console showing "withdrawn as of ..." must show the instant the
        # database holds rather than the one its own clock read.
        "effective_at": (
            None if outcome.effective_at is None else outcome.effective_at.isoformat()
        ),
        # How many recordings the chosen instant is a statement *about*,
        # and which this revocation did not touch. A back-dated
        # revocation deletes nothing; the console offers `/audio purge`
        # for that, as the separate deliberate act it is.
        "recordings_from_effective_at": outcome.recordings_from_effective_at,
    }


def _person_json(person: PersonRevocation) -> dict[str, object]:
    """One person's outcome inside a batch: who, and then what happened.

    `_outcome_json` unchanged with an id in front of it, rather than a
    second shape for the batch case. A withdrawal answers the same thing
    whether one person or nine were named, and two spellings of that
    answer would be two definitions of what a revocation reports --
    agreeing right up until one of them changed.
    """
    return {
        # A Discord snowflake exceeds JavaScript's safe integer range,
        # where a JSON number silently loses its last digits and produces
        # an id that looks right and names nobody.
        "discord_user_id": str(person.discord_user_id),
        **_outcome_json(person.outcome),
    }


def _log_revocation(
    guild_id: int,
    discord_user_id: int,
    requested_by: int,
    outcome: RevocationOutcome,
    *,
    effective_at_given: bool,
) -> None:
    """The audit line for one withdrawal, whoever asked for it and however.

    **One line per person, always, and a batch is no exception.** A
    single line saying "9 people" could not answer "was this person's
    consent withdrawn, and by whom" without somebody first knowing which
    batch that person was in, and `consent.revoked_at` records no actor
    to fall back on -- these lines are the whole of the audit. Shared
    between the single and the bulk handler so the two cannot drift into
    writing the pair down differently.
    """
    if not outcome.revoked:
        # INFO, not WARNING: two administrators reaching for the same name
        # is this feature working. The interesting line is the one below.
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_CONSENT_REVOKE_REFUSED,
            "Refused a consent revocation asked for from the console",
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            requested_by=requested_by,
            reason=outcome.refusal,
        )
        return

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
        discord_user_id=discord_user_id,
        requested_by=requested_by,
        # Whether the instant was chosen or defaulted, which is the one
        # thing about this act the timestamps cannot recover. An
        # administrator back-dating a revocation to last March is making
        # a claim about three months of recordings that already exist;
        # one clicking "withdraw" is stopping something tomorrow. Both
        # produce a `revoked_at` and only this field tells them apart --
        # and by the time anybody asks, `revoked_at` will look like a
        # perfectly ordinary date either way.
        effective_at_given=effective_at_given,
    )


def _no_such_guild() -> web.Response:
    """One refusal for every reason there is to refuse.

    "No such guild" and "you do not administer that guild" are
    deliberately indistinguishable; see the module docstring.
    """
    return web.json_response({"error": _NO_SUCH_GUILD}, status=404)
