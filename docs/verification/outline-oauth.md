# Outline OAuth: assumptions the account-link client is built on

Date: 2026-08-20 · `sturnus.infrastructure.documents.outline_oauth.OutlineOAuth`

**Nothing in this document was exercised against a running Outline
instance.** Spec 8.4 flags this OAuth shape as unverified, and this task's
brief asks for exactly that verification: register an OAuth application on
a live Outline instance, run a real authorization-code exchange with a
revocable client, and record what actually comes back.

That live exercise did not happen here, for the same reason it did not
happen for the document adapter (`docs/verification/outline-api.md`): the
only Outline access reachable in this environment is an MCP connector
against what appears to be OneLiteFeather's real Outline workspace, not a
disposable instance with a registerable-and-revocable OAuth application,
and there is no tool available for registering an OAuth application
through that connector even if it were disposable (application
registration is a Settings-UI action, not an API call). Per the task
dispatch, this client was built against Outline's documented OAuth 2.0
shape instead.

Unlike the document adapter's verification pass, this one is not built from
memory of Outline's public docs alone. It used web search and page/source
fetches against Outline's own hosting docs, its merged OAuth-provider pull
request (`outline/outline#8884`), a related CLI project's issue thread
describing a real integration attempt, and Outline's published OpenAPI
spec. That is real secondary evidence, not a guess out of nothing -- but it
is still not a live request-response pair captured from this bot's actual
target instance, and every item below is labelled accordingly:

- **Documented** -- stated directly in Outline's own hosting docs or
  source, not inferred.
- **Inferred** -- reconstructed from source code, PR descriptions, or an
  OpenAPI spec description rather than seeing the literal schema.
- **Assumed** -- no source found either way; carried over from general
  OAuth 2.0 / Outline API convention.


## How to close this document

Do not verify these by hand — `scripts/verify_outline_api.py` makes exactly
the calls this document describes and reports each assumption below as
CONFIRMED, CONTRADICTED, SKIPPED or ERROR, exiting non-zero if anything is
contradicted. It needs a scratch collection and a revocable token, and it
redacts the token from all output:

```bash
uv run python scripts/verify_outline_api.py \
  --base-url https://outline.example.com \
  --token "$OUTLINE_TOKEN" \
  --collection-id <scratch-collection-id>
```

Add `--oauth-client-id` and `--oauth-redirect-uri` to cover the OAuth
assumptions in `outline-oauth.md` in the same run; the script prints an
authorize URL, and `--oauth-client-secret` with the `--oauth-code` from the
redirect completes the exchange.

Record the run's output here, replacing this section.

## What is assumed, and where it lives in the code

| Item | Where it is spelled | If verification finds it wrong |
|---|---|---|
| Authorize / token endpoint paths | `_AUTHORIZE_PATH`, `_TOKEN_PATH` | Change the one constant each |
| Identity endpoint path | `_IDENTITY_PATH` | Change the one constant |
| Identity response shape / which field is the user id | `_extract_identity()` | Change the one function body |
| Requested scope | `SCOPE` | Change the one constant |
| Token-endpoint client authentication | `identity_from_code()`, the `client_secret` body field | Change the one dict literal |

## Finding 1: Outline has a real, built-in OAuth 2.0 provider

**Documented.** This was the first thing worth confirming, because Outline
is also an OIDC *client* (it can log users in via Google, GitLab, generic
OIDC) -- a different feature that is easy to confuse with this one. Outline
separately ships its own OAuth 2.0 **provider**, added to let internal and
external tools integrate "with scoped authentication without the need for
manual management of API keys" (Outline's hosting docs, "OAuth provider"
page). Applications are registered under **Settings → Applications** on the
Outline instance itself. Three server-side lifetimes are configurable via
environment variables and default to: access tokens 1 hour
(`OAUTH_PROVIDER_ACCESS_TOKEN_LIFETIME`), refresh tokens 30 days
(`OAUTH_PROVIDER_REFRESH_TOKEN_LIFETIME`), authorization codes 5 minutes
(`OAUTH_PROVIDER_AUTHORIZATION_CODE_LIFETIME`). Dynamic Client Registration
is also supported (for tools like Cursor and Claude that register
themselves) and can be disabled with `OAUTH_DISABLE_DCR=true`; this client
assumes an application registered manually in Settings, not DCR.

