# Outline API: assumptions the document adapter is built on

Date: 2026-08-20 · `sturnus.infrastructure.documents.outline.OutlineSink`

**Nothing in this document has been verified against a running Outline
instance.** Spec 8.4 flags the endpoint path, request field names, and
response shape as unverified, and this task's brief calls for exactly that
verification: create a scratch collection on a live Outline instance, send
real requests with a revocable token, and record what actually comes back.

That verification did not happen here. This task ran without credentials
for a running Outline instance and without a safe, disposable collection to
test against — the only Outline access available in this environment is an
MCP connector against what appears to be OneLiteFeather's real Outline
workspace, which is not a scratch environment with a revocable token, and
creating documents through it would pollute a real collection rather than
a throwaway one. Per the task dispatch, the adapter was built against
Outline's publicly documented API shape instead, with every assumption
recorded below. **Nothing below is a substitute for Step 1 of the task
brief.** It must still be performed against a real instance, with a scratch
collection and a revocable token, before this adapter ships.


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

The adapter deliberately concentrates each guess into exactly one place, so
that correcting it after real verification touches one line, not a method
scattered through call sites:

| Assumption | Where it is spelled | If verification finds it wrong |
|---|---|---|
| Endpoint path | `_CREATE_DOCUMENT_PATH` constant | Change the one constant |
| Collection-list endpoint path | `_LIST_COLLECTIONS_PATH` constant | Change the one constant |
| Collection-list response shape | `_extract_collections()` | Change the one function body |
| Request field names | `_build_payload()` | Change the one function body |
| Response shape / where the URL lives | `_extract_created_document()` | Change the one function body |
| Auth header format | `OutlineSink.create()`, the `Authorization` header on the client | Change the one header line |
| Retryable vs. permanent status codes | `_PERMANENT_STATUS_CODES` constant | Change the one set |

## Assumption 1: the endpoint

Assumed: `POST {base_url}/api/documents.create`.

Outline's public API is documented as an RPC-style API where every action is
a `POST` to `/api/<resource>.<action>`, not a REST-style
`POST /api/documents` — this is the pattern the constant follows. **Not
checked**: whether this specific route exists, is still current, or requires
a different HTTP method, on the instance this bot will actually target.

## Assumption 2: the request fields

Assumed JSON body:

```json
{
  "title": "Voice session 2026-08-20 14:30",
  "text": "... Markdown body ...",
  "collectionId": "col-1",
  "publish": true
}
```

- `title` — plain text title, assumed to match `document_title()`'s output
  from `sturnus.application.documents` directly (no length limit applied by
  the adapter).
- `text` — assumed to be Markdown, matching what `render_transcript()`
  produces. **Not checked**: whether Outline expects Markdown here or its
  own ProseMirror/rich document JSON format instead — if it is the latter,
  every protocol this bot creates would render as broken or literal
  Markdown syntax rather than formatted text, which is the single riskiest
  assumption in this file.
- `collectionId` — assumed to be the field name (camelCase, matching the
  rest of Outline's documented API surface) that targets the destination
  collection.
- `publish` — assumed `true` publishes the document immediately, making it
  visible to the collection rather than leaving it a private draft attached
  only to the creating token's user. **Not checked**: what the default is if
  this field is omitted, and whether a draft document even produces a
  shareable `url` the bot could post into Discord.

**Not checked at all**: whether there is a request body size limit. Spec 8.4
notes protocols can run to hundreds of kilobytes; if Outline caps request or
document size, a long session's protocol could be rejected outright, and the
adapter currently has no chunking or truncation strategy for that case.

## Assumption 3: the response shape

Assumed successful (200) response:

```json
{
  "data": {
    "id": "doc-1",
    "url": "/doc/protocol-abc-c4c4c4c4"
  }
}
```

- The document lives under a top-level `data` object — this matches the
  documented shape of most Outline API responses, which wrap the payload
  under `data` alongside `ok`, `status`, and `pagination` fields the adapter
  ignores.
- `id` — assumed to be Outline's internal document id, opaque to this bot.
- `url` — assumed to be a **path relative to the Outline instance**, not a
  full URL. This is why `_extract_created_document()` resolves it against
  `base_url` before returning it: the bot posts this URL directly into
  Discord, and a relative path posted as-is would render as a broken link
  rather than a clickable one. If verification finds `url` is already
  absolute, the adapter still behaves correctly — `_extract_created_document`
  returns any string already starting with `http://` or `https://`
  unchanged, so no correction would be needed there even if this specific
  assumption turns out wrong.

**Not checked**: the exact key name (`url` vs. `shareUrl` vs. something
else), and whether the URL returned at creation time is the same one a
human would see after the document is later renamed or moved (Outline
documents are commonly addressed by a slug that can change).

## Assumption 4: authentication

Assumed: `Authorization: Bearer <api_token>` header, matching Outline's
documented API token authentication. **Not checked** against a live
instance — in particular, whether an API token scoped to a single
collection behaves differently from a full-access token for this
specific endpoint.

## Assumption 5: retryable vs. permanent failures

The adapter treats `401`, `403`, and `404` as permanent (raises
`PermanentDocumentError`, which the job queue must not retry) and anything
else that is not a 2xx — in particular `5xx` — as retryable (raises
`httpx.HTTPStatusError` via `response.raise_for_status()`).

This mapping is an assumption about *general* HTTP semantics, not something
specific to Outline, and is considered lower-risk than Assumptions 1-3.
**Not checked against Outline specifically**:

- The exact status code and body for an invalid/revoked token — assumed to
  be `401`. Some APIs return `403` instead for this case; if Outline does,
  the adapter's behaviour is still correct (both are in
  `_PERMANENT_STATUS_CODES`), but the verification step should confirm
  which one actually happens so the distinction between "bad token" and
  "token valid but forbidden" isn't lost.
