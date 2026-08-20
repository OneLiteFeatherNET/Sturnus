# Operations guide

This is the operator-facing counterpart to the [design document](superpowers/specs/2026-08-19-sturnus-design.md):
what you need to know to deploy Sturnus, configure a guild, and diagnose it
when something goes wrong, that the code alone does not spell out. Where a
section describes a piece that has not landed on this branch yet, it says
so rather than describing a procedure that would not actually work.

## 1. Environment variables

Sturnus reads two kinds of configuration:

- **Process configuration** (`sturnus.config.Settings`): read once from the
  environment at startup, prefixed `STURNUS_`. This is what must exist
  before the process can even reach a database — connection strings,
  tokens, the master key. It is frozen for the life of the process; changing
  it means restarting.
- **Runtime configuration** (`/config` in Discord, backed by `ConfigStore`
  and `guild_config` in the database): everything an administrator can
  change per guild without a restart — the recording channel, timeouts,
  the retention window, the privacy policy version. See section 4.

This section covers the first kind only — the environment variables. The
following are read by `Settings` today (i.e. by the `bot` process; `link`
and `worker` are separate console scripts — `sturnus-link` and
`sturnus-worker` — that exist in `pyproject.toml` but whose own entrypoint
modules have not landed on this branch yet, so they do not yet define
their own settings classes):

| Variable | Secret | Purpose |
|---|---|---|
| `STURNUS_DISCORD_TOKEN` | **yes** | The bot's Discord token. |
| `STURNUS_DATABASE_URL` | **yes** | SQLAlchemy async connection string (e.g. `postgresql+asyncpg://user:pass@host/db`). Treat as secret because it embeds the database credential. |
| `STURNUS_S3_ENDPOINT` | no | S3-compatible endpoint URL for the audio bucket. |
| `STURNUS_S3_BUCKET` | no | Name of the (dedicated, per Spec 12.1) audio bucket. |
| `STURNUS_S3_ACCESS_KEY` | **yes** | Access key for that bucket. |
| `STURNUS_S3_SECRET_KEY` | **yes** | Secret key for that bucket. |
| `STURNUS_MASTER_KEY` | **yes** | Base64-encoded 32-byte AES-256 key. Wraps every session's per-recording data key. See section 2 — this is the single most consequential variable in this list. |
| `STURNUS_MASTER_KEY_ID` | no | Free-text label for the master key currently in `STURNUS_MASTER_KEY` (the chart defaults it to `v1`). Stored alongside every wrapped data key as `encryption_key_id`, never itself secret — it is a name, not key material. |
| `STURNUS_RECORDING_DIR` | no | Filesystem path the bot writes in-progress recordings to before upload (a PVC in the chart, `/data/recordings`). |
| `STURNUS_HEALTH_PORT` | no | Port the `/healthz`, `/readyz`, `/metrics`, `/version` HTTP endpoints listen on. Defaults to `8080`. |

All values are validated by `pydantic-settings` at startup: a missing
required variable fails immediately with a `ValidationError` rather than
later with a confusing runtime error, and `Settings.__repr__` never
renders a secret value (`SecretStr` masks it) — safe to include in a crash
log or traceback.

**Not yet defined in code.** The Helm chart's `worker.env` already
anticipates `STURNUS_MODEL_CACHE_DIR` and `STURNUS_WHISPER_MODEL` (see
`charts/sturnus/values.yaml`), and the design document additionally
names a Whisper device/compute-type choice, a default-language fallback,
a per-job retry limit, an Outline service token, and an OAuth client
id/secret as environment-driven — but none of these exist as a `Settings`
field yet, because the `sturnus-worker` and `sturnus-link` entrypoint
modules that would read them have not landed on this branch. Do not
configure a deployment against variable names guessed from the design
document; wait for the corresponding code (and update this table
alongside it) rather than relying on this note. `WhisperEngine` itself
already exists and takes `model_size`, `device`, `compute_type`, and
`default_language` as plain constructor arguments — whatever code wires
those to the environment is what will define their actual variable names.

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
the Discord token, S3 credentials, and the Outline/OAuth secrets once
those land — is not set directly in the chart. It goes into a
SOPS-encrypted secret file in the cluster's GitOps repository, which
decrypts into the Kubernetes `Secret` the chart expects to already exist
(`existingSecret: sturnus-secrets` in `values.yaml`). The chart only ever
references that secret by name through `envFrom.secretRef` — it never
places a secret value into `values.yaml` or a template, and this
repository holds none of the cluster's actual key material.

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
incoming audio packet from a user who does not hold the consent role,
regardless of what channel permissions say — this exists specifically
because a guild administrator bypasses channel overwrites and could
otherwise speak in the channel without the role.

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
durable proof the deletion happened is separate I/O, driven by whatever
process runs that sweep periodically; a bucket lifecycle rule is a second,
independent line of defence, never a substitute for that database record.

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
already consented and whether `/consent status` reports them active — it
does **not** by itself revoke the consent role or stop someone from being
recorded. The packet-level filter that decides whether to keep someone's
audio (`sturnus.infrastructure.discord.voice`) currently checks only
Discord role membership, not policy version. If a hard cutover is
required — nobody may keep recording under the old policy basis at all —
the consent role must be explicitly removed from existing holders as
well; bumping `policy_version` alone leaves current role-holders able to
keep being recorded until they happen to re-run `/consent grant` or
`/consent revoke` themselves.
