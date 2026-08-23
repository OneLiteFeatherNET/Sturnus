# Sturnus — several destinations, several tenants, and an administrator who can see the work

**Status:** design, being implemented
**Date:** 2026-08-23
**Follows:** [2026-08-23-sturnus-console-personalisation.md](2026-08-23-sturnus-console-personalisation.md), delivered as v0.15.0
**Amends:** [2026-08-21-sturnus-console-design.md](2026-08-21-sturnus-console-design.md) §1.1, §3.3, §5, §6

## 1. What this round is about

v0.15.0 made the console personal: a person decides what is recorded of them, in
their language, in their theme. This round is about the two audiences it left
underserved.

**The administrator** still reads snowflakes. `GET /api/guilds/{id}/directory`
was built in v0.15.0 and is consumed by exactly one page; every other admin
surface renders `Channel 1289374650912837465` where a name is already available
one HTTP call away. The consent roster is a card wall with no pagination and no
bulk action, so withdrawing consent for four people is four confirmations. The
settings page is one flat column of nineteen keys ordered alphabetically. The
queue page cannot be influenced at all — no priority, no ordering, no way to say
"the eight-person meeting first".

**The operator** cannot point Sturnus anywhere but one Outline collection. The
`DocumentSink` port exists and its docstring already names Confluence, but
`entrypoints/worker` constructs `OutlineSink` unconditionally and
`document_provider` selects nothing. One deployment serves one tenant.

## 2. What is decided here, and by whom

Two decisions in this document were taken by the repository owner after being
shown the consequence. They are recorded with that provenance because both of
them change a promise, and a reader a year from now deserves to know that the
change was deliberate rather than drift.

### 2.1 An administrator may download any recording of their guild

**Decided by the repository owner, 2026-08-23**, having been shown that §1.1 of
the console design says the opposite:

> **Only participants of the same session may play its audio.** Not
> administrators-in-general, not anyone with a link.

That promise is now narrower than the system's behaviour will be, so it is
amended rather than left to rot. The new rule:

| Who | May play | May download |
|---|---|---|
| Participant of the session | yes, unchanged | no |
| Administrator of the guild, participant | yes | yes |
| Administrator of the guild, **not** a participant | **no**, unchanged | **yes** |

The asymmetry in the last row is deliberate and worth stating: an administrator
who was not in the room still cannot press play in the console, but can obtain
the file. That is not a security boundary — anyone who can download can play
what they downloaded — it is an interface that refuses to make casual listening
convenient while permitting the deliberate act. The distinction that matters is
in the audit log, not in the bytes.

**Three things make it defensible, and all three are load-bearing:**

- **It arrives switched off.** A per-guild `admin_audio_download_offered`,
  default `false`, in `BOOLEAN_KEYS`. While false the route refuses everyone.
- **Switching it on is an assertion about a document software cannot read.** An
  administrator enabling it asserts that the text at `policy_url`, at the
  current `policy_version`, tells participants that guild administrators may
  obtain copies of their recordings. This is the construction
  `video_consent_offered` already uses, and it applies here with more force
  because this grants access rather than withholding it.
- **Every download is audited at WARNING**, recording who, which session, which
  speaker's track, and **whether the downloader was in the room**. That last
  field is the point: an administrator downloading a meeting they attended and
  one they did not are different events, and a log that cannot tell them apart
  cannot answer the only question anybody will ask afterwards.

**Enabling it requires a `policy_version` bump and a matching change to the
document at `policy_url`, before production.** Bumping invalidates every consent
naming the old version, and people re-consent — that is the mechanism, and it is
deliberate. Enabling the flag does not retroactively make old consents cover
this.

### 2.2 OAuth becomes per-guild, addressed by a guild-specific sign-in link

**Decided by the repository owner, 2026-08-23**, choosing between three ways to
answer a real chicken-and-egg problem.

The problem: `GET /api/auth/login` takes no parameters and reads no cookie —
there is no session yet, that is what login is for. It calls
`authorize_url(state)` on a process-wide client. To choose a *guild's* OAuth
client it would need the guild; to learn the guild it needs an identity; to get
an identity it must already have chosen a client.

The answer chosen: **guild-specific sign-in paths.** `/g/{slug}/sign-in` carries
the guild in the URL, so the guild is known before the round trip starts. The
alternative — a public page listing every guild Sturnus serves — was rejected
because it discloses which organisations use the service to anyone, signed in or
not. An administrator distributes their guild's link themselves.

**How it works:**

