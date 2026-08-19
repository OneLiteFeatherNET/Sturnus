# Sturnus — Design

Discord voice transcription with Outline storage for OneLiteFeather.

Status: Draft for review · Date: 2026-08-19

## 1. Goal

A dedicated Discord voice channel is recorded automatically and stored as a
chronological transcript in a document system — in this phase, Outline,
through a swappable adapter (Section 8). Speakers appear in it as real users
of the target system, provided they have linked their Discord account to it
once.

The name follows the organization's bird-naming convention (Falco, Otis,
Ducula, Pica, Guira, Aves): *Sturnus vulgaris*, the common starling, is known
for its vocal mimicry.

## 2. Scope of this phase

The goal is an MVP on the **Discord → Outline** path that can be tested in
real operation. Anything that doesn't get this path working is deferred.

Deliberately outside this phase:

- **No LLM summarization.** The document contains only the raw transcript. A
  summarization stage built on the existing Ollama instance is a possible
  later phase, not part of this design.
- **No speaker diarization.** Discord delivers separate audio streams per
  user, which means speaker separation is already a solved problem here and
  needs neither pyannote nor WhisperX.
- **No per-user OAuth tokens.** The bot writes to Outline with a single
  service token. The OAuth flow serves only to establish identity once.
- **No live transcript.** Transcription only begins once the session has
  ended, and runs over the complete recording.
- **Only the Outline adapter.** The port for document storage is drawn, but
  only Outline is implemented behind it. Confluence, Notion, and Markdown
  files are planned extensions, not part of this phase's deliverable.
- **Templates ship with the image, they are not configurable.** The Jinja2
  templates for the document and for Discord messages come from the image; an
  admin command for setting custom templates comes later. This saves on UI
  surface and at the same time defuses the design's biggest security risk
  (Section 8.2), because in the MVP no externally supplied template is ever
  executed.
- **Only one recording channel per guild.** The data model is prepared for
  multiple channels; the configuration in this phase is not.

## 3. Legal framework

Recording non-publicly spoken words without consent is a criminal offense
under § 201 StGB. This design therefore treats consent not as a feature, but
as a precondition for any audio processing.

### 3.1 Two-tier protection

**Primary — Discord permissions.** In the recording channel, `@everyone` gets
`Speak: deny`, and the consent role gets `Speak: allow`. Anyone who has not
consented technically cannot send audio.

**Secondary — bot-side SSRC filter.** Users with `Administrator` permission
bypass channel overrides and can speak regardless of role. The bot therefore
checks, for every incoming stream, whether the associated user holds the
consent role, and discards the packets otherwise — before they reach any
buffer. This filter is not redundant; it covers a real, existing bypass.

The check happens continuously, not once at session start. If someone revokes
their consent during a running session, their stream is discarded from that
moment on; audio already recorded remains untouched, since under Art. 7(3)
GDPR a revocation only takes effect going forward.

### 3.2 Deployment prerequisites

These points are not optional — they are a condition for operation:

- **Non-recorded voice channels** must exist as an alternative. If consent is
  the only way to take part in voice at all, its voluntariness is open to
  challenge under Art. 7(4) GDPR (the prohibition on tying).
- **The channel name and channel topic name the recording.** Because the bot
  joins automatically, without an explicit start command, there is no moment
  at which someone consciously triggers the recording — the labeling has to
  live on the channel itself.
- The bot posts a visible announcement in the channel's text part on every
  join.
- **The privacy policy states the retention period for the audio recordings
  and their purpose.** Because recordings outlive the transcription
  (Section 12), this is not an implementation detail but part of what
  consent is actually given to. If the duration changes, `policy_version`
  changes and consent has to be obtained anew.

### 3.3 Revocation

`/consent revoke` withdraws the role and sets `revoked_at` (Art. 7(3) —
revocable at any time). Transcripts already created remain in place; the
revocation takes effect from the moment it is issued.

## 4. Architecture

Three deployments, one shared PostgreSQL database, one S3 bucket.

### 4.1 `bot`

Python 3.12, `discord.py` 2.x with `discord-ext-voice-recv`. 1 CPU,
`replicas: 1` fixed, no HPA — a single gateway connection is not horizontally
scalable. No ingress.

