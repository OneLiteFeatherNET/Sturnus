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
| `STURNUS_SHARD_COUNT` | unset | no | How many Discord gateway connections this one process opens. Unset or empty — the chart's default — means **let Discord decide**: `AutoShardedClient` asks `/gateway/bot` and uses the recommendation, which grows with the guild count on its own. An explicit value is for an operator who knows why; anything below `1` is refused at startup. It does **not** make the bot horizontally scalable — every shard lives in this one process and the Deployment stays at one replica. See section 3.6. |
| `STURNUS_HEALTH_PORT` | `8080` | no | Port the `/healthz`, `/readyz`, `/metrics`, `/version` HTTP endpoints listen on. `/metrics` answers **`501 Not Implemented`**: metrics are *pushed* over OTLP, not scraped — see section 7. |
| `STURNUS_SENTRY_DSN` | unset | no | Sentry DSN for error reporting. Empty disables it entirely: `sentry_sdk.init()` is never called, so no instrumentation is installed and the process runs exactly as it does without Sentry. Supplied through the `Secret`, and required to be present even when blank; see section 1.4 for why a value that is not a credential is stored like one. |
| `STURNUS_SENTRY_ENVIRONMENT` | `production` | no | Names the deployment in Sentry's environment filter **and** supplies OpenTelemetry's `deployment.environment.name`. One variable for both on purpose — `OtelSettings.environment` reads this exact name rather than adding a second one, so the environment filter in Sentry and the one in Grafana can never disagree. |
| `STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT` | unset | no | Base URL of an OTLP/HTTP receiver — in this cluster `http://alloy-receiver.grafana.svc:4318`. Unset or empty — the chart's default — disables traces and metrics entirely: no provider is constructed, so every span is a no-op, nothing connects, nothing retries, and no export failure is ever logged. Give the base URL with no `/v1/traces` suffix; the exporters append their own paths. Not a credential; see section 1.4. |
| `STURNUS_OTEL_TRACES_SAMPLE_RATIO` | `1.0` | no | Fraction of traces sampled. `1.0` is correct today and the arithmetic is in `sturnus/infrastructure/telemetry.py`: the worker processes one job at a time and each is minutes of CPU work, the bot opens a handful of sessions per guild per day, and the packet path emits no spans at all. This exists as a valve for a future high-volume path. Note that job outcome is *also* an unsampled counter, so sampling can never be why a failed job is invisible. |
| `STURNUS_OTEL_METRIC_EXPORT_INTERVAL_SECONDS` | `60.0` | no | How often metrics are pushed to the OTLP endpoint. |
| `STURNUS_LOG_LEVEL` | `INFO` | no | Level for **Sturnus's own** loggers only, and safe to set to `DEBUG` in production: Sturnus's own DEBUG lines are held to ids, counts, sizes and durations by the same field registry that governs INFO. It cannot turn up a third-party logger — see section 7.2 for why that restriction is a security control rather than tidiness. |
| `STURNUS_LOG_THIRD_PARTY_LEVEL` | `WARNING` | no | Level for every other library. **Raised to `INFO` if you set it lower**, and the per-logger floors in section 7.2 clamp several libraries tighter still; no value here undercuts either. A clamped value is not ignored silently — the process logs one `log.level_clamped` line at startup saying it happened. |
| `STURNUS_LOG_FORMAT` | `json` (auto) | no | `json` — one object per line on stdout, which is what `alloy-logs` scrapes into Loki — or `console` for a human at a terminal. Defaults to `console` when stdout is a TTY and `json` otherwise, so local development needs no setting. Both formats share the same redaction filter; the choice is presentation only. |

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
| `STURNUS_OUTLINE_SERVICE_KEY` | **yes** | **yes** | Outline API token `OutlineSink` authenticates with when creating documents, and — hourly — reads the collection list with, so the console can show `document_target` as a name instead of the UUID somebody pasted. Note the name — it is `OUTLINE_SERVICE_KEY`, not an `API_TOKEN` variant. A token that is invalid, lacks access, or points at a collection that does not exist surfaces as `PermanentDocumentError`; see section 5. A token that cannot list collections costs only the picker's names: the sweep leaves the previous mirror standing rather than emptying it, and nothing about transcription depends on it. |
| `STURNUS_WHISPER_MODEL` | `large-v3` | no | faster-whisper model to load. Larger models are more accurate and markedly slower, and this deployment transcribes on CPU (see the chart's `worker.resources`), so the difference is measured in minutes per recording rather than seconds. It is deliberately not `large-v3-turbo`: turbo is a distilled decoder with four layers instead of thirty-two, and what it gives up is concentrated outside English. Transcription happens offline, per speaker, after the meeting, so the time it costs is time nobody is waiting on — the memory it costs is real, and the chart's `worker.resources` comment works it out. |
| `STURNUS_WHISPER_DEFAULT_LANGUAGE` | `de` | no | Language reported when faster-whisper's own detection comes up empty. This is the floor under the per-guild `transcription_language` (section 4.1), not the usual setting to reach for — it is consulted only for a guild that asked for detection (`transcription_language auto`) and got nothing back. It still matters more than a fallback usually does: the first transcription for a speaker in such a session pins that speaker's language, and every later job for them reuses it. |
| `STURNUS_MODEL_CACHE_DIR` | unset | no | Where model weights are cached. When set, the worker exports it as `HF_HOME` before loading the model, so the download lands on a persistent volume; left unset, every cold start re-downloads several gigabytes of weights. |
| `STURNUS_WORK_DIR` | `/tmp` | no | Scratch directory the encrypted recording is downloaded and decrypted into before transcription. It must be large enough for the biggest single recording — the chart sizes the corresponding volume with `worker.tmpSizeLimit`. |
| `STURNUS_MAX_JOB_ATTEMPTS` | `3` | no | How many failed attempts a job gets before `JobQueue.fail` marks it `dead`. See section 5 for what a `dead` job means for the rest of its session. |
| `STURNUS_JOB_LEASE_SECONDS` | `1800.0` | no | How long a claimed job may stay `running` before `JobQueue.claim` reclaims it for another worker. It is generous on purpose: it must exceed the longest plausible transcription, or a still-running job gets picked up a second time. |
| `STURNUS_HEALTH_PORT` | `8080` | no | Port the `/healthz`, `/readyz`, `/metrics`, `/version` HTTP endpoints listen on. `/metrics` answers **`501 Not Implemented`**: metrics are *pushed* over OTLP, not scraped — see section 7. |
| `STURNUS_SENTRY_DSN` | unset | no | Sentry DSN for error reporting. Empty disables it entirely: `sentry_sdk.init()` is never called, so no instrumentation is installed and the process runs exactly as it does without Sentry. Supplied through the `Secret`, and required to be present even when blank; see section 1.4 for why a value that is not a credential is stored like one. |
| `STURNUS_SENTRY_ENVIRONMENT` | `production` | no | Names the deployment in Sentry's environment filter **and** supplies OpenTelemetry's `deployment.environment.name`. One variable for both on purpose — `OtelSettings.environment` reads this exact name rather than adding a second one, so the environment filter in Sentry and the one in Grafana can never disagree. |
| `STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT` | unset | no | Base URL of an OTLP/HTTP receiver — in this cluster `http://alloy-receiver.grafana.svc:4318`. Unset or empty — the chart's default — disables traces and metrics entirely: no provider is constructed, so every span is a no-op, nothing connects, nothing retries, and no export failure is ever logged. Give the base URL with no `/v1/traces` suffix; the exporters append their own paths. Not a credential; see section 1.4. |
| `STURNUS_OTEL_TRACES_SAMPLE_RATIO` | `1.0` | no | Fraction of traces sampled. `1.0` is correct today and the arithmetic is in `sturnus/infrastructure/telemetry.py`: the worker processes one job at a time and each is minutes of CPU work, the bot opens a handful of sessions per guild per day, and the packet path emits no spans at all. This exists as a valve for a future high-volume path. Note that job outcome is *also* an unsampled counter, so sampling can never be why a failed job is invisible. |
| `STURNUS_OTEL_METRIC_EXPORT_INTERVAL_SECONDS` | `60.0` | no | How often metrics are pushed to the OTLP endpoint. |
| `STURNUS_LOG_LEVEL` | `INFO` | no | Level for **Sturnus's own** loggers only, and safe to set to `DEBUG` in production: Sturnus's own DEBUG lines are held to ids, counts, sizes and durations by the same field registry that governs INFO. It cannot turn up a third-party logger — see section 7.2 for why that restriction is a security control rather than tidiness. |
| `STURNUS_LOG_THIRD_PARTY_LEVEL` | `WARNING` | no | Level for every other library. **Raised to `INFO` if you set it lower**, and the per-logger floors in section 7.2 clamp several libraries tighter still; no value here undercuts either. A clamped value is not ignored silently — the process logs one `log.level_clamped` line at startup saying it happened. |
| `STURNUS_LOG_FORMAT` | `json` (auto) | no | `json` — one object per line on stdout, which is what `alloy-logs` scrapes into Loki — or `console` for a human at a terminal. Defaults to `console` when stdout is a TTY and `json` otherwise, so local development needs no setting. Both formats share the same redaction filter; the choice is presentation only. |

Whisper's device and compute type are deliberately *not* environment-driven:
the worker constructs `WhisperEngine` with `"cpu"` and `int8_float32`
hardcoded, because Spec 7 sizes this deployment for CPU inference. There is
no `STURNUS_WHISPER_DEVICE` to set — moving to GPU is a code change, not a
configuration change.

`int8_float32` rather than plain `int8`: the weights are quantised to int8
either way, and the suffix names the type everything else runs in.
CTranslate2 treats bare `int8` as an alias and picks that float type for
whichever machine it finds itself on, which would leave transcription
quality depending on the node the pod was scheduled to. It also falls back
silently rather than refusing a compute type it cannot provide, so a wrong
value here costs accuracy with nothing in the logs to say so.

Neither the decoding parameters (`beam_size`, `condition_on_previous_text`,
the VAD filter and the two hallucination thresholds) is configurable
either. They are quality decisions with one right answer for this workload,
argued in `sturnus/infrastructure/whisper.py` and pinned by
`tests/infrastructure/test_whisper.py`; what *is* per-guild is the language
and the vocabulary, and those are runtime configuration rather than
environment variables — see section 4.1.

### 1.3 `sturnus-link` (`sturnus.entrypoints.link.LinkSettings`)

| Variable | Required | Secret | Purpose |
|---|---|---|---|
| `STURNUS_DATABASE_URL` | **yes** | **yes** | Same connection string as the other two. `link` reads and writes only `oauth_state` and `account_link`, and waits for those tables rather than migrating anything itself. |
| `STURNUS_OUTLINE_BASE_URL` | **yes** | no | Base URL of the Outline instance the authorization code is exchanged against. |
| `STURNUS_OUTLINE_CLIENT_ID` | **yes** | no | The same public OAuth client id the bot uses. |
| `STURNUS_OUTLINE_CLIENT_SECRET` | **yes** | **yes** | OAuth client secret for that same application. This process is the only one that holds it, because it is the only one that exchanges an authorization code for a token. |
| `STURNUS_OUTLINE_REDIRECT_URI` | **yes** | no | The callback URL, repeated here because the token exchange sends it again for verification. It must match the bot's value exactly — see section 1.5. |
| `STURNUS_HEALTH_PORT` | `8080` | no | Port the `/healthz`, `/readyz` and `/oauth/callback` routes are served on. |
| `STURNUS_SENTRY_DSN` | unset | no | Sentry DSN for error reporting. Empty disables it entirely: `sentry_sdk.init()` is never called, so no instrumentation is installed and the process runs exactly as it does without Sentry. Supplied through the `Secret`, and required to be present even when blank; see section 1.4 for why a value that is not a credential is stored like one. |
| `STURNUS_SENTRY_ENVIRONMENT` | `production` | no | Names the deployment in Sentry's environment filter **and** supplies OpenTelemetry's `deployment.environment.name`. One variable for both on purpose — `OtelSettings.environment` reads this exact name rather than adding a second one, so the environment filter in Sentry and the one in Grafana can never disagree. |
| `STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT` | unset | no | Base URL of an OTLP/HTTP receiver — in this cluster `http://alloy-receiver.grafana.svc:4318`. Unset or empty — the chart's default — disables traces and metrics entirely: no provider is constructed, so every span is a no-op, nothing connects, nothing retries, and no export failure is ever logged. Give the base URL with no `/v1/traces` suffix; the exporters append their own paths. Not a credential; see section 1.4. |
| `STURNUS_OTEL_TRACES_SAMPLE_RATIO` | `1.0` | no | Fraction of traces sampled. `1.0` is correct today and the arithmetic is in `sturnus/infrastructure/telemetry.py`: the worker processes one job at a time and each is minutes of CPU work, the bot opens a handful of sessions per guild per day, and the packet path emits no spans at all. This exists as a valve for a future high-volume path. Note that job outcome is *also* an unsampled counter, so sampling can never be why a failed job is invisible. |
| `STURNUS_OTEL_METRIC_EXPORT_INTERVAL_SECONDS` | `60.0` | no | How often metrics are pushed to the OTLP endpoint. |
| `STURNUS_LOG_LEVEL` | `INFO` | no | Level for **Sturnus's own** loggers only, and safe to set to `DEBUG` in production: Sturnus's own DEBUG lines are held to ids, counts, sizes and durations by the same field registry that governs INFO. It cannot turn up a third-party logger — see section 7.2 for why that restriction is a security control rather than tidiness. |
| `STURNUS_LOG_THIRD_PARTY_LEVEL` | `WARNING` | no | Level for every other library. **Raised to `INFO` if you set it lower**, and the per-logger floors in section 7.2 clamp several libraries tighter still; no value here undercuts either. A clamped value is not ignored silently — the process logs one `log.level_clamped` line at startup saying it happened. |
| `STURNUS_LOG_FORMAT` | `json` (auto) | no | `json` — one object per line on stdout, which is what `alloy-logs` scrapes into Loki — or `console` for a human at a terminal. Defaults to `console` when stdout is a TTY and `json` otherwise, so local development needs no setting. Both formats share the same redaction filter; the choice is presentation only. |

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
- `STURNUS_SENTRY_DSN` — for a different reason; see below

Everything else is plain configuration and should be readable in the
manifests, where it can be reviewed and diffed. That is not laxity, it is
accuracy about what these values are: `STURNUS_OUTLINE_CLIENT_ID` is public
by design — the OAuth spec has it travelling in a query string of a URL the
user's own browser opens — and `STURNUS_OUTLINE_BASE_URL`,
`STURNUS_OUTLINE_REDIRECT_URI` and `STURNUS_S3_ENDPOINT` are addresses, not
credentials. Encrypting an address buys nothing and costs review: a wrong
redirect URI hidden inside a SOPS blob is a great deal harder to spot than
a wrong one sitting in a values file.

`STURNUS_SENTRY_DSN` is the eighth entry, and it is there for a different
reason than the other seven. Everything the earlier version of this section
said about what a DSN *is* still holds: the key embedded in it is Sentry's
*public* key, it authorises submitting events to one project, it grants no
read access to anything, and Sentry documents putting one in browser
JavaScript for frontend projects. What that reasoning missed is where this
particular DSN would be written down.

The manifests supplying it live in `OneLiteFeatherNET/Kubernetes-FLUX`,
which is a **public** repository, and GitHub is continuously scraped for
exactly this shape of string. A browser DSN is unavoidably public — the code
holding it runs on the reader's machine. A server-side DSN is not, and
publishing one anyway invites a stranger to fill the project with events.
Being unable to *read* anything is not the same as being harmless to
publish.

So it goes through the `Secret`, and the chart's guard
(`sturnus.secretEnv`) now refuses to render if a values file puts it back
into `env` or `commonEnv` — the same protection the real credentials have.
Two consequences follow, both deliberate:

- **The key must be present, even when Sentry is off.** Every key in those
  lists is wired with `optional: false`, so a missing one stops the pod
  rather than injecting an empty string. A cluster that does not report
  errors therefore sets `STURNUS_SENTRY_DSN=` — blank, which is the off
  switch (`SentrySettings._blank_dsn_is_absent`) — rather than omitting the
  line.
- **It joins the SOPS rotation procedure**, which for this one key means
  rotating a Sentry project key rather than a credential. That is a small
  cost, and it is the honest place to pay it.

`STURNUS_SENTRY_ENVIRONMENT` stays in `commonEnv`: it is a label, not a
key.

So do the OpenTelemetry and logging variables, and for the *first* of the
two reasons above rather than the second: `STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT`
is a cluster-internal service address, and `STURNUS_LOG_LEVEL`,
`STURNUS_LOG_THIRD_PARTY_LEVEL` and `STURNUS_LOG_FORMAT` are enum-shaped
words. None of them authorises anything, so finding one in a public
repository costs nothing — which is precisely the test the DSN failed.
`STURNUS_LOG_THIRD_PARTY_LEVEL` is worth one extra sentence because it
*sounds* like a lever on a security control and is not: any value below
`INFO` is raised to `INFO` before anything is configured, and the per-logger
floors in section 7.2 are applied after that. Finding `DEBUG` there in a
public repository would tell a reader that somebody tried, not that they
succeeded.

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

- **View Channel** and **Connect**, on **every** recording voice channel
  named by `voice_channel_ids` — the bot joins automatically once enough
  consenting members are present (`SturnusClient.on_voice_state_update`);
  it never needs **Speak**, since it only receives audio. A channel in the
  list that the bot cannot see is skipped with a warning naming it, and
  does not stop the other channels working.
- **Manage Roles** — needed for three separate things `/setup` and
  `/consent` do: creating the consent role when none exists yet
  (`SetupCog`), editing each recording channel's `Speak` permission
  overwrites for `@everyone` and the consent role, and granting/revoking
  the consent role itself on `/consent grant` / `/consent revoke`. Discord
  additionally requires the bot's own highest role to sit above the
  consent role in the guild's role list — granted permission alone is not
  enough; a role edit or assignment fails with a permissions error if the
  bot's role is positioned below it.

Guild administrators bypass all of the above at the Discord-permission
level and can always run `/setup` and `/config` regardless of role
assignment (see `require_admin`).

**View Channel, guild-wide, if the console is in use.** Every ten seconds
the bot mirrors the guild's channels, roles and the display names of the
consent-role and admin-role holders into the database, because the
console's API process has no Discord token and must never be given one
(see `docs/superpowers/specs/2026-08-21-sturnus-console-design.md`
Section 6.1). That mirror is what lets the console show "meeting" where it
would otherwise show a raw snowflake. Discord only pushes channels the bot
can see, so a channel the bot lacks **View Channel** on is simply absent
from the mirror and the console shows its id — which is what it does today
for every channel, so this costs nothing beyond a less helpful picker.

**No new intent for that sweep.** It reads `Guild.voice_channels`,
`Guild.roles` and `Role.members`, all of them gateway-cache lookups rather
than API calls. The last one needs the **Server Members Intent**, which is
already required above for the consent gate.

### 3.2 Why the recording channels' permissions matter

`/setup` denies `Speak` to `@everyone` on every allowed recording channel
and allows it for the consent role — this is not cosmetic, it is the primary
layer of the consent protection (Spec 3.1): someone who has not consented
cannot technically produce audio in that channel at all. The bot enforces
a second, independent layer on top of it: `VoiceReceiveAdapter` drops any
incoming audio packet unless the speaker holds the consent role **and** a
stored consent record matching the guild's current `policy_version`,
regardless of what channel permissions say. It exists specifically because
a guild administrator bypasses channel overwrites and could otherwise
speak in the channel without the role — and because the record check is
what makes bumping `policy_version` take effect on its own (see section 6).

### 3.2.1 What a consent covers: `scope`

A consent record names a **scope**, either `audio` (the default, and what
every record written before this existed says) or `audio_video`.

**Sturnus does not record video, and the scope does not change that.**
What exists is a diagnostic that measures whether Discord will send a bot
the video streams it announces at all; packets that arrive are counted and
dropped without a byte being decoded, and the whole thing is off unless
`STURNUS_CAPTURE_DIAGNOSTICS` is set. The scope exists *before* the
capability on purpose: a system must be able to record that somebody said
no before it acquires the ability to do the thing they said no to, and
built the other way round there is a window in which every grant taken has
to be taken again.

What the scope enforces today is narrower and comes first: **the bot does
not ask Discord for a speaker's video unless that speaker's consent says
`audio_video`.** At connect it sends Media Sink Wants with `{"any": 0}` —
send me no stream I have not named — and it names a stream only after
reading the consent behind it. Asking for somebody's camera and discarding
the packets is not the same act as not asking: a person's client can show
them that a stream is being consumed, and nothing about the discard
reaches them.

Two things to look at when a video diagnostic reports nothing:

- `sturnus.voice.packets{outcome="video_no_consent"}` — Discord sent a
  stream this connection asked it not to send.
- The probe's own line, which now counts announced streams refused for
  lack of video consent separately. A run in which every speaker consented
  to audio only says nothing about whether Discord sends video; the
  verdict in that line says so rather than reading as a finding.

### 3.3 Why non-recorded channels must also exist

If the recording channels are the *only* voice channels available,
consenting to recording stops being a real choice — it becomes the price of
admission for talking to anyone by voice at all, and consent extracted that
way is not "freely given" under Art. 7(4) GDPR (the prohibition on tying an
unrelated condition to consent). At least one voice channel that Sturnus
never joins must exist alongside the recording channels for consent on
them to be legally meaningful, not merely technically present. Adding a
room to `voice_channel_ids` is therefore not a free action: each one taken
into the list is one fewer room where a member can talk without being
recorded.

### 3.4 `/setup` applies the permissions itself

`/setup` is not just a configuration-writing command: it reads the current
`Speak` overwrites of **every** allowed voice channel — not only the one
it was just given — and, wherever they do not already match (`@everyone`
denied, the consent role allowed), applies the change itself through the
Discord API, with every partial failure reported back per channel rather
than swallowed. This is deliberate (see `setup_cog.py`'s module docstring)
— the one step that must never be gotten wrong is not left to prose in
this document for whoever reads it least carefully.

### 3.5 One voice connection per server

A Discord bot holds exactly **one** voice connection per guild. That is a
platform limit, not a Sturnus decision, and no configuration changes it.
`voice_channel_ids` therefore means "Sturnus may record in any of these,
and follows the one that is meeting" — never "Sturnus records all of them
at once".

When more than one allowed channel holds consenting members, Sturnus picks
one deterministically: **the most consenting members wins, and the lowest
channel id breaks a tie** (`sturnus.application.channel_choice`). A session
already in progress is never moved — its `sessions` row names the channel
its audio came from — so the second room waits for the first meeting to
end. `/config show` says how many of the allowed channels are being served
("serving 1 of 3 allowed channels"), names which one it is and which are
not, and marks any that have people waiting in them. The count is there so
the other rooms do not read as ones Sturnus is choosing to leave alone.

Recording two rooms of one server simultaneously would need a second bot
identity (a second Discord application and a second deployment). That is a
deployment decision nobody has taken. The runtime no longer stands in the
way of it — a recording is keyed by the room it happens in, and the limit
is one constant (`MAX_CONCURRENT_SESSIONS_PER_GUILD`) that the code asks
rather than assumes — but raising that constant without supplying the
second identity breaks the bot instead of improving it.

### 3.6 Shards, and the four things they do not change

A Discord bot's guilds are split across **shards**. A shard is one gateway
connection, and Discord routes a guild to shard `(guild_id >> 22) %
shard_count`. Sturnus's bot process holds all of them
(`discord.AutoShardedClient`), and `STURNUS_SHARD_COUNT` is the only knob.

**Leave it unset.** Unset means the bot asks Discord `/gateway/bot` and
opens the number Discord recommends — which tracks the guild count by
itself, where a number pinned in a values file does not. Discord *requires*
sharding above 2500 guilds; below that, shards buy headroom and one
concrete piece of resilience: a shard that has to reconnect stalls only its
own guilds' events, instead of every guild's queueing behind one socket.

Set it explicitly only with a reason — matching a `max_concurrency` bucket
during a large-bot rollout, or reproducing a routing problem on a known
shard layout. Values below `1` are refused when the process starts, naming
the variable, rather than producing a bot that opens no connection and sits
there looking alive.

**What sharding changes about `/readyz`.** Readiness is now "every shard
this process holds is up", rather than one boolean set at startup and never
cleared. With four shards and one reconnecting, `/readyz` answers `503`
with a body naming the missing shard:

```json
{"status": "not ready", "discord_connected": false, "database_reachable": true,
 "shards_connected": 3, "shards_expected": 4, "shards_missing": [2]}
```

That is deliberate. Three shards up means a quarter of the servers Sturnus
is in cannot be recorded in at all, and a probe reporting perfect health
through that is worse than one that occasionally flaps. It does **not**
flap on a routine reconnect: the probe polls every 10 s with
`failureThreshold: 3`, so a shard has to be absent for 30 continuous
seconds before Kubernetes marks the pod NotReady, and an ordinary RESUME
takes a second or two. The bot's Service carries no traffic, so a NotReady
pod here is a signal to go and look, not a load-shedding action.

**What sharding changes about the logs.** `bot.shard_ready`,
`bot.shard_resumed` and `bot.shard_disconnected` carry `shard_id` and
`shard_count`. Per-guild lines — the guild tick, the voice lifecycle, the
capture failures — carry `shard_id` **only when this process holds more
than one shard**: with one shard it would be `0` in every line for ever,
which is a key in every Loki stream that answers nothing. So, once a shard
count above one is in use:

```logql
# Which guilds went with the shard that just dropped
{app="sturnus-bot"} | json | shard_id="2"
```

**What sharding does not change.** All four are worth stating plainly,
because the word invites the opposite conclusion:

1. **The Deployment stays at `replicas: 1`, with `strategy: Recreate`.**
   Four shards is still one process. Two pods would each open the same
   shards, hold two gateway connections to the same guilds, and record
   every session twice — exactly the situation `replicas: 1` exists to
   prevent, shard count or not.
2. **One voice connection per guild.** That is a platform limit and section
   3.5 is unaffected: a guild with three allowed channels still records one
   of them at a time.
3. **One recording PVC.** In-progress recordings live on the pod's own
   `ReadWriteOnce` volume, and the SIGTERM handler that flushes them is
   still one handler in one process.
4. **One announcement sweep.** The poll that posts a finished session's
   document link reads `sessions` rows for every guild there is. With one
   process that is exactly right; it is the one sweep in the bot that would
   stop being right otherwise.

#### What running one shard range per pod would actually take

Written down so that it stays a decision rather than becoming an
archaeology exercise. Sturnus has **not** built this. In rough dependency
order:

1. **A StatefulSet, not a Deployment.** Each pod needs a stable ordinal to
   derive its shard range from; a Deployment's pods are interchangeable and
   have no such identity. `sturnus.application.sharding.shards_this_process_owns`
   is the one function that would read it — it returns every shard today,
   and would return `range(ordinal * per_pod, (ordinal + 1) * per_pod)`.
2. **`shard_ids` passed to `AutoShardedClient`** alongside `shard_count`.
   discord.py requires both when shard ids are given by hand, and refuses
   ids without a count outright.
3. **A `volumeClaimTemplate` instead of the single shared PVC.** The
   recording volume is `ReadWriteOnce` and holds in-progress audio; two
   pods cannot share it, and a pod that restarts must come back to *its
   own* unflushed recordings for `recover_orphans` to find them.
4. **The announcement sweep scoped to the pod's shards.** Already asked:
   `announce_ready_sessions` calls `process_serves_guild` for every
   candidate session, so this half is a change to
   `shards_this_process_owns` and nothing else. Without it, four pods post
   the same document link four times.
5. **A PodDisruptionBudget that means something at N replicas.**
   `minAvailable: 1` at one replica blocks every voluntary eviction, which
   is the point today; at four it would permit three quarters of the bot to
   be drained at once, mid-recording.
6. **Session-start rate limiting across pods.** Discord's `max_concurrency`
   bounds how many shards may IDENTIFY per five seconds. One process
   serialises its own launches; N pods starting together do not, so a
   rollout would need `AutoShardedClient.fetch_session_start_limits` and a
   `before_identity_hook` coordinating between them.
7. **The console's mirrors read as though they were complete.** The
   administrator and directory mirrors are written per guild off each
   process's own gateway cache, so they stay correct — but a pod that is
   down leaves its guilds' rows stale with nothing saying so, and the API
   currently has no way to express that.

Items 1, 3, 5 and 6 are deployment work with no code in this repository to
change. Item 4 is one function body. Items 2 and 7 are small. The ordering
is the useful part: none of it is hard, and none of it happens by raising
`replicas`.

## 4. First run

1. Run `/setup`, supplying a voice channel to record, the URL of the
   privacy/consent policy, and a policy version identifier. Optionally
   supply an existing role as the consent role; if omitted, `/setup`
   reuses whatever role is already configured (if it still exists) or
   creates a new one. Run it again, with a different channel, for every
   further room Sturnus should be allowed to record: `/setup` **adds** to
   `voice_channel_ids` rather than replacing it, so setting up a second
   meeting room does not un-configure the first. To remove one, edit the
   list with `/config set voice_channel_ids <the remaining ids>` — the
   reply to `/setup` prints the current list so that is a copy and a
   deletion, not a lookup. Removing a channel does not undo its `Speak`
   overwrites; restore those by hand if the room should go back to being
   an ordinary one.
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

Worth setting even though it is not required: `timezone` decides the times
written into the protocol and its title. It defaults to `Europe/Berlin` and
takes any IANA name (`Europe/London`, `UTC`). This one is easy to leave
wrong, because a wrong offset does not look wrong -- every hour reads as a
plausible meeting time. An unusable value falls back to UTC with a warning
naming the guild, rather than costing the protocol.

```
/config set timezone Europe/Berlin
```

Worth setting for the same reason, and for a bigger one: `transcription_language`
decides what language the recordings are transcribed as. It defaults to
`de`. The alternative is not "no language" but detection, and detection is
weak exactly where it is used here — it runs on one speaker's track with
the silence already cut out of it, so a participant whose first
contribution is a three-second "ja, genau" gives it almost nothing to work
with. Whatever it guesses is then pinned for that speaker for the rest of
the session, so one unlucky guess is not one bad job, it is every job for
that person from then on. Naming the language removes the guess.

```
/config set transcription_language de
```

A guild that genuinely meets in more than one language sets it to `auto`,
which is what asks for detection-and-pinning explicitly. There is no third
state: clearing the key restores the `de` default rather than removing it.

`transcription_prompt` is the vocabulary Whisper is biased towards while
decoding — Whisper's `initial_prompt`. It defaults to OneLiteFeather's own
project names and stack, written as an ordinary German sentence so the
style it biases towards is punctuated prose as well. Proper nouns are both
what a general model reliably gets wrong and what a protocol is read for: a
decision minuted about the wrong project is worse than no minutes. Set it
if your names are different ones:

```
/config set transcription_prompt "Protokoll eines Meetings über Foo, Bar und Baz."
```

Keep it a sentence rather than a word list, keep it in the transcription
language, and keep it short — Whisper only sees the last ~224 tokens of it,
and a long prompt bleeds its own wording into the transcript.

`video_consent_offered` is `true` or `false`, and defaults to `false`. It
decides whether the people in this guild may widen their consent scope to
`audio_video` at all (section 3.2.1). While it is false the API refuses a
widening with `video_consent_not_offered` and the console leaves the
option out of its interface entirely — absent, not disabled.

**Turning it on is an assertion, and nothing can check it for you.**
Switching it to `true` says: the document at `policy_url`, at the current
`policy_version`, describes video recording. Software cannot read that
document, so it does not pretend to have checked it. A consent record
naming `audio_video` under a policy that describes only audio is not
consent, and the switch is where that judgement is recorded. The same
construction as the audio-playback question in section 6.2, and honest for
the same reason: the implementation cannot enforce it, so it is written
down where the person switching it on will read it.

The value must be exactly `true` or `false`. `/config set` refuses
anything else, deliberately: readers treat an unrecognised value as false,
so `yes` would fail nowhere at all and quietly mean the opposite of what
whoever typed it meant.

### 4.0 `voice_channel_ids`, and the key it replaced

`voice_channel_ids` names every voice channel Sturnus is allowed to record
in. Its value is a **comma-separated list of channel ids**; whitespace
around the commas is ignored, and the order carries no meaning (it is
normalised to ascending ids on read):

```
/config set voice_channel_ids 123456789012345678,987654321098765432
```

The value is validated at the write, not at the join: a non-integer entry,
an empty entry (`12,,34`), a repeated id, or an empty list is refused by
`/config set` and by the console with a message naming the entry it could
not read. A guild that reports itself configured and then records nothing
is the failure that check exists to prevent.

It says **where Sturnus may record, not how many rooms it records at
once** — see 3.5 above for the one-connection-per-server limit and the
rule that picks between two busy rooms.

**The legacy alias.** The setting used to be called `voice_channel_id`
(singular, one id). That key is still read, indefinitely, and a guild that
has never touched its configuration since the rename keeps recording
exactly as before — there is no migration, and none is planned.
`guild_config` is an EAV table keyed `(guild_id, key)`, so the old name is
a row rather than a column and nothing is in the way.

The precise rules:

- `voice_channel_ids` wins outright whenever it is set. `voice_channel_id`
  is read only when the plural key is unset, so a stale singular row cannot
  overrule a deliberate write.
- `/setup` and `/config set voice_channel_ids` write the plural key. Only
  the plural key can name more than one channel.
- Either key alone satisfies the "required" check, and the missing-key
  report always names the plural one — telling an administrator to
  configure a deprecated setting would be worse than saying nothing.
- `/config show` says so when a guild is still on the old key, and says so
  again (differently) when *both* are set, naming which of the two is
  actually in use.
- The old key stays writable and clearable from `/config` so a guild can
  move off it. The console will not clear either channel key: neither has a
  default to fall back to, so clearing one from a web form would take a
  guild out of service while looking like tidying up.

Until every required key (`voice_channel_ids`, `consent_role_id`,
`document_target`, `policy_version`, `policy_url`, `admin_role_id`) is set,
the bot logs a warning naming the guild and skips building that guild's
recording pipeline entirely (`SturnusClient._desired_config`) — it will
not join any voice channel or record anything for that guild. It re-checks
every ten seconds, so the pipeline appears as soon as the keys are there;
no restart is involved (see 4.1).

### 4.1 Changing configuration at runtime

The bot reconciles every guild against `guild_config` roughly every ten
seconds (`SturnusClient._tick_guild`), and immediately whenever `/config
set`, `/config clear` or `/setup` writes something. Configuration changes
therefore take effect without restarting the pod — with the exceptions
below, which the commands themselves also say out loud.

**Live immediately, even in the middle of a recording.**
`empty_grace_seconds`, `idle_timeout_minutes`, `max_session_hours`,
`audio_retention_days`. Each is read at exactly one point — the next
timeout check, or the moment the session is filed — so the new value is
simply the one used next. Shortening a timeout below the age of the
session in progress closes that session on the next tick; that happens
through the ordinary path (encrypt → upload → enqueue), so it ends the
recording earlier but never discards it. `/config set` warns explicitly
when it detects this.

**Live immediately, and never were stale.** `admin_role_id`,
`policy_version`, `policy_url`, `video_consent_offered` (read per command
invocation, per API request, and by the consent cache with a five-second
TTL), and `transcription_language`,
`transcription_prompt`, `document_target`, `document_provider`,
`merge_gap_seconds` (read per job by the *worker* process, not the bot at
all). The two transcription keys apply to the next job the worker claims,
which means a session already recording is still transcribed with the new
value — and a job that has already run is not redone. Changing them
because a protocol came out wrong therefore affects the next meeting, not
the one you are looking at.

**Deferred until the recording in progress ends.** `voice_channel_ids`
(and the `voice_channel_id` it replaced) and `consent_role_id`. These
decide which channels a session may open against, which channel a
session's row names, and which role both the headcount and the per-packet
filter agree on;
swapping them mid-session would mean a protocol whose header names one
channel while its audio came from another, an orphaned plaintext file on
the PVC, and a voice connection nobody would ever disconnect. The change
is stored right away and applied the instant the session has finished
closing — after its audio is encrypted, uploaded and enqueued. **The
recording is never lost.** The wait is bounded by `max_session_hours`
(default 4 h). `/config show` names the keys still waiting, and `/config
apply force:true` ends the session in progress deliberately — uploading
and transcribing it normally — and applies the change at once. A deferral
can equally be withdrawn: put the old value back (or re-set a key you
cleared) before the session ends and nothing is applied — the pipeline
keeps running untouched and `/config show` stops naming the key.

**Needs a pod restart.** `publish_poll_seconds` only. The publish sweep
runs on one process-wide interval taken from that setting's *default*
rather than per-guild scheduling (a deliberate simplification, see
`_PUBLISH_POLL_SECONDS` in `sturnus.entrypoints.bot`), so a per-guild
value for it is not read at runtime at all. `/config set
publish_poll_seconds` says so instead of pretending it applied.

**After editing `guild_config` directly with SQL.** `ConfigStore.set`
validates integer keys; a raw `UPDATE` does not. An unparseable value is
logged once and the guild keeps its last known-good configuration rather
than falling back to defaults (which would silently un-configure a
working guild). Run `/config apply` to re-read immediately instead of
waiting for the next tick; `/config show` will tell you whether what is
stored is actually what is running.

## 5. Troubleshooting

**A pod is SIGKILLed at 137 shortly after starting, with `connection
refused` on its probes.** The process is not crashing; its liveness probe
is killing it while it is still importing. Look at the gap between the
container's `startedAt` and its first "listening" log line: on 0.6.0 a
`link` pod took 55 seconds on 200m of CPU, against a liveness budget of
`initialDelaySeconds + periodSeconds x failureThreshold` = 65 seconds.

Every component has a `startupProbe` for exactly this, and liveness and
readiness are not evaluated at all until it succeeds. If you see this
again, the startup budget has been outgrown rather than the process broken
-- raise `<component>.startupProbe.failureThreshold`, or give the component
more CPU so it imports faster. `.github/workflows/chart.yml` asserts that
every component's startup budget exceeds its liveness budget, so the two
cannot drift apart silently.


**`/queue`, the admin command for most of what follows.** `QueueCog`
(`src/sturnus/infrastructure/discord/queue_cog.py`) adds three subcommands,
all admin-gated the same way `/setup` and `/config` are (`require_admin`,
section 3.1) and all replying `ephemeral=True`:

- **`/queue status`** — a guild-wide, counts-only overview: jobs by status
  (`pending` / `running` / `done` / `dead`), how many `running` jobs are
  past the default lease, the age of the oldest `pending` job, and how many
  `closed` sessions have every job finished but no document yet. Read-only.
- **`/queue session <session_id>`** — one session in detail: its status,
  end time and reason, its document URL and announcement time, and one
  line per speaker giving job status, attempts, whether the audio is still
  present, the last error, and the *length* of the stored transcript —
  never its text; a slash command is deliberately not a way to read
  meeting content. Read-only. A Discord message holds 2000 characters, and
  this reply is bounded to fit inside one: each speaker's `error` is shown
  up to 160 characters (whitespace collapsed, an `…` marking the cut), and
  a session with more speakers than fit ends in a line saying how many are
  not shown. That is a real limit for a large channel — roughly the first
  twenty speakers fit — so for a full picture of a big session, or for an
  error too long to display, read the rows directly with the query at the
  end of "A job is `dead`" below.
- **`/queue requeue <session_id>`** — the only one that writes. It resets
  the session's finished jobs back to `pending` so the worker transcribes
  them again from the still-stored audio, discarding whatever was there
  before. Nothing is written until an explicit Confirm press on a message
  that names, in full, what cannot be undone: the discarded transcripts, a
  second Outline document (the old one is left in place, not deleted or
  updated — `DocumentSink` has no update path), and a second announcement
  posted to the recording channel. It refuses outright if any job of the
  session is still `pending` or `running` (there is nothing to redo yet,
  and resetting a job a worker is about to finish would just let that
  worker overwrite the reset), and it skips — rather than resets — any
  speaker whose audio has already been erased, carrying their existing
  transcript into the new document unchanged.

  **It also refuses any session that is not `documented` yet**, and says
  which state it is in instead. `documented` is the only status in which
  nothing else in the pipeline still has a claim on the session, and the
  two other states are refused for two different reasons:

  - `open` — the recording has not finished, or the bot is still uploading
    the speakers it recorded. Sessions are enqueued one speaker at a time
    and only marked `closed` after the last upload, so re-queueing here
    would close the session while speakers are still being added to it,
    and the next job to finish would be taken for the session's last: the
    document would then be built from part of the meeting. This is the
    likely mistake right after a long meeting ends — the recording looks
    over in Discord well before the bot has finished uploading it.
  - `closed` — transcription finished but no document exists yet. The
    worker's retry sweep still owns the session and creates that document
    on its next pass; a re-queue landing in the middle of the sweep can
    leave the session documented from the transcripts the re-queue just
    discarded, with nothing revisiting it afterwards.

  In both cases the remedy is to wait and re-run `/queue session
  <session_id>` until it reports `documented`. If a session never gets
  there, that is a separate fault — `/queue status` counts the sessions
  stuck in it, and "Telling a transcription failure from a document
  failure apart" below is where to start on it.

Every `/queue` query is scoped to the guild the command was run in; a
session id from another guild gets the same reply as one that does not
exist, so it cannot be used to probe another guild's sessions.

**A speaker's audio arrives with no level.** During a meeting the bot may
post, into the recording channel and naming the person:

> Audio is arriving from @Name but at no audible level. The microphone is
> most likely muted at system level. Recording continues.

That message means something narrower than "it is quiet", and the
distinction is the whole point. Three states exist and only the third is
reported:

| What is happening | Reported? |
|---|---|
| No packets at all from that speaker | No — they are not speaking, which is normal |
| Packets arrive but will not decode | No — that is `EndReason.DECODE_FAILURE`, section 5 below |
| Packets arrive, decode, and every sample stays at the noise floor | **Yes** |

It fires after 30 seconds of *received* audio at the noise floor
(`SILENCE_EVIDENCE_SECONDS` in `sturnus/domain/silence.py`), counted in
bytes of PCM rather than wall-clock — somebody who transmits nothing for
half an hour has produced no evidence about their microphone and is never
warned. Any audible packet discards that speaker's evidence and starts
over. Once per speaker per session, never repeated.

The usual cause is a microphone muted below Discord: in the operating
system's mixer, on the device itself, or a hardware mute switch. Discord's
own mute sends no packets at all and therefore never triggers this.

It is also recorded, so it can be seen after the fact:

```sql
SELECT discord_display_name, silent_audio_detected_at
FROM session_participant WHERE session_id = <id>;
```

A row with `silent_audio_detected_at` set will have contributed an empty
or near-empty transcript. That is the signal to look at before concluding
that Whisper failed: a transcript of one short hallucinated phrase over a
long recording is what silence looks like after transcription, not what a
broken model looks like.

Only amplitude is ever measured. No sample is buffered, logged or sent
anywhere, which is why this applies equally to people who have not
consented — a peak is a number about loudness, not about content.


**A job is `dead`.** `transcription_job.status` becomes `dead` once
`attempts` reaches the worker's configured retry limit
(`JobQueue.fail`). A dead job is deliberately excluded from the
remaining-jobs count that decides whether a session is finished
(`JobQueue.complete`) — so one unreadable recording does not block the
rest of that session's document from being produced; it just means that
one speaker's portion is permanently missing from it. `/queue status`
gives the guild-wide count and `/queue session <id>` names which speaker
and shows the last error for one session; for anything that cuts across
sessions or guilds, or that needs the raw row, query directly:
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

**A third kind, distinct from both of the above: a job that is `done`,
raised no error, and is still wrong.** Every session transcribed by a
worker running Silero VAD (`vad_filter=True`, before this fix landed —
`sturnus.infrastructure.whisper.WhisperEngine._transcribe`, commit
`12d4299`) produced an empty or hallucinated transcript while reporting
complete success: `status` is `done`, `transcript` is non-`NULL`, nothing
is logged, and `/queue status` shows nothing wrong, because nothing failed
from the code's point of view. The mechanism (full reasoning in
`sturnus.infrastructure.speech_gate`'s module docstring) was Silero's
recurrent state collapsing on the bit-exact digital-zero padding
`SpeakerWriter` writes into every gap between packets — on a real
100-minute recording it reported about one second of speech in two
minutes, and the transcript for that speaker came back
`" Copyright WDR 2021"`, a stock Whisper hallucination on near-silence
that has nothing to do with anything anyone said. `"Thank you."` is
another shape of the same failure: a short, generic, plausible-sounding
sentence standing in for a much longer real recording.

*How to recognise it, without reading a transcript's content at all.*
`/queue session <session_id>` reports `transcript: N characters` per
speaker. A session that ran for an hour with a transcript of a few dozen
characters is the tell — the test fixture for this exact failure
(`tests/infrastructure/discord/test_queue_cog.py`) uses the real observed
hallucination `" Copyright WDR 2021"`, all of 19 characters, precisely
because a number that small against a long session is unambiguous at a
glance, and the command deliberately never echoes the transcript text
itself (only its length), so this check does not require reading meeting
content to make the call.

*This affects only sessions transcribed before the fix.* A session
transcribed by a worker running the amplitude gate does not exhibit this —
the gate carries no state across frames, so there is no history for
padding to corrupt (again, see the module docstring for why). Do not chase
this section for a session transcribed after the fix shipped; a short
transcript there has some other cause.

*What to do.* Check that `/queue session <session_id>` reports the session
as `documented` — a re-queue of an `open` or `closed` session is refused,
for the reasons listed under `/queue requeue` above — then check `audio:
present` vs. `audio: erased` per speaker — erased audio cannot be re-transcribed, only
carried forward unchanged (section 6 covers when audio is erased) — and
then run `/queue requeue <session_id>`. Read what its confirmation says
before pressing Confirm: it names the old (bad) document and states that
it stays and is not deleted or updated, and it says a new link will be
posted publicly in the recording channel once the redo finishes. The
worker's ordinary pipeline carries the redo the rest of the way on its own
— nothing else needs to be run by hand.

One more thing worth expecting rather than being surprised by: a redone
job actually transcribes the speech instead of ~1% of the file, so it
takes far longer than the original (garbage) run did — the design that
shipped this fix estimated roughly 20-25x, turning a couple of minutes
into tens of minutes for a long session. If `/queue status` starts
reporting `running` jobs past the default lease after a batch of
re-queues, that is very plausibly this, not a stuck worker — check whether
`STURNUS_JOB_LEASE_SECONDS` (section 1.2) has been raised to match before
assuming something is broken.

A second, harmless surprise: a re-queue run in the same seconds as the
bot's announcement poll can put the *old* link into the channel one last
time before the redo starts. The poll posts the link and only afterwards
stamps `session.announced_at`; a re-queue that lands in between clears
that column on purpose, and the late stamp is then rejected
(`SessionRepository.mark_announced` only stamps a session that is still
`documented` and still unannounced) so that the redo's new link is
announced when it is ready. The bot logs `Session N changed while its
announcement was being posted` when that happens. The alternative —
letting the late stamp through — would mean the corrected transcript is
documented and its link never posted at all, so the duplicate is the
deliberate choice.

**The bot is sitting out of the channel while people are in it.** A
session that ended with `capture_failure` or `decode_failure` means the bot
could not hear, not that nobody spoke, and the guild is then held out of
that channel for fifteen minutes
(`sturnus.infrastructure.discord.client.REJOIN_COOLDOWN`) — the bot's own
departure is itself a voice-state update, so rejoining immediately would
just meet the same fault and open another empty session row. It is not a
state anyone has to clear: the tick loop lifts it on its own and picks the
channel back up if consenting members are still there, without waiting for
anyone to leave and rejoin. The log says which of the two it is:

- `The session in channel N ended with capture_failure; not recording there
  again before <timestamp>` — armed, with the deadline in the line.
- `Ignoring a voice-state update in channel N: capture failed and no
  session will start there before <timestamp>` — someone came or went
  while it was in force.
- `The capture-failure cooldown for channel N has passed; recording may
  resume` — lifted.

If there is none of that in the log, the silence has some other cause.
Sessions that ended this way are also visible directly:
```sql
SELECT id, guild_id, channel_id, ended_at, end_reason FROM session
WHERE end_reason IN ('capture_failure', 'decode_failure') ORDER BY ended_at DESC;
```
A run of them fifteen minutes apart is a fault that is not transient; the
ERROR that the voice adapter logged at the time says what it was.

**A stalled queue.** `JobQueue.claim` uses `SELECT ... FOR UPDATE SKIP
LOCKED`, so any number of `pending` jobs sitting unclaimed with `attempts`
not increasing is not a database contention problem — it means nothing is
calling `claim()` at all. Check that the worker process is actually
running and check its `/readyz` endpoint before looking any further at
individual jobs.

**What a Sentry issue contains, and what it deliberately does not.** When
`STURNUS_SENTRY_DSN` is set, an issue carries: the exception type, the
module, a stack trace with five lines of surrounding *source* per frame, the
`component` tag (`bot`/`worker`/`link`), the release
(`sturnus@<version>`), the pod name, and — for errors raised from one of
*Sturnus's own* log calls — that log message's uninterpolated format string,
e.g. `job %s failed`.

It does not carry local variables, breadcrumbs, request data, or the
interpolated log line and its arguments. Two things are narrower than they
look, and both are deliberate:

- **The log format string survives only for the `sturnus` logger
  namespace.** That guarantee rests on ruff's `G` ruleset, which only
  governs the log calls written in this repository. Records the SDK captures
  from anywhere else are dropped whole — most importantly asyncio's "Task
  exception was never retrieved", whose message asyncio composes itself out
  of `repr(task)` and which therefore embeds the raised exception's own
  message. An issue from a third-party logger has no message line; read the
  pod log.
- **`OSError` messages are rebuilt from `errno` and `strerror`.** So
  `ConnectionRefusedError: [Errno 111] Connection refused` survives, as do
  the DNS, TLS and timeout failures that matter for a process talking to
  Discord, S3, Postgres and Outline — but the filename `OSError.__str__`
  normally appends becomes `<redacted>`, because the file an `OSError` here
  is most likely to name is
  `<recording_dir>/session-<session_id>/<discord_user_id>.wav`, which says
  who was recorded and when. An `OSError` carrying no errno at all (the
  free-form `OSError("cannot open ...")` form) is redacted entirely.

Otherwise **exception messages are redacted to `<redacted>` unless the
exception subclasses `sturnus.domain.errors.DiagnosticSafeError`**, the
explicit opt-in whose docstring carries the contract.

That is a deliberate trade, not an oversight, and it should not be undone
during an incident. Sturnus records people talking; Spec 3 makes consent a
precondition for processing any of it and Spec 12.4 requires that neither
audio nor transcript content appears in logs, and Sentry is a second system
holding a copy of whatever it is sent. Under this configuration
Sentry receives strictly less than the pod log does. **So when a message is
`<redacted>`, read it with `kubectl logs` on the pod named in the issue** —
the information has moved, not vanished. The enforcement lives in
`sturnus.infrastructure.observability` (an allowlist, so anything a future
SDK version adds is dropped by default) and in ruff's `G` ruleset, which
forbids f-string logging so that the format string Sentry does see stays a
literal written in reviewable source.

**A DSN Sentry rejects turns reporting off, not the process.** `init_sentry`
runs before the event loop in all three `main()`s, so an unparseable
`STURNUS_SENTRY_DSN` used to raise `BadDsn` out of `main()` and
CrashLoopBackOff bot, worker and link — an outage of the recording caused by
a typo in optional telemetry. It is now caught, and the process runs on
without reporting. The log line to grep for is

```
sentry error reporting is DISABLED for component <bot|worker|link>: the SDK
rejected the configured DSN (...)
```

at `ERROR`, from `sturnus.infrastructure.observability`. The DSN itself is
never echoed, only the SDK's own reason (unsupported scheme, missing
hostname, missing public key, invalid project). Its counterpart on success is
`sentry error reporting enabled for component <...>` at `INFO`; if neither
line appears, no DSN was configured.

One consequence worth knowing: `sentry_sdk` calls the scrubbing hook inside
its own exception guard, so a bug there makes Sentry go *quiet* rather than
leak. "No issues since the deploy" is therefore not by itself proof that
nothing is wrong — force one error in a non-production namespace after a
deploy and confirm it lands.

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
(`sturnus.infrastructure.discord.voice` calling `ConsentCache.may_record`,
which applies `sturnus.domain.consent.may_record` — role membership **and**
a consent record matching the current `policy_version`). So a bump takes
effect on its own, within the cache's five-second TTL: role-holders whose
consent names the superseded version stop being recorded mid-session, and
`/consent grant` under the new version is what puts them back. Removing
the role by hand is not required for a hard cutover, and doing so only
costs the affected members a second step when they re-consent.

## 6.2 The console

A web console at `https://sturnus.onelitefeather.dev` where somebody who
took part in recorded meetings can see what Sturnus holds about them, play
back the sessions they were in, and — if they administer the bot — change
its runtime settings without `kubectl` or a slash command.

Design: `docs/superpowers/specs/2026-08-21-sturnus-console-design.md`.

### 6.2.1 Before switching it on

**The consent question is not a technicality, and the code cannot answer
it.** Audio playback is a wider use of a recording than a transcript is.
The people in a session consented to being recorded so that a protocol
could be written; playing their voice back to another participant is a
different act, even though that participant heard them live.

Three things make it defensible, and the implementation can only enforce
the first two:

1. **Only participants of the same session may play its audio.** Checked
   per request against `session_participant`, scoped in the query rather
   than filtered afterwards. Not administrators-in-general, not anyone
   with a link.
2. **Everyone in a session already heard everyone else in it.** The
   console gives back what was in the room, to the people who were in it.
3. **It is stated in the policy.** `policy_version` must be bumped and the
   document at `policy_url` updated to say that participants can play back
   the audio, *before* the console is reachable in production.

The third is yours. Bumping `policy_version` invalidates every consent
naming the old one (Section 6), which is the mechanism working: people
re-consent under wording that covers what the system now does. An operator
who skips it has a working console and no lawful basis for its second
section.

### 6.2.2 What each process holds

| Process | Discord token | S3 + master key | OAuth secret | Database |
|---|---|---|---|---|
| `bot` | yes | yes | no | yes |
| `worker` | no | yes | no | yes |
| `link` | no | no | yes | yes |
| `api` | **no** | **yes** | yes | yes |
| `console` | no | no | no | **no** |

`api` holds S3 and the master key because it decrypts audio on the way to
the browser. It must never hold the Discord token: a process that can read
every recording ever made is not one to also give the ability to act as
the bot. Which guilds somebody administers is therefore read from
`admin_member`, mirrored by the bot on its ordinary tick, rather than
asked of Discord.

`console` holds nothing at all. `sturnus.secretEnv` in the chart refuses
outright to render a `secretKeyRef` for it, and the chart job asserts the
absence.

### 6.2.3 Signing in

OAuth against Outline, then **the identity is looked up in
`account_link`**. No link, no session: every console query is scoped by
Discord id, because that is what `session_participant` names, and the only
bridge from an Outline identity to one is a link the person made
themselves with `/link`. The console says so specifically, because "run
`/link` in Discord" is an instruction somebody can act on.

`STURNUS_SESSION_SECRET` signs the session cookie (HMAC-SHA256, at least
32 bytes — `SessionCookie` refuses shorter at construction, so a
placeholder fails at startup rather than serving forgeable sessions).
Rotating it signs everybody out and does nothing else: there is no
server-side session store, the cookie *is* the session.

**Two callbacks, two paths, deliberately.** `/oauth/callback` completes
`/link` in Discord and belongs to the `link` service;
`/api/auth/callback` completes a console sign-in and belongs to `api`.
Both must be registered as redirect URIs on the Outline OAuth
application. Registering one for both sends each flow into the other's
service, and each failure looks like the other's bug.

### 6.2.4 Saving a setting is not the same as it taking effect

`api` has no Discord gateway, so a write through the console is a database
write and nothing more. `/config set` in Discord writes *and* reconciles;
the console can only do the first half. Every settings response therefore
carries `takes_effect`:

| Value | What happens |
|---|---|
| `immediately` | Read per use — `policy_version`, `policy_url`, `admin_role_id` |
| `next_reconcile` | Cached by the bot; picked up within about ten seconds |
| `process_restart` | Read once at startup — `publish_poll_seconds`. **No amount of waiting lands it**; the deployment has to be restarted |

Plus `deferred_while_recording` for `voice_channel_ids` and
`consent_role_id`, which a reconcile holds back for the length of a
running session.

Every settings response also carries `may_clear`, and a front end must
gate its Clear control on that flag rather than on `!required`. The two
are not the same question. `DELETE` restores a default, so a key with no
default is refused with 409 — and there are now three classes of key, not
two: required (no default, must be set), defaulted (clearable), and the
deprecated `voice_channel_id` (no default, required of nobody, still
serving any guild that has not moved to `voice_channel_ids`). Deriving
the button from `required` puts a live Clear beside that last one and
then explains the refusal as "this key is required" on a field the same
page has just rendered as optional.

There is no console equivalent of `/config apply force:true`, and there
cannot be without giving `api` a Discord token.

### 6.2.5 Known costs

- **Administrator membership is stale by up to one bot tick** (~10 s). A
  revoked administrator keeps console write access until the next sweep.
  That is the price of the mirror, and it is what buys the API having no
  Discord token.
- **The calendar groups by UTC day.** `timezone` is per guild and one
  person's sessions can span guilds, so no guild's zone is right on the
  server; the console converts for display. A meeting at 00:30 Berlin time
  falls on the previous UTC day in the underlying data.
- **A track with no `session_participant` row keeps its audio and loses
  its name.** There is no foreign key from `transcription_job` to
  `session_participant`, so a job whose participant row is gone still has
  a recording. The console shows the track and omits the speaker rather
  than dropping a recording that exists.

### 6.2.6 Consent from the console: scope, and withdrawing it

An administrator can end a member's consent from the console's **Admin
View → User Settings**, per guild; a person can end their own from their
settings page. These are the third and fourth ways a consent can end, and
the four are not interchangeable:

| Way | Who it covers | Removes the role? | Effect on recording |
|---|---|---|---|
| `/consent revoke` in Discord | The person themselves | **Yes** | Immediate (role check is per frame, uncached) |
| Bumping `policy_version` | Everybody in the guild at once | No | Within the consent cache's 5 s TTL |
| Console → Admin View → User Settings | One named person | **No** | Within the consent cache's 5 s TTL, or from the chosen instant |
| Console → the person's own settings | Themselves | **No** | Within the consent cache's 5 s TTL |

**`revoked_at` is an effective instant, not a tombstone.** Any non-null
value used to mean "not active"; it now means "not active from then on",
and `is_consent_active` reads `now < revoked_at` on every check. An
administrator may therefore send an optional `effective_at` (ISO-8601,
with an offset) with a withdrawal:

- **Absent** means now, which is exactly what this endpoint always did. No
  client breaks by not sending it.
- **A future instant** is a scheduled withdrawal — "from the end of the
  month". Nothing new fires it: the bot re-reads the record through the
  consent cache, and the cache stores the record rather than a verdict, so
  the moment passes and recording stops within five seconds of it. There
  is no timer, no sweep and no job to watch.
- **A past instant** is a correction — somebody left in March and nobody
  wrote it down until June.

The only value refused is one before `granted_at`, which would claim a
grant ended before it began.

**A back-dated revocation deletes nothing.** It is a statement about
recordings that already exist, not an erasure of them. The response says
how many recordings with audio fall on or after the chosen instant so the
console can offer the erasure path; `/audio purge` (§12.3) and the
retention sweep remain the only two things in this system that delete
audio, and correcting a date must never quietly become a third.

The audit line distinguishes the two acts. `console.consent_revoked`
carries `effective_at_given`, because an administrator back-dating a
withdrawal and one clicking "withdraw" both leave a perfectly ordinary
date in `revoked_at`, and by the time anybody reads the row nothing else
can tell them apart. A person withdrawing their own consent from the
console emits `console.consent_self_revoked` instead, at INFO —
`requested_by` equals `discord_user_id` there and is written anyway, so a
query over both events answers "who withdrew whose consent" without a row
falling out of it.

**The console cannot remove the Discord role, and this is deliberate.**
`api` holds no Discord token (§6.2.2), so it writes `consent.revoked_at`
and nothing else. That is enough to stop the recording: the packet filter
checks *both* layers on every frame, and the stored record is the layer
that exists precisely because somebody with Discord's `administrator`
permission bypasses channel permissions and could speak without the role.
Recording of that person stops mid-session, within five seconds.

What it leaves is a member who still holds the consent role. Nothing
records them, but Discord looks as though something might, and `/consent
status` will report "role assigned: yes, consent active: no". **If the
role should go too, remove it in Discord** — either by hand or by asking
the person to run `/consent revoke`, which does both.

Note the asymmetry with removing the role *only*, which is what an
administrator would otherwise do by hand: that stops the recording and
leaves `revoked_at` NULL, so the record still reads as consent given, and
re-adding the role at any point silently resumes recording somebody who
never re-consented. Withdrawing through the console is the half that
lasts.

**It does not delete anything already recorded.** Withdrawing consent is a
decision about the future. Erasing what was recorded under the consent
that existed at the time is `/audio purge <user>` in Discord (§12.3), and
it is deliberately a separate act with a separate command. The User
Settings page shows, per person, how many recordings the guild still holds
for them, so the distinction is on the screen rather than in this
document.

**A consent record is never deleted.** `revoked_at` is stamped on the
newest grant; earlier grants keep their history. The row *is* the evidence
that consent was once given, which is what Art. 7(1) requires be
demonstrable, so a revocation modifies the grant it revokes rather than
removing it.

**Who did it is only in the log.** `consent` has no column naming the
person who performed a revocation — `/consent revoke` never needed one,
because the only person who could run it was the subject. So the audit
trail for a third-party revocation is the log line and nothing else:

```logql
{namespace="sturnus"} | json | sturnus_event="console.consent_revoked"
```

It is emitted at **WARNING** with `guild_id`, `discord_user_id` (whose
consent) and `requested_by` (who withdrew it). Retention of that line is
therefore the retention of the audit trail; if a longer one is needed, it
has to become a column, which is a migration and a change to the shared
`ConsentRepository.record_revocation`.

**A consent that is inactive is not the same as one that was withdrawn.**
The page distinguishes them, and so should anybody reading it. A grant
naming a superseded `policy_version` has no force and a NULL `revoked_at`:
nobody withdrew anything, the guild's policy moved on under them (§6).
Restoring the old `policy_version` would bring every one of those back.
Withdrawing through the console is what survives that.

The roster also shows each grant's **scope** (§3.2.1). Every row reads
`audio` until a guild turns `video_consent_offered` on; a setting an
administrator can switch on with no readout of who then used it is a
setting nobody can audit.

#### What a person may do to their own consent

`GET /api/me/consents` lists every guild the signed-in person holds a
consent record in — the state, the scope, the policy version their grant
names alongside the guild's current one, the instants, and whether that
guild offers video consent at all. There is no user in any of those paths:
the session decides whose records they are, so there is no parameter for
somebody else's.

They may do two things with them.

**Change the scope** (`PUT /api/me/consents/{guild_id}/scope`).

- **Narrowing** (`audio_video` → `audio`) takes effect immediately and
  needs nothing from the guild. Nobody needs permission to consent to
  less, and a guild that switched `video_consent_offered` back off must
  not trap the people who consented while it was on.
- **Widening** is a new grant. It **inserts a new `consent` row** carrying
  the guild's current `policy_version`, because the table is an
  append-only history and a widened scope sitting under a superseded
  policy is exactly the record that history exists to prevent. It is
  refused with `video_consent_not_offered` while the guild does not offer
  it — refused, not silently narrowed: a success answering a question the
  person did not ask has told them something false about their own
  consent.

**Withdraw entirely** (`POST /api/me/consents/{guild_id}/revoke`),
effective now. There is no `effective_at` here on purpose: back-dating is
an administrator's correction of a record and scheduling is a guild's
arrangement, while a date field offered to the person themselves would
invite somebody to withdraw retroactively believing it erases something.
It does not.

**It cannot remove the Discord role**, for the same reason the
administrator's path cannot: `api` holds no Discord token (§6.2.2). The
answer carries `role_stays: true` so the console says so next to the
button rather than leaving the person to discover it from `/consent
status`. If the role should go too, run `/consent revoke` in Discord,
which does both.

### 6.2.7 The queue overview

**Admin View → Queue**, per guild: the same figures `/queue status` prints
in Discord, plus the sessions they are made of. Both read `load_status`,
so the two never disagree.

A session is listed while it is **unfinished**, which is two conditions
rather than one:

- its status is not `documented` — it is recording now, waiting for a
  worker, being transcribed, or stuck; **or**
- it has a `dead` job. A session reaches `documented` once every job is
  terminal, and `dead` is terminal, so a speaker whose transcription
  failed permanently would otherwise disappear from this view at exactly
  the moment somebody needs to notice them.

A session with status `open` and no jobs at all is a recording happening
right now. It is listed on purpose.

Three numbers need reading with their caveats, and the page prints them:

- **`running` past its lease** — a job whose worker died holding it. No
  amount of waiting fixes one; it needs `/queue requeue` or the re-queue
  control on the recording page. The count is computed against an
  *assumed* lease, because `api` cannot see the worker's
  `job_lease_seconds`; the lease it used is shown beside the number. If
  the worker's setting differs, the count is wrong in the direction the
  difference points.
- **Closed and undocumented** — nothing is queued for these and nothing
  will happen on its own. The worker's `retry_pending_documents` sweep is
  what normally clears them; a count that stays put across several
  refreshes means the document write is failing, and §5 is where to look.
- **Oldest pending** — dated by the *session's end*, not by the job.
  `transcription_job` has no enqueue timestamp at all. A session's end is
  within seconds of when its jobs were created, which answers "has this
  been sitting here for hours?" and nothing more precise. A re-queued job
  keeps its session's original end, so a redo makes this read older than
  the job actually is.

The list is cut at twenty sessions, newest first, and says so when it was
cut. The totals above it are the guild's and are never cut.

There is no re-queue control on this page. Each row links to
`/recordings/{id}`, which carries the per-session panel — and the decision
of whether a re-queue is safe stays in one place (`plan_requeue`) rather
than being made twice.

### 6.2.8 The report

**Admin View → Reporting**, per guild: how often this guild meets, how
long its meetings run, how many of them produced a protocol, how big they
get, and the same broken down by month.

It answers the question an administrator configuring Sturnus otherwise
cannot: is this working out. Every figure comes from rows the system
already writes.

Two properties are worth knowing before reading a number off it.

**Months are cut in the guild's `timezone`**, the same calendar the
protocols are written in (§Spec 11), and the payload names which zone was
used. A meeting that opened at 00:30 Berlin time belongs to the month the
people in it think it does; bucketing by UTC would file it under the
previous one and disagree with the timestamps printed in the protocol of
that very meeting. An unusable `timezone` value falls back to UTC — the
same fallback the worker applies — and the named zone in the report is how
you find out that happened. Note this is a *different* choice from the
per-person calendar view (§6.2.5), which groups by UTC day because one
person's sessions can span guilds and no guild's zone is right for them.

**"Unmeasured tracks" is not zero speech.** `speech_seconds` is nullable
and null means nobody ever measured, while zero means somebody did and it
was nothing. Recordings from before the measurement columns existed have
null, and `SUM` skips them silently. The count of skipped tracks is
therefore printed beside the total: a large one means the speech figure
describes only part of what was recorded, not that the guild was quiet.

**What the report is not.** It is about a guild and never about a named
person. It says how big meetings get and how many distinct people the
guild has recorded; it does not say who they were or rank them.

That boundary is a decision rather than an omission. A per-person readout
of meeting attendance and speaking time is a means of monitoring
performance and conduct — in Germany and the EU a matter for a works
council (BetrVG §87(1)(6)) rather than something a console adds because
the columns happen to be there. The rows exist and the ranking is
buildable; building it is a separate, deliberate act.

### 6.3 Listening to a recording by hand

Every automated check this system has can describe a track — its level,
its spectrum, its autocorrelation, whether a model produced words from it
— without answering the one question that decides what to do next: **is
there speech on it, and is it speech a person could understand?** Only
ears answer that. `scripts/audio_sample.py` is how a recording is put in
front of any.

It exists because of one track that measured speech-like on every axis
(lag-1 autocorrelation 0.756, 26% of its energy in the 2–8 Hz syllable
band) and that neither `tiny` nor `large-v3` could transcribe a word of.
When there is nothing left to measure, somebody has to listen.

**What comes out is other people's voices.** It is personal data under
the consent those speakers gave for a transcript, and for nothing else.
Write it somewhere private, listen to what you need, delete it. Do not
forward it — not into a chat, not into a bug report, not to a colleague
who was not in the meeting. `--duration` defaults to 60 seconds rather
than the whole track for that reason.

From a workstation, both services have to be reachable and the
credentials have to be in the environment. The secret is read straight
into the shell rather than written anywhere:

```bash
kubectl port-forward -n cnpg-system svc/feather-core-cluster-pg-pooler-rw 15432:5432 &
kubectl port-forward -n rook-ceph-fr01 svc/rook-ceph-rgw-feather-s3 18081:80 &

sec() { kubectl get secret sturnus-secrets -n sturnus -o jsonpath="{.data.$1}" | base64 -d; }
export STURNUS_DATABASE_URL="$(sec STURNUS_DATABASE_URL | sed -E 's#@[^/]+/#@127.0.0.1:15432/#')"
export STURNUS_S3_ENDPOINT="http://127.0.0.1:18081"
export STURNUS_S3_ACCESS_KEY="$(sec STURNUS_S3_ACCESS_KEY)"
export STURNUS_S3_SECRET_KEY="$(sec STURNUS_S3_SECRET_KEY)"
export STURNUS_S3_BUCKET="sturnus-audio"
export STURNUS_MASTER_KEY="$(sec STURNUS_MASTER_KEY)"

uv run python scripts/audio_sample.py list
uv run python scripts/audio_sample.py extract 4 --start 4:00 --out ~/sample.wav
```

`list` shows every job and whether its audio still exists — a job the
retention sweep has already cleaned up cannot be sampled, and saying so
up front beats a 404 from S3 later. `extract` writes a WAV any player
opens.

Two things the output reports are worth reading before the audio itself:

- **The track's real length, and its sample rate.** The length is the
  *file's*, which is not the meeting's: the writer only extends a
  speaker's track while packets arrive from them, so a speaker who said
  little has a short track and that is not a fault.

  The rate is printed because it is read from the file rather than
  assumed. An earlier draft of this script assumed 48 kHz stereo against
  a track that is 16 kHz mono and reported every length at a sixth of the
  truth — which is where the "52-minute session, 8:41 track" reading came
  from. That track was 52 minutes long; the reader was wrong. If a length
  here looks impossible, check the rate on the same line before
  concluding anything about the recording.
- **The peak.** A peak of exactly 0 means the slice is digital silence
  rather than quiet speech, which is a different fault with a different
  cause. A peak at 32767/-32768 means the track clips — samples pinned to
  the rail are not recoverable signal, and a decoder reads them as noise.
  A handful is normal; a sustained fraction is not.

### 6.4 Measuring a live capture

`audio_sample.py` above answers "what is on this track". When the answer is
"noise", the next question is where the noise came from — and a finished
WAV cannot say, because both candidates (the frames Discord sends, and what
the Opus decoder makes of them) live upstream of it.

`STURNUS_CAPTURE_DIAGNOSTICS=true` on the **bot** turns on a measurement of
exactly that. It is off by default and meant to be turned on for one
recording, then off again.

```bash
kubectl set env -n sturnus deploy/sturnus-bot STURNUS_CAPTURE_DIAGNOSTICS=true
# ... have somebody speak in the recorded channel for a minute ...
kubectl logs -n sturnus deploy/sturnus-bot | grep "capture diagnostics"
kubectl set env -n sturnus deploy/sturnus-bot STURNUS_CAPTURE_DIAGNOSTICS-
```

Each line covers one speaker and carries two halves.

**What arrived.** Packet count, the shape libopus reads out of each packet's
TOC byte, and the size distribution. A healthy stream is `1f/960spf/2ch` and
nothing else — one 20 ms stereo frame per packet, which is what Discord
sends. Any other shape, or a non-zero `unreadable`, means the bytes handed
to the decoder are not the packet that was sent: a header not stripped, a
payload cut short, an offset out by a few. Sizes should spread across
several bands; voice is variable-bitrate, so a stream where every packet
lands in one band is not voice.

**What came out of the decoder**, measured before anything else touches it.
`autocorr` above 0.4 with `step/rms` below 0.3 is speech. The four degraded
tracks that prompted this read **autocorr 0.21–0.26 and step/rms about
0.44**, so a live capture reading the same confirms the damage is already
present at the decoder's output — and a live capture reading clean means it
is done later, by `to_mono_16k` or the writer.

**It records no audio.** Counts, size bands and three aggregate numbers over
sampled frames; no frame and no sample is kept, which is what makes it safe
to run against a real conversation — the only kind that reproduces the
defect. One frame in fifty is measured, so the cost on the capture thread is
a dictionary bump per packet.

### 6.5 Does Discord send this bot video at all?

The same switch answers a second question, and one that decides whether
recording a shared screen is possible at all.

**What the first measurement found.** With a screen share running, the bot
received **no `VIDEO` gateway event and no video packets** -- not a stream
that failed to arrive, but a share Discord never mentioned. The listener
was registered, the session closed cleanly. That is upstream of every
decoding question.

**Why.** A `discord.py` voice connection never asks for video, in three
separate places, none of them documented by Discord:

| Missing | Where | Consequence |
|---|---|---|
| `video: true` in `IDENTIFY` | `DiscordVoiceWebSocket.identify` omits the field, which defaults to false | The connection declares itself video-incapable, so the server has no reason to describe video to it |
| op 12 (`VIDEO`) outbound | `DiscordVoiceWebSocket.client_connect` builds this payload and is **called from nowhere** | *"You must send at least one Video payload before sending or receiving video data, or you will be disconnected with an Invalid SSRC error"* |
| op 15 (`MEDIA_SINK_WANTS`) | `voice_recv/gateway.py` labels it `(useless)` and never sends it | This is how a receiver names the SSRCs it wants and at what layer; the SFU forwards nothing it was not asked for |

`STURNUS_CAPTURE_DIAGNOSTICS=true` now sends all three
(`sturnus/infrastructure/discord/video_subscription.py`) and reports the
result **once a minute**, not once per capture -- the previous version only
spoke from `cleanup()`, so whoever started a share to test it had to end
the call to learn anything:

```
video probe: asked for video with [op12-video=sent, op15-any=sent] |
  2 stream(s) announced, 0 delivered any packet |
  ssrc=5001/screen@1920x1080 user=... packets=0, ssrc=5002/video@1280x720 ... |
  packets on unannounced ssrcs: 0 || <what that means>
```

The line ends with the conclusion spelled out, because the three outcomes
lead to three different projects:

- **Packets arriving** -- the rest is worth building: depacketisation, DAVE
  decryption for `MediaType.video`, storage, and a **second consent role**,
  since consent to be recorded speaking is not consent to have a screen
  recorded.
- **Announced but nothing delivered** -- the subscription is wrong, not the
  decoding. Check which SSRCs op 15 named.
- **Nothing announced** -- Discord is refusing a bot outright, *or* the
  thing being tested was Go Live.

**The Go Live caveat, which decides how to read a negative result.** "Share
Your Screen" in a guild voice channel is Go Live, and Go Live is a
*separate* RTC connection: a client watches one by sending main-gateway op
20 `WATCH_STREAM` and opening a second voice websocket from the resulting
`STREAM_SERVER_UPDATE`. Op 20 is an undocumented user-client opcode with no
bot-API equivalent, and Sturnus does not attempt it. So **test with a
camera as well as a screen share** -- a camera that works while a share does
not is a completely different finding from neither working, and only the
camera test tells them apart.

**Also known:** Discord blocks bots from *sending* video (which is why
`Discord-video-stream` requires a user token). Nothing in the protocol
documentation conditions receiving on account type, and `self_video` is an
outbound flag, not a receive gate -- but no working implementation of a bot
receiving video was found either. This is an experiment, not a capability.

**The switch is why this is not on in production.** Declaring video support
changes the live voice handshake, and a handshake Discord rejects is a bot
that cannot join a channel at all. Without the switch the connection is
byte-for-byte the one that has been working.

It reads no payload byte and records no video -- SSRCs, counts and size
bands only.

## 7. Observability

Three retained stores hold a copy of what Sturnus emits, and all three are
reachable by a wider audience than `kubectl`:

| Store | What reaches it | How |
|---|---|---|
| **Loki** | pod stdout, one JSON object per line | `alloy-logs` runs as a DaemonSet and scrapes container stdout. Sturnus ships no logs itself. |
| **Tempo** | spans | pushed over OTLP/HTTP to `alloy-receiver.grafana.svc:4318`, which fans out. |
| **Sentry** | errors | `sturnus/infrastructure/observability.py`. |

Because Alloy does the shipping, "optimising for Loki" means changing what
Sturnus *prints*, not adding a log shipper.

### 7.1 One registry decides what may be emitted

`sturnus/observability/fields.py` holds a closed list of field names, and
`sturnus/observability/redaction.py` holds a single scrubbing function that
log lines, span attributes and metric labels all pass through. **Adding a
field is one edit, in one file, and it shows up in review as "we decided to
put this in Loki, Tempo and Grafana" — which is what it is.**

The list is an allowlist that gets *rebuilt*, not a denylist that gets
stripped, so an unregistered name is dropped rather than forwarded. The
failure mode is a missing panel in Grafana, never a transcript in Tempo.
Read that file before adding to it.

Three things are excluded and the reasons differ:

- **Transcript text, audio bytes, display names, tokens and keys** — the
  content Spec 15 and the blocking gate in
  `docs/verification/end-to-end-checklist.md` exist to protect. `bytes` are
  never rendered at all, whatever they are called, which closes raw PCM,
  Opus frames and wrapped keys as a class rather than by name.
- **The S3 object key** — its format is
  `sessions/{session_id}/speakers/{discord_user_id}.enc`, so it *embeds* a
  user id, and both halves are separately loggable. Logging the key is pure
  duplication with a wider blast radius; `audio_key()` reconstructs it.
- **`discord_user_id` and `external_user_id` in spans and metrics** — these
  *are* logged, because "did this person's `/audio delete` actually erase
  their recordings" is a compliance question that cannot be answered
  without them. They are kept out of Tempo because a user id joined to a
  session id and precise timestamps in a searchable, trace-indexed store is
  a record of who was in which voice channel when, which is a different
  artifact from a line in `kubectl logs`. Nothing is lost: `sturnus.session_id`
  on the span joins to the row that has the user id.

Exception *messages* never travel unless their class is `OSError` or
subclasses `DiagnosticSafeError` — one rule, shared with Sentry, in
`redaction.SAFE_MESSAGE_TYPES`, which
`sturnus.infrastructure.observability.SAFE_VALUE_TYPES` aliases rather than
restates. What you get instead is the exception type and a full traceback,
which locates the failure without carrying whatever the message happened to
interpolate.

Sentry is *narrower* than that shared rule rather than equal to it, and the
difference is deliberate: it rebuilds an `OSError` message from `errno`
instead of reading the exception's own string (section 5 gives the reason).
The shared list is the gate — a class it does not vouch for says nothing
anywhere — and each transport may then say less. Narrower is always
allowed; wider is what the alias makes impossible.

### 7.2 What is safe to raise, and what deliberately is not

**The short version, for 3am.**

| You want | Set | Effect |
|---|---|---|
| More detail from Sturnus | `STURNUS_LOG_LEVEL=DEBUG` | Safe. Do it. Restart the affected Deployment. |
| More detail from `discord.py`, `botocore`, `aiohttp`, SQLAlchemy | `STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` | **Does nothing.** The value is raised to `INFO`, and the process logs one `log.level_clamped` line saying so. |
| Less noise from everything | `STURNUS_LOG_THIRD_PARTY_LEVEL=ERROR` | Works. Quieter than the floor is always allowed. |
| Third-party `DEBUG` anyway | — | A code change to `THIRD_PARTY_FLOOR` in `sturnus/observability/setup.py`, on a non-production deployment. There is no environment variable for it, on purpose. |

`STURNUS_LOG_LEVEL` applies to `logging.getLogger("sturnus")` and nothing
else. Turning it up is safe because Sturnus's own DEBUG output goes through
the same closed field registry as its INFO output (section 7.1): it can
carry ids, counts, sizes and durations, and structurally cannot carry a
transcript or a key.

Everything that is not Sturnus is held down by two mechanisms:

1. **`THIRD_PARTY_FLOOR`** — the level below which no logger outside
   `sturnus.*` may go, including the root logger that every unnamed
   third-party logger inherits from. Today it is `INFO`.
2. **`NEVER_BELOW`** — named loggers clamped tighter still, listed below.

This is a security control, not tidiness. Verified against the installed
packages rather than assumed:

| Logger | At DEBUG it prints |
|---|---|
| `botocore.auth` | `CanonicalRequest`, `StringToSign`, and the **SigV4 signature** |
| `botocore.endpoint` | the prepared request, including the `Authorization` header |
| `discord.ext.voice_recv.reader` | the **Discord voice secret key** and raw voice payload bytes |
| `discord.ext.voice_recv.gateway` | the whole voice-gateway payload for every op except 3 and 6 — and op 4 is `SESSION_DESCRIPTION`, which is **where that same secret key comes from** |
| `discord.ext.voice_recv.voice_client` | the voice state update, which carries the voice `token` and `session_id` |
| `discord.http` | whole REST **response bodies** — message content, display names, nicknames |
| `sqlalchemy.engine` | bound parameters, i.e. transcript text on its way into the database |
| `aiohttp.access` | at **INFO**, `request.path_qs` — the path *with* its query string, and `link`'s only route is `/oauth/callback?code=…&state=…` |

**Why a floor and not just the list.** The list came first and was not
enough. `NEVER_BELOW` names 17 loggers; a running worker has around 90, and
a logger absent from the list carries no level of its own and inherits one
from the root logger. `configure_logging` used to set root to
`min(sturnus_level, third_party_level)`, so `STURNUS_LOG_LEVEL=DEBUG` — a
variable whose documented scope is Sturnus's own loggers — put the root
logger at DEBUG and turned on 24 third-party loggers with it. Rows three
to seven of that table were reachable that way. The floor is what makes
the claim structural: it applies to names nobody enumerated, including
names in libraries this repository has not imported yet, and
`tests/observability/test_third_party_log_floor.py` asserts it as a
property over every logger that exists rather than as another list.

**Why `INFO` and not `WARNING`.** `discord.voice_state` logs the voice
connect narrative at INFO — "Starting voice handshake… (connection attempt
2)", "Voice handshake complete", "Timed out connecting to voice",
"Disconnected from voice by discord, close code 4006". That is the evidence
base for telling apart the three ways capture fails, which is what
`voice.join_failed` / `voice.reader_stopped` / `voice.decode_failed` exist
to distinguish, and a WARNING floor would delete it. What `INFO` does cost
is real and worth naming: gateway IDENTIFY/RESUME and session-invalidation
tracing ("the bot silently stopped receiving events"), voice websocket
close codes below the INFO cases, and `discord.http` rate-limit bucket
diagnosis are no longer reachable without a code change. Those are exactly
the cases where someone wants DEBUG at 3am — and they are also the cases
where the same switch would publish a session key, which is why the switch
is a source edit on a non-production deployment rather than a Helm value.

