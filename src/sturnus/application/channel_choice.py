"""Which of a guild's allowed channels Sturnus records, when more than one is busy.

A guild names a *list* of channels Sturnus may record in
(`settings.VOICE_CHANNEL_IDS`), so that a meeting can happen in whichever
room suits it. It does not follow that Sturnus records all of them: **a
Discord bot holds one voice connection per guild.** That is a platform
limit -- discord.py enforces it and `infrastructure.discord.voice` holds a
single connection slot -- not a design decision, and no amount of
configuration changes it. Recording two rooms of one guild at the same
time needs a second bot identity.

So on any tick where two allowed channels both hold consenting members,
something has to choose one, and the choice has to be the *same* one every
time it is asked. An arbitrary choice -- whatever `dict` iteration or
gateway cache order happened to yield -- would be worse than a bad rule:
two passes moments apart could disagree, and the bot would hop between
rooms, opening a session row in each and recording neither meeting whole.

The rule, in order:

1. **Most consenting members first.** The larger group is the more likely
   meeting, and the people who lose are the fewer.
2. **Lowest channel id breaks a tie.** Not because a low id means
   anything, but because it is stable, needs nothing but the ids
   themselves, and cannot be affected by cache ordering or by which member
   happened to emit the last voice-state update.

Nothing here knows about Discord, sessions or configuration -- it takes
headcounts and returns a decision, which is what lets every row of the
rule be pinned down in `tests/application/test_channel_choice.py` without
a gateway.

**A session in progress is not re-decided.** This function is asked only
while the guild is idle. Once a session is open, the channel it is open
against is the channel it stays in until it closes -- moving it would mean
a `sessions` row naming one room while the audio came from another. The
caller (`SturnusClient._sync_participants`) is what enforces that; it is
recorded here because it is the assumption the rule above is safe under.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelChoice:
    """Which allowed channel to record, and which ones are left waiting.

    `channel_id` is `None` when no allowed channel holds a consenting
    member -- the ordinary, silent case, and not the same as "the answer
    could not be read", which the caller handles by not asking at all.

    `waiting` names the other allowed channels that *do* hold consenting
    members and are not being served. It exists so somebody sitting in the
    second room can be told why nothing is happening: an unexplained
    silent bot is indistinguishable from a broken one.
    """

    channel_id: int | None
    consenting: int
    waiting: tuple[int, ...]


def choose_channel(consenting_per_channel: Mapping[int, int]) -> ChannelChoice:
    """Picks the one channel to record from the headcount of each allowed channel.

    `consenting_per_channel` maps a channel id to how many members
    carrying the consent role are sitting in it right now. Channels the
    process cannot see are simply absent from it -- an unreadable channel
    is not a channel with nobody in it, and the caller drops it before
    asking rather than passing a zero that would read as "everybody left".
    """
    busy = {channel_id: count for channel_id, count in consenting_per_channel.items() if count > 0}
    if not busy:
        return ChannelChoice(channel_id=None, consenting=0, waiting=())
    # Most consenting members first, lowest id to break the tie. Negating
    # the count rather than reversing the sort keeps the id ascending
    # within a tie, which is the half that makes the answer stable.
    chosen = min(busy, key=lambda channel_id: (-busy[channel_id], channel_id))
    return ChannelChoice(
        channel_id=chosen,
        consenting=busy[chosen],
        waiting=tuple(sorted(channel_id for channel_id in busy if channel_id != chosen)),
    )


__all__ = ["ChannelChoice", "choose_channel"]
