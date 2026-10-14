# Sturnus — one installation, one guild, held by Discord

**Status:** implemented with the OpenSpec change `single-guild-instance`
(`openspec/changes/single-guild-instance/`)
**Date:** 2026-10-14
**Supersedes:** the `single-guild-instance` proposal dated 2026-09-19, which
justified the same scope by customer isolation and enforced it in code at three
places (gateway, worker, console). That text was a draft of the OpenSpec change
and never landed on `main`; this document is the record of why the change that
did land differs from it.
**Amends:** [2026-08-19-sturnus-design.md](2026-08-19-sturnus-design.md) where
it treats the set of guilds as open-ended: an installation serves the one guild
named in `STURNUS_GUILD_ID`.

## 1. What this round is about

The 2026-09-19 decision said one installation serves exactly one guild, and
gave as its reason that Sturnus was being offered to external customers whose
isolation had to be promised. **That reason no longer holds.** Sturnus is used
only by the owner's own team and is offered to no one else.

**The scope stands; the reason is a single team.** A single team has a single
guild, so everything built to serve several is machinery without a user. What
changes is how the scope is held, because the isolation promise that justified
three self-built enforcement points is gone.

## 2. What is decided here, and by whom

Decided by the repository owner on 2026-10-14.

### 2.1 Discord holds the boundary, not the code

Established standards and Discord's own features are preferred to self-built
mechanisms. Discord already provides what the 2026-09-19 design hand-rolled:

- **A private application.** With "Public Bot" off only the application owner
  can add it to a guild. This removes the invitation path, which is the failure
  three code checks existed to catch.
- **Guild-scoped commands.** Commands registered for one guild exist only
  there, and updates are instant. A command that does not exist in a foreign
  guild needs no reply explaining why, so the old "answer with a reason"
  requirement is dropped rather than weakened.
- **Command permissions.** `default_member_permissions` makes Discord withhold
  a command from members without the permission, and server admins adjust it
  per role, member and channel in Server Settings > Integrations.

### 2.2 One guard remains

A misconfigured portal setting should cost a log line and not a foreign
recording. A pure domain predicate (`sturnus.domain.guild_scope.serves_guild`)
is checked at the head of the reconcile path every recording start passes
through; a guild that fails it is reconciled into nothing, and the refusal is
logged with the two guild ids and no guild name. The bot never leaves a guild
by itself: a mistyped `STURNUS_GUILD_ID` must not make it leave the guild it is
meant to serve.

### 2.3 Command access moves from the stored role to Discord

`/config`, `/queue`, `/audio-purge` and `/setup` default to Manage Server, enforced by
Discord; the in-process `require_admin()` check is removed from them.
`/audio purge` is renamed `/audio-purge` and becomes a top-level command,
because Discord applies the default permission per top-level command and
`/audio delete` has to stay open to members. Keeping both checks would give two authorities that can disagree the moment an admin
restricts a command in Integrations. `admin_role_id` and its mirror into
`admin_member` stay untouched, because the operator console authorizes through
them.

### 2.4 Removed, not postponed

The worker check and the console read confinement of the 2026-09-19 design.
They defended an isolation promise to customers who do not exist, and each
added a configuration variable, a code path and tests whose only user was the
promise. `WorkerSettings` therefore does not gain `STURNUS_GUILD_ID`.

## 3. What this gives up

Nothing here makes a second guild safe to host. If Sturnus is ever offered
externally again, that is a new decision with its own design, not a
configuration of this one. A guild administrator can loosen or tighten a
command in Integrations; with one team, the guild's admins are the team's.

## 4. Consequences outside the code

`STURNUS_GUILD_ID` is required on the bot (blank in the chart so the pod stops
at startup until it is set). `docs/operations.md` section 1.1.1 and
`docs/first-deployment.md` section 2 carry the portal settings and the one-time
check of `bot_public: false` against the Team-owned application.
