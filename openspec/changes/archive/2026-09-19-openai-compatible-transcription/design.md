# Design

## Context

See proposal.md — Why. The technical facts that shape everything below:

- `TranscriptionEngine` in `sturnus.application.transcription` already isolates
  the decoder behind four arguments, and there is exactly one call site
  (`application/worker.py:835`). The port does not change by one character, so the
  whole change is one new adapter plus deletions.
- `sturnus.infrastructure.audio` writes 16 kHz mono 16-bit PCM and inserts real
  silence for every gap Discord sent no packets for. A 100-minute track is
  therefore about 192 MB against OpenAI's 25 MB request limit — chunking is a
  precondition for transcribing a long session at all, not an optimisation.
- `sturnus.infrastructure.whisper` attributes each decoded segment to a clip by
  `Segment.seek`, the encoder window it came out of. Its docstring records why
  reading the clip from the segment's reported times cannot be made safe: on a
  100-minute recording with 187 clips, 57 of 462 segments overran their window,
  5 of them by more than half their own length, landing 20 to 31 seconds from
  where they were spoken. **An HTTP response carries no `seek`**, so that restore
  cannot be expressed here — which decides the chunking strategy below.

The full source design, with the measurements behind every number, is
`docs/superpowers/specs/2026-09-17-sturnus-openai-transcription-design.md`.

## Goals / Non-Goals

**Goals:**

- One adapter behind the unchanged port, so `tests/application/` passes untouched
  and that fact is the evidence the port did not move.
- Segment times that remain true to when a word was spoken, across any number of
  windows.
- Hallucination defences that survive the move to HTTP, and degrade to fewer
  defences rather than to failed jobs when an implementation omits a field.