**Second lock: the value is redacted as well as suppressed.** A level
control only helps for records that are not emitted. `redaction.PATTERNS`
carries a `secret_value` rule that scrubs anything assigned to a name in
`fields.CREDENTIAL_NAMES` — `secret_key: [...]`, `token=…`, `password="…"` —
in *any* record, including a message string a library composed. It exists
because the voice secret key has no recognisable shape: it reaches a log
record as thirty-two small integers, which no token pattern matches, inside
`record.msg`, where the field allowlist cannot see it. If a future release
of `discord-ext-voice-recv` moves that line to a level the floor permits,
the key is still replaced by `«redacted:secret_value»` and the rest of the
line survives.

**The list itself.** `discord.ext.voice_recv.router` is clamped to
`WARNING`, which deliberately still lets through its own
`log.exception("Error in %s loop")` at ERROR — the one library line that
matters when the packet-router thread dies.

`discord.voice_state` is a level decision rather than a list one, and it is
the only logger held **open** as well as shut. It is pinned at exactly
`INFO`, by two entries that check each other:

* `NEVER_BELOW` keeps its DEBUG lines out. They are connection-state
  transitions and DAVE upgrade notices, and they were read at the installed
  version and found to carry no secret — but DEBUG is where the leak lives
  on every other logger in that table, and a name absent from the list
  reads afterwards as "considered and cleared" when it was never considered
  at all.
