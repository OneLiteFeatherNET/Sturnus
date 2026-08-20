# Operations guide

This is the operator-facing counterpart to the [design document](superpowers/specs/2026-08-19-sturnus-design.md):
what you need to know to deploy Sturnus, configure a guild, and diagnose it
when something goes wrong, that the code alone does not spell out. Where a
section describes a piece that has not landed on this branch yet, it says
so rather than describing a procedure that would not actually work.

## 1. Environment variables

Sturnus reads two kinds of configuration:

- **Process configuration** (`sturnus.config.Settings`,
  `sturnus.entrypoints.worker.WorkerSettings`,
  `sturnus.entrypoints.link.LinkSettings`): read once from the
  environment at startup, prefixed `STURNUS_`. This is what must exist
  before the process can even reach a database — connection strings,
  tokens, the master key. It is frozen for the life of the process; changing
  it means restarting.
- **Runtime configuration** (`/config` in Discord, backed by `ConfigStore`
  and `guild_config` in the database): everything an administrator can
  change per guild without a restart — the recording channel, timeouts,
  the retention window, the privacy policy version. See section 4.

This section covers the first kind only — the environment variables.

One image ships three console scripts — `sturnus-bot`, `sturnus-worker` and
`sturnus-link` — and each of them reads its *own* settings class rather than
a single shared one. That separation is deliberate (Spec 13.2): the bot's
`Settings` requires a Discord token the worker never uses, and the OAuth
client secret exists only in `LinkSettings`, so that the one publicly
reachable process holds no S3 credentials and no master key. The
consequence for whoever writes the deployment manifests is the reason there
are three tables below rather than one: **give each component exactly the
variables its own table lists.** The shared `STURNUS_` prefix does not make
the three sets interchangeable — a variable that is required for one
process is, for another, either unused or actively something that process
is designed not to possess.

In each table, *Required* is either **yes** (the process refuses to start
without it) or the default value the field takes when the variable is
absent; *Secret* marks the values that are credentials and must come from
the Kubernetes `Secret` rather than from plain manifest text (see section
1.4).

### 1.1 `sturnus-bot` (`sturnus.config.Settings`)

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `STURNUS_DISCORD_TOKEN` | **yes** | **yes** | The bot's Discord token. |
| `STURNUS_DATABASE_URL` | **yes** | **yes** | SQLAlchemy async connection string (e.g. `postgresql+asyncpg://user:pass@host/db`). Treat as secret because it embeds the database credential. |
| `STURNUS_S3_ENDPOINT` | **yes** | no | S3-compatible endpoint URL for the audio bucket. |
| `STURNUS_S3_BUCKET` | **yes** | no | Name of the (dedicated, per Spec 12.1) audio bucket. |
| `STURNUS_S3_ACCESS_KEY` | **yes** | **yes** | Access key for that bucket. |
| `STURNUS_S3_SECRET_KEY` | **yes** | **yes** | Secret key for that bucket. |
| `STURNUS_MASTER_KEY` | **yes** | **yes** | Base64-encoded 32-byte AES-256 key. Wraps every session's per-recording data key. See section 2 — this is the single most consequential variable in this list. |
| `STURNUS_MASTER_KEY_ID` | **yes** | no | Free-text label for the master key currently in `STURNUS_MASTER_KEY` (the chart defaults it to `v1`). Stored alongside every wrapped data key as `encryption_key_id`, never itself secret — it is a name, not key material. |
| `STURNUS_RECORDING_DIR` | **yes** | no | Filesystem path the bot writes in-progress recordings to before upload (a PVC in the chart, `/data/recordings`). |
| `STURNUS_OUTLINE_BASE_URL` | **yes** | no | Base URL of the Outline instance. The bot needs it only to build the authorization URL `/link` sends a user's browser to; it never calls Outline's API itself. |
| `STURNUS_OUTLINE_CLIENT_ID` | **yes** | no | OAuth client id of the Sturnus application registered in Outline. Public by design — it travels in the query string of the authorization URL every user's browser opens. |
| `STURNUS_OUTLINE_REDIRECT_URI` | **yes** | no | The callback URL that authorization returns to. Must be the same value `sturnus-link` is given, and must actually route to `link` — see section 1.5. |
| `STURNUS_HEALTH_PORT` | `8080` | no | Port the `/healthz`, `/readyz`, `/metrics`, `/version` HTTP endpoints listen on. |

