# Design

## Context

See proposal.md — Why. Three facts about the code decide the shape below.

**Sharding reaches further than the client class.** `SturnusClient` extends
`commands.AutoShardedBot` (`client.py:312`), but the shard concept is also in
`application/sharding.py`, in `config.py` (`shard_count`, its validator), in the
readiness state of `infrastructure/health.py` (which asks whether "every shard
this process is supposed to hold" is up), in the `shard_id` field of many log
calls (`self._shard(...)`), in `entrypoints/bot.py`, and in the chart. The
readiness change is the one with behaviour: a plain `Bot` has one connection, so
readiness is one flag.

**The per-guild sign-in added a branch rather than replacing one.**
`console/app.py:196` starts a sign-in "against this deployment's client or a
guild's own". With the guild option gone, `begin()` takes no argument and the
`UnknownSignIn` 404 path (designed so the response cannot be used to enumerate
tenants) has nothing left to hide.

**Not everything from migration 0013 is multi-guild.** 0013 was deliberately one
migration for a whole round of features. Only some of its tables and columns exist
because of tenancy.

## Goals / Non-Goals

**Goals:**

- Delete what a single guild makes unreachable, and only that.
- Leave the deployment-wide Outline sign-in exactly as it is.
- Make the irreversible step explicit, ordered after the change that makes it
  safe, and guarded by a backup.

**Non-Goals:**

- Simplifying the console's guild switcher or any other UI.
- Touching #149 and #150.
- Changing how many bot pods run; sharding never allowed more than one.

## Decisions

### 1. Remove, do not disable

Both features could be left in place behind configuration that is never set.
That keeps the cost this change is about: tests asserting behaviour the
deployment cannot exhibit, and branches in authentication code that every future
change has to reason about. They also keep a table that stores wrapped OAuth
client secrets.

**Rejected: keep sharding as an option "in case the team grows".** Growth to
2,500 guilds is not a plausible path for a bot only the owner's team uses, and
the work to reintroduce it is in git history. If the premise reverses, that is a
new decision.

### 2. `AutoShardedBot` becomes `Bot`, readiness becomes one flag

The per-shard hooks and the `shards_this_process_owns` rule are deleted;
`ReadinessState` reports ready when the single connection is up. The reconcile
that `on_shard_ready` carries moves back to `on_ready`, where it fires once per
(re)IDENTIFY as plain discord.py behaves. The ten-second tick loop remains the
safety net, as it already was.

### 3. Drop the table and the column, in one revision, and say it cannot be undone

A new revision after the current head executes `DROP TABLE guild_oauth_client`
and drops `console_state.guild_id`. `downgrade` recreates the empty table and the
nullable column so that the revision graph can be walked backwards, but it cannot
restore rows or the wrapped secrets in them. The docstring says so in the first
paragraph, in the style of the existing migrations.

**Why drop `console_state.guild_id` too.** Its only reader was the per-guild
code-exchange branch. Leaving a nullable column nobody writes is the kind of
unexplained residue this change exists to remove.

**Rejected: leave the table and delete only the code.** The table holds wrapped
secrets; keeping them while nothing can read them is worse than either deleting
or using them.

**Rejected: a soft step first (stop reading, drop later).** With one team and a
backup it only adds a release in which the schema and the code disagree.

### 4. Other 0013 structures are listed, not removed

Each was checked against the question "is it unreachable with one guild?":

| Structure | Verdict |
|---|---|
| `guild_oauth_client`, `console_state.guild_id` | Removed: only the per-guild sign-in uses them |
| `guild` (names) | Stays: the console still needs the guild's name |
| `guild_export_target`, `session_document` | Stay: #150, multiple destinations for one guild |
| `guild_setup_intent` | Stays: #149, one guild still needs setting up from the console |
| `GET /api/guilds` switcher | Candidate only: it lists one entry, harmless |

Anything not clearly unreachable stays; a follow-up can remove candidates once
someone has looked at the console as a one-guild product.

### Effect on the consent and telemetry-privacy constraints

**Consent.** None. Consent role, deny-Speak overwrites and the packet drop are
independent of shards and of the sign-in client.

**Telemetry privacy.** Removing the `shard_id` log field removes a field, never
adds one; no logging call gains data. The cleanup must keep every touched log
call in lazy `%s` style (Ruff G/LOG).

## Risks / Trade-offs

- **Irreversible data loss of `guild_oauth_client`.** → Precondition and backup
  in the Migration Plan; the rows are only meaningful if someone configured a
  per-guild client, which for a single-team installation they will not have.
- **Readiness semantics change.** Behaviour is the same for one connection; the
  per-shard tests that encoded the multi-shard rule are deleted. → Keep the tests
  for connect, disconnect and the 30-second tolerance that Kubernetes relies on.
- **Wrong deploy order.** If this ships before `single-guild-instance`, a second
  guild could still be reconciled, now with no per-guild sign-in. → Stated as a
  precondition in the proposal and checked in task 1.1.
- **Removal wider than the diff looks.** About 4,300 lines across many files
  invites an accidental deletion of #149/#150 code. → Delete by the named
  modules, keep the full suite green after each group of tasks.

## Migration Plan

**Preconditions, all required:**

1. `single-guild-instance` is deployed and `STURNUS_GUILD_ID` is set.
2. `STURNUS_SHARD_COUNT` is unset in the deployment values (it is ignored from
   this release on, but unsetting it first avoids a silent no-op).
3. `SELECT count(*) FROM guild_oauth_client` is checked and its result noted. A
   non-zero count is a question for the owner, not something the migration
   resolves.
4. **A database backup exists.** `DROP TABLE` cannot be undone; the backup is the
   only rollback for the table's data.

Order of work: delete the per-guild OAuth code, then the sharding code, then add
the migration last, so that every commit before it leaves the schema compatible
with the previous release. The migration runs in the worker at startup, as every
migration does.

**Rollback** of the code is a revert; of the table, only a restore from the
backup. The empty-shape `downgrade` exists so Alembic can step back, not to bring
data back.

## Open Questions

- Does the console's guild switcher (`GET /api/guilds`) deserve its own
  simplification change once the product is known to be single-guild?
- Does the team want `docs/superpowers/specs/2026-08-23-sturnus-phase-2.md`
  annotated, since it describes the sharding and per-guild sign-in decisions
  this change removes, or left as history?