On the choice of library: `discord.py` does not support voice receive. The
alternative would be `py-cord`, whose `WaveSink` simply concatenates packets
per user without padding in silence — the streams drift apart and the
transcript's chronology becomes unusable. `discord-ext-voice-recv` grants
access to the RTP timestamps, from which the absolute position of every
segment can be reconstructed. That is the reason for the choice.

Responsibilities: slash commands, the session state machine, voice receive,
the consent filter, recording, S3 upload on session end, job enqueueing,
publishing the document link. Health and metrics endpoints on an internal
port (`/healthz`, `/readyz`, `/metrics`, `/version`).

### 4.2 `link-service`

Python 3.12, a small HTTP service. The only deployment with ingress (via
Cloudflare Tunnel, analogous to the Outline installation).

Responsibilities: the OAuth callback for account linking. Kept separate from
the bot because the bot pod holds both the Discord and the Outline service
token and should not be publicly reachable — and because a deploy to the link
flow would otherwise force a bot restart, which would lose every recording in
progress.

### 4.3 `worker`

Python 3.12, `faster-whisper`. 4 CPU. No ingress.

Responsibilities: pull transcription jobs from the queue, transcribe the
complete speaker stream, delete the audio; once every speaker in a session is
done, merge the transcripts and create the Outline document.

### 4.4 Code structure

All three deployments share one package with an inward-facing dependency
rule:

```
src/sturnus/
  domain/          pure logic, no I/O
    session.py       session state machine
    timeline.py      RTP time reconstruction, segment merge
    transcript.py    Markdown and mention rendering
    consent.py       consent resolution
  application/     use cases, orchestrate ports
    ports.py         Protocol definitions
    record_session.py
    transcribe_speaker.py
    publish_document.py
    link_account.py
  infrastructure/  adapters to concrete tech
    discord/         voice-receive adapter, cogs
    db/              ORM models, repositories, migrations
    objectstore/     S3, encryption
    whisper/         faster-whisper
    documents/       DocumentSink adapters, currently Outline
    templates/       Jinja2 environment and bundled default templates
```

**The dependency rule:** `domain` imports neither `application` nor
`infrastructure`, and no third-party library that does I/O — no `discord`, no
`sqlalchemy`, no `boto3`. `application` knows only `domain` and its own
ports, never a concrete adapter.

This is not an end in itself. All the logic that makes this project difficult
— time reconstruction, session transitions, segment merging — ends up in
code that is testable without a Discord connection, without a database, and
without an audio file. And the adapter that, per Section 15, carries the
biggest third-party risk, is swappable without touching the core logic.

So the rule doesn't erode over time, it is **enforced as a test**
(Section 14), not documented as a convention.

### 4.5 Ports and their boundary

Abstracted as a Protocol are the systems that have to be replaced by fakes in
tests, or whose implementation may change:

| Port | Rationale |
|---|---|
| `TranscriptionEngine` | Faked in unit tests; switched between `large-v3-turbo` and `small` |
| `AudioStore` | S3 replaced by an in-memory fake in unit tests |
| `DocumentSink` | The actual extension point (Section 8): Outline today, Confluence, Notion, or file storage later |
| `VoiceReceiver` | Encapsulates `discord-ext-voice-recv`, keeps a library swap local |

**Repositories deliberately have no interfaces defined for them.** The data
access layer is tested against a real PostgreSQL instance via Testcontainers
(Section 14); an interface with exactly one implementation and a real
database test behind it would be ceremony without benefit. SOLID calls for
abstraction where implementations vary — not everywhere. This boundary is
part of the design, not negligence.

## 5. Session lifecycle

The bot observes `on_voice_state_update` for the configured channel.

### 5.1 State transitions

| Trigger | Transition |
|---|---|
| First user **with the consent role** joins the channel | `IDLE` → `RECORDING`, the bot joins, an announcement is posted |
| A user without the consent role joins the empty channel | no transition — nobody can speak |
| Last eligible user leaves the channel | `RECORDING` → `GRACE` |
| An eligible user returns during `GRACE` | `GRACE` → `RECORDING`, the same session continues |
| `empty_grace_seconds` elapses | `GRACE` → `CLOSING` |
| `idle_timeout_minutes` with no audio at all | `RECORDING` → `CLOSING` |
| `max_session_hours` reached | `RECORDING` → `CLOSING` |