**Non-Goals (design-level, beyond the proposal's scope):**

- Preserving every decoding parameter `whisper.py` sets. The HTTP interface
  exposes `prompt` and `temperature` and nothing else; which defences survive and
  how is Decision 4, and a beam wider than the library default is simply gone.
- Any behaviour that depends on which OpenAI-compatible implementation is on the
  other end.

## Decisions

### 1. Cut in silence; never concatenate the speech

The cheaper shape is to drop the gaps and concatenate the clips, which is what
`WhisperEngine` does today. **Rejected**, and it is the load-bearing decision of
the change: without `seek`, a segment can only be attributed to a clip by its
reported times, and a decoded `end` routinely overruns its window — which then
adds the *next* clip's offset and moves the text across an entire removed silence.
The error is always exactly one gap wide, so on a session with three utterances
forty minutes apart it is forty minutes.

Cutting in silence makes attribution unnecessary instead of safe: if nothing
inside a window was removed, every segment maps by one constant addition, and an
overrunning `end` can at worst claim a few tenths of a second of the silence
beside it, which the clamp takes back.

### 2. Windows of at most twelve minutes, cut at the cap when no gap exists

Twelve minutes at 32 kB/s is about 23 MB, inside the 25 MB limit with room for the
multipart envelope, and the cap is configurable because 25 MB is OpenAI's number
rather than the protocol's.

When a window reaches the cap with no gap inside it, the cut happens anyway,
through whatever is being spoken. The order of the two constraints is deliberate:
a cut word costs one word's accuracy at one boundary, while an oversized window is
a rejected request and a failed job. Twelve minutes of continuous speech from one
participant, after the gate's hangover, is the rare case and is allowed to be the
worse one — and it is logged, so an unexplained broken sentence is explicable from
the worker's output.

### 3. The official `openai` SDK, not a hand-rolled request

`AsyncOpenAI` with `response_format="verbose_json"` and
`timestamp_granularities=["segment"]`. vLLM documents this client against its own
transcription endpoint, so "OpenAI-compatible" is tested by somebody other than
us. It brings a typed response (this repository runs `mypy --strict`; the
alternative is `dict[str, Any]` and hand-written narrowing), retries with backoff
honouring `Retry-After` below `max_job_attempts`, per-client and per-request
timeouts, and native `async` — so `transcribe` is genuinely awaited instead of
handed to `asyncio.to_thread` as the CPU-bound engine had to be. Added weight is
`distro`, `jiter` and `sniffio`; `httpx`, `pydantic`, `anyio` and `tqdm` are
already in `uv.lock`.

**Rejected:** `litellm` — an abstraction across providers buys nothing when the
target is OpenAI-compatible by definition. **Rejected:** a hand-rolled `httpx`
POST — it re-invents exactly what the SDK is being trusted for. **Rejected:**
`pydub` for the chunking — it needs ffmpeg in the image, it depends on the
`audioop` module removed in Python 3.13, and it decodes and re-encodes container
formats when the input is already raw PCM that a numpy slice and
`wave.writeframes` handle in three lines.

### 4. Two client-side vetoes, and `avg_logprob` is not one of them

`no_speech_threshold` and `compression_ratio_threshold` cannot be passed over
HTTP, but the responses carry the same three numbers those vetoes were computed
from. Two of them become rules applied to returned segments, as module-level
constants with their reasoning attached rather than as configuration:
`no_speech_prob > 0.6` and, independently, `compression_ratio > 2.4`. Both values
are carried over from `whisper.py:517,518` unchanged.

**`avg_logprob` must not be added as a third condition**, and this is the one
place where getting it wrong reintroduces a defect already paid for.
faster-whisper's silence skip is switched *off* when `avg_logprob` exceeds
`log_prob_threshold`, and an invented subtitle credit is a fluent,
high-probability sequence: its `avg_logprob` of about −0.88 sits above the −1.0
default. That is how `" Untertitelung des ZDF, 2020"` reached a production
protocol with `no_speech_threshold` already set. The same measurement notes that
lowering `no_speech_threshold` to 0.4 changed nothing, so that is not a lever
either.

`condition_on_previous_text=False` survives for free: each window is a separate
request carrying the guild's configured prompt and nothing else, no returned text
is ever fed back, so the cascade it guarded against is structurally impossible
rather than switched off.

### 5. Language pinned by the first window

`verbose_json` returns a language, and `whisper-1` returns it as an English name
while every language in this codebase is an ISO-639-1 code. The adapter normalises
names to codes and passes codes through unchanged. The first window's result is
supplied as the `language` argument to every later window of the same track — the
same pinning `set_detected_language` performs across a session's jobs, applied one
level down. An unrecognised spelling falls back to the configured default rather
than failing the job.

### 6. The model registry stays hardcoded — deferred by decision

The source design argues the registry should become configuration, since a model
name is now a string handed to somebody else's server rather than an instruction
to this worker to fetch and execute arbitrary weights. **That part is deliberately
cut from this change** (see proposal.md — Out of scope): it is the only piece that
reaches outside the worker, touching `console/routes_queue.py` and `queue_cog.py`,
and the source design itself marks it as a clean cut on which nothing else
depends. The list stays hardcoded with `whisper-1` as its only entry.

The cost, stated so nobody rediscovers it: a self-hoster whose vLLM serves a model
under a different name needs a fork until the follow-up change lands.

### 7. Tests run against a local HTTP server, not a mocked SDK

`tests/infrastructure/test_openai_transcription.py` starts a real HTTP server and
points `base_url` at it. Mocking `AsyncOpenAI` would test our mock's idea of the
protocol — precisely what the SDK was chosen to stop us inventing.
`tests/infrastructure/test_whisper.py` (1741 lines) is deleted rather than
adapted: about half of it pins decoding parameters that no longer exist and the
rest pins the `seek` restore that Decision 1 removes.

### 8. The stall alert's threshold moves with the window cap

`sturnus.transcription.seconds_since_progress` reports the seconds since the job
in flight last produced a segment, and `docs/operations.md` names it as the one
signal to page on. Today `whisper.py:645` advances it once per decoded segment, so
on a healthy job the clock resets every few seconds and the documented threshold

    sturnus_transcription_seconds_since_progress > 600

has roughly two orders of magnitude of headroom.

**After this change it does not.** Progress advances once per completed window,
and the window cap defaults to 720 seconds — longer than the 600-second threshold.
A perfectly healthy multi-window job would therefore page, reliably, on every
window. This is not a threshold somebody should discover in production and tune:
the two numbers are in the same change and contradict each other on arrival.

**Decided:** the threshold is raised in this change and documented as a function
of the window cap rather than as a bare number — it must exceed
`STURNUS_TRANSCRIPTION_MAX_CHUNK_SECONDS` plus the endpoint's own processing time
and the SDK's retry budget (`STURNUS_TRANSCRIPTION_MAX_RETRIES` attempts with
backoff up to 8 s, bounded by `STURNUS_TRANSCRIPTION_TIMEOUT`). An operator who
raises the window cap must raise the threshold with it, and `docs/operations.md`
says so beside both variables.

