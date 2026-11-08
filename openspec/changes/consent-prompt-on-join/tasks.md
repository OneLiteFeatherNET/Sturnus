# Tasks

Test first, in the order below. Each behaviour gets its failing test before the
code that makes it pass, and each test builds its own fixture: a fresh prompt
state, a fake clock passed in explicitly, no shared module state. No sleeps, no
reads of the system clock, no network. One behaviour per test, named after it.
Layers follow `tests/test_architecture.py`. Consent expiry is not part of this
change: no task adds time-based logic to consent. The code ships as `feat(bot)`.

## 1. Decision logic (domain and application)

- [ ] 1.1 (application) Write failing tests for `needs_prompt(consent, linked,
      policy_version, now)`: a member without active consent is prompted; a member
      with active consent and a link is not; a member with active consent and no
      link is prompted for the link only; a member with consent given under an old
      policy version is prompted; a member whose consent was revoked is prompted.
      Reuse `is_consent_active` from `domain/consent.py` with the instant passed in
- [ ] 1.2 (application) Write the failing test
      `test_active_consent_is_not_prompted_weeks_later`: consent granted at one
      instant, a join at a much later instant under the same policy version
      produces no prompt. Implement `needs_prompt` to satisfy 1.1 and 1.2
- [ ] 1.3 (application) Write failing tests for the per-channel record: a second
      join by the same member while another human stays produces no second prompt;
      clearing the record when the last human leaves makes the next join prompt
      again. Implement `PromptRecord` in `application/` with no Discord imports

## 2. Consent and link reuse (application)

- [ ] 2.1 (application) Write a failing test that the consent use case invoked
      from the prompt produces the same result as `/consent grant` for the same
      inputs: role assignment is called, then `record_grant`, with the same
      failure messages when the role cannot be assigned. Extract the shared call
      into one use case if the test requires it; `consent_cog.py` keeps its
      behaviour
- [ ] 2.2 (application) Write a failing test that a grant without the checkbox
      writes nothing; then implement the guard in the use case caller, not in the
      view
- [ ] 2.3 (application) Write a failing test that the link start for the presser
      issues a state for the presser's own Discord id and nothing for the mentioned
      member; reuse the `/link start` path

## 3. Presentation (infrastructure)

- [ ] 3.1 (infrastructure) Write failing tests that the prompt message is a
      Components V2 layout with no `content`, mentions exactly the joining member
      through a Text Display, and sets `allowed_mentions` to that one user. Build
      the `LayoutView` in `infrastructure/discord/`, mentioning only the joining
      member
- [ ] 3.2 (infrastructure) Write failing tests that the message offers the
      consent button only, the link button only, or both, as the missing steps
      dictate. Implement the button selection from the output of task 1.1
- [ ] 3.3 (infrastructure) Write failing tests for the link button: pressing it
      replies ephemerally with a link button to the presser's personal URL, and
      the channel message contains no URL. Implement with `ui.DynamicItem` and a
      fixed custom id, so the button survives a restart
- [ ] 3.4 (infrastructure) Write failing tests for the consent modal: pressing the
      consent button opens a modal as the first response, the modal holds a Text
      Display with the host and policy version and a Checkbox in a Label, and a
      member with active consent gets an ephemeral answer and no modal. Implement
      the modal and the button as `DynamicItem`s
- [ ] 3.5 (infrastructure) Write failing tests for the modal submission: checkbox
      set grants through the use case from 2.1; checkbox unset records nothing and
      answers ephemerally. Implement the submit handler
- [ ] 3.6 (infrastructure) Write a failing test that the message is edited to
      "@person consented" after consent through the prompt, with no ping, and that
      the edit is skipped without error when the message reference is gone after a
      restart. Implement the edit

## 4. Join hook (infrastructure)

- [ ] 4.1 (infrastructure) Write failing tests for `on_voice_state_update`: a
      member joining a recording channel is prompted; a bot account is not; a
      member moving between two recording channels is handled as a join only when
      the record for the new channel has no entry. Implement the hook in
      `client.py` calling the application logic of section 1
- [ ] 4.2 (infrastructure) Write a failing test that a Discord permission error
      on posting is logged with guild and channel identifiers and does not raise,
      and that a consenting member's recording is unaffected in the same event.
      Implement the guarded post
- [ ] 4.3 (infrastructure) Write a failing test that the logs contain identifiers
      and outcome labels only, with no URL, state token or consent text; use a
      captured log appender. Ensure lazy `%s` logging throughout (Ruff G/LOG)

## 5. Permissions and wiring (application and entrypoints)

- [ ] 5.1 (application) Write a failing test in `tests/application/test_setup_plan.py`
      that the plan for each recording channel grants the bot's role View Channel
      and Send Messages. Extend `setup_plan.py` to match
- [ ] 5.2 (entrypoints) Write a failing test that the persistent prompt buttons are
      registered at bot startup, so that a `DynamicItem` interaction matches after
      startup without a prior prompt. Register them in `entrypoints/bot.py`
- [ ] 5.3 (infrastructure) Write a failing test that the metric counter for prompt
      outcomes has labels from the fixed outcome set and no person or channel
      identifier. Implement the counter

## 6. Verification

- [ ] 6.1 Run `uv run pytest tests/application tests/infrastructure/discord` and
      confirm it is green, with no sleeps or wall-clock reads in the new tests
- [ ] 6.2 Run `uv run pytest tests/test_architecture.py` and confirm the layers
      are unchanged
- [ ] 6.3 Run `uv run ruff check`, `uv run ruff format --check` on changed files,
      and `uv run mypy`; all must pass under strict mode
- [ ] 6.4 Run the full suite once under the shared lock
      (`flock ~/.cache/claude-heavy.lock uv run pytest`) and confirm it is green
- [ ] 6.5 On a preview deployment in a test guild: join a recording channel
      without consent and see the prompt; press Link account and confirm the reply
      is visible only to the presser; complete consent through the modal and see
      the message change; leave and rejoin and confirm no second ping

## 7. Pull request

- [ ] 7.1 Open the code pull request titled
      `feat(bot): ask for consent and linking when someone joins a recording channel`.
      Its description is prose from the team's point of view: a member who joins a
      recording channel today gets no sign that they are not being recorded, and
      now the channel asks them directly. It states the one-ping-per-occupied-channel
      rule, that consent is not asked again until revoked or the policy changes, and
      the permissions the bot now needs. No session links and no trailers