`CLOSING` closes the recording files, uploads them to S3, enqueues one
transcription job per speaker, sets the session to `closed`, and leaves the
channel.

One session corresponds to exactly one Outline document.

### 5.2 Testability

The state machine is implemented as a pure class with an **injected clock**
and knows neither Discord nor the database. Exactly the part that would
otherwise only be verifiable with real people in a voice channel becomes
deterministically unit-testable this way.

### 5.3 Completion and document creation

One transcription job is created per speaker. After every successfully
completed job, the worker checks whether another job is still open for that
job's session; if none remains open, the same run performs the merge and
creates the document. The check runs in the same transaction as the job's
status change, so that two jobs ending at the same time don't produce two
documents.

A job per speaker instead of one per session has two reasons: a failed
attempt only retries the affected speaker instead of the entire session, and
progress on a multi-hour recording is observable. Jobs are worked through
sequentially — `faster-whisper` already uses all of the worker's cores, so
parallel jobs would just slow each other down.

## 6. Audio pipeline

### 6.1 Capture

Incoming Opus packets are decoded per user **to 16 kHz mono PCM immediately
on receipt** — Whisper's target format. That eliminates a later resampling
step.

Silence is **padded in** based on the RTP timestamps. This keeps all of a
speaker stream's buffers the same length and exactly in sync, which turns
merging into a trivial operation on a shared timeline instead of an
error-prone heuristic. The padding costs memory, but no compute time:
`vad_filter=True` lets Whisper skip over the silence.

### 6.2 Time reconstruction

RTP timestamps run at 48000 ticks per second for Opus/48 kHz. The starting
value is random per SSRC, which is why absolute time is determined via a
reference point:

On the **first packet of an SSRC**, the pair `(wall_clock_now, rtp_ts_first)`
is recorded. For every subsequent packet:

```
absolute_time = wall_clock_first + (rtp_ts - rtp_ts_first) / 48000
```

This gives sample-accurate timing within a single user's stream and
wall-clock accuracy for aligning between users; the deviation corresponds to
the network jitter of each stream's first packet and is typically under
100 ms — enough for a readable transcript.

**Pitfall:** a user's SSRC changes on reconnect. The SSRC → Discord-user
mapping is therefore continuously maintained via the speaking events, and
every new SSRC gets its own reference point.

### 6.3 Recording and hand-off

The recording runs across the entire session as **one continuous stream per
speaker**, with no segmentation. Only once the session transitions to
`CLOSING` are the files closed, uploaded to S3, and one transcription job
enqueued per speaker.

The reason is quality: every cut in the audio is a place where Whisper loses
its context, sentences spanning the cut get recognized worse, and language
detection has less material to work with. A single pass over the complete
recording delivers the best transcription this model can produce.

The price is latency. Transcription only begins after the session ends and,
at roughly 1× realtime, takes about as long as the actual speaking time: a
four-hour session with two hours of actual speech is available as a document
roughly two hours after it ends. That's why the document link is
subsequently posted to the channel (Section 8.5) — otherwise, anyone who has
left the session would never learn that the transcript is ready.

**The bot does not buffer in memory; it writes continuously to a volume.**
This decouples recording length from RAM. At 16 kHz mono, one hour per
speaker takes up roughly 115 MB; the `max_session_hours` cap bounds the worst
case, and the volume is sized for `max_session_hours × expected number of
speakers`.

**The volume is a PVC, not an `emptyDir`.** Since the recording is not
segmented, an entire session hangs on this one file — an `emptyDir` would
lose it on every reschedule. A SIGTERM handler closes the files cleanly and
enqueues the jobs; if the bot can't get ahead of a hard kill, it finds the
orphaned recordings on the PVC at its next start, uploads them, and enqueues
them after the fact. At `replicas: 1`, an RWO PVC is sufficient for this.

Without these two precautions, a single deploy would destroy a multi-hour
recording — with segmentation into sections, the loss would have been
limited to one section; here, it is not.

## 7. Transcription

`faster-whisper` with `large-v3-turbo` in int8 as the default. Turbo is a
distilled decoder, runs on 4 cores at roughly 1× realtime with about 1.6 GB
of RAM, and delivers noticeably better results for German than `small`.
`small` remains a configurable fallback in case the sizing doesn't work out
in production.