**Not checked**: whether the workspace this bot will actually target has
this feature enabled at all, and what version of Outline introduced it (the
provider shipped as `outline/outline#8884`, a 70-commit PR; an older
self-hosted instance may predate it).

## Finding 2: endpoint paths

**Inferred**, from Outline's own `server/routes/oauth/index.ts` (fetched
from the `main` branch) and independently corroborated by a third-party CLI
project's issue thread describing a real integration attempt against a live
instance:

- Authorize: `{base_url}/oauth/authorize` -- a `GET` the browser is sent
  to (it renders a consent screen), assumed distinct from the `POST
  /authorize` API route the frontend itself calls after the user clicks
  Allow. This client only ever builds the browser-facing `GET` URL.
- Token: `POST {base_url}/oauth/token`

The source file also defines `POST /revoke`, `POST /register`, and
`GET|PUT|DELETE /register/:clientId` (client management), none of which
this client uses.

**Not checked**: whether these routes are mounted at `/oauth/*` on the root
domain or under a different prefix on a self-hosted instance, and whether
`GET /oauth/authorize` is the actual browser entry point rather than an
internal API path with the same name as the frontend page.

## Finding 3: token-endpoint request shape and client authentication

**Inferred**, from the same route source. The token endpoint's request
schema (`T.TokenSchema`) was seen to include `grant_type`, `refresh_token`,
`client_id`, and `client_secret` as body fields -- notably `client_id` and
`client_secret` are sent **in the POST body**, not as an `Authorization:
Basic` header, matching the rest of Outline's API which is JSON/form-body
RPC style rather than REST-with-headers. The fields visible were for a
refresh-token grant; `code` and `redirect_uri` for the authorization-code
grant were not directly observed but are assumed present since Outline's
provider claims to be "full OAuth 2.0 spec compatible" (PR #8884
description) and RFC 6749 §4.1.3 requires both.

Request body this client sends:

```json
{
  "grant_type": "authorization_code",
  "code": "<code>",
  "redirect_uri": "<redirect_uri>",
  "client_id": "<client_id>",
  "client_secret": "<client_secret>"
}
```

**Not checked**: whether Outline instead expects `Authorization: Basic
base64(client_id:client_secret)` (the other RFC 6749-sanctioned method) in
addition to or instead of body fields, and whether `client_secret` in the
body is rejected outright if a Basic header is also required.

## Finding 4: token-endpoint response shape

