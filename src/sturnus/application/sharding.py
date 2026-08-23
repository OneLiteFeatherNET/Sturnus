"""How many gateway connections this process holds, and which guilds are its business.

Discord splits a bot's guilds across *shards*: a shard is one gateway
connection, and a guild belongs to shard `(guild_id >> 22) % shard_count`.
Discord requires sharding above 2500 guilds; below that it is headroom and
resilience, because one shard's reconnect no longer stalls every other
guild's events behind it.

**There are two different changes hiding under the word "sharding", and
this module exists to keep them apart.**

*Stage one -- several shards in one process.* `discord.AutoShardedClient`
opens N gateway connections from a single process and routes each guild's
events down the connection that owns it. **It changes no deployment
invariant**, because there is still exactly one process: one voice
connection per guild, one PVC of in-progress recordings, one announcement
poll. That is what Sturnus does today, and `STURNUS_SHARD_COUNT` is its
only knob.

*Stage two -- several processes, each owning a shard range.* That one does
break an invariant this repository states out loud.
`charts/sturnus/templates/bot-deployment.yaml` pins `replicas: 1` with
`strategy: Recreate` precisely because two bot processes would hold two
gateway connections to the same guilds and record every session twice.
Stage two makes that safe only by giving each process a *disjoint* shard
range -- plus a StatefulSet for stable ordinals, a per-pod PVC, and an
announcement poll that no longer speaks for guilds it cannot see.
**Sturnus has not built stage two.** `docs/operations.md` section 3.6
lists what it would take.

**The predicate below is the seam.** `process_serves_guild` is `True` for
every guild today because `shards_this_process_owns` returns every shard
there is. Stage two narrows that one function to this pod's range, and the
sweeps that ask the predicate start declining guilds that belong to another
pod -- rather than each pod posting the same document link.

Which sweeps ask, and which do not, is itself a decision worth writing
down:

- **The announcement poll asks** (`publishing.announce_ready_sessions`).
  It reads `sessions` rows for *every* guild out of the database, which is
  the only sweep in the bot whose input is not already scoped by what this
  process's gateway cache holds. Under stage two, without the predicate,
  four pods would each post the same link four times.
- **The guild tick, the administrator mirror and the directory mirror do
  not need to ask.** They iterate `SturnusClient.guilds` -- the gateway
  cache -- which under stage two already contains exactly this process's
  shards' guilds and nothing else. Adding a guard there would be a check
  that can never fail, which is worse than no check: it reads as though
  something were being enforced.
- **Orphan recovery and the retention sweep do not need to ask either.**
  Both work off this pod's own recording directory, and stage two gives
  each pod its own.

Standard library only, like every module under `sturnus.application` that
`sturnus.domain` neighbours: `tests/test_architecture.py` forbids reaching
into `sturnus.infrastructure` from here, and nothing in this file wants to.
"""

from __future__ import annotations

from typing import Final

#: Discord's own routing rule shifts a snowflake right by this many bits
#: before taking the shard modulus: the low 22 bits are a snowflake's
#: worker, process and sequence counters, which carry no guild identity.
_SNOWFLAKE_SHARD_SHIFT: Final = 22

#: The smallest shard count that describes a running bot. Zero shards is
#: not a smaller deployment, it is a process that never connects, and
#: `sturnus.config.Settings` refuses it at startup rather than letting the
#: gateway reject it later.
MIN_SHARD_COUNT: Final = 1

#: Whether one bot process holds every shard Sturnus's identity has.
#:
#: `True`, and it is the assumption behind `replicas: 1`. It is written
#: here as a name rather than left implicit in a dozen sweeps for the same
#: reason `channel_choice.MAX_CONCURRENT_SESSIONS_PER_GUILD` is: the value
#: of making an assumption single-sited is that lifting it becomes a list
#: somebody can read.
#:
#: Note what it is *not*: it is not "there is only one shard". Sturnus may
#: run four shards today, and this stays `True` -- all four are in this
#: process. It is `False` only once shard ranges are split across pods.
PROCESS_HOLDS_EVERY_SHARD: Final[bool] = True


def shard_of(guild_id: int, shard_count: int | None) -> int:
    """The shard id Discord routes this guild's events to.

    `(guild_id >> 22) % shard_count`, which is Discord's published rule and
    the same arithmetic `discord.Guild.shard_id` performs. Reimplemented
    here rather than read off a `Guild` object because the callers that
    need it most -- a tick for a guild that has just left the cache, a log
    line that has only an id -- have no `Guild` to ask.

    `None` means the gateway has not told this process its shard count yet
    (`discord.Client.shard_count` before `launch_shards` has run). Every
    guild is on shard 0 in that window, which is what `Guild.shard_id`
    answers too.
    """
    if shard_count is None or shard_count <= MIN_SHARD_COUNT:
        return 0
    return (guild_id >> _SNOWFLAKE_SHARD_SHIFT) % shard_count


def shards_this_process_owns(shard_count: int | None) -> frozenset[int]:
    """Every shard id this process holds a gateway connection for.

    **This is the one function stage two changes.** Today it returns all of
    them, because `PROCESS_HOLDS_EVERY_SHARD`. Under stage two it would
    return this pod's slice -- derived from a StatefulSet ordinal and the
    cluster-wide shard count -- and `process_serves_guild` would start
    answering `False` for guilds another pod owns without a single call
    site changing.
    """
    if not PROCESS_HOLDS_EVERY_SHARD:  # pragma: no cover - stage two
        raise NotImplementedError(
            "Splitting shards across processes needs a shard range per pod; "
            "see docs/operations.md section 3.6."
        )
    if shard_count is None:
        return frozenset({0})
    return frozenset(range(shard_count))


def process_serves_guild(guild_id: int, shard_count: int | None) -> bool:
    """Whether this process is the one responsible for `guild_id`.

    Asked by every sweep whose input is the *database* rather than this
    process's gateway cache -- see the module docstring for which those
    are and why the others deliberately do not ask.

    Always `True` today. That is not a reason to skip the call: the call
    is what makes the assumption a thing the code states rather than a
    thing it happens to get away with.
    """
    return shard_of(guild_id, shard_count) in shards_this_process_owns(shard_count)


def shard_id_for_logging(guild_id: int, shard_count: int | None) -> int | None:
    """This guild's shard id, or `None` while this process holds only one shard.

    Deliberately conditional. `shard_id` is a pure function of `guild_id`
    and the shard count, so on a single-shard process it is `0` on every
    line for ever: a key in every Loki stream that answers no question
    anybody could ask, and cardinality spent on a constant.

    Once there is more than one shard it stops being a constant and starts
    being the grouping an operator actually wants -- "shard 3 just
    dropped, which guilds went with it" -- which LogQL cannot compute from
    `guild_id`, because it has no `>>` and no `%`. So the field earns its
    place exactly when there is more than one shard, and not before.

    `None` rather than an absent key because rule R3 in
    `tests/test_logging_discipline.py` forbids `**kwargs` into a log event
    -- every emitted field name must stay statically readable at its call
    site -- and `sturnus.observability.redaction.scrub_fields` drops a
    `None` value rather than writing `null`. Between them, a call site
    names the field in source and the line carries it only when it says
    something.
    """
    if shard_count is None or shard_count <= MIN_SHARD_COUNT:
        return None
    return shard_of(guild_id, shard_count)