**Language is detected automatically, but only once per speaker and
session.** Whisper determines the language from the first 30 seconds of a
run — with silence padding, that could well be silence. Detection therefore
runs on the **first VAD segment with substantial speech**, not on the start
of the file. The result is recorded in `session_participant` and set as
`language` for that speaker's entire run.

If detection doesn't find anything solid to go on — for example for a
speaker with only a few utterances — a configurable default language kicks
in.

`vad_filter=True` skips the padded-in silence.

**Hallucination risk on long runs.** Whisper carries context between its
30-second windows via `condition_on_previous_text`, which improves quality —
but on long audio it can lead to cascades in which text that has once
drifted off reinforces itself. This risk is greatest on a run over a
complete session, because no cut ever resets the context. The
`compression_ratio_threshold` and `no_speech_threshold` thresholds therefore
stay active, and turning off `condition_on_previous_text` is the fallback if
repetition artifacts show up in production.

Each user stream is transcribed individually. The resulting segments carry
offsets relative to that speaker's recording start, which are converted to
absolute time via that speaker's reference point (Section 6.2).

## 8. Document storage

The transcripts' destination is swappable. Outline is the first, and in this
phase the only, adapter built; Confluence, Notion, or storing as Markdown
files should be addable later without touching the core logic.

What gets built, then, is **the seam, not the stock**: the port is drawn
cleanly and Outline is implemented behind it. Building further adapters now,
before a real target exists, would cement assumptions that would turn out
wrong the moment a second real adapter arrives.

### 8.1 The port

`domain` produces a **target-neutral transcript model**: a list of blocks
made of a timestamp, a speaker identity, and text, plus session metadata and
the participant list. It contains no markup whatsoever.

The speaker identity carries every known component — Discord ID, the frozen
Discord display name, and, if linked, the identifier at the target system.
Which of these show up in the result, and in what form, is decided by the
adapter alone.

The `DocumentSink` port covers creating a document from this model and
returning a callable URL. It knows nothing of collections, spaces, or file
paths — those concepts live in the configuration of the respective adapter.

### 8.2 Rendering via Jinja2

Every adapter renders the transcript model via a **Jinja2 template**. The
reason is not configurability for its own sake, but that the target formats
have no shared representation: Outline uses `@[Name](mention://user/<id>)`,
Confluence `<ac:link><ri:user/></ac:link>`, Notion structured JSON, and plain
Markdown has no concept of mentions at all. A single hard-wired renderer
could only serve the lowest common denominator.

Every adapter ships with a default template. Resolving a different template
stored in the configuration is provided for in the model, but not usable in
the MVP (Section 2).

The same engine renders the Discord messages — the recording announcement,
the consent notice, the completion message with the document link — so that
the wording and language of these texts can be adjusted without a code
change.

**Templates run inside a `SandboxedEnvironment`.** Jinja2 is not a sandbox
out of the box: an expression like `{{ ''.__class__.__mro__ }}` opens a path
to arbitrary code execution. The environment is therefore given only the
transcript model and a fixed set of filters — nothing that performs I/O.

In the MVP, all templates come from the image, so this safeguard doesn't yet
cover an actual attack surface — it prepares for one: as soon as templates
can be set via a command, an unprotected environment would be equivalent to
handing every guild administrator a shell in the bot pod. Putting the sandbox
in place now costs nothing and prevents that later extension from landing on
ground that can't bear it.

**Values inserted into a template are escaped specifically for the target
format.** Discord display names and transcript text are not trustworthy —
anyone who names themselves `[hier klicken](https://…)` would otherwise
inject a link into every transcript they appear in, or break out of the
surrounding markup with `](`. Every adapter provides an escaping filter for
its format; the templates use it for every value drawn from the model. HTML
autoescaping is no substitute for this, because the target format is not
HTML in most cases.

### 8.3 The Outline adapter

One document per session in the configured collection, a title with date and
time, no H1 at the top — in Outline the title is its own field. Consecutive
segments from the same speaker are merged into a single block.

Linked users are rendered as a real Outline mention, which notifies them;
behind it, in parentheses, sits the Discord display name, linked to the
Discord profile via the Discord ID:

```markdown
**14:32:05** · @[Max Mustermann](mention://user/9c8b…) ([maxm](https://discord.com/users/1234…))

Der gesprochene Text dieses Blocks.
```

