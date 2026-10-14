# Design

## Context

See proposal.md — Why. Four facts about the code decide the shape below.

**Commands are synced globally today.** `client.py:657` runs `await
self.tree.sync()` once the cogs are added, and the groups are marked
`@app_commands.guild_only()`. A globally registered command exists in every guild
the bot is in; guild-only restricts DMs, not which guild.

**`require_admin()` is an in-process check.** `permissions.py:56-84` lets a
member through if they hold the Administrator permission or the role stored as
`admin_role_id` in `ConfigStore`. It runs *after* Discord has delivered the
interaction, and it is applied to `/config`, `/queue`, `/audio purge` and
`/setup`.

**`admin_role_id` has a second consumer.** `admin_sync.py:53-60` mirrors the
members of that role into the `admin_member` table, and the console, which signs
in through Outline OAuth (`infrastructure/documents/outline_oauth.py`,
`console/auth.py`), authorizes through that table. Removing the role from the
commands must not remove it from the console.

**`reconcile_guild` is the single chokepoint.** Every path that makes the bot act
in a guild runs through it: `on_guild_join`, `on_shard_ready`, the ten-second
tick loop, `/config apply` and `/setup`. A predicate at its head covers every
route by which a recording could begin.

## Goals / Non-Goals

**Goals:**

- Make the commands exist only where the team is, using Discord's own
  registration scope instead of a reply explaining a refusal.
- Make "who may administer" Discord's decision for commands, so there is one
  place a team admin looks (Server Settings > Integrations).
- Keep a cheap guard against the one configuration mistake that matters (public
  application, foreign invitation), without building isolation machinery.

**Non-Goals:**

- Isolating guilds from each other. There is one team; no promise is made.
- Detecting or reporting *why* the bot is in another guild.
- Making the boundary a list. A list of length one invites a list of two.

## Decisions

### 1. Primary protection is the platform: private application, GUILD_INSTALL only

"Public Bot" off means only the owner can add the application to guilds; the
installation context is configured as GUILD_INSTALL only, with no user install.
Together they remove the invitation path. This is deployment work, written into
`docs/operations.md` and `docs/first-deployment.md`.

**Rejected: enforcing the scope entirely in code, as 2026-09-19 did.** It
reproduced, three times, a decision Discord already makes once, and it would have
to be kept right for as long as the code exists.

**Rejected: relying on the platform alone.** The portal setting is a checkbox
somebody can flip. One guard costs a few lines and a log line (Decision 4).

### 2. Guild-scoped commands, and the old globals cleared

At startup the bot copies its command tree to the configured guild and syncs
there (`tree.copy_global_to(guild=...)`, `tree.sync(guild=...)`), then clears the
global commands and syncs globally (`tree.clear_commands(guild=None)`,
`tree.sync()`). Order matters: sync the guild first so the commands never vanish
from the served guild, then remove the globals. The global removal is what stops
an upgrade from showing every old command, twice, until the global registration
is gone.

Guild command updates apply instantly, so a deployment shows its command set
immediately, which is also why guild registration is preferable to global
registration for a single-guild bot at all.

**Effect on the old design.** A command invoked in a foreign guild cannot happen:
the command is not registered there. The requirement that it reply with a reason
is dropped, not weakened.

**Rejected: keep global sync and refuse in each cog.** It is the self-built
mechanism this change exists to remove, and it leaves commands visible in guilds
that cannot use them.

### 3. `default_member_permissions` instead of `require_admin()`

The admin command groups declare *Manage Server* as their default member
permission. Discord then does not deliver the interaction to a member without it,
and server admins can override per role, user and channel in Server Settings >
Integrations. `require_admin()` is deleted from those commands.

**Does `require_admin()` keep an Administrator fallback? No.** The in-process
check's only unique contribution was the stored role, and the role no longer
decides command access. An Administrator fallback would be a second authority
that disagrees with Integrations the moment an admin restricts a command there:
Discord would hide the command while the code would still admit the same person
by another route. One authority, Discord's, is simpler to reason about and to
explain to the team.

**Accepted risk.** A guild administrator can loosen a command's permission in
Integrations, or restrict it so that nobody can run it. That is the point of the
native mechanism; the team's admins are the guild's admins. The stored
`admin_role_id` is not consulted by commands, so a stale value cannot widen
access.

**Rejected: keep `require_admin()` on top of `default_member_permissions`.** Two
checks that can disagree, one of them invisible to the people who administer the
other.

### 4. One guard at the head of `reconcile_guild`