* `NEVER_ABOVE` keeps its INFO lines in. Those are the connect narrative —
  "Starting voice handshake… (connection attempt 2)", "Voice handshake
  complete. Endpoint found: …", "Timed out connecting to voice",
  "Disconnected from voice by discord, close code 4014" — and they are the
  evidence base for the capture-failure cooldown and for telling
  `voice.join_failed` from `voice.reader_stopped` from
  `voice.decode_failed`. All three entrypoints emitted them before this
  package existed, because they called `basicConfig(level=INFO)`.

`NEVER_BELOW` alone could not have kept them: it is applied as
`max(third_party_level, floor)`, so every entry in it can only make a
logger *quieter*, and with `STURNUS_LOG_THIRD_PARTY_LEVEL` at its default
of `WARNING` an entry of `INFO` there is a no-op. `NEVER_ABOVE` installs
the level outright — which is also why it is not an exemption:
`STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` cannot make a pinned logger any
louder than its pin either.

The `aiohttp.access` row was a live leak before it was clamped: with the
root logger at `INFO`, every successful account link wrote an Outline
authorization code into Loki. Nothing is lost by silencing it — the ingress
already logs requests, and `link.callback_rejected` / `link.established`
carry the diagnostic content without the credential.

**How to check the floor is doing its job**, on a running deployment:

```logql
# Should return nothing, ever. If it returns rows, the floor is broken.
{app_kubernetes_io_name="sturnus"} | json | level="DEBUG" | logger !~ "sturnus.*"
```

### 7.3 Loki labels: what Alloy may promote

Sturnus cannot set Loki labels; `alloy-logs` derives them from Kubernetes
metadata. This is the policy for whoever edits the Alloy configuration.

**Already labels, free, no code change:** `namespace`, `pod`, `container`,
`app_kubernetes_io_name`, `app_kubernetes_io_component` (this is how
bot/worker/link separate).

**Promote from the JSON line — exactly two:** `level` (5 values) and
`guild_id` (a handful). `guild_id` is the one dimension an operator
genuinely slices by, and it is the first thing to review if Sturnus is ever
deployed multi-tenant at scale.

**Never a label:** `session_id`, `job_id`, `discord_user_id`, `ssrc`,
`document_id`, `external_user_id`, `trace_id`. Each is unbounded or
fast-growing, and promoting one multiplies the cluster's stream count
without limit. They are line content, queried with `| json | session_id="4711"`
— which is exactly what LogQL's parser stage is for.

`event` (~40 stable values) was considered and rejected: bounded, but
~40 × 3 components × 5 levels is a few hundred streams for a query that
`| json | event="job.dead"` answers just as well.