1. `/g/{slug}/sign-in` resolves the slug to a guild and its OAuth client.
2. `GET /api/auth/login?guild={slug}` issues a state row **carrying the guild**
   (`console_state` gains a nullable `guild_id`) and redirects to that guild's
   authorize URL.
3. The callback consumes the state, and **the state is what selects the client**
   for the code exchange. `ConsoleAuth` stops holding one `OAuthClient` and
   starts holding a factory keyed by what the state says.
4. `/sign-in` with no guild keeps working exactly as today, against the
   environment-configured client. A deployment that never configures a per-guild
   client behaves identically to v0.15.0.

**The secret.** `guild_oauth_client` stores the client id in the clear and the
secret **wrapped by `KeyWrapper`**, alongside its `encryption_key_id` so key
rotation works the way it already does for audio data keys. Two notes the
implementer must respect:

- `KeyWrapper` passes no AAD today, so a wrapped blob is not bound to the row it
  sits in — a wrapped secret moved between guild rows would decrypt fine. **Bind
  it**: pass the guild id as associated data, or accept and document why not.
- **`api` holds the master key; `link` does not**, and the chart's
  `_helpers.tpl` actively prevents adding it. So per-guild OAuth is available to
  the **console sign-in flow only**. The Discord account-link flow (`/link`)
  stays on the environment-configured client. Saying this out loud is what
  stops somebody "fixing the asymmetry" later by handing `link` the master key,
  which is the one change this architecture exists to prevent.

**Never send a secret back.** `GET` on an OAuth configuration returns the client
id, the base URL, the redirect URI and whether a secret is set — never the
secret, not even masked-but-recoverable. The settings API already renders every
`guild_config` key back to administrators, which is precisely why the secret
must not live there.

## 3. Export becomes a destination, not a hard-coded one

### 3.1 Two seams, not one

`DocumentSink` (`application/documents.py`) is already the right port:
`create(title, body, target) -> CreatedDocument`, with `target` as a parameter
because it is per-guild. What is missing is a **registry** — `document_provider`
is read and used only to pick an account-link mapping, never to select a sink.

But a second seam is needed and is easy to miss. `render_transcript` produces
one Markdown string, and the packaged template emits Outline's `mention://`
chips while `escape_markdown` escapes Markdown specials. **A PDF or HTML sink
handed that string gets Outline's mention syntax as literal text and
Markdown-escaped HTML.** So the renderer must vary with the destination too:

```
Transcript ──▶ Renderer (template + escaper) ──▶ body ──▶ Sink ──▶ CreatedDocument
                    ▲                                      ▲
                    └── chosen by format ──────────────────┘
```

A format is therefore a **pair**: a renderer and a sink. That pairing is the
thing the registry holds, and it is why `document_provider` alone was never
enough.

### 3.2 The formats

| Format | Renderer | Sink | Notes |
|---|---|---|---|
| `outline` | Outline-flavoured Markdown, `mention://` chips | HTTP to `documents.create` | today's behaviour, unchanged |
| `markdown` | plain CommonMark, mentions as names | object store | no external service |
| `html` | HTML template, HTML escaping | object store | must not reuse `escape_markdown` |
| `pdf` | HTML, then rendered | object store | see §3.4 |
| `confluence` | Confluence storage format | HTTP | its own escaping rules |

`CreatedDocument(id, url)` is the contract, and the announcement path only ever
posts `document_url` — so an object-store sink must produce a URL a participant
can actually open. That means a console route serving the artefact under the
same participant authorisation the audio path uses, **not** a presigned S3 URL
that outlives the session's access rules.

### 3.3 Configuration, per guild, in a table

`guild_export_target(id, guild_id, format, name, target, config, wrapped_secret,
encryption_key_id, enabled, created_at, updated_at)`.

Not in `guild_config`, for two reasons: an export target has **structure** (a
base URL, a space key, credentials), and `guild_config` is a flat text registry
whose every value the settings API renders back to administrators. A Confluence
token must not be renderable.

A guild may have **several enabled targets**. Publishing writes to each and
records each outcome; one failing destination must not lose the others. That
makes `session.document_url` insufficient — a session now has *documents*, so
`session_document(session_id, target_id, provider, document_id, url,
created_at)` records them, and `session.document_url` stays as the primary for
the announcement and for everything already reading it.

### 3.4 What is deliberately not built

**PDF rendering in the worker image.** Every route to a PDF (weasyprint,
wkhtmltopdf, a headless browser) is a large native dependency in an image that
today holds Python and a Whisper model. The `pdf` format is specified here and
its renderer/sink pair is defined, but the implementation is a separate decision
about image size and attack surface that this document does not pre-empt. If it
is built, it belongs behind the same registry and needs no other change.