If no Outline account is linked, only the mention is omitted; the linked
Discord identity remains:

```markdown
**14:33:11** · [gastnutzer](https://discord.com/users/9876…)
```

The Discord ID is the stable anchor: display names change, the ID doesn't. A
transcript therefore stays attributable even after someone has renamed
themselves or left the server. The names come from `session_participant` and
are frozen at the time of the session; the participant list at the top of the
document is built from the same source.

> **To verify during implementation:** whether Outline generates a
> notification per mention or per document and user. In a long transcript,
> the same person may be named in hundreds of blocks — if notification
> happens per mention, that's unusable. Fallback in that case: render only a
> speaker's first mention as an actual mention, and all subsequent ones as
> plain text with a Discord link.

### 8.4 Account linking

`/link` generates a short-lived, signed state and replies ephemerally with an
authorization URL. After the user consents, the `link-service` receives the
callback, exchanges the code for a token, uses it to query the user's
identity **once**, stores the target system's identifier along with the
display name — and discards the token.

No access token is ever persisted. That eliminates token encryption, refresh
handling, and revocation entirely.

The link is stored **per target system** (`provider` in `account_link`), not
globally. A future Confluence adapter needs its own mapping; the Outline link
is no good for that.

`/link remove` deletes the mapping.

> **To verify during implementation:** Outline runs on version 1.9.1 and
> supports OAuth applications as a provider. The exact endpoint paths, the
> scope names, and the endpoint for querying one's own identity need to be
> checked against the running instance, rather than assumed from the
> documentation.

### 8.5 Publishing the link

Because transcription only begins after the session ends, and can take hours
depending on speaking time, the channel has long since emptied out by the
time the document exists. The bot therefore posts the link to the finished
document in the text part of the recording channel as soon as it's ready —
the wording comes from a Jinja2 template.

The worker itself does not post. It holds no gateway connection, and it
shouldn't own the Discord token either; it sets the session to `documented`
and stores `document_url`. The bot polls this state every
`publish_poll_seconds` (default 30), posts the message, and sets
`announced_at` — this field prevents duplicate announcements after a
restart.

Polling instead of `LISTEN`/`NOTIFY`: the database is reached through
PgBouncer, and in transaction pooling mode, notifications don't get passed
through. With only a handful of sessions per day, polling every 30 seconds is
the more robust choice compared to a direct connection that bypasses the
pooler.

## 9. Data model

PostgreSQL via CloudNativePG, its own database following the cluster's
existing `database/` pattern.

Access is exclusively through **SQLAlchemy 2.0 in async mode**
(`DeclarativeBase`, `Mapped[...]`, `async_sessionmaker`) with `asyncpg` as
the driver. Raw SQL access alongside the ORM is ruled out: in the RAG bot,
ORM models and direct `asyncpg` access exist side by side, which results in
two parallel data-access paths to the same database. Sturnus has exactly
one.

Schema changes go through **Alembic** migrations, applied when `worker`
starts up — not via `create_all()` and not via manual DDL. The RAG bot has no
migrations; that's a gap, not a pattern to adopt.

| Table | Contents |
|---|---|
| `guild_config` | Runtime configuration per guild (Section 11) |
| `account_link` | `discord_user_id` + `provider` (composite PK), `external_user_id`, `display_name`, `linked_at` |
| `consent` | `discord_user_id`, `guild_id`, `granted_at`, `revoked_at`, `policy_version`, `source` |
| `oauth_state` | `state` (PK), `discord_user_id`, `created_at`, `expires_at` |
| `session` | `id`, `guild_id`, `channel_id`, `started_at`, `ended_at`, `end_reason`, `status`, `document_provider`, `document_id`, `document_url`, `announced_at` |
| `session_participant` | `session_id`, `discord_user_id`, `discord_display_name` (frozen at session time), `detected_language`, `first_seen_at` |
| `transcription_job` | `id`, `session_id`, `discord_user_id`, `s3_key`, `encryption_key_id`, `retention_until`, `audio_deleted_at`, `status`, `attempts`, `error`, `transcript` |

The queue is `transcription_job`, consumed via
`select(TranscriptionJob).with_for_update(skip_locked=True)`.
A message broker is deliberately not used: PostgreSQL is needed for the
mapping regardless, and the expected volume of a handful of sessions per day
doesn't justify an additional operational component.