### 7.4 A LogQL cookbook

```logql
# The whole story of one session, across bot and worker in one stream
{app_kubernetes_io_name="sturnus"} | json | session_id="4711"

# A session ended having recorded nothing despite participants — the
# highest-value single query here
{app_kubernetes_io_name="sturnus"} | json | event="session.closed" | jobs_enqueued == 0

# We were in the channel and could not hear. Three different faults, one
# question: capture never started, capture died, or nothing decodes any
# more. Each ends the session with an end_reason that says so, rather than
# letting it time out looking like a meeting where nobody spoke.
{app_kubernetes_io_name="sturnus"} | json | event=~"voice.join_failed|voice.reader_stopped|voice.decode_failed"

# The same three, from the metric side, which is what to alert on
sum by (end_reason) (rate(sturnus_session_duration_count{end_reason=~"capture_failure|decode_failure"}[15m]))

# Permanent loss: audio that will never become a transcript
{level="error"} | json | event=~"job.dead|session.unrecoverable|session.document_rejected"

# Whisper throughput against real material (Spec 15's unmeasured risk)
{app_kubernetes_io_component="worker"} | json | event="job.transcribed" | unwrap realtime_factor

# Per-guild error rate — the one place the guild_id label earns itself
sum by (guild_id) (rate({level="error"}[5m]))
```