## 4. The transcript, on demand

The transcript is **already retained indefinitely** — `transcription_job.transcript`
holds the serialised segments, the retention sweep deletes only the S3 audio, and
nothing clears the column except an enqueue or a requeue. So "the bot keeps the
transcript" needs no new storage; what is new is **showing it and exporting it**.

- `GET /api/sessions/{id}/transcript` — the assembled, chronological transcript,
  under the **same participant authorisation as the session's metadata**. This
  grants nothing new: the transcript is already in the published document, and
  whoever may read the session may read what it said.
- `POST /api/sessions/{id}/export` with a target id — renders and publishes to
  one configured destination on demand, rather than only automatically at
  session close. Administrator-gated, because it spends an external call and
  writes into somebody else's system.

**One thing to be careful about:** a session whose audio has been deleted by
retention still has its transcript. That is correct and intended — the retention
window is about the recording, not about the minutes. But it means the transcript
tab must exist for sessions whose audio tab does not, and must say why rather
than looking broken.

## 5. Spectrograms by default — and what that costs

A spectrogram is computed per request today and never stored. "Generate them by
default" can mean two things with very different consequences:

- **A stored artefact** computed once at job completion, when the worker already
  has the plaintext WAV on disk.
- **A warm cache** of the same on-demand computation.

This document chooses the **stored artefact**, with one rule attached:

> **A stored spectrogram is deleted when its audio is deleted.**

Without that rule, enabling the setting would create a retained rendering of a
person's voice activity that **outlives the retention window their audio was
subject to** — because the retention sweep deletes the S3 object and nothing
else. The console design already argues that a spectrogram "is less than the
audio and it is not nothing"; it must therefore not quietly become the thing
that survives.

So: the artefact lives beside the audio in the object store, is keyed from the
job, and the retention sweep deletes both in the same pass. After deletion the
track is neither playable nor visualisable, exactly as today.

The setting is per-guild, default `false`. Turning it on costs storage and worker
time and buys an instant spectrogram; leaving it off costs a full streamed
decrypt and 600 FFTs per view, which is what happens today.

## 6. Parallel transcription, and where the cap lives

Concurrent claiming is **already safe** — `JobQueue.claim` uses `FOR UPDATE SKIP
LOCKED`, and `complete` takes a session-wide lock so exactly one worker sees
"I was last" and creates the document. What is missing is a limit, because
letting every worker pile onto one session starves every other and multiplies
peak memory (each loaded Whisper model is large and the cache is unbounded by
design).

`max_parallel_tracks` is a per-guild integer, and **it is enforced inside the
claim statement**. Counting after the claim cannot work: by then the row is
`running`, and releasing it back fights the lease logic.

Two things this exposes, both of which belong to whoever implements it:

- **The lease is a reclaim lease and nothing renews it.** With one worker that
  was latent. With several it is reachable: a job outliving 30 minutes is
  claimable while the first worker still holds it, and then two workers call
  `complete`. Whether that is safe today must be established and pinned, or
  fixed.
- **Storage is the real blocker to more than one worker**, not the queue. The
  worker's `work_dir` and model cache are RWO volumes with a `Recreate`
  strategy. The cap is theoretical until that is addressed, and the chart must
  say what an operator has to change.

## 7. Sharding, honestly

`AutoShardedClient` opens several gateway connections **from one process**. That
is worth having — a reconnecting shard no longer stalls the others — and it
changes no deployment invariant.

**Several processes each owning a shard range is a different change and is not in
this round.** The chart pins the bot to `replicas: 1` with `strategy: Recreate`
and states why: two instances would hold two gateway connections and record
every session twice. Four dictionaries key runtime state per guild in process
memory, and the reconfigure lock is an `asyncio.Lock` that does not cross
processes. Stage two needs shard-aware guild ownership, a StatefulSet with
per-pod storage, and `recover_orphans` taught that it is not the sole owner of
its directory. The invariant should be **named and single-sited** now, the way
`MAX_CONCURRENT_SESSIONS_PER_GUILD` was, so that lifting it later is a constant
and whatever the type checker then points at.

## 8. Guild onboarding from the web

Every step of setting up a guild that matters needs a Discord token, and `api`
must never hold one. So the console cannot do it directly — but it does not need
to, because the pattern already exists in reverse.

Today the bot **mirrors** Discord state into the database for `api` to read. The
inverse is an **intent** the bot applies: `api` writes what should be true,
the bot's existing ten-second reconcile tick makes it true and writes back what
happened.

