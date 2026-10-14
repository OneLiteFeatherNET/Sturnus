# Proposal

## Why

**Decided by the repository owner, 2026-10-14, reversing the reason and keeping
the scope.** The decision of 2026-09-19 said that one installation serves exactly
one guild, and it gave as its reason that Sturnus was being offered to external
customers whose isolation had to be promised. That reason is void: Sturnus is
used only by the owner's own team and is not offered to anyone else. The scope
decision stands — one installation serves one guild — and the reason for it is
now simply that a single team has a single guild, so everything built to serve
several is machinery with no user.

The reversal also changes *how* the scope is held. The 2026-09-19 design built
its own enforcement in three places (gateway, worker, console) because it had to
back an isolation promise to strangers. With no such promise, the project's
preference applies: established standards and, for Discord, the native features
of the developer documentation, over self-built mechanisms. Discord already has
the three things the old design hand-rolled:

- **A private application.** With `bot_public` off ("Public Bot" off in the
  Developer Portal) only the application owner can add the application to a
  guild
  (<https://docs.discord.com/developers/resources/application>). That removes
  the invitation, which is the failure the old design spent three checks on.
- **Guild-scoped commands.** Commands registered under
  `/applications/{app}/guilds/{guild}/commands` are "available only within the
  guild specified" and updates apply instantly
  (<https://docs.discord.com/developers/interactions/application-commands>).
  A command that does not exist in a foreign guild needs no reply explaining
  why.
- **Command permissions.** `default_member_permissions` lets Discord decide who
  may run a command, and server admins fine-tune it per role, user and channel
  in Server Settings > Integrations (same page). Discord does not deliver the
  interaction to a member who lacks the permission.

**The failure this prevents is narrower than before, and so is the remedy.**
Nothing today stops the bot from being invited to a second guild and
reconciling it: it would create configuration, record sessions and write them
into the team's database and bucket. The primary protection is now Discord's —
a private application nobody else can invite. One cheap guard in the bot
remains, because a misconfigured portal setting should cost a log line and not a
foreign recording.

## What Changes

- **BREAKING for operators:** `STURNUS_GUILD_ID` becomes required on the bot's
  settings, with no default. A bot started without it fails to construct its
  settings and does not run. `WorkerSettings` does not gain it (see *Out of
  scope*).
- The bot registers its application commands **only for that guild**
  (`tree.copy_global_to(guild=...)` then `tree.sync(guild=...)`, discord.py
  2.7.1) and removes the commands it previously registered globally
  (`tree.clear_commands(guild=None)` then `tree.sync()`), so a global command
  from an earlier release disappears instead of lingering in every guild the bot
  is in. This replaces the global `await self.tree.sync()` at
  `client.py:657`. Commands therefore do not exist in any other guild, and the
  old requirement that a command in a foreign guild answers with a reason is
  dropped.
- **Commands carry `default_member_permissions`.** The admin groups `/config`,
  `/queue`, `/audio purge` and `/setup` require *Manage Server*; `/consent`,
  `/link` and `/about` stay open. The in-process `require_admin()` check
  (`infrastructure/discord/permissions.py:56-84`) is removed from the commands,
  and Discord's enforcement is the check. Team admins adjust it in Server
  Settings > Integrations; that a guild administrator can loosen or tighten it
  there is intended behaviour, not a hole.
- **`admin_role_id` stays**, and so does its mirror into `admin_member`
  (`infrastructure/discord/admin_sync.py:53-60`). The console signs in through
  Outline OAuth and authorizes through `admin_member`; that login is unchanged
  and must keep working after the commands stop reading the role.
- **Operations, not code, are the primary protection:** "Public Bot" off and
  installation context GUILD_INSTALL only (application
  `integration_types_config`,
  <https://docs.discord.com/developers/topics/oauth2>), documented in
  `docs/operations.md` and `docs/first-deployment.md`, with a pointer from the
  chart's values.
- **One guard at the head of `reconcile_guild`.** A domain predicate answers
  "does this installation serve this guild"; a guild that fails it is reconciled
  into nothing and the refusal is logged with the guild ids only. It is
  defence in depth for a misconfigured portal, not an isolation promise.
- **The bot never leaves a guild on its own**, as before.

## Capabilities

### New Capabilities

- `guild-scope`: which guild an installation serves, how commands are scoped and
  permissioned to it, and the one guard that holds if the platform setting is
  wrong.

### Modified Capabilities

None. `transcription` gains no requirement: nothing here concerns how audio
becomes text.

## Impact

**Configuration:** `STURNUS_GUILD_ID` in `sturnus.config.Settings` (bot only), in
the chart's bot environment. Not a credential, so not in the chart's secret
lists.

**Code:** `infrastructure/discord/client.py` — the command sync and the head of
`reconcile_guild`, the single chokepoint every path that starts recording passes
through; the cogs that declare the admin commands, for
`default_member_permissions`; `permissions.py`, for the removal.

**Docs:** `docs/operations.md`, `docs/first-deployment.md`, and a dated design
document under `docs/superpowers/specs/` recording the reversal.

**Commit type.** Ships as `feat(bot)!`, a single type: the guild-scoped and
permissioned command surface is one behaviour. Breaking for operators because of
the required variable and because command permissions move from the stored
role to Discord's own settings.

**Out of scope, and each for its own reason.**

- **The worker check and the console read confinement of the 2026-09-19 design.**
  Removed rather than postponed. They defended a promise to external customers
  that no longer exists; for one team with a private application they are
  mechanism without a threat, and the worker therefore does not need
  `STURNUS_GUILD_ID` either.
- **Removing the multi-guild machinery** — gateway sharding (#135) and per-guild
  OAuth sign-in clients (#147). That is the separate change
  `remove-multi-guild-machinery` and must ship after this one is deployed.
- **Per-guild setup from the console (#149) and per-guild export targets (#150)**
  are not touched; an installation with one guild still has to set it up and
  still wants more than one destination.
- **The standards backlog** — replacing the hand-rolled console session cookie
  with `itsdangerous` or Starlette's session handling, the `/metrics`
  placeholder, and the custom AES-GCM file framing. Same preference, different
  changes; none belongs here.
- **Existing data of a foreign guild is not deleted.** Nothing is known to exist.
- **Whether `bot_public: false` works for a Team-owned application.** Discord
  documents "only the app owner can add the app to guilds" but not what that
  means for a Team. See design.md — Open Questions.