`kubectl logs` shows JSON, which is a genuine regression for a human under
pressure. Use:

```sh
kubectl logs deploy/sturnus-worker | jq -r '"\(.ts) \(.level) \(.event) \(.msg)"'
```

Every line carries `trace_id` when telemetry is enabled, so a Grafana
derived field turns a Loki row into a click through to the Tempo waterfall
for the same job.

### 7.5 Metrics are pushed, not scraped

`/metrics` answers **`501 Not Implemented`** and names
`STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT` in its body. It used to return `200`
with an empty exposition; that inverts the signal, because an empty `200`
is indistinguishable from "every counter is legitimately zero", so a
completely uninstrumented process would look healthy to a ServiceMonitor. A
`501` marks the target down, which is the true statement.

**Before merging, confirm nothing outside this repository scrapes it.**
Nothing in `charts/` does, but a cluster-side `ServiceMonitor` or a
`prometheus.io/scrape` annotation living in another repository would not
show up in that check.

| Instrument | Type | Unit | Attributes | Question it answers |
|---|---|---|---|---|
| `sturnus.job.stage.duration` | histogram | s | `stage`, `outcome` | Which pipeline stage is slow, across all jobs? |
| `sturnus.job.outcome` | counter | 1 | `outcome` | How many jobs died today? **The alerting signal** — unsampled, unlike a span. `outcome` is one of `done` / `failed` / `dead` / `crashed`, and it is recorded by the transition that decided it (`infrastructure/db/queue.py`), never inferred from the worker loop's return value — see the note below the table. |
| `sturnus.queue.depth` | gauge | 1 | `status` | Is the worker keeping up? One series per status: `pending`, `running`, `done`, `failed`, `dead`. **Sampled once per poll**, so during a long transcription it is as old as that job — see the caveat below. |
| `sturnus.transcription.audio_duration` | histogram | s | — | Paired with the `transcribe` stage histogram, gives the realtime factor. |
| `sturnus.transcription.decoded_seconds` | counter | s | `model` | **Seconds of audio handed to the decoder** — the gated speech, concatenated, not the padded recording it was cut from (§ the worker hands the model only what the speech gate found). Divided by wall time this is the real-time factor, which is the single most useful operational number here — see 7.5.1. |
| `sturnus.transcription.position_seconds` | gauge | s | `model` | How far into that concatenated speech the job in flight has got. Not a position in the recording: the times that reach the document are mapped back onto the recording's timeline afterwards, these are not. Absent while nothing is transcribing. |
| `sturnus.transcription.total_seconds` | gauge | s | `model` | How much speech that job has to get through; the denominator for `position_seconds`, on the same timeline. Absent while nothing is transcribing. |
| `sturnus.transcription.seconds_since_progress` | gauge | s | `model` | Seconds since the job in flight last produced a segment, or since it started. **The alert.** Absent while nothing is transcribing. |
| `sturnus.session.close.duration` | histogram | s | `end_reason`, `outcome` | **Will a deploy lose a session?** Compare p99 to `terminationGracePeriodSeconds`. |
| `sturnus.session.duration` | histogram | s | `end_reason`, `guild_id` | Are sessions ending by timeout, by people leaving, or because we could not hear? `end_reason` is one of `empty` / `idle_timeout` / `max_duration` / `shutdown` / `capture_failure` / `decode_failure` / `unknown`. The last three are the ones that cost a meeting; `unknown` means the close itself raised. |
| `sturnus.session.active` | up/down counter | 1 | `guild_id` | Is anything recording right now — and did a session leak? Incremented when the session *row* opens, decremented on every close path there is, so a capture failure cannot make it drift. |
| `sturnus.recording.upload.bytes` | histogram | By | — | Capacity planning against the retention window. |
| `sturnus.voice.packets` | counter | 1 | `outcome`, `guild_id` | **Why is person X missing from the transcript?** `outcome` is one of `recorded` / `no_role` / `no_consent` / `not_recording` / `unknown_user` / `video` / `undecryptable` / `undecodable` / `loop_gone`. `undecryptable` is Discord's end-to-end layer (DAVE) refusing a frame: a handful during a key rotation is ordinary, a sustained run means this session is not in the group and **no audio is being recorded at all**. `undecodable` is the early warning the decode-failure threshold deliberately does not give — that fires once, after five consecutive seconds of nothing, and this is visible from the first frame. |
| `sturnus.voice.packet_errors` | counter | 1 | `error_type` | Is the voice adapter throwing? The rate, not the log line: the matching `voice.packet_handler_failed` line is rate limited to one in a thousand because it is per-frame in origin. |
| `sturnus.document.create.duration` | histogram | s | `outcome` | Is Outline down, slow, or rejecting us? |
| `sturnus.oauth.callback` | counter | 1 | `outcome` | Are account links failing, and at which step? |

