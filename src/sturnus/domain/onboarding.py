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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: The bot did what was asked.
APPLIED = "applied"

#: The bot tried and could not -- `error` says what Discord answered.
#: Terminal, not a retry: see the module docstring.
FAILED = "failed"

#: What may be written into `guild_setup_intent.outcome`. Plain strings
#: rather than a database enum for the reason `guild_channel.kind` is
#: one: a value this code has never seen must be a row a reader can
#: ignore rather than a write that fails inside a reconcile tick.
OUTCOMES = frozenset({APPLIED, FAILED})


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
