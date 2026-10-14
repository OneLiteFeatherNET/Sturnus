# Proposal

## Why

**Decided by the repository owner, 2026-10-14:** Sturnus is used only by the
owner's own team, and one installation serves one guild (change
`single-guild-instance`). Two features were built for a bot that serves many
guilds, and with one guild neither has a user:

- **Gateway sharding (#135).** `commands.AutoShardedBot`, `STURNUS_SHARD_COUNT`,
  `application/sharding.py`, per-shard readiness in
  `infrastructure/discord/client.py` and `infrastructure/health.py`. Discord
  requires sharding only at 2,500 or more guilds per shard
  (<https://docs.discord.com/developers/topics/gateway>); one guild is three
  orders of magnitude below that. The cost of keeping it is not zero: `/readyz`
  waits for "every shard this process is supposed to hold", `on_ready` no longer
  fires the way plain discord.py code expects, and `docs/operations.md` section
  3.6 explains a mechanism nobody can use.
- **Per-guild OAuth sign-in clients (#147).** A guild could bring its own console
  identity provider, addressed by a slug. That exists to separate tenants. The
  team signs in through the deployment-wide Outline client, which stays.

The failure this prevents is the one dead code always causes: tests that assert
behaviour the deployment cannot exhibit, a login branch (`console/app.py:196`)
that reads "against this deployment's client *or* a guild's own" and has to be
reasoned about on every authentication change, and an unused table holding
wrapped OAuth secrets that nobody rotates because nobody knows they exist.

This follows the project preference for established standards over self-built
mechanisms and for fewer moving parts where a single team is the only user.

## What Changes

- **BREAKING for operators:** `STURNUS_SHARD_COUNT` is removed. A deployment that
  sets it must unset it; the chart's value and its comments go with it.
- `commands.AutoShardedBot` becomes `commands.Bot`
  (`infrastructure/discord/client.py:312`). `application/sharding.py`, the
  per-shard `on_shard_ready` / `on_shard_resumed` / `on_shard_disconnect` hooks
  and the `shard_id` log field go; readiness returns to a single connected /
  disconnected state. `tests/application/test_sharding.py` and
  `tests/infrastructure/discord/test_client_shards.py` are deleted, and the
  readiness tests that exercise shard ranges are reduced to the one-connection
  case.
- Per-guild OAuth sign-in is removed: table `guild_oauth_client`
  (`infrastructure/db/models.py:678`), `domain/oauth_clients.py`,
  `infrastructure/db/guild_oauth.py`, `GuildSignInClients` in
  `console/adapters.py`, `console/routes_oauth.py`, the `?guild=` branch and the
  `GuildOAuthClients` wiring in `console/app.py` and `entrypoints/api.py`, and
  the matching ports. `console_state.guild_id` goes with it, since its only job
  was to carry the slug across the round trip.
- **Irreversible migration:** a new Alembic revision runs
  `DROP TABLE guild_oauth_client` and drops `console_state.guild_id`. The
  revision's `downgrade` recreates the empty shapes only; it cannot restore rows
  or wrapped secrets.
- The deployment-wide Outline client login is unchanged.
- Chart bits for sharding (`STURNUS_SHARD_COUNT` in `bot.env`, comments in
  `bot-deployment.yaml` and `values.yaml`) and `docs/operations.md` section 3.6
  are removed or rewritten to say a single connection is all there is.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. **No spec change**, for the same reason `single-guild-instance` leaves
`transcription` alone: the `guild-scope` capability that change introduces says
which guild is served, how its commands are scoped and permissioned, and that the
bot never leaves a guild. None of those sentences mentions a shard or a
per-guild sign-in client, so deleting both makes none of them false. Removal of
code that no requirement describes is not a requirement.

## Impact

**Precondition: `single-guild-instance` is deployed first.** Until it is, a
second guild can still be reconciled and may rely on a per-guild sign-in client;
this change makes that unreachable by deleting it, and it must not be the thing
that makes it unreachable. Deploy, confirm `STURNUS_GUILD_ID` is set and that the
table is unused (see Migration Plan in design.md), then ship this.

**Code:** `infrastructure/discord/client.py`, `infrastructure/health.py`,
`config.py` (`shard_count` and its validator), `application/sharding.py`,
`entrypoints/bot.py`, and on the console side `console/app.py`,
`console/adapters.py`, `console/ports.py`, `console/routes_oauth.py`,
`entrypoints/api.py`, `domain/oauth_clients.py`,
`infrastructure/db/guild_oauth.py`, `infrastructure/db/models.py`. Roughly 4,300
lines including tests.

**Database:** one new migration after the current head (the table was created in
`0013_destinations_tenants_and_names.py`).

**Chart and docs:** `charts/sturnus/values.yaml`,
`charts/sturnus/templates/bot-deployment.yaml`, `docs/operations.md` (section
3.6 and the env tables), `docs/first-deployment.md`.

**Commit type.** Ships as `refactor(bot)!`: behaviour for the one supported
shape of deployment does not change, which is what makes it a refactor; the `!`
is for the removed environment variable and the dropped table.

**Candidates left in place, and why.** Migration 0013 created other
multi-guild-looking structures. Reading the code, none is clearly unreachable
with one guild, so none is in scope:

- `guild` (name mirror) and the `GET /api/guilds` guild switcher in
  `console/routes_directory.py` and neighbours: still true with one guild — the
  console shows the guild's name instead of a snowflake — and the switcher
  merely lists one entry. A candidate for a later UI simplification, not dead.
- `guild_export_target` and `session_document` (#150): stay, per the owner's
  decision.
- `guild_setup_intent` (#149): stays, per the owner's decision.
- `admin_member` and `guild_config`, keyed by guild id: still needed, with one
  value.

**Out of scope.** Per-guild setup (#149) and export targets (#150); the standards
backlog (console session cookie, `/metrics` placeholder, AES-GCM file framing);
horizontal scaling of the bot, which sharding never provided anyway.