The bot has no `STURNUS_OUTLINE_CLIENT_SECRET`, and that is not an
oversight: building an authorization URL needs only the public client id,
and the code exchange that does need the secret happens exclusively in
`link`. Do not add the secret to the bot's environment "for symmetry" — it
would widen the blast radius the split exists to keep narrow, and nothing
in the bot would read it.

### 1.2 `sturnus-worker` (`sturnus.entrypoints.worker.WorkerSettings`)

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `STURNUS_DATABASE_URL` | **yes** | **yes** | Same connection string as the bot's. The worker owns the schema: it runs the Alembic migrations to head before anything else starts (Spec 13.1), which is why the other two processes only wait for tables to appear. |
| `STURNUS_S3_ENDPOINT` | **yes** | no | S3-compatible endpoint URL for the audio bucket. |
| `STURNUS_S3_BUCKET` | **yes** | no | Name of the audio bucket — the same one the bot uploads to. |
| `STURNUS_S3_ACCESS_KEY` | **yes** | **yes** | Access key for that bucket. |
| `STURNUS_S3_SECRET_KEY` | **yes** | **yes** | Secret key for that bucket. |
| `STURNUS_MASTER_KEY` | **yes** | **yes** | The same base64-encoded 32-byte key the bot wraps data keys with; the worker unwraps them with it to decrypt each downloaded recording. It must be byte-identical to the bot's value, or nothing the bot recorded can be transcribed. See section 2. |
| `STURNUS_MASTER_KEY_ID` | **yes** | no | Label recorded as `encryption_key_id` when a data key is wrapped. Not key material. |
| `STURNUS_OUTLINE_BASE_URL` | **yes** | no | Base URL of the Outline instance the finished protocol is posted to. |
| `STURNUS_OUTLINE_SERVICE_KEY` | **yes** | **yes** | Outline API token `OutlineSink` authenticates with when creating documents. Note the name — it is `OUTLINE_SERVICE_KEY`, not an `API_TOKEN` variant. A token that is invalid, lacks access, or points at a collection that does not exist surfaces as `PermanentDocumentError`; see section 5. |
| `STURNUS_WHISPER_MODEL` | `large-v3-turbo` | no | faster-whisper model to load. Larger models are more accurate and markedly slower, and this deployment transcribes on CPU (see the chart's `worker.resources`), so the difference is measured in minutes per recording rather than seconds. |
| `STURNUS_WHISPER_DEFAULT_LANGUAGE` | `en` | no | Language reported when faster-whisper's own detection comes up empty. It matters more than a fallback usually does: the first transcription for a speaker in a session pins that speaker's language, and every later job for them reuses it. |
| `STURNUS_MODEL_CACHE_DIR` | unset | no | Where model weights are cached. When set, the worker exports it as `HF_HOME` before loading the model, so the download lands on a persistent volume; left unset, every cold start re-downloads several gigabytes of weights. |
| `STURNUS_WORK_DIR` | `/tmp` | no | Scratch directory the encrypted recording is downloaded and decrypted into before transcription. It must be large enough for the biggest single recording — the chart sizes the corresponding volume with `worker.tmpSizeLimit`. |
| `STURNUS_MAX_JOB_ATTEMPTS` | `3` | no | How many failed attempts a job gets before `JobQueue.fail` marks it `dead`. See section 5 for what a `dead` job means for the rest of its session. |
| `STURNUS_JOB_LEASE_SECONDS` | `1800.0` | no | How long a claimed job may stay `running` before `JobQueue.claim` reclaims it for another worker. It is generous on purpose: it must exceed the longest plausible transcription, or a still-running job gets picked up a second time. |
| `STURNUS_HEALTH_PORT` | `8080` | no | Port the `/healthz`, `/readyz`, `/metrics`, `/version` HTTP endpoints listen on. |

Whisper's device and compute type are deliberately *not* environment-driven:
the worker constructs `WhisperEngine` with `"cpu"` and `int8` hardcoded,
because Spec 7 sizes this deployment for CPU inference. There is no
`STURNUS_WHISPER_DEVICE` to set — moving to GPU is a code change, not a
configuration change.

### 1.3 `sturnus-link` (`sturnus.entrypoints.link.LinkSettings`)

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `STURNUS_DATABASE_URL` | **yes** | **yes** | Same connection string as the other two. `link` reads and writes only `oauth_state` and `account_link`, and waits for those tables rather than migrating anything itself. |
| `STURNUS_OUTLINE_BASE_URL` | **yes** | no | Base URL of the Outline instance the authorization code is exchanged against. |
| `STURNUS_OUTLINE_CLIENT_ID` | **yes** | no | The same public OAuth client id the bot uses. |
| `STURNUS_OUTLINE_CLIENT_SECRET` | **yes** | **yes** | OAuth client secret for that same application. This process is the only one that holds it, because it is the only one that exchanges an authorization code for a token. |
| `STURNUS_OUTLINE_REDIRECT_URI` | **yes** | no | The callback URL, repeated here because the token exchange sends it again for verification. It must match the bot's value exactly — see section 1.5. |
| `STURNUS_HEALTH_PORT` | `8080` | no | Port the `/healthz`, `/readyz` and `/oauth/callback` routes are served on. |

`link` holds no Discord token, no S3 credentials and no master key — by
construction, not by omission. It is the only publicly reachable component,
so anything it does not need, it deliberately does not have.

### 1.4 Which values belong in the Kubernetes `Secret`

Exactly seven variables are credentials, and only these belong in the
`Secret` the chart consumes through `envFrom.secretRef` (see section 2 for
how it gets there via SOPS):

- `STURNUS_DISCORD_TOKEN`
- `STURNUS_DATABASE_URL`
- `STURNUS_S3_ACCESS_KEY`
- `STURNUS_S3_SECRET_KEY`
- `STURNUS_MASTER_KEY`
- `STURNUS_OUTLINE_SERVICE_KEY`
- `STURNUS_OUTLINE_CLIENT_SECRET`

Everything else is plain configuration and should be readable in the
manifests, where it can be reviewed and diffed. That is not laxity, it is
accuracy about what these values are: `STURNUS_OUTLINE_CLIENT_ID` is public
by design — the OAuth spec has it travelling in a query string of a URL the
user's own browser opens — and `STURNUS_OUTLINE_BASE_URL`,
`STURNUS_OUTLINE_REDIRECT_URI` and `STURNUS_S3_ENDPOINT` are addresses, not
credentials. Encrypting an address buys nothing and costs review: a wrong
redirect URI hidden inside a SOPS blob is a great deal harder to spot than
a wrong one sitting in a values file.

`STURNUS_DATABASE_URL` is the one entry on that list that is not typed
`SecretStr` in the code — it is a plain `str`, because it is a connection
string rather than a bare credential. Treat it as a secret regardless: it
embeds the database password, and a `SecretStr` field's masking is a
convenience in tracebacks, not the thing that decides where a value is
stored.

One `Secret` holds all seven keys; the separation is made by the chart,
which gives each Deployment only the keys that component's own table
lists. So provision the Secret with the full seven — it is shared — and
rely on the wiring rather than on splitting the file. Handing `link` the
master key or the Discord token because they happen to sit in the same
secret undoes the blast-radius separation described above, which is
exactly what that wiring exists to prevent.

### 1.5 The redirect URI must match the route that reaches `link`

`STURNUS_OUTLINE_REDIRECT_URI` appears twice — once in the bot, which puts
it into the authorization URL, and once in `link`, which sends it again
during the token exchange — and it is also registered on the Outline side
against the OAuth application. All three must be the same string, and it
must resolve, through whatever Ingress or Gateway fronts the cluster, to
`link`'s `/oauth/callback` route on `STURNUS_HEALTH_PORT`.

Getting this wrong fails late and unhelpfully. The user runs `/link`, opens
the URL, authenticates against Outline and consents — and only *then* does
the flow break, on a redirect that goes nowhere or on a `redirect_uri`
mismatch rejected at the token exchange. Nothing surfaces at deploy time,
no readiness probe catches it, and the person who sees the failure is an end
user in a browser rather than an operator looking at logs. Verify it by
actually walking the flow once after a deploy, not by reading the values
file.

All values are validated by `pydantic-settings` at startup: a missing
required variable fails immediately with a `ValidationError` rather than
later with a confusing runtime error, and a settings object's `repr` never
renders a secret value (`SecretStr` masks it) — safe to include in a crash
log or traceback.

## 2. The master key

`STURNUS_MASTER_KEY` is the AES-256-GCM key that wraps every session's
data key (`sturnus.infrastructure.crypto.KeyWrapper`). It is not used to
encrypt audio directly — a fresh 32-byte data key is generated per
session, encrypts that session's audio, and is itself encrypted
("wrapped") with the master key before being stored in the database
(`transcription_job.wrapped_data_key`). Only the wrapped form is ever
persisted.

**Generating it.** It must decode to exactly 32 bytes
(`KeyWrapper.__init__` raises `ValueError` otherwise):

```bash
openssl rand -base64 32
```

**Reaching SOPS.** Per the design (Section 13.5), this value — along with
the other six credentials listed in section 1.4 — is not set directly in
the chart. It goes into a SOPS-encrypted secret file in the cluster's
GitOps repository, which decrypts into the Kubernetes `Secret` the chart
expects to already exist (`existingSecret: sturnus-secrets` in
`values.yaml`). The chart only ever references that secret by name, and it
does so one key at a time through `valueFrom.secretKeyRef`: each of the
three components is handed only the keys its own settings class declares,
so `link` — the only component reachable from outside the cluster, via the
Cloudflare Tunnel serving the OAuth callback — never receives
`STURNUS_MASTER_KEY` or the Discord token at all. The per-component key
lists live in the `sturnus.secretEnv` helper in
`charts/sturnus/templates/_helpers.tpl`. The chart never places a secret
value into `values.yaml` or a template, never creates the `Secret` itself,
and this repository holds none of the cluster's actual key material.

**What `encryption_key_id` is for.** Every wrapped data key is stored next
to the id of the master key that wrapped it (`STURNUS_MASTER_KEY_ID` at
the time of encryption, defaulted to `v1` in the chart). That is what
makes rotation *possible* in principle: a worker that knows both the old
and the new master key could unwrap material tagged with the old id and
re-wrap it under the new one — or simply keep the old key available
alongside the new one, and pick which key to use to unwrap by looking at
the id.

**Rotation is not implemented today.** `Settings` reads exactly one
`STURNUS_MASTER_KEY` and one `STURNUS_MASTER_KEY_ID` — there is no code
path anywhere in this repository that holds more than one master key at a
time, looks one up by id, or re-wraps existing data keys under a new key.
`encryption_key_id` records which key wrapped a given recording; nothing
yet *uses* that record to support more than one key being valid at once.
Do not attempt a "rotation procedure" against this codebase as it stands —
there isn't one to run. Changing `STURNUS_MASTER_KEY` today makes every
previously wrapped data key permanently unreadable (see below), it does
not rotate anything.

**Losing a master key destroys every recording it wrapped.** This is the
single most consequential operational fact about this system, so it is
stated here plainly: audio is only ever stored encrypted, and only the
*wrapped* data key is stored — never the plaintext data key, never the
master key itself. If `STURNUS_MASTER_KEY` is lost, every recording whose
`encryption_key_id` names it becomes permanently unrecoverable. No
database backup helps — the database backup would contain the same
wrapped bytes, unreadable without the same master key. No S3 backup
helps, for the same reason. There is no recovery path but keeping the key.
Treat it accordingly: back it up as carefully as you would back up the
recordings themselves, because losing it *is* losing the recordings.

## 3. Discord setup

### 3.1 What the bot needs

**Privileged gateway intents** (`SturnusClient` requests `Intents.default()`
plus `members` and `voice_states`, both privileged): enable **Server
Members Intent** and **Voice States Intent** for the application in the
Discord Developer Portal, or the bot will fail to connect.

**Bot permissions**, when generating the invite/OAuth2 URL (scopes `bot`
and `applications.commands` — the latter is what makes slash commands
appear at all):

- **View Channel** and **Connect**, on the recording voice channel — the
  bot joins automatically once enough consenting members are present
  (`SturnusClient.on_voice_state_update`); it never needs **Speak**, since
  it only receives audio.
- **Manage Roles** — needed for three separate things `/setup` and
  `/consent` do: creating the consent role when none exists yet
  (`SetupCog`), editing the recording channel's `Speak` permission
  overwrites for `@everyone` and the consent role, and granting/revoking
  the consent role itself on `/consent grant` / `/consent revoke`. Discord
  additionally requires the bot's own highest role to sit above the
  consent role in the guild's role list — granted permission alone is not
  enough; a role edit or assignment fails with a permissions error if the
  bot's role is positioned below it.

Guild administrators bypass all of the above at the Discord-permission
level and can always run `/setup` and `/config` regardless of role
assignment (see `require_admin`).

### 3.2 Why the recording channel's permissions matter

`/setup` denies `Speak` to `@everyone` on the recording channel and
allows it for the consent role — this is not cosmetic, it is the primary
layer of the consent protection (Spec 3.1): someone who has not consented
cannot technically produce audio in that channel at all. The bot enforces
a second, independent layer on top of it: `VoiceReceiveAdapter` drops any
incoming audio packet unless the speaker holds the consent role **and** a
stored consent record matching the guild's current `policy_version`,
regardless of what channel permissions say. It exists specifically because
a guild administrator bypasses channel overwrites and could otherwise
speak in the channel without the role — and because the record check is
what makes bumping `policy_version` take effect on its own (see section 6).

### 3.3 Why non-recorded channels must also exist

If the recording channel is the *only* voice channel available, consenting
to recording stops being a real choice — it becomes the price of admission
for talking to anyone by voice at all, and consent extracted that way is
not "freely given" under Art. 7(4) GDPR (the prohibition on tying an
unrelated condition to consent). At least one voice channel that Sturnus
never joins must exist alongside the recording channel for consent on the
recording channel to be legally meaningful, not merely technically
present.

### 3.4 `/setup` applies the permissions itself

`/setup` is not just a configuration-writing command: it reads the voice
channel's current `Speak` overwrites and, if they do not already match
(`@everyone` denied, the consent role allowed), applies the change itself
through the Discord API, with every partial failure reported back rather
than swallowed. This is deliberate (see `setup_cog.py`'s module docstring)
— the one step that must never be gotten wrong is not left to prose in
this document for whoever reads it least carefully.

## 4. First run

1. Run `/setup`, supplying the voice channel to record, the URL of the
   privacy/consent policy, and a policy version identifier. Optionally
   supply an existing role as the consent role; if omitted, `/setup`
   reuses whatever role is already configured (if it still exists) or
   creates a new one.
2. Run `/config show`. It lists every known key, its effective value and
   source (`stored` vs. `default` vs. `unset`), and a **Missing required
   keys** line. `/setup` does not set every required key — in particular
   it never touches `document_target` (the Outline collection id, or
   equivalent for a future adapter) or `admin_role_id`, since neither has
   a natural argument to `/setup`. Expect `/config show` to report both as
   missing after a first `/setup` run and set them explicitly:
   ```
   /config set document_target <outline-collection-id>
   /config set admin_role_id <role-id>
   ```
3. Re-run `/config show` until "All required keys are set." appears.

Until every required key (`voice_channel_id`, `consent_role_id`,
`document_target`, `policy_version`, `policy_url`, `admin_role_id`) is set,
the bot logs a warning naming the guild and skips building that guild's
recording pipeline entirely (`SturnusClient._configure_guild`) — it will
not join the voice channel or record anything for that guild.

## 5. Troubleshooting

**A job is `dead`.** `transcription_job.status` becomes `dead` once
`attempts` reaches the worker's configured retry limit
(`JobQueue.fail`). A dead job is deliberately excluded from the
remaining-jobs count that decides whether a session is finished
(`JobQueue.complete`) — so one unreadable recording does not block the
rest of that session's document from being produced; it just means that
one speaker's portion is permanently missing from it. There is no admin
command yet to list dead jobs; query them directly:
```sql
SELECT id, session_id, discord_user_id, attempts, error
FROM transcription_job WHERE status = 'dead';
```
The `error` column holds the last failure's message (`JobQueue.last_error`
reads the same column) — never audio or transcript content, only the
exception text.

**Telling a transcription failure from a document failure apart.**
These happen at different granularities and leave different traces:

- A *transcription* failure is per job, i.e. per speaker's audio file.
  Look at `transcription_job` rows for the session: a `dead` or repeatedly
  `pending`-with-rising-`attempts` row, with `transcript` still `NULL`,
  is a transcription-stage failure.
- A *document* failure happens once per session, after every one of its
  jobs already reached `done` (each with a non-`NULL` `transcript`).
  Check `session.status` and `session.document_url`: if every job for a
  session is `done` but the session's `document_url` is still unset and
  its `status` never advanced to `documented`, the failure is in
  assembling or posting the document, not in transcribing the audio. If
  the failure was `PermanentDocumentError` (raised by `OutlineSink` on
  HTTP 401/403/404 — a bad token, a token without access, or a missing
  collection), retrying will not help until the underlying Outline-side
  problem is fixed; anything else raised there is a transient failure
  worth retrying.

**A stalled queue.** `JobQueue.claim` uses `SELECT ... FOR UPDATE SKIP
LOCKED`, so any number of `pending` jobs sitting unclaimed with `attempts`
not increasing is not a database contention problem — it means nothing is
calling `claim()` at all. Check that the worker process is actually
running and check its `/readyz` endpoint before looking any further at
individual jobs.

**After a bot restart.** On startup the bot recovers whatever a previous
process left on disk under `STURNUS_RECORDING_DIR` and logs `Recovered N
orphaned recording(s) left behind by a previous process` when it finds
any — this is expected after a crash or a deploy that landed mid-session,
not itself a failure; it is what stops an in-progress recording from
being silently discarded when the pod that was recording it goes away.

## 6. Retention

`audio_retention_days` (runtime-configurable, default 30, worker-consumed)
is how long a session's encrypted audio is kept *after* transcription
before deletion — not how long it takes to transcribe. The reason it
outlives transcription at all is reprocessing: without the audio still
being there, a poor transcription could never be redone, and an improved
model could never be applied to already-recorded material. It is enforced
by `retention_until`, stamped on each job when created, and the pure
selection `expired_jobs` picks out jobs whose `retention_until` has
strictly passed and whose `audio_deleted_at` is still unset — the
adapter that actually deletes from S3 and stamps `audio_deleted_at` as
durable proof the deletion happened is separate I/O. The worker drives it,
once an hour, alongside its job loop
(`sweep_expired_audio` in `sturnus.entrypoints.worker`), so nothing else
needs to: a bucket lifecycle rule is a second, independent line of
defence, never a substitute for that database record, and never a
replacement for this sweep.

Because recordings outlive their transcription by weeks, not minutes, the
retention period is not merely an implementation detail — **it belongs in
the privacy policy shown to participants** (the `policy_url` document),
since it is part of what consent is actually being given to.

Changing `audio_retention_days` therefore is not just a config edit: bump
`policy_version` at the same time, and update the policy document at
`policy_url` to state the new duration. `is_consent_active`
(`sturnus.domain.consent`) compares a stored consent record's
`policy_version` against the guild's *current* `policy_version` — the
moment they differ, that consent is no longer active, and `/consent
grant`/`/consent status` will show it as such and offer the consent flow
again. Be aware of what this does and does not do on this branch today,
though: that check gates whether `/consent grant` treats someone as
already consented and whether `/consent status` reports them active — but
it does stop them from being recorded, without any further action.

The packet-level filter checks both layers: the Discord role, per packet,
and the stored consent record behind it
(`sturnus.infrastructure.discord.voice` calling `ConsentCache.verdict`,
which applies `sturnus.domain.consent.may_record` — role membership **and**
a consent record matching the current `policy_version`). That verdict is
served from cache without ever blocking the audio drain, and a stale entry
is refreshed beside it rather than on it, so a bump takes effect within the
cache's five-second TTL plus one refresh: role-holders whose
consent names the superseded version stop being recorded mid-session, and
`/consent grant` under the new version is what puts them back. Removing
the role by hand is not required for a hard cutover, and doing so only
costs the affected members a second step when they re-consent.