## 10. Slash commands

| Command | Permission | Effect |
|---|---|---|
| `/consent` | everyone | Ephemeral embed with a privacy notice and *Accept* / *Decline* buttons. On acceptance: role granted, entry created with the current `policy_version` |
| `/consent revoke` | everyone | Withdraw role, set `revoked_at` |
| `/consent status` | everyone | Own consent and linking status |
| `/link` | everyone | Ephemeral reply with authorization URL |
| `/link remove` | everyone | Delete link |
| `/audio delete` | everyone | Delete own audio recordings immediately, regardless of the retention period |
| `/audio purge` | Admin | Delete a named user's recordings (Art. 17 GDPR) |
| `/config …` | Admin | Read and set runtime configuration |

All replies are ephemeral. Admin commands adopt the existing
`require_admin()` pattern from the RAG bot.

## 11. Configuration

Configurable at runtime via the `/config` group, stored in `guild_config`.
The bot and worker share the store.

| Key | Default | Consumer |
|---|---|---|
| `voice_channel_id` | — | Bot |
| `consent_role_id` | — | Bot |
| `empty_grace_seconds` | 60 | Bot |
| `idle_timeout_minutes` | 15 | Bot |
| `max_session_hours` | 4 | Bot |
| `publish_poll_seconds` | 30 | Bot |
| `document_provider` | `outline` | Worker |
| `document_target` | — | Worker |
| `audio_retention_days` | 30 | Worker |
| `policy_version` | — | both |
| `policy_url` | — | both |

Not configurable at runtime, but via environment variables instead: the
Whisper model, the default language used as a fallback for auto-detection,
the maximum number of retries per transcription job (default 3), the
database and S3 connection, tokens, and the master key for audio encryption.

`document_target` is deliberately named generically: for the Outline adapter
it's a collection ID, for a future Confluence adapter a space key, for file
storage a path. Its interpretation is up to the adapter.

## 12. Retention and protection of the recordings

Audio recordings are **not deleted immediately after transcription**;
instead they are kept for `audio_retention_days` (default 30). The purpose
is reprocessing: without it, a failed or noticeably bad transcription
couldn't be retried, and an improved model couldn't be applied to existing
material.

This extension is the most security-critical part of the system. Raw audio
from private conversations is considerably more sensitive than the
transcript produced from it, and it now sits around for weeks instead of
minutes.

### 12.1 Protective measures

- **Encryption before upload.** The bot encrypts every recording locally with
  AES-256-GCM before it leaves the pod. Anyone gaining access to the object
  store therefore gets no audible data. Server-side encryption alone would be
  insufficient here, because it offers no protection against the storage
  operator or against a stolen access key.
- **Envelope encryption.** A dedicated data key is generated per session and
  stored alongside the job, encrypted with the master key from SOPS;
  `encryption_key_id` points to the master key that was used. This means
  rotating the master key doesn't require re-encrypting existing material.
- **A dedicated bucket with its own credentials**, separate from every other
  application, with no public access. The bot writes, the worker reads and
  deletes; nobody else holds the credentials.
- **No object lock.** An immutability lock would conflict with the deletion
  obligation under Art. 17 GDPR — it would prevent exactly the deletion that
  must be possible on request.

### 12.2 Enforcing the retention period

`retention_until` is set when a job is created. A periodic run in the worker
deletes expired objects and records `audio_deleted_at`. A bucket lifecycle
rule additionally acts as a second layer, so that a permanently failed worker
doesn't lead to unbounded retention.

The rule alone is not enough: retention also has to be traceable in the
database, and `audio_deleted_at` is the proof that deletion actually
happened.

### 12.3 Deletion on request

`/audio delete` deletes a user's own recordings from every session
immediately, regardless of the retention period. For administrators,
`/audio purge` does the same for a named user, so that information and
erasure requests under Art. 17 GDPR can be handled.

Transcripts already created are unaffected by this — they are a separate
processing result and live in the document system, not with Sturnus.

### 12.4 Remaining data

- The local recording file on the PVC is removed after a successful upload.
- **`consent` entries are kept permanently**, even after revocation — this is
  the record-keeping obligation under Art. 7(1) GDPR; `revoked_at` documents
  the revocation instead of deleting the record.
