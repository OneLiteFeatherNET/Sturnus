# Tasks

Test first, in the order below. Each behaviour gets its failing test before the
code that makes it pass, and each test builds its own `SessionCookie`. No sleeps,
no reads of the system clock: every instant is passed in, as the existing tests
already do. One behaviour per test, named after it. Layers follow
`tests/test_architecture.py`; this change touches the console layer and the
project metadata only. The commit ships as `refactor(console)`.

## 1. Tests first (console)

- [ ] 1.1 (console) Write failing tests in `tests/console/test_session_cookie.py`:
      a token read at the instant it was issued names the same person; a token
      read after its lifetime raises `ExpiredSession`, not `InvalidSession`
- [ ] 1.2 (console) Write a failing test that a token in the pre-change format
      (`payload.signature`, the old hand-written layout) raises `InvalidSession`,
      which pins the one-time re-login
- [ ] 1.3 (console) Write a failing test that a token is valid at exactly its
      lifetime and expired one second later, which pins decision 8 of design.md
- [ ] 1.4 (console) Keep the existing tests for tampered payloads, foreign
      secrets, truncated signatures, gibberish input and the 32-byte refusal;
      rename any whose name refers to the old layout
- [ ] 1.5 Run `uv run pytest tests/console/test_session_cookie.py` and confirm the
      new tests from 1.1 to 1.3 fail for the expected reason, not for an import error

## 2. Implementation (console and metadata)

- [ ] 2.1 (metadata) Add `itsdangerous>=2.2` to `pyproject.toml` and run
      `uv lock`; record the resolved version in the pull request and confirm it
      was released at least two weeks ago
- [ ] 2.2 (console) Replace the body of `SessionCookie` in
      `src/sturnus/console/session.py` with `URLSafeTimedSerializer` and the fixed
      salt; bind the instant through a `TimestampSigner` subclass passed as
      `signer`; map `SignatureExpired` to `ExpiredSession` (caught before
      `BadSignature`) and every other failure to `InvalidSession`; keep the
      32-byte check; remove the now unused `base64`, `hashlib`, `hmac` and `json`
      imports. Verify the tests from section 1 pass
- [ ] 2.3 (console) Update the module docstring so that it describes the cookie
      as an `itsdangerous` timed signature, keeps the argument against a JWT, and
      still states the three decisions with the lifetime now enforced from the
      signed timestamp. Verify that every sentence still matches the code

## 3. Verification

- [ ] 3.1 Run `uv run pytest tests/console` and confirm it is green
- [ ] 3.2 Run `uv run ruff check` and `uv run ruff format --check` on the changed
      files, and `uv run mypy`; all must pass under strict mode
- [ ] 3.3 Run the full suite once under the shared heavy-job lock
      (`flock ~/.cache/claude-heavy.lock uv run pytest`) and confirm it is green
- [ ] 3.4 On a preview deployment, confirm that an old cookie is sent to login,
      that a fresh sign-in works, and that the session survives a restart of the
      API process

## 4. Pull request

- [ ] 4.1 Open the code pull request titled
      `refactor(console): sign the console session with itsdangerous`. Its
      description is two or three sentences of prose: the team's preference for
      established libraries, what the hand-written signer risked, and the one-time
      re-login every team member will see after deployment. No session links and
      no trailers
