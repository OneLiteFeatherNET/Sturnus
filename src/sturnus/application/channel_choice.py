"""Which of a guild's allowed channels Sturnus records, and how many of them.

A guild names a *list* of channels Sturnus may record in
(`settings.VOICE_CHANNEL_IDS`), so that a meeting can happen in whichever
room suits it. It does not follow that Sturnus records all of them: **a
Discord bot holds one voice connection per guild.** That is a platform
limit -- discord.py enforces it and `infrastructure.discord.voice` holds a
single connection slot -- not a design decision, and no amount of
configuration changes it. Recording two rooms of one guild at the same
time needs a second bot identity.

That limit is spelled once, here, as `MAX_CONCURRENT_SESSIONS_PER_GUILD`,
and everything that used to *assume* it now *asks* it. This module
therefore answers two questions rather than one: which rooms are worth
recording, in what order (`choose_channels`), and how many of them may be
served at this moment (`ChannelSelection.take`). With the limit at one the
answer to the second is one, and the rooms after the first are told they
are waiting.

So on any tick where two allowed channels both hold consenting members,
something has to choose, and the choice has to be the *same* one every
time it is asked. An arbitrary order -- whatever `dict` iteration or
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

The rule orders *every* busy room, not only the first one. Ranking past
first place is what a second bot identity would consume, and it is already
worth having with one: the rooms left waiting are reported in the order
they would be picked up, so `/config show` names the next room in line
rather than an arbitrary list.

Nothing here knows about Discord, sessions or configuration -- it takes
headcounts and returns a decision, which is what lets every row of the
rule be pinned down in `tests/application/test_channel_choice.py` without
a gateway.

**A session in progress is not re-decided.** This function is asked only
about rooms that are idle. Once a session is open, the channel it is open
against is the channel it stays in until it closes -- moving it would mean
a `sessions` row naming one room while the audio came from another. The
caller (`SturnusClient._sync_participants`) is what enforces that; it is
recorded here because it is the assumption the rule above is safe under.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: How many of one guild's rooms Sturnus can record at the same moment.
#:
#: **One, and this is not a tuning knob.** One bot identity holds one
#: voice connection per guild: discord.py refuses a second, and
#: `infrastructure.discord.voice.VoiceReceiveAdapter` holds a single
#: connection slot, so a second adapter for the same guild would orphan
#: one of the two connections with nothing left to disconnect it.
#:
#: Raising this number alone therefore breaks the bot rather than
#: improving it. What lifting the limit actually takes:
#:
#: 1. A **second Discord application and a second deployment** -- a second
#:    token, its own gateway session, its own voice connection. Nothing in
#:    this repository can conjure that, and nobody has decided to run one.
#: 2. Whatever the type checker and the test suite then point at. That is
#:    the reason this constant exists at all: the runtime is keyed per
#:    room and asks this value rather than assuming it, so the work left
#:    is a list somebody can read rather than a rewrite they have to
#:    discover.
#:
#: Two things do **not** follow from a second identity and must be decided
#: deliberately when one arrives: the capture-failure cooldown (guild-wide
#: today, because it is a property of one process's connection -- see
#: `REJOIN_COOLDOWN` in `infrastructure.discord.client`) and deferred
#: configuration changes (guild-wide, because the configuration is one row
#: set, so a change may only land when *every* room it touches is idle).
MAX_CONCURRENT_SESSIONS_PER_GUILD = 1


@dataclass(frozen=True)
class ChannelRanking:
    """One busy channel and the headcount that earned it its place."""

    channel_id: int
    consenting: int


@dataclass(frozen=True)
class ServedChannels:
    """Which rooms a caller may record now, and which are left waiting.

    `serving` is empty when no allowed channel holds a consenting member
    -- the ordinary, silent case, and not the same as "the answer could
    not be read", which the caller handles by not asking at all.

    `waiting` names the busy channels that are not being served, in the
    order they would be picked up. It exists so somebody sitting in the
    second room can be told why nothing is happening: an unexplained
    silent bot is indistinguishable from a broken one.
    """

    serving: tuple[ChannelRanking, ...]
    waiting: tuple[int, ...]


@dataclass(frozen=True)
class ChannelSelection:
    """Every allowed channel that holds a consenting member, best first.

    An *ordering*, not a decision: how many of these rooms are actually
    recorded is a question about voice connections, which this module
    answers separately in `take` rather than baking into the ranking.
    """

    ranked: tuple[ChannelRanking, ...]

    def take(self, limit: int) -> ServedChannels:
        """The first `limit` rooms to record, and the ids of the rest.

        `limit` is `MAX_CONCURRENT_SESSIONS_PER_GUILD` for every caller
        there is today, and passing it in rather than reading it here is
        deliberate: the ranking is a fact about the meetings, the limit is
        a fact about the process recording them, and a test may hand this
        a two to show that the ordering past first place is real.
        """
        served = self.ranked[:limit]
        return ServedChannels(
            serving=served,
            waiting=tuple(ranking.channel_id for ranking in self.ranked[limit:]),
        )


def choose_channels(consenting_per_channel: Mapping[int, int]) -> ChannelSelection:
    """Orders the allowed channels by the headcount each of them holds.

    `consenting_per_channel` maps a channel id to how many members
    carrying the consent role are sitting in it right now. Channels the
    process cannot see are simply absent from it -- an unreadable channel
    is not a channel with nobody in it, and the caller drops it before
    asking rather than passing a zero that would read as "everybody left".

    An empty channel is not ranked at all: there is no meeting in it, so
    it is neither served nor waiting.
    """
    busy = {channel_id: count for channel_id, count in consenting_per_channel.items() if count > 0}
    # Most consenting members first, lowest id to break the tie. Negating
    # the count rather than reversing the sort keeps the id ascending
    # within a tie, which is the half that makes the answer stable.
    ordered = sorted(busy, key=lambda channel_id: (-busy[channel_id], channel_id))
    return ChannelSelection(
        ranked=tuple(ChannelRanking(channel_id, busy[channel_id]) for channel_id in ordered)
    )


__all__ = [
    "MAX_CONCURRENT_SESSIONS_PER_GUILD",
    "ChannelRanking",
    "ChannelSelection",
    "ServedChannels",
    "choose_channels",
]
