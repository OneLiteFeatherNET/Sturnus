"""What the console asked the bot to do, and what happened when it did.

Every step of setting up a guild that matters needs a Discord token, and
`api` must never hold one: it already holds S3 and the master key, so it
can decrypt every recording ever made, and that is not a process to also
hand the ability to act as the bot (Spec 13.2). So the console cannot
create the consent role, set the Speak overwrites or sync the commands.

It does not need to, because the pattern already exists in reverse. The
bot mirrors Discord state into the database for `api` to read; an intent
is the same arrangement run the other way -- `api` writes down what
should be true, and the bot's existing ten-second reconcile tick makes it
true and writes back what happened, through the **same `plan_setup`** the
slash command uses. One planner, two callers; a second implementation of
the consent protection is the last thing this system should grow.

**An intent is settled exactly once, and a failure settles it.** The tick
runs six times a minute forever, so an intent that stayed unapplied after
being applied would re-create the role and re-write the overwrites for
the life of the guild; and one that stayed unapplied after failing would
retry a permission error against Discord's rate limiter just as often.
Both outcomes are terminal. An administrator who has fixed the permission
asks again, which is a new intent, which is a row that says who asked and
when.

**The newest ask wins, and it wins outright.** Two administrators
submitting different intents thirty seconds apart, or one impatient
person pressing twice, leave a guild with two unapplied rows. They are
not a queue of two jobs: an intent states what should be *true*, and two
statements of what should be true do not compose -- applying both in
request order would finish on the older list, which is the correction
being overwritten by the mistake it corrected. So `select_intent` applies
the newest and settles every older one as `SUPERSEDED`, unapplied.

The rejected alternative was refusing a request while one is pending.
That reads tidier and is worse in both directions: an administrator who
mistyped a channel would have to wait out a tick before they could
correct it, and their correction would then be a second full setup
applied on top of the first; and an intent that never settles -- because
the bot has not joined the guild yet, which is the ordinary state during
onboarding -- would lock that guild out of being set up at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

#: The bot did what was asked.
APPLIED = "applied"

#: The bot tried and could not -- `error` says what Discord answered.
#: Terminal, not a retry: see the module docstring.
FAILED = "failed"

#: The bot never tried: a newer intent for the same guild replaced this
#: one before the tick reached either. Terminal like the other two, and
#: written by the bot rather than by `api` -- the row stays exactly as it
#: was asked for, so who asked for what and when survives being overtaken.
#: See `select_intent` for why the newer one wins outright.
SUPERSEDED = "superseded"

#: What may be written into `guild_setup_intent.outcome`. Plain strings
#: rather than a database enum for the reason `guild_channel.kind` is
#: one: a value this code has never seen must be a row a reader can
#: ignore rather than a write that fails inside a reconcile tick.
OUTCOMES = frozenset({APPLIED, FAILED, SUPERSEDED})


@dataclass(frozen=True, slots=True)
class SetupIntent:
    """One request from the console for the bot to configure a guild.

    `channel_ids` is stored in exactly the format `guild_config` holds it
    in -- the comma-separated list `settings.parse_channel_ids` reads --
    so that applying an intent is a write of the value rather than a
    second serialisation nobody would keep in step with the first.

    `consent_role_name` is a name and not an id, because the role does
    not exist yet: naming it is the whole request. `None` means the
    console asked for the channels and nothing else.

    `applied_at`, `outcome` and `error` are all null while the intent is
    pending, and all three are written together when it settles. An
    intent with an `applied_at` and no `outcome` would be a row that
    cannot say what happened, which is the state this table exists to
    make impossible.
    """

    id: int
    guild_id: int
    requested_by: int
    requested_at: datetime
    channel_ids: str | None
    consent_role_name: str | None
    applied_at: datetime | None
    outcome: str | None
    error: str | None

    @property
    def is_pending(self) -> bool:
        return self.applied_at is None


@dataclass(frozen=True, slots=True)
class IntentSelection:
    """Which of a guild's unapplied intents the bot acts on, and which it buries.

    `apply` is the one the bot configures the guild from, or `None` when
    there is nothing to do. `supersede` is every other unapplied intent,
    which the bot settles as `SUPERSEDED` without ever acting on it.

    Two fields rather than one, because both halves have to happen in the
    same pass. Applying the newest and leaving the older ones pending
    would have the next tick apply the newest all over again -- there
    would still be an unapplied row, and nothing in the table would say
    the guild had already been configured from it.
    """

    apply: SetupIntent | None
    supersede: tuple[SetupIntent, ...]


def select_intent(pending: Sequence[SetupIntent]) -> IntentSelection:
    """The newest unapplied intent wins; every older one is superseded.

    See the module docstring for why the newest rather than the oldest,
    and why "refuse while one is pending" was not the rule chosen.

    Ordered by `requested_at` and then by `id`, not by the order the
    caller happened to read the rows in. The id breaks a tie because two
    requests can share an instant -- a pinned clock in a test, a coarse
    one in production -- and it is monotonic, so it settles the tie in the
    same direction the timestamp would have.

    Already-settled rows are ignored rather than settled a second time.
    The store hands over unapplied rows only, so this is belt and braces;
    but rewriting an `applied` outcome to `superseded` would lose the one
    record that the bot ever did anything to this guild.
    """
    unapplied = sorted(
        (intent for intent in pending if intent.is_pending),
        key=lambda intent: (intent.requested_at, intent.id),
    )
    if not unapplied:
        return IntentSelection(apply=None, supersede=())
    return IntentSelection(apply=unapplied[-1], supersede=tuple(unapplied[:-1]))


#: Where Discord takes somebody who is adding an application to a server.
_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"

#: What the bot is invited with. `bot` is what puts it in the server;
#: `applications.commands` is what lets its command tree be registered
#: there, so `/setup`, `/config` and `/consent` exist for the guild
#: without a second authorisation round.
INVITE_SCOPES: tuple[str, ...] = ("bot", "applications.commands")

#: View Channel, Connect, Send Messages, Manage Roles -- the four
#: `docs/first-deployment.md` section 2 tells an operator to tick, as the
#: number Discord's own URL generator produces for them.
#:
#: `Manage Roles` is the one that fails late if it is missing. It covers
#: both halves of what a setup intent asks for -- creating the consent
#: role, and writing the Speak overwrites that are the primary layer of
#: the consent protection (Spec 3.1) -- and a bot invited without it
#: joins happily, mirrors happily, and fails the first intent it is
#: handed. `Send Messages` fails later still: everything records,
#: transcribes and publishes, and only posting the link back to the
#: channel fails.
#:
#: `Speak` is deliberately absent. The bot only ever listens, and the
#: overwrites it writes are about everybody else.
#:
#: **Discord's role position is not in here and cannot be.** The bot's own
#: role has to sit above the consent role or Discord refuses the edit, and
#: no bitmask expresses that -- it is a drag in Server Settings after the
#: invite, which is why the deployment guide says so, and why an intent
#: can still fail with a permission error on a guild that granted every
#: permission on this list.
INVITE_PERMISSIONS = "269487104"


def invite_url(client_id: str) -> str:
    """The `bot`-scope authorize link for this deployment's application.

    The one onboarding step that is genuinely web-doable: this URL is
    public and buildable from the application's client id alone. `api`
    holds no Discord token and never will (Spec 13.2), so everything else
    -- the consent role, the Speak overwrites, the command tree -- is an
    intent the bot applies. This is the link that gets the bot in the door
    so that there is a bot to apply them.

    Refuses a client id that is not a snowflake. It is the one value here
    that reaches a URL somebody is asked to click, and building a link out
    of whatever happened to be configured would put that into a query
    string the console hands an administrator. A misconfigured id is a
    deployment error, and `ApiSettings` raises it at startup rather than
    on the first onboarding page.
    """
    if not client_id.isdigit():
        raise ValueError(
            "A Discord application id is a snowflake, so it is digits and nothing else."
        )
    query = urlencode(
        {
            "client_id": client_id,
            # Space-separated, which `urlencode` renders as `+` -- the
            # spelling Discord's own URL generator produces.
            "scope": " ".join(INVITE_SCOPES),
            "permissions": INVITE_PERMISSIONS,
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"
