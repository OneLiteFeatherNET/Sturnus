# Design

## Context

`SessionCookie` in `src/sturnus/console/session.py` is constructed once in
`entrypoints/api.py` with the deployment's session secret and lifetime, and is
used by `console/app.py` in two places: reading the cookie on each request
(mapping `ExpiredSession` to a redirect through login and `InvalidSession` to a
refusal) and issuing it after the Outline callback. Its tests in
`tests/console/test_session_cookie.py` build a fresh instance per test and pass
the instant explicitly, so no test reads the system clock.

The session cookie is the console's only authentication. The consent constraint
is untouched by this change: the token still names a Discord user id and nothing
else, and the recording gate does not read the token's contents beyond that id.
The telemetry constraint applies too: the token must never be logged, and this
change adds no log line that could carry it.

## Decisions

**1. `URLSafeTimedSerializer` behind the existing class.** `SessionCookie`
remains the only place that knows about the library. Callers see the same
`issue(session, now)` and `read(token, now)`, so the change stays inside
`session.py` and its tests.

**2. The instant is bound per call.** `TimestampSigner.get_timestamp` reads the
system clock by default. A small subclass returns the `now` the caller passed,
and `issue` and `read` build a serializer with that subclass as its `signer`.
Constructing a serializer is cheap, and binding per call means no shared mutable
clock state exists between calls or tests.

**3. A fixed salt.** The serializer is created with a salt that names this use,
for example `sturnus.console.session`. The same secret used elsewhere cannot then
produce a token this verifier accepts, and the reverse holds too.

**4. The lifetime is enforced from the signed timestamp.** The timestamp is the
issue time and is covered by the signature. `read` passes the lifetime as
`max_age`, so the holder can neither change the timestamp nor extend it. The
lifetime itself is now verifier configuration rather than a claim inside the
payload. This is the one reading change to the old wording "the expiry is inside
the signed payload": the protection is the same, and the owner should confirm it
is acceptable.

**5. Exception mapping.** `SignatureExpired` maps to `ExpiredSession`, and
`BadSignature` (which covers every other timed-signature failure) maps to
`InvalidSession`. `SignatureExpired` is a subclass of `BadSignature`, so it is
caught first. Every malformed shape still ends as `InvalidSession`, so a prober
cannot tell which parsing step it reached.

**6. The payload stays minimal.** The signed value is `{"sub": <discord user id>}`.
The expiry claim `exp` disappears because the timestamp replaces it.

**7. The secret-length check stays in our code.** `itsdangerous` does not enforce
a minimum key length, and the 32-byte refusal at construction is a deployment
safety check that belongs to this project.

**8. Boundary behaviour changes by one second.** The old check refused at
`now >= exp`. `itsdangerous` refuses when `age > max_age`, so a token is valid
at exactly the lifetime and expired one second later. This is accepted and is
pinned by a test, so the change is visible in review and not accidental.

## Alternatives considered

- **Keep the hand-written signer and add tests.** Rejected. The tests already
  exist, and they only check the author's reading of the format. The project's
  stated preference is established standards over self-built mechanisms, and the
  library gives the same properties with a maintained implementation.
- **Starlette's or Flask's session middleware.** Rejected. Sturnus serves the
  console through aiohttp, and either middleware would bring a second web
  framework's session model in for the sake of one cookie.
- **PyJWT.** Rejected, and the module docstring already explains why: a JWT
  carries its algorithm in a header the verifier is invited to trust. The
  library chosen here has one fixed algorithm and no such header.
- **Server-side session store.** Rejected. It would contradict the stateless
  design the docstring defends: a restart would no longer be free, and a second
  replica would need shared state.

## Risks

- **Re-login for everyone once.** Expected and documented. It is announced to
  the team before the deployment.
- **Library upgrades.** `itsdangerous` is pinned through `uv.lock`, so an upgrade
  is a reviewed lock change and not a silent one.
- **Misuse of the clock injection.** If `issue` and `read` were given different
  instants by a caller, tokens would expire early or late. The tests issue and
  read at explicit instants and cover the boundary, so this is caught in the
  suite, not in production.

## Migration

Deploy the change. Every cookie issued before it fails verification and is sent
to the login flow, and each team member signs in once through Outline. No data
is migrated and no rollback is needed beyond redeploying the previous image,
which accepts only the old format, so cookies issued after a rollback would again
require one sign-in.