**Assumed**, standard RFC 6749 §5.1 shape, not directly observed in source:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_in": 3600,
  "token_type": "Bearer",
  "scope": "read"
}
```

This client reads only `access_token` and discards the rest, including
`refresh_token` -- Spec 8.4's simplification depends on never persisting a
refresh token, so it is not even looked at.

**Not checked** against a real response.

## Finding 5: which endpoint returns the identity, and which field is the user id

This is the highest-stakes assumption in this file: Spec 8.4 has
`external_user_id` become `account_link.external_user_id`, and mention
rendering resolves it back into an `@[..](mention://user/..)` reference. A
wrong field here does not raise an exception anywhere -- Outline still
renders the mention markup, just pointing at a user id that does not exist,
so a wrong guess fails silently in a document nobody is likely to notice is
broken until someone clicks the mention.

**No dedicated `/oauth/userinfo`-style endpoint was found** in Outline's
OAuth route file -- unlike a standard OIDC provider, Outline's OAuth
implementation does not appear to expose its own identity endpoint separate
from the general API. What was found instead: Outline's `auth.info` RPC
method (`POST {base_url}/api/auth.info`), documented as "retrieve
authentication details for the current API key" and able to "check that a
token is still valid and load the IDs for the current user and workspace."
This client assumes an OAuth-issued access token authenticates against
`auth.info` exactly as a personal API token does (`Authorization: Bearer
<access_token>`), since OAuth access tokens and personal API tokens are
documented as sharing the same authentication mechanism for the rest of
Outline's API.

**Inferred** response shape, from Outline's published OpenAPI spec
(`outline/openapi`, `spec3.json`/`spec3.yml`, the `auth.info` operation and
its referenced `Auth` schema component) as summarized by two independent
fetches of that spec:

```json
{
  "data": {
    "user": {
      "id": "9c8b1e2a-....",
      "name": "Max Example",
      "email": "max@example.com",
      "...": "..."
    },
    "team": {
      "id": "...",
      "name": "..."
    }
  }
}
```

**`data.user.id` is the field this client uses as `external_user_id`.** It
is documented as the user's id within the OpenAPI schema description (and
matches Outline's general API convention, also used by the document
adapter, of wrapping responses in a `data` object). `data.user.name` is
used as `display_name`.

**Not checked against a live response**: the raw JSON was not seen, only
descriptions of the schema. In particular:

- Whether `id` is genuinely stable and unique per user (assumed: yes, it is
  described as Outline's internal user id, the same one addressable
  elsewhere in the API), versus something that could be reassigned.
- Whether an OAuth access token scoped to `read` is actually accepted by
  `auth.info`, or whether that endpoint requires a scope this client did
  not request.
- Whether `email` or some other field would in fact be a more stable
  choice than `id` for this bot's purpose -- `id` was chosen because it is
  Outline's own opaque identifier, the same category of value
  `account_link.external_user_id` is modelled to hold, but this reasoning
  was not checked against how Outline documents id stability specifically.

**This is the one finding that most needs a real token exchange against a
live instance before this ships**, because a wrong scope here is silent
by design (Spec 8.4), unlike a wrong document-adapter field, which fails
loudly.

## Finding 6: scope

**Inferred**, from Outline's `Scope` enum in `shared/types.ts` (fetched
from the `main` branch): three string values, `read`, `write`, `create`.
This is **not a granular per-resource scope model** (there is no
`identity:read` or similar) -- `read` is the narrowest of the three, and is
what this client requests, but it is also broader than "read one's own
identity": it grants read access to every document and collection the
authorizing user can see. There is no narrower scope that reads identity
alone.

This is worth flagging as a real gap, not just an implementation detail:
Spec 8.4's premise is that this token is used once, for identity, and
discarded -- but the token briefly held during that one use has read access
to the user's entire visible Outline workspace, not just their name. The
window is short (one coroutine, no persistence) and the token is never
logged or stored, but "one use for identity, discarded" is a usage
discipline enforced by this code, not a scope boundary enforced by
Outline's authorization server.

**Not checked**: whether `read` is genuinely required (versus, say, a
lighter public scope that DCR-registered tools get by default), and whether
requesting `read` triggers a consent screen listing document access in a
way that might confuse a participant linking their account, who has no
reason to expect a "link your name" flow to mention document permissions.

## Finding 7: PKCE

**Documented as supported**: Outline's `/oauth/authorize` accepts
`code_challenge` and `code_challenge_method` parameters (confirmed by PR
#8884's commit history, "Add PKCE parameters to /authorize", and
independently by a third-party CLI project's real integration attempt
against a live Outline instance, which builds authorize URLs of the form
`{base_url}/oauth/authorize?client_id=...&code_challenge=...&state=...&
redirect_uri=...`). Outline's public-client (secret-less) refresh-token
flow is still being extended for PKCE clients as of this writing, but the
authorization-code exchange itself supports PKCE today.

