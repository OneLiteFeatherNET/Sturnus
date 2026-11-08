# Proposal

## Why

The console's session cookie is the one piece of security-relevant code in
Sturnus that the project wrote itself. `src/sturnus/console/session.py` builds
its own token format, signs it with HMAC-SHA256, compares signatures in
constant time, carries the expiry inside the signed payload and keeps expiry
and forgery apart as two exceptions. Each of those is well understood, and each
is a place where a small mistake would let an unauthenticated person choose
whose recordings to listen to.

Sturnus serves only the owner's own team, so nothing about the console needs a
home-made cryptographic mechanism, and the project prefers established standards
to self-built ones. `itsdangerous` (Pallets, the library under Flask's and
Starlette's session cookies) provides the same properties as maintained,
tested primitives: `URLSafeTimedSerializer` signs a timestamp into the token,
`SignatureExpired` is a subclass of `BadSignature` that a caller can catch
first, the comparison is constant-time, and a list of secrets allows key
rotation.

The failure this prevents is a verifier that drifts from its own docstring
during a later edit. Such a drift would stay unnoticed, because the tests
exercise only the author's reading of the format and not an independent
implementation of it.

## What Changes

- `SessionCookie` keeps its public interface (`issue`, `read`, `SignedSession`,
  `InvalidSession`, `ExpiredSession`) and its contract. Its internals move to
  `itsdangerous.URLSafeTimedSerializer`.
- Kept as they are: no server-side session store; the lifetime is bound to the
  signature, so a holder cannot extend it; an expired session and a forged one
  are different exceptions with different responses; the secret must be at
  least 32 bytes or construction fails; the cookie name and attributes set in
  `console/app.py` do not change.
- **One-time re-login.** The token format changes, so every cookie issued
  before the deployment is refused once. Each team member signs in again through
  Outline once. Nothing else changes for users.
- New runtime dependency: `itsdangerous`.
- Not changed: database and migrations, the chart, environment variables, console
  routes and the consent and recording logic.

The commit is `refactor(console)` without `!`. No configuration, API or stored
data changes shape for operators; the re-login is an operational note in the
pull request description, not a breaking change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. **No spec change.** `openspec/specs/` contains only `transcription`, and no
requirement there describes the console session. The behaviour that matters
here (a forged token is refused, an expired one is sent back through login) is
stated in the module docstring and pinned by `tests/console/test_session_cookie.py`.
Writing a console session spec would add a new capability, which a refactor
does not do.

## Impact

**Code:** `src/sturnus/console/session.py` (body and docstring),
`tests/console/test_session_cookie.py`, `pyproject.toml` and `uv.lock`.
`console/app.py` and `entrypoints/api.py` keep calling the same interface.

**Dependencies:** `itsdangerous>=2.2` at runtime, with no transitive surprises
beyond what its own metadata declares.

**Out of scope.** Key rotation through a list of secrets: the library supports it,
but using it means a configuration change (`STURNUS_SESSION_SECRET` becoming a
list) and belongs in its own proposal. Changing the lifetime or the cookie
attributes. Server-side sessions, which would contradict the stateless design.
The other items on the standards backlog (the `/metrics` placeholder, AES-GCM
file framing).