- `account_link` can be deleted at the user's request.
- Neither audio data nor transcript content appears in logs.
- Finished transcripts are subject to the target system's lifecycle and are
  not managed by Sturnus beyond that.

## 13. Repository, delivery, and operations

### 13.1 Versioning and release

**Release Please**, per OLF standard, fed from Conventional Commits. No
`@semantic-release`, no manual tagging — the RAG bot still uses
`.releaserc.json`, but that's legacy, not a model to follow.

The three standard files live at the repository root:
`release-please-config.json` with `release-type: "simple"`,
`.release-please-manifest.json`, and an initially empty `CHANGELOG.md`.

For a Python project, the version marker sits in `pyproject.toml` instead of
`build.gradle.kts`; Release Please's `generic` updater works on arbitrary
text files and finds it there just as well:

```toml
version = "0.1.0" # x-release-please-version
```

**The chart and the application are versioned together.** `extra-files`
additionally points at `charts/sturnus/Chart.yaml`, whose `version` and
`appVersion` carry the same marker. Separate version streams would be effort
without payoff for a chart that ships exactly this one application.

The `publish` job hangs off the Release Please job via `needs`/`if`. An
additional, tag-triggered workflow is explicitly **not** set up: Release
Please tags with the default `GITHUB_TOKEN`, which doesn't re-trigger tag
workflows within the same repository — such a workflow would either never
run, or would collide with the chained job.

### 13.2 Container

All three processes share **one image with three entry points**
(`sturnus-bot`, `sturnus-link`, `sturnus-worker` as console scripts).
Building three images from the same codebase would triple build time, review
effort, and registry storage, without separating anything that isn't already
separated — that job belongs to the deployment.

`faster-whisper` builds on CTranslate2 instead of PyTorch, which keeps it
moderately sized; the model weights are **not part of the image** — they are
downloaded on startup and cached on a volume, the same pattern the cluster's
Ollama installation already uses.

Publishing goes through `docker-publish.yml` from
`OneLiteFeatherNET/workflows`, pinned with a full SemVer tag (`@v2.4.0`,
never `@main` or a bare major alias). The workflow is tool-agnostic and needs
only a build context and a Dockerfile — the fact that no Gradle is involved
here doesn't matter. **The target is the organization's Harbor registry, not
GHCR.**

### 13.3 Checks on pull requests

The central catalog contains `gradle-build-pr.yml`, but **no counterpart for
Python**. Linting, type checking, and tests therefore initially run as a job
owned by this repository: `uv sync`, `ruff`, `mypy`, `pytest`.

A reusable `python-build-pr.yml` would be the cleaner place for this — with
the RAG bot, a second Python consumer already exists. But drawing a shared
abstraction from a single, as-yet-unproven consumer would mean guessing
rather than working from evidence. Extracting it is therefore planned as
follow-up work, once the pattern has proven itself here, and it will then
touch a different repository.

Also drawn from the catalog: `markdown-lint.yml` for the documentation, and
`close-invalid-prs.yml`.

### 13.4 Dependencies

The organization's central Renovate preset. The exact wiring follows the
`renovate` skill and should be looked up there at setup time rather than
guessed. Renovate also keeps the version pin of the reusable workflows
current — which is why that pin is set as a full tag rather than an alias.

Jinja2 counts among the security-relevant dependencies (Section 15).

### 13.5 Cluster

In the Kubernetes FLUX repository:

- `apps/base/sturnus/` and `apps/clusters/feathre-core/base-apps/sturnus/`
- A CloudNativePG database following the existing `database/` pattern
- An `ObjectBucketClaim` for the audio bucket, following the pattern of
  `outline.yaml`, with its own credentials (Section 12.1)
- Secrets via SOPS: the Discord token, the Outline service key, the OAuth
  client secret, the master key for audio encryption
- Inbound traffic only for `link-service`, via Cloudflare Tunnel

For `bot`, a PodDisruptionBudget is set that limits unwanted evictions during
active sessions, along with an **RWO PVC for the recording in progress**
(Section 6.3). Its size follows from `max_session_hours` and the expected
number of speakers — roughly 5 GB at four hours and ten speakers. Because the
PVC ties the pod to a zone, `bot` is pinned to the region via node affinity,
the same way the cluster's other stateful applications are.

