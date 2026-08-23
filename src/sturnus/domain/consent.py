"""Consent resolution.

The Discord role is the first line of defense, but not the only one:
users with administrator rights bypass channel permissions and could
speak without the role. That's why the stored record always decides too.

**A record says what it covers, and when it stops.** Two things this
module used to leave implicit are now written into the record itself:

* `scope` -- being recorded is not one thing. Audio and video are
  different amounts of a person's presence in a room, and a grant that
  cannot tell them apart is a grant that answers a question nobody was
  asked. See `ConsentScope`, and see `may_record_video` for what the
  system does with the answer today, which is: refuse to ask Discord for
  a stream nobody offered it.
* `revoked_at` as an *instant* rather than a tombstone. "Withdraw from
  the end of the month" and "withdraw as of Tuesday's meeting" are
  ordinary things to want, and a column that only ever holds `now()`
  cannot express either.

The second of those is why every function here takes `now`. The domain
has no clock and must not grow one -- a rule that decides "is this
consent in force" from a time it read itself is a rule no test can pin
and no caller can reason about. The clock is threaded in from the
process that has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ._time import require_aware as _require_aware


class ConsentScope(StrEnum):
    """What a person agreed to have recorded.

    One spelling, in one place, because these two strings travel through
    a database column, a JSON body and a metric label -- and a literal
    `"audio_video"` typed out at any one of those is a mismatch nothing
    catches until a grant silently reads as narrower than it is.

    **Ordered by width, and only two wide.** `AUDIO_VIDEO` includes
    audio; there is no video-without-audio scope, because Sturnus records
    a meeting rather than a screen and a protocol written from a silent
    screen recording is not a thing anybody asked for.
    """

    AUDIO = "audio"
    AUDIO_VIDEO = "audio_video"


def scope_of(value: str | None) -> ConsentScope:
    """Reads a stored scope, and reads anything unrecognised as `AUDIO`.

    The narrow direction is the safe one and the only defensible one. A
    row carrying a scope this code cannot name is a row about which
    nothing is known, and the two ways to be wrong about it are not
    symmetric: reading it as `AUDIO` costs somebody a capability they
    asked for and can ask for again, while reading it as `AUDIO_VIDEO`
    would have the bot ask Discord for a stream on the strength of a
    string it does not understand.

    `None` -- a column that has not been backfilled, a row read through
    an older mapping -- resolves the same way and for the same reason.
    """
    if value is None:
        return ConsentScope.AUDIO
    try:
        return ConsentScope(value)
    except ValueError:
        return ConsentScope.AUDIO


@dataclass(frozen=True)
class ConsentRecord:
    granted_at: datetime | None
    revoked_at: datetime | None
    policy_version: str | None
    #: Defaulted, and to the narrower of the two on purpose. Every row
    #: written before the column existed was given for audio, which is
    #: exactly what the migration backfills -- and a record that does not
    #: say what it covers must read as covering less rather than more.
    scope: ConsentScope = ConsentScope.AUDIO


def is_consent_active(
    record: ConsentRecord | None,
    current_policy_version: str,
    now: datetime,
) -> bool:
    """Consent expires through revocation and through a changed policy.

    `revoked_at` is an effective instant, not a tombstone: a revocation
    dated next week leaves consent in force until then, and one dated
    last Tuesday has already ended it. `now < revoked_at` is the whole
    rule, and the instant itself is the moment it stops -- a revocation
    effective at exactly `now` is effective.

    `now` is a parameter rather than a reading this function takes for
    itself. See the module docstring.
    """
    _require_aware(now)
    if not current_policy_version:
        return False
    if record is None or record.granted_at is None:
        return False
    if record.revoked_at is not None and now >= record.revoked_at:
        return False
    return record.policy_version == current_policy_version


def may_record(
    record: ConsentRecord | None,
    current_policy_version: str,
    has_consent_role: bool,
    now: datetime,
) -> bool:
    return has_consent_role and is_consent_active(record, current_policy_version, now)


def may_record_video(
    record: ConsentRecord | None,
    current_policy_version: str,
    has_consent_role: bool,
    now: datetime,
) -> bool:
    """Everything `may_record` requires, and a scope that names video.

    **Nothing in this repository records video, and this function does
    not change that.** What it decides is narrower and comes first:
    whether the bot may *ask Discord for* a person's stream at all
    (`sturnus.infrastructure.discord.voice`). Asking for a stream from
    somebody who said no is the thing to stop; discarding it after it
    arrives is not the same act, and a person watching their client show
    "someone is watching your camera" would be right to say so.

    The scope is built before the capability deliberately: a system must
    be able to record that somebody said no before it acquires the
    ability to do the thing they said no to. Building it the other way
    round means a window in which the only available answer is yes.
    """
    return (
        record is not None
        and record.scope == ConsentScope.AUDIO_VIDEO
        and may_record(record, current_policy_version, has_consent_role, now)
    )
