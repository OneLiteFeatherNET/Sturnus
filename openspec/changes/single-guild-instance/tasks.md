# Tasks

Every task is test first (F.I.R.S.T.): write the failing test, then the code.
Tests need no network, no real Discord and no sleeps; time comes from the
injected clock, each test builds its own fixture and shares no static state.
Layers follow the boundary `tests/test_architecture.py` enforces. This change
ships as one commit of type `feat(bot)!`.

## 1. The boundary, named once (domain + entrypoints)

- [ ] 1.1 (domain) Add the predicate "does this installation serve this guild",
      a pure function over two ids; verify a new `tests/domain/` module, written
      first, covers match, mismatch and totality (no id is special)
- [ ] 1.2 (entrypoints) Add `STURNUS_GUILD_ID` to `sturnus.config.Settings`,
      required with no default, beside `transcription_base_url`; do NOT add it
      to `WorkerSettings`; verify a test asserts the bot's settings refuse to
      construct without it and `tests/test_component_credentials.py` still
      passes (configuration, not a credential)
- [ ] 1.3 Verify `uv run pytest tests/test_architecture.py` passes

## 2. Guild-scoped commands and native permissions (infrastructure)

- [ ] 2.1 (infrastructure) Replace the global `tree.sync()` at
      `infrastructure/discord/client.py:657` with `copy_global_to(guild=...)`
      then `sync(guild=...)`, followed by `clear_commands(guild=None)` and a
      global `sync()`; verify a test with a fake tree asserts the call order
      (guild sync before global clear), the guild argument, and that a failing
      global clear is logged and does not stop startup
- [ ] 2.2 (infrastructure) Declare `default_member_permissions` (Manage Server)
      on `/config`, `/queue`, `/audio purge` and `/setup` in their cogs and
      leave `/consent`, `/link`, `/about` open; verify tests assert each
      command's `default_permissions` value on the built tree
- [ ] 2.3 (infrastructure) Remove `require_admin()` from those commands and from
      `permissions.py` if nothing else uses it; verify the affected cog tests
      pass without in-process role stubs and no code path reads `admin_role_id`
      to authorize a command
- [ ] 2.4 (infrastructure) Verify `admin_role_id` and the `admin_member` mirror
      (`admin_sync.py`) are untouched: existing `admin_sync` tests pass
      unchanged, and a test asserts a role member is still mirrored

## 3. The one guard (infrastructure)

- [ ] 3.1 (infrastructure) Call the predicate at the head of `reconcile_guild`
      and refuse a foreign guild before any plan is computed; verify tests assert
      a foreign reconcile opens no voice connection, writes no configuration and
      creates no session, while the configured guild is unaffected
- [ ] 3.2 (infrastructure) Log the refusal once per reconcile, with the
      configured and seen ids only (no guild name); verify
      `tests/test_logging_discipline.py` passes and a captured-appender test
      asserts both ids are present and no name is
- [ ] 3.3 (infrastructure) Pin that no code path calls `Guild.leave`; verify a
      test asserts it
- [ ] 3.4 Verify `uv run pytest` is green on the whole tree

## 4. Deployment and the texts that would otherwise be wrong

- [ ] 4.1 Add `STURNUS_GUILD_ID` to the chart's bot environment, blank, with a
      comment that blank stops the pod at startup on purpose; verify
      `helm template` renders and the chart CI job passes
- [ ] 4.2 Document in `docs/operations.md` section 1.1 and `docs/first-deployment.md`:
      the variable, "Public Bot" off, GUILD_INSTALL only, where command access
      now lives (Server Settings > Integrations), that `admin_role_id` remains
      for the console, and the one-time check of `bot_public: false` against the
      Team-owned application; verify the markdown-lint job passes
- [ ] 4.3 Write the dated design document under `docs/superpowers/specs/`
      recording the 2026-10-14 reversal: the 2026-09-19 text named external
      customers, the scope stands, the reason is a single team; verify it names
      what it supersedes

## 5. Whole-system verification

- [ ] 5.1 Verify `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`
      and `uv run pytest` all pass on the finished tree
- [ ] 5.2 Verify in the real guild: the commands appear instantly, no global
      command remains, an unprivileged member cannot see the admin commands, and
      an Integrations override is honoured
- [ ] 5.3 Verify the Team-owned application cannot be added by an unrelated
      account, and record the result in `docs/first-deployment.md`

## 6. Pull request

- [ ] 6.1 Open the pull request titled
      `feat(bot)!: serve one guild through guild-scoped commands and Discord's own permissions`
      with a `BREAKING CHANGE:` footer naming `STURNUS_GUILD_ID` and the move of
      command permissions to Integrations; the description states the decision
      and the failure it prevents
