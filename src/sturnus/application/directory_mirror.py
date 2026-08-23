"""What the bot mirrors of a guild so that `api` can name things.

`api` has no Discord token and must never be given one (the console
design's Section 2.1): it already holds S3 and the master key, so it can
decrypt every recording ever made, and that is not a process to also hand
the ability to act as the bot (Spec 13.2). It therefore cannot ask what
channel `1234...` is called, which is why the console shows an
administrator the same snowflake they pasted in. `bot` holds the gateway,
so `bot` writes the names down -- exactly the arrangement
`sturnus.application.admin_mirror` already established for `admin_member`.

What is *decided* about that sweep lives here, apart from the gateway
reads and the database writes, for the same reason it does there: every
case that matters is a decision, and none of them needs a Discord
connection to exercise.

**The three things carried, and why those three.** Channels and roles
because they are what `voice_channel_id`, `consent_role_id` and
`admin_role_id` point at. Members because a consent roster, the speakers
in a queue and an administrator list all name people -- but only the
holders of the consent role and the admin role, never the guild's whole
membership. See `members_to_mirror`.

**Skip versus clear, again.** `admin_mirror` draws the distinction for
one role; the same distinction applies here and means the same thing. A
guild the bot cannot currently see is skipped by the caller rather than
written empty, because "we could not look" is not "there is nothing
there" -- and a mirror emptied on a gateway hiccup would make the console
stop naming anything until the next sweep landed. A guild that has
configured neither naming role is likewise skipped: mid-`/setup` is not
the same fact as a guild whose roles have nobody in them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

#: The two channel kinds this code writes into `guild_channel.kind`.
#: Stored as plain strings, never an enum: Discord keeps adding channel
#: types, and a type this code has never seen must be a row a reader
#: ignores rather than a failed write that takes the sweep with it.
VOICE = "voice"
TEXT = "text"


@dataclass(frozen=True, slots=True)
class MirroredChannel:
    """One channel, as the console will eventually offer it.

    `position` is carried because it is the order Discord itself shows
    channels in. A picker that reorders them is a picker that does not
    look like the server it configures, and an administrator comparing it
    against Discord side by side would have to read every entry.
    """

    channel_id: int
    name: str
    kind: str
    position: int


@dataclass(frozen=True, slots=True)
class MirroredRole:
    role_id: int
    name: str
    position: int


@dataclass(frozen=True, slots=True)
class MirroredMember:
    discord_user_id: int
    display_name: str


class DirectorySyncDecision(Enum):
    """What the sweep should do with one of a guild's mirrored lists."""

    #: Read it from the gateway and write it, empty included.
    SYNC = "sync"
    #: Leave whatever is mirrored alone -- there is nothing configured to
    #: mirror yet, which is not the same as there being nothing.
    SKIP = "skip"


def parse_role_id(configured: str | None) -> int | None:
    """A stored role setting read leniently, or `None` if it is not one.

    Whitespace and leading zeroes come from hand-editing `guild_config`
    and from copy-pasting out of Discord, and neither changes which role
    is meant.

    A value that cannot be a role id at all comes back as `None` rather
    than raising. `guild_config` stores text, so a hand-edited row can
    hold anything, and a value nobody can interpret must not stop a sweep
    that has other work to do.
    """
    if configured is None or not configured.strip():
        return None
    try:
        return int(configured.strip())
    except ValueError:
        return None


def decide_member_mirror(configured_role_ids: Iterable[str | None]) -> DirectorySyncDecision:
    """Whether to write this guild's mirrored member names at all.

    SKIP only when not one of the naming roles has been configured. Such
    a guild is mid-`/setup`: it has no consent roster and no
    administrators *yet*, and writing an empty membership would make that
    indistinguishable from a guild whose roles genuinely have nobody in
    them.

    Once at least one role is configured, SYNC -- including when the
    result is empty. A configured role that was deleted, or that has lost
    its last member, must stop naming people rather than go on naming
    them from a mirror nothing ever refreshes. That is the same
    skip-versus-clear boundary `admin_mirror.decide_admin_sync` draws,
    read across the two roles the console names people from.
    """
    if any(value and value.strip() for value in configured_role_ids):
        return DirectorySyncDecision.SYNC
    return DirectorySyncDecision.SKIP


def members_to_mirror(*groups: Iterable[MirroredMember]) -> Sequence[MirroredMember]:
    """The bounded set of people the console is allowed to name.

    Callers pass the consent role's members and the admin role's members;
    this is their union, deduplicated by id and ordered by id so two
    sweeps of an unchanged guild produce the same write.

    **The bound is the point, not an optimisation.** The alternative --
    mirroring every member of the guild -- would copy a Discord user
    directory into a database that exists to hold recordings of meetings,
    covering people who never joined a recorded channel and consented to
    nothing. Every page that names a person names one of these two role
    memberships: a consent roster, the speakers in a queue, an
    administrator list. A person this does not cover is a person the
    console shows as an id, which is the intended outcome rather than a
    gap.

    Where the same person appears twice -- somebody who both consented
    and administers -- the first group's name wins. Both come from the
    same `Member.display_name` in the same sweep, so they agree; fixing
    an order anyway means a tie is never resolved by dictionary
    iteration order.
    """
    seen: dict[int, MirroredMember] = {}
    for group in groups:
        for member in group:
            seen.setdefault(member.discord_user_id, member)
    return sorted(seen.values(), key=lambda member: member.discord_user_id)