**This client does not use it**, despite the task brief's instruction to
use PKCE even for a confidential client. Reason: PKCE requires the
`code_verifier` generated alongside the `code_challenge` in
`authorize_url()` to be presented again in `identity_from_code()`, which
means it has to survive the trip from the browser redirect to the OAuth
callback -- the same gap `state` already crosses via
`sturnus.infrastructure.db.link_state.LinkStateStore` and
`sturnus.application.linking.PendingLink`. Neither has a field for a
verifier, and this task's scope is exactly the two files that make up
`OutlineOAuth` plus this document -- it may not touch `link_state.py` or
`linking.py`. The alternative, caching the verifier keyed by `state` inside
`OutlineOAuth` itself, was rejected: `OutlineOAuth` is expected to hold no
per-flow state (mirroring the "stores no token" rule this whole client is
built around), and an in-process cache would silently break the moment
Sturnus runs more than one replica, or restarts between the redirect and
the callback -- worse than the missing PKCE it would add, since a broken
link flow surfaces immediately while PKCE's protection (defence against
code interception) is not otherwise load-bearing here: the redirect is
same-origin HTTPS and the code exchange happens from Sturnus's own backend,
not a public client.

**Follow-up this implies**: if PKCE is required (confidential clients using
it costs one hash, per the task brief), `PendingLink` needs a
`code_verifier: str` field and `LinkStateStore.issue`/`consume` need to
carry it, generated in `authorize_url()` and threaded back in via whatever
calls `identity_from_code()`. That is a change to files this task does not
own; it is recorded here for whoever picks it up next rather than made
unilaterally.

## Finding 8: invalid or reused authorization code

**Assumed**, standard RFC 6749 §5.2 shape, not directly observed:

```
HTTP 400
{"error": "invalid_grant"}
```

This client treats any non-200 response from the token endpoint as a
`LinkExchangeError` uniformly, rather than branching on the specific
`error` value -- so even if the real error code differs (`invalid_request`,
`invalid_client`, or a different status such as `401`), the client's
behaviour (raise, do not retry silently, do not leak the code or secret)
is unaffected. What is not covered: whether an *expired but syntactically
valid* code returns something distinguishable from a *reused* code, which
would matter if a future feature wanted to tell a participant "that link
expired, try again" versus "that link was already used" -- this client
currently cannot make that distinction and does not try to.

**Not checked**: the actual status code and body for a rejected code, nor
whether a rejected `auth.info` call (after a technically valid token) looks
different from a rejected token exchange.

## What must happen before this ships

1. Register a real OAuth application in **Settings → Applications** on a
   disposable or scratch Outline instance, using a redirect URI that can
   actually receive the callback.
2. Run one real browser-based authorize → callback → token exchange →
   `auth.info` call, and paste the actual request/response pairs here with
   the client secret and access token redacted -- settling in particular
   whether `data.user.id` is really the field, and whether `read` scope is
   accepted by `auth.info` at all.
3. Send a request with an invalid or already-used authorization code and
   paste the actual status code and body.
4. Confirm whether `client_secret` belongs in the token request body or an
   `Authorization: Basic` header (or both are accepted).
5. Revoke the test application afterwards if it was created for this
   purpose only.
6. Decide on the PKCE follow-up in Finding 7 -- either accept the current
   no-PKCE state as a deliberate tradeoff, or schedule the `PendingLink`/
   `LinkStateStore` change needed to carry a `code_verifier`.
7. Update the constants and functions listed in the table above, and
   update this document to describe them as verified rather than
   inferred/assumed.

Until that happens, `OutlineOAuth` should be treated as **untested against
the real API it targets**. Its test suite
(`tests/infrastructure/test_outline_oauth.py`) only proves the client
behaves correctly *given* the shapes assumed above (state carried, secret
never in the browser URL, a clear error on a rejected code or a rejected
identity lookup, nothing sensitive in logs), via `httpx.MockTransport`. It
proves nothing about whether those assumed shapes match reality -- least of
all Finding 5, the user-id field, where a wrong guess would not even show
up as a bug report.