`sturnus.queue.depth` costs no extra database load: the worker's poll loop
already ran `SELECT 1` as a liveness probe, and that query is now a grouped
count over `transcription_job.status` — still one round trip that either
answers or does not, and it feeds the gauge as well. **The caveat that
comes with that:** it is sampled once per poll, and the worker does not
poll while it is transcribing. A ninety-minute job means a ninety-minute-old
depth reading, so alert on it over a long window (`for: 30m`) and use
`sturnus.transcription.seconds_since_progress` — which does not depend on
the loop coming back round — for anything sharper.

**`sturnus.job.outcome` says what happened, not what was returned.**
`process_one` returns `True` after `queue.fail(...)` exactly as it does
after `queue.complete(...)`: the boolean means "work was attempted", never
"work succeeded". The worker loop used to turn it into `outcome="done"`, so
every failed and every dead job was published as a success — a metric that
reports failures as successes is worse than no metric, because it will be
believed. The label is now recorded by `JobQueue.complete` and
`JobQueue.fail`, the two transitions that decide a job's terminal state,
and `crashed` is the one the loop still owns: `process_one` raised, so the
worker is about to die with a job possibly stuck in `running` (the lease in
`JobQueue.claim` is what reclaims it).

### 7.5.1 Transcription progress: telling fast-because-broken from slow-because-working

