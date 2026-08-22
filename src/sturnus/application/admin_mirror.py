"""Whether, and how, to mirror a guild's administrators for the console.

The console's API process has no Discord gateway (Spec 13.2, and the
console design's Section 2.1), so it cannot ask who holds `admin_role_id`.
The bot mirrors that membership into `admin_member` on the sweep it
already runs.

What is *decided* about that sweep lives here, apart from the gateway
lookup and the database write, because every case that matters is a
decision and none of them needs a Discord connection to exercise: a guild
that never configured a role, a role that has since been deleted, a stored
value that is not a role id at all.

The distinction the whole module turns on is **skip versus clear**, and it
is not cosmetic:

- *Skip* leaves whatever is mirrored alone. It is for a guild that has not
  finished `/setup` -- it has no administrators *yet*, which is not the
  same as having none.
- *Clear* removes every mirrored row. It is for a role that was
  configured and no longer exists, where the alternative is a standing
  privilege: nobody holds a deleted role, so its former members would
  administer the console forever.

Collapsing the two would make an unconfigured guild indistinguishable
from a deliberate removal.
"""

from __future__ import annotations

from enum import Enum


class AdminSyncDecision(Enum):
    """What the sweep should do with one guild's mirrored administrators."""

    #: Read the role's members and write them.
    SYNC = "sync"
    #: Leave the mirror untouched -- nothing is configured to mirror.
    SKIP = "skip"
    #: Remove every mirrored row -- what was configured no longer grants
    #: anything, and the mirror must not keep granting on its behalf.
    CLEAR = "clear"


def decide_admin_sync(*, configured_role_id: str | None, role_exists: bool) -> AdminSyncDecision:
    """Decides from the stored setting and whether that role still exists.

    `configured_role_id` is read leniently -- whitespace and leading
    zeroes come from hand-editing `guild_config` and from copy-pasting out
    of Discord, and neither changes which role is meant.

    A value that cannot be a role id at all clears rather than raises.
    `guild_config` stores text and `admin_role_id` is not among
    `INTEGER_KEYS`, so a hand-edited row can hold anything; treating
    unparseable as "no valid role" errs towards removing access rather
    than granting it, which is the only safe direction for a value nobody
    can interpret.
    """
    if configured_role_id is None or not configured_role_id.strip():
        return AdminSyncDecision.SKIP
    try:
        int(configured_role_id.strip())
    except ValueError:
        return AdminSyncDecision.CLEAR
    return AdminSyncDecision.SYNC if role_exists else AdminSyncDecision.CLEAR