**Rejected:** advancing progress mid-window from an upload or streaming-response
callback to keep the clock fine-grained. It would restore the resolution, but it
would report progress on the transfer rather than on transcription — an endpoint
that accepts the whole upload and then wedges is exactly the failure this
instrument exists for, and that shape would mask it. **Rejected:** leaving the
threshold alone and letting operators tune it, which trades one page-storm per
deployment for a number this change already knows.

**Rejected:** shrinking the default window cap below 600 s so the existing
threshold survives. The cap is set by the 25 MB request limit and Decision 2, not
by an alert, and letting a monitoring number dictate how audio is divided inverts
the dependency.

### Effect on the consent and telemetry-privacy constraints

Both of this project's standing constraints are touched, and neither is weakened
by accident:

- **Consent.** Plaintext audio now leaves the pod, so the README's "never leave
  the pod" promise is narrowed rather than deleted, and the `/consent` prompt and
  `/about` name the configured host. The endpoint address has no default, so there
  is no configuration under which audio flows anywhere because nobody chose.
- **Telemetry privacy.** The host appears in `Event.WORKER_STARTED` in place of
  `device` and `compute_type`; the API key appears nowhere — not in a log field,
  a span attribute or a metric label. `Event.TRANSCRIPTION_DECODED` gains the
  window count, since "one window" and "nine windows" are different jobs. Dropped
  segments are logged with their own figures, which is strictly better than
  faster-whisper discarding a window internally behind a DEBUG line on an
  unconfigured logger. Logging stays `%s`-style throughout, as `G`/`LOG` requires.

## Risks / Trade-offs

- **Transcription now depends on the network.** → A deployment whose endpoint is
  down has a queue that drains into `attempts` and then `dead`, where before it
  needed only its own disk. Accepted as the price of Decision in proposal.md; the
  retry budget and the dead state already exist to absorb it.
- **The stall clock coarsens.** A twelve-minute window is one request with no
  intermediate signal, so the interval between two progress advances is a whole
  round trip, and a real hang is indistinguishable from a slow window until a
  window's worth of time has passed. → Decision 8 raises the alert threshold in
  the same change; `docs/operations.md` states the weakened resolution beside the
  window-cap variable.
- **vLLM's `verbose_json` omits `no_speech_prob`.** → A missing field means the
  rule does not apply, never that the job fails; such a deployment keeps the
  speech gate and `temperature=0` only. `docs/operations.md` says plainly that an
  endpoint reporting `no_speech_prob` is the safer one to point at.
- **0.6 is not a margin.** On the measured set (`tiny`, German, 111 fragments)
  real 0.4 s fragments reached 0.804 while invented lines went as low as 0.639;
  the classes overlap and no threshold separates them. 0.6 rejects every invented
  line at the cost of four 0.4 s fragments of a real word. → Recorded at the
  constant. Whoever moves it should move it against a second speaker and a second
  fixture, and should expect a trade rather than a gap.
- **Egress cost is now a running cost.** → No compression before upload: FLAC or
  Opus would mean a codec dependency to solve a problem the twelve-minute window
  already solves. Revisit only if a deployment's bill says so.
- **`gpt-4o-transcribe` and `gpt-4o-mini-transcribe` cannot be used at all** —
  neither supports `verbose_json` or `timestamp_granularities`, and a protocol
  interleaving several speakers needs segment times. → `docs/operations.md` states
  this beside the model variable, because "use the newer model" is the obvious
  wrong move and the resulting API error will not explain itself.

## Migration Plan

Derived from §11 of the source design, with its step 2 removed by Decision 6.

1. Add the adapter and its tests against the port as it stands, while
   `WhisperEngine` still exists and still runs. Nothing is wired; the tree is
   deployable at every commit.
2. **Point of no return, and one commit.** `WorkerSettings` and the worker's
   wiring swap engines; `whisper.py`, `test_whisper.py` and `faster-whisper` are
   deleted. A tree with both engines and neither wired is not a state anybody
   should bisect through.
3. Chart, `Dockerfile`, `README.md`, `docs/operations.md`,
   `docs/first-deployment.md`, `/about` and `CONSENT_STRINGS`.

**Rollback.** Before step 2, revert is free. After it, rollback means reverting the
commit and restoring the model-cache volume — so the operator-facing prerequisite
(`STURNUS_TRANSCRIPTION_BASE_URL` reachable) must be verified before step 2 is
deployed, not after. Step 3's text changes are the only part that may lag the
deployment, and they must not: between step 2 shipping and step 3 shipping, the
README and the consent prompt assert something untrue.