- `guild_setup_intent(guild_id, requested_by, requested_at, channel_ids,
  consent_role_name, applied_at, outcome, error)`.
- The console offers the bot's invite URL — buildable from the client id alone,
  and the one genuinely web-doable step.
- Everything else (creating the consent role, setting Speak overwrites, syncing
  commands) becomes an intent the bot applies through the **same `plan_setup`**
  the slash command uses. One planner, two callers; a second implementation of
  the consent protection is the last thing this system should grow.
- A guild the bot has not joined yet has empty mirrors, so the interface must
  say "waiting for the bot to arrive" rather than showing an empty channel
  picker as though the guild had no channels.

## 9. New persisted data

One migration, **0013**, owns all of it, so parallel branches do not collide on
revision numbers.

| Where | What | Why |
|---|---|---|
| `session.title`, `session.description` | free text, participant-editable | §10 the recording page's own tab; searchable |
| `transcription_job.priority` | integer, default 0 | queue ordering the console can influence |
| `transcription_job.sample_rate`, `channels`, `stored_bytes` | audio metadata | today re-read from the RIFF header on every request |
| `transcription_job.spectrogram_key` | object-store key, nullable | §5, deleted with the audio |
| `guild_export_target` | per-guild destinations | §3.3 |
| `session_document` | one row per published destination | §3.3 |
| `guild_oauth_client` | per-guild OAuth, secret wrapped | §2.2 |
| `guild_setup_intent` | what the console asked the bot to do | §8 |
| `console_state.guild_id` | nullable | §2.2 — the state selects the client |

Settings added to `guild_config` (no migration — they are rows):
`max_parallel_tracks`, `spectrograms_by_default`, `admin_audio_download_offered`.

## 10. The interface

Every control below comes from the primitives built in
`feat/console-ui-primitives`: `UiSelect`, `UiCombobox`, `UiDatePicker`,
`UiChipInput`, `UiTabs`, `UiDisclosureList`, `UiPagination`. **No page hand-rolls
a `<select>` again** — there are seven of them today, four of which are the same
guild switcher copy-pasted.

- **Names, not ids, everywhere.** `GuildDirectory.members` is already parsed and
  consumed nowhere; wiring it up resolves six of the surfaces on its own. The
  rule from the previous round stands: the name is the value, the id is the
  subtext, and an id with no mirror row renders as the id plus a note — never
  as a blank and never silently dropped.
- **Consents** become a paginated `UiDisclosureList`: a row per person,
  expanding to its actions, with selection for bulk withdrawal. A bulk action
  must state exactly what it will apply to before it applies to anything.
- **Bot settings** group into `UiTabs` by what a key is *for* — recording,
  consent and policy, transcription, publishing, retention — and each save
  reports whether the change is live, which the API already answers through
  `takes_effect`. "Live settings reload" is the bot's ten-second reconcile: the
  interface should say that plainly rather than implying a push.
- **Recordings** get leaner cards with no player; playback moves entirely to the
  detail page, which gains `UiTabs`: the meeting, each track, the transcript,
  metadata, title and tags, export.
- **Search** becomes one `UiChipInput` mixing tag chips and free text, and the
  free text now reaches titles. It still does not reach transcripts, and the
  page must keep saying so.
- **The queue** gets the sections it lacks, pagination, drag-and-drop
  prioritisation writing `transcription_job.priority`, and quick actions that
  reprioritise by participant count or by recording length. Drag-and-drop needs
  a keyboard path — a control only reachable with a mouse is not a control.

## 11. Delivery

Independent, in parallel, each its own branch and pull request:

1. `feat/console-ui-primitives` — §10's toolbox, consumed by nobody yet
2. `feat/bot-sharding` — §7
3. `feat/api-model-registry` — the models and who may choose one
4. `feat/worker-parallel-tracks` — §6
5. `feat/api-admin-audio-download` — §2.1
6. `feat/db-phase-2-schema` — migration 0013, §9, plus the stores
7. `feat/api-export-targets` — §3, on 6
8. `feat/api-transcript-and-export` — §4, on 6
9. `feat/api-spectrogram-artefacts` — §5, on 6
10. `feat/api-queue-priority` — §10's queue backend, on 6
11. `feat/api-guild-oauth` — §2.2, on 6
12. `feat/api-guild-onboarding` — §8, on 6 and 11
13. The console pull requests, on 1 and on whichever API they consume

Test-driven throughout. Decisions belong in pure modules where a test can reach
them without a database or a DOM; that convention is why this codebase can be
changed at this pace at all.