**Why these four exist.** Whisper's own failure mode is silence. When
Silero's VAD collapsed on the bit-exact padding Sturnus writes between
packets, the model was handed a 100-minute recording, returned no segments,
and the job "finished" in 43 seconds. What everybody saw was an empty
transcript — which is exactly what a participant who never spoke also
produces, and it was read as that for a day. What the metric would have
shown is a real-time factor of **140x**: a hundred minutes of audio decoded
in forty-three seconds, which is not physically possible. Meanwhile a
genuine job on that same recording ran for **98 minutes**. The honest range
is that wide, which is precisely why "it is taking a long time" is not a
diagnosis and this number is.

`large-v3` on the worker's CPU allocation measures **1.94x** — that is
`wall / audio`, so audio accrues at roughly half of wall-clock. Read
"audio" as *speech*: the decoder is handed the gated clips concatenated, so
a 100-minute recording holding 41 minutes of speech accrues 41 minutes
here, not 100. That makes the ratio a statement about decoding rather than
about how much of the meeting was silence, and it leaves the impossible-
throughput alert below firing on the same failure — the 43-second collapse
is a rate of ~57 against a ceiling of 10 either way:

```promql
# Real-time factor, the way the 1.94x figure is quoted (wall per second of audio)
1 / (sum by (model) (rate(sturnus_transcription_decoded_seconds_total[30m])))

# Alert: physically impossible throughput. Nothing is being decoded.
sum by (model) (rate(sturnus_transcription_decoded_seconds_total[15m])) > 10
```

Ten is a deliberately loose ceiling — five times faster than the fastest
plausible model on this hardware — because the failure it catches is three
orders of magnitude out, not a few percent.

**The stall alert, which is the one to page on.**

```promql
# A job that has produced nothing for ten minutes. Absent when idle, so this
# expression is simply empty on a worker with no work.
sturnus_transcription_seconds_since_progress > 600
```

This is the only signal that covers a job which wedges **before its first
segment** — inside feature extraction or language detection, which is where
the collapse happened. A position gauge cannot: a job stuck at zero looks
exactly like a job that has only just started, and every real job passes
through that state. The clock therefore starts when the model is called,
not when the first segment arrives.

`position_seconds / total_seconds` is the dashboard number ("this job is 12
minutes into 43"). Both are on the concatenated-speech timeline, which is
why the fraction means anything; it is not an alert on its own, since a
large `total_seconds` is a talkative meeting, not a fault.

**No id is a label on any of these, and that is not an oversight.** A
session id, job id, guild id or user id would be unbounded cardinality
*and* a record of who was in which voice channel when, kept for as long as
the metric store keeps anything. `model` plus the resource's `service.name`
are enough to read every number above. To go from a suspicious rate to the
job responsible, jump to the log: `transcription.decoded` and
`transcription.skipped` carry `speech_seconds`, `clips` and `segments`, and
`job.transcribed` next to them carries `job_id` and `session_id`.

**The log line that pairs with these.** `speech_seconds` against
`audio_seconds` on `transcription.decoded` is the speech gate's own
signature, and it is what would have named Silero as the culprit on the
first read: one second of speech in two minutes of recording is not a
plausible meeting.

```logql
# The gate found almost nothing in a long recording
{app_kubernetes_io_component="worker"} | json | event="transcription.decoded"
  | speech_seconds < 5 | audio_seconds > 120

# The model was never called at all — a genuinely silent participant, and
# the *other* explanation for an empty transcript
{app_kubernetes_io_component="worker"} | json | event="transcription.skipped"
```

Histogram buckets are set explicitly. The SDK's defaults top out at 10 000
in *milliseconds*; Sturnus's durations are seconds and run to an hour, so
the defaults would put every transcription in one bucket.

### 7.6 Traces

Root spans: `job.process` (worker, per poll), `session.open` and
`session.close` (bot), `document.create` (Outline).

**The packet path emits no spans, by construction.** `sink.py`'s `write()`
runs ~50×/s per speaker; ten speakers is 500/s, which at a measured 16.7 µs
per recorded span is 8.4 ms of CPU per second of wall clock *and* 43 million
spans a day from one bot. They would also all be orphaned roots, because the
extension's router thread inherits no context. Counters go there instead, at
a measured **2.2 µs** per `add` with a provider installed and **0.10 µs**
without — 1.1 ms/s at 500 packets/s, under 0.15% of one core. That is not a
consolation prize: a rate graph split by `outcome` answers "why is X missing
from the transcript" in one panel, which fifty spans a second would not.

`session.close` is the span worth watching. It encrypts, uploads and
enqueues **serially, per speaker**, and it runs during SIGTERM. If that
takes longer than `terminationGracePeriodSeconds`, Kubernetes kills the pod
mid-loop and Spec 15's "the entire session is lost, not just a portion" is
what happens. Nothing measured that before.

**Traces are not wired into Sentry.** `sentry_sdk` can consume OTel spans,
and `observability.py` locks that door twice on purpose
(`traces_sample_rate=0.0` *and* `before_send_transaction=drop_transaction`),
because `before_send` is never called for transactions and span data would
route around `scrub_event` entirely. The pod log line, carrying `trace_id`,
is the correlation point instead.

### 7.7 After a deploy: confirm telemetry actually arrives

If the endpoint is misconfigured, spans and metrics vanish and every
dashboard shows a flat, healthy-looking zero — which is easy to misread as
"nothing is wrong".

Export failures are still visible, but only in one place. The OTLP logger is
clamped to `ERROR`, so a lost batch is one line in Loki rather than the four
the exporter emits per batch (three retries plus the give-up), and
`ignore_logger("opentelemetry")` keeps the same failures out of Sentry
entirely, where an unreachable Alloy would otherwise be an issue per retry
per batch from all three pods, forever. So:

```logql
{app_kubernetes_io_name="sturnus"} | json | logger =~ "opentelemetry.*"
```

is the standing alert for "telemetry is being dropped". A *misconfigured*
endpoint that happens to resolve — pointing at the wrong service, say —
produces no error at all, and for that the smoke check below is the only
detector:

1. `kubectl logs deploy/sturnus-worker | jq 'select(.event=="telemetry.enabled")'` —
   confirms which endpoint was installed.
2. In Grafana, search Tempo for `service.name = sturnus-worker` and confirm
   one `job.process` trace has arrived.
3. Confirm `sturnus.queue.depth` has a recent sample.