Resources: `bot` 1 CPU, `link-service` minimal, `worker` 4 CPU with a memory
request matching the chosen model (roughly 2 GB for `large-v3-turbo` in int8,
plus buffer) and a volume for the model weights.

### 13.6 Development workflow

Trunk-Based Development with Conventional Commits, as elsewhere in the
organization. The commit format is not cosmetic — it's the input Release
Please derives version and changelog from.

## 14. Test strategy

`pytest` with `pytest-asyncio`, mirroring the RAG bot.

Unit-testable without a Discord dependency, and cut accordingly as pure
functions or classes:

- The session state machine with an injected clock — every transition from
  Section 5.1
- Time reconstruction from RTP timestamps, including SSRC changes
- Language detection: locking in per speaker, falling back to the default
  language
- Recovery of orphaned recordings from the PVC after a crash
- Uniqueness of the link announcement via `announced_at`
- Merging segments across all speakers
- Rendering the transcript model via Jinja2, including the speaker line with
  and without a linked account
- **Sandbox escape:** a template with `{{ ''.__class__… }}` and related
  expressions must fail, not execute
- **Escaping:** a display name like `[klick](https://boese.example)` must not
  produce a link in the generated document
- Encryption and decryption of a recording under the envelope scheme
- The retention run: deletes expired objects, leaves running ones untouched,
  sets `audio_deleted_at`
- Consent resolution, including the administrator-bypass case

PostgreSQL via Testcontainers — repositories are verified against a real
database, not against fakes. Whisper is faked in unit tests via the
`TranscriptionEngine` port, supplemented by one integration test with a
short, real audio file, so the model integration works in practice and not
only in theory.

**An architecture test enforces the dependency rule from Section 4.4:** it
inspects the import graph and fails the moment `domain` imports anything from
`application`, `infrastructure`, or an I/O library. A layering rule that only
lives in documentation will be violated within a few months; as a test, it's
an assertion.

Voice receive itself stays a thin adapter with no tests of its own — the
logic deliberately lives outside it.

## 15. Open risks

- **`discord-ext-voice-recv` is a community extension** with no official
  support from discord.py. If Discord breaks the voice protocol, the fix
  depends on a third-party project. The adapter is therefore deliberately
  kept thin, so that swapping the library doesn't touch the core logic.
- **The worker's sizing is an estimate.** The figure of roughly 1× realtime
  for `large-v3-turbo` on 4 cores needs to be measured against real material
  before rollout; the fallback to `small` is planned for.
- **The bot is a singleton with no takeover, and a session is indivisible.**
  On an orderly restart, the SIGTERM handler closes the recording; on a hard
  kill, recovery from the PVC takes over at the next start (Section 6.3). If
  both fail — say, on loss of the volume — the entire session is lost, not
  just a portion. That is the deliberately accepted price for not segmenting
  the recording. A PodDisruptionBudget limits unwanted evictions, but it
  doesn't prevent a deploy.
- **The latency is significant and grows with speaking time.** A long
  evening in the voice channel delivers its transcript only hours later.
  Should this prove untenable in production, segmenting the recording into
  sections is the way back — it costs transcription quality at every cut.
- **The Outline OAuth details are unverified** (see Section 8.4) and need to
  be checked against the running instance before the link flow is
  implemented.
- **The Jinja2 sandbox is a hurdle, not a guarantee.** Escapes from
  `SandboxedEnvironment` have become known in the past. In the MVP this has
  no consequences, because every template comes from the image. If setting
  custom templates is added later, it must stay restricted to administrators
  and be logged; the Jinja2 version then counts among the security-relevant
  dependencies that Renovate keeps promptly updated.
- **The extended retention period increases the damage from a compromise.**
  Raw audio of private conversations, kept around for weeks, outweighs any
  other piece of data in this system. Encryption before upload limits this to
  the cases where the master key also leaks — which makes handling of that
  key the actual asset to protect. A shorter retention period remains the
  most effective countermeasure at any time.
- **A second adapter will test the port boundary.** The shape of
  `DocumentSink` so far rests on a single implementation; Confluence or
  Notion may bring requirements — block structures instead of text,
  mandatory fields, different mention models — that require adjusting the
  transcript model. That's expected, and it's the reason not to build more
  than one adapter right now.