- The exact status code and body for a non-existent `collectionId` —
  assumed to be `404`. **Not checked**: whether Outline instead returns a
  `200` with an error payload, or a `400`, for a bad collection id. If it is
  either of those, the current adapter would misclassify the failure —
  a `400` would fall through to `raise_for_status()` and be treated as
  retryable, which is exactly the "queue that never drains" failure mode
  this task exists to prevent. **This is the second-highest-risk assumption
  in this file, after the Markdown-vs-ProseMirror body format.**
- Whether Outline returns `429` for rate limiting, which the adapter
  currently treats as retryable by falling through to `raise_for_status()`
  — consistent with general HTTP practice, but not confirmed for this API.

## Assumption 6: listing collections

Added when the console needed to show `document_target` as a name rather
than as the UUID an administrator pasted (see the console design's
Section 6.1). `worker` holds the Outline token, so `worker` reads the list
hourly and mirrors it into `outline_collection`.

Assumed: `POST {base_url}/api/collections.list`, with an `offset`/`limit`
body, answering with the same `data`-wrapped shape the rest of the API
uses:

```json
{ "offset": 0, "limit": 100 }
```

```json
{ "data": [{ "id": "col-1", "name": "Meetings" }] }
```

- The RPC-style path follows the same pattern as Assumption 1 and is
  **not checked** against a live instance.
- `offset`/`limit` pagination is assumed from Outline's documented list
  endpoints; the adapter stops at the first page shorter than `limit`.
  **Not checked**: whether the parameters are named this, and whether a
  server that ignores them would return a full page forever —
  `_MAX_COLLECTION_PAGES` bounds that case rather than trusting it not to
  happen.
- Only `id` and `name` are read. **Not checked**: whether `name` is the
  field a person actually sees in Outline's sidebar, as opposed to a
  title, a slug or a localised label.
- Failure classification is shared with Assumption 5, deliberately: the
  same `_PERMANENT_STATUS_CODES` decides both.

**Lower stakes than Assumptions 1-3, and the code is built to keep them
that way.** A wrong guess here costs the console a collection name, not a
protocol: `sturnus.application.collection_mirror.sweep_outline_collections`
writes nothing when the call fails, so the previous mirror stands and
transcription is untouched. `scripts/verify_outline_api.py` does not cover
this call yet.

## What must happen before this ships

Step 1 of this task's brief, performed against a real instance:

1. Create a scratch collection and a token that can be revoked afterward.
2. Send a real `documents.create` (or whatever the real endpoint turns out
   to be) request and paste the actual request and response here, with the
   token redacted — settling in particular whether `text` must be Markdown
   or Outline's own document format.
3. Send a request with an invalid token and paste the actual status code
   and body.
4. Send a request against a collection id that does not exist and paste the
   actual status code and body — this is the assumption most likely to be
   wrong in a way that breaks the retryable/permanent distinction silently.
5. Confirm whether there is a body size limit, given protocols can run to
   hundreds of kilobytes.
6. Update the constants and functions listed in the table above to match
   what was actually found, and update this document to describe them as
   verified rather than assumed.

Until that happens, `OutlineSink` should be treated as **untested against
the real API it targets** — its test suite
(`tests/infrastructure/test_outline.py`) only proves the adapter behaves
correctly *given* the response shapes assumed above (absolute URLs,
no secrets or transcript content in logs, retryable vs. permanent
distinction), via `httpx.MockTransport`. It proves nothing about whether
those assumed shapes match reality.