A pure domain predicate over two guild ids ("does this installation serve this
guild") is called at the head of `reconcile_guild`. A guild that fails it is
reconciled into nothing: no voice connection, no configuration write, no setup.
The refusal is logged with the guild id and the configured id, nothing else.

The predicate lives in `domain` because `tests/test_architecture.py` lets
everything import `domain` and lets `domain` import nothing outward.

**Rejected, and removed from scope: the worker check and the console read
confinement.** Both existed to cover state created *before* enforcement and
stale `admin_member` rows of departed guilds, on behalf of a customer isolation
promise. With one team and a private application there is no foreign data to
defend against, and each adds a configuration variable, a code path and tests
whose only user was the promise. Hence `WorkerSettings` does not need
`STURNUS_GUILD_ID` either.

**Rejected: leaving automatically.** Leaving is destructive, a mistyped
`STURNUS_GUILD_ID` would make the bot leave the guild it is meant to serve, and
undoing it needs a person with *Manage Server*. That good part of the old design
holds unchanged: the bot never leaves a guild on its own.

**Rejected: refusing to start when the bot can see more than one guild.** It
makes availability depend on who invites the bot.

### 5. Configuration, not a database row

`STURNUS_GUILD_ID` is an environment variable on the bot's settings, required
and defaulted to nothing. Stored in the database it would be data the running
system can edit, and the first question after an incident would be who changed it.

### 6. The Outline console login is untouched

`admin_role_id`, `admin_sync.py` and `admin_member` keep working exactly as
before. Moving commands to Discord's permissions changes who may run a command,
not who may open the console. Folding the console's authorization into Discord
too is a separate decision and is not made here.

### Effect on the consent and telemetry-privacy constraints

**Consent.** The consent a person gives is to be recorded by this installation,
in their guild. The guard in Decision 4 keeps the bot from recording a guild
the installation does not serve, so it is a consent control as well as a
configuration check. `STURNUS_GUILD_ID` having no default follows the same
reasoning that gave `STURNUS_TRANSCRIPTION_BASE_URL` none. The consent role and
the packet drop for users without it are not touched.

**Telemetry privacy.** A refusal is logged with guild ids only, never a guild
name (`infrastructure/discord/directory_sync.py` says why: it names an
organisation). It is reported when a guild is reconciled, not per frame or per
packet. Ruff's G/LOG rules still require lazy `%s` logging.

## Risks / Trade-offs

- **`bot_public: false` for a Team-owned application is not documented.** Discord
  says the app owner only; for a Team it does not say whether every member or
  only the owner counts. → Verify once during deployment (invite from a non-owner
  team member, and from an unrelated account) and record the result in
  `docs/first-deployment.md`. The reconcile guard covers the gap meanwhile.
- **A mistyped guild id leaves the bot with no commands in the real guild and a
  refused reconcile for it.** → Nothing is deleted and the bot stays in every
  guild; correcting the variable and restarting restores service. The guard's log
  line names the expected and the seen id.
- **Admin access is now whatever Integrations says.** A guild admin can open
  `/config` to everyone. → Intended; see Decision 3.
- **Stale global commands.** If clearing globals fails, old commands persist.
  → The sync error is logged, the bot continues, and the next start retries.
- **The old design's isolation is gone by decision.** Nothing here makes a
  second guild safe to host; if Sturnus is ever offered externally again, that is
  a new decision, not a configuration.

## Migration Plan

1. Land the predicate, `STURNUS_GUILD_ID` on bot settings, and the guard.
2. Switch command registration to guild scope with global clearing, and add
   `default_member_permissions`, removing `require_admin()` from the commands, in
   one change: splitting leaves commands either unpermissioned or checked twice.
3. Chart, `docs/operations.md`, `docs/first-deployment.md`, and the dated design
   document recording the reversal.

**Before deploying:** set `STURNUS_GUILD_ID`; set "Public Bot" off and
GUILD_INSTALL only in the Developer Portal; check Server Settings > Integrations
afterwards so the team knows where command access now lives. A rollout without
the variable stops the bot at startup, loudly.

**Rollback** is reverting: the previous release's sync re-registers global
commands, and nothing is deleted or migrated. The Developer Portal settings can
stay as they are.

## Open Questions

- Does `bot_public: false` restrict adding a Team-owned application to the
  owner (the Team) or to every Team member? Verify once at deployment.
- Which Manage Server holders should be able to run the admin commands is a team
  matter, set in Integrations after rollout; no default beyond Manage Server is
  proposed.
