# Tasks

Every task is test first where it adds behaviour (F.I.R.S.T.): write the failing
test, then the code; where a task only deletes, the proof is that the remaining
suite stays green and that an assertion for the absence exists. No sleeps, no
wall-clock, no shared state; time comes from the injected clock. Layers follow
`tests/test_architecture.py`. This change ships as `refactor(bot)!`.

## 1. Preconditions

- [ ] 1.1 Confirm `single-guild-instance` is deployed and `STURNUS_GUILD_ID` is
      set in the running installation; record the date in the pull request
- [ ] 1.2 Take a database backup and note the result of
      `SELECT count(*) FROM guild_oauth_client`; verify the backup restores
      into a scratch database

## 2. Per-guild OAuth sign-in (domain + infrastructure + console + entrypoints)

- [ ] 2.1 (console) Write a failing test that `begin()` takes no guild argument
      and that `/api/auth/login?guild=x` behaves as the plain login; then remove
      the `?guild=` branch and `UnknownSignIn` handling in `console/app.py`
      (around line 196); verify `tests/console` passes for the plain login
- [ ] 2.2 (console) Delete `console/routes_oauth.py`, `GuildSignInClients` in
      `console/adapters.py`, the `GuildOAuthClients`/`GuildSignIn` ports in
      `console/ports.py`, and the wiring in `console/app.py` and
      `entrypoints/api.py`; delete `tests/console/test_oauth_routes.py` and the
      matching parts of `tests/console/test_adapters.py` and
      `tests/console/conftest.py`; verify `uv run pytest tests/console` is green
- [ ] 2.3 (infrastructure) Delete `infrastructure/db/guild_oauth.py`, its model
      `guild_oauth_client` in `infrastructure/db/models.py` and
      `tests/infrastructure/test_guild_oauth.py`; remove `console_state.guild_id`
      from the model; verify the outline-login tests still pass
- [ ] 2.4 (domain) Delete `domain/oauth_clients.py` and
      `tests/domain/test_oauth_clients.py`; verify
      `uv run pytest tests/test_architecture.py` passes

## 3. Gateway sharding (application + infrastructure + entrypoints)

- [ ] 3.1 (infrastructure) Write the failing test first: readiness is ready when
      the single connection is up, not ready when it is down, and tolerates a
      short blip as before (injected clock); then collapse `ReadinessState` in
      `infrastructure/health.py` to one flag and drop
      `shards_this_process_owns`
- [ ] 3.2 (infrastructure) Change `SturnusClient` to `commands.Bot`, remove
      `on_shard_ready` / `on_shard_resumed` / `on_shard_disconnect`, `_shard()`
      and the `shard_id` log fields, and move the per-guild reconcile back to
      `on_ready`; verify a test asserts the reconcile runs once per ready for
      every guild, and `tests/test_logging_discipline.py` passes
- [ ] 3.3 (entrypoints) Remove `shard_count` and its validator from `config.py`
      and any use in `entrypoints/bot.py`; verify a test asserts that setting
      `STURNUS_SHARD_COUNT` no longer changes the settings and does not fail
      startup (an operator who forgot to unset it is told in the log, not
      crashed)
- [ ] 3.4 (application) Delete `application/sharding.py`,
      `tests/application/test_sharding.py` and
      `tests/infrastructure/discord/test_client_shards.py`; verify
      `uv run pytest` is green

## 4. The irreversible migration (infrastructure)

- [ ] 4.1 (infrastructure) Add the next Alembic revision after the current head:
      `DROP TABLE guild_oauth_client` and drop `console_state.guild_id`, with a
      docstring that leads with "this cannot be undone" and a `downgrade` that
      recreates the empty shapes only; verify with testcontainers in
      `tests/infrastructure/test_migrations.py` (written first) that upgrade
      removes both, downgrade restores empty ones, and a single head remains
- [ ] 4.2 Verify `uv run pytest tests/infrastructure/test_migrations.py` against
      a database seeded at the previous head, including a populated
      `guild_oauth_client` row, so the drop is exercised on real data

## 5. Chart and docs

- [ ] 5.1 Remove `STURNUS_SHARD_COUNT` and the sharding comments from
      `charts/sturnus/values.yaml` and `templates/bot-deployment.yaml`, keeping
      the explanation of why the bot is a single pod; verify `helm template`
      renders and the chart CI job passes
- [ ] 5.2 Rewrite `docs/operations.md` section 3.6 to state there is one gateway
      connection, remove the variable from the env tables, and note the
      precondition for the migration in the upgrade guidance; update
      `docs/first-deployment.md`; verify the markdown-lint job passes
- [ ] 5.3 Add a dated note under `docs/superpowers/specs/` that this change
      removes the #135 and #147 decisions of 2026-08-23, naming the reason

## 6. Whole-system verification

- [ ] 6.1 Verify `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`
      and `uv run pytest` all pass; `grep -rn "shard\|guild_oauth" src tests
      charts` finds only intentional mentions
- [ ] 6.2 Verify against the running deployment after upgrade: Outline sign-in
      works, the bot records and `/readyz` is 200, and the migration reached its
      head

## 7. Pull request

- [ ] 7.1 Open the pull request titled
      `refactor(bot)!: remove gateway sharding and per-guild sign-in clients`
      with a `BREAKING CHANGE:` footer naming `STURNUS_SHARD_COUNT` and the
      dropped `guild_oauth_client` table; the description states the decision,
      the failure it prevents, the precondition on `single-guild-instance` and
      the backup note
