# Proposal

## Why

Sturnus transcribes in-process: `WhisperEngine` loads a CTranslate2 conversion of
Whisper into the worker's own memory and decodes each speaker's track on a thread.
That single decision is why the image carries CTranslate2, why
`charts/sturnus/templates/worker-pvc.yaml` exists, why the worker's first start
downloads several gigabytes of weights, and why somebody who has a Discord server
but no GPU-adjacent hardware cannot stand this project up at all.

The decision taken here is to remove the in-process model rather than make it
optional, and to call an OpenAI-compatible `/audio/transcriptions` endpoint
instead. The failure this prevents is the one a backend switch would have
created: two live code paths, two sets of tests and two sets of failure modes for
a project with one deployment's worth of operational attention. A self-hoster who
wants local computation runs a vLLM or Speaches container and points
`STURNUS_TRANSCRIPTION_BASE_URL` at it — a second container, not a second code
path, and one that somebody else maintains.

## What Changes

- A new adapter, `sturnus/infrastructure/openai_transcription.py`, implements the
  existing `TranscriptionEngine` port against an OpenAI-compatible endpoint using
  the official `openai` SDK's `AsyncOpenAI` client.
- **BREAKING for operators:** `sturnus.infrastructure.whisper` (729 lines) and
  `tests/infrastructure/test_whisper.py` (1741 lines) are deleted, and
  `faster-whisper` leaves the dependency list. There is no configuration under
  which the old engine still runs.
- **BREAKING for operators:** `STURNUS_TRANSCRIPTION_BASE_URL` is required and has
  no default, so a worker without it fails to construct its settings and does not
  start. `STURNUS_WHISPER_MODEL` and `STURNUS_MODEL_CACHE_DIR` are removed;
  `STURNUS_WHISPER_DEFAULT_LANGUAGE` is renamed to
  `STURNUS_TRANSCRIPTION_DEFAULT_LANGUAGE` with unchanged meaning and default.
- Audio is chunked into windows of at most twelve minutes because the endpoint has
  a request size limit — a 100-minute track is about 192 MB of 16 kHz PCM against
  OpenAI's 25 MB. Every cut lands inside a gap the speech gate reported as
  silence; silence inside a window is never compressed away.
- Two of the three decode-time hallucination vetoes move from decoder arguments to
  rules applied to the returned segments (`no_speech_prob > 0.6`,
  `compression_ratio > 2.4`), and degrade to no-ops when an implementation omits
  the field rather than failing the job. `avg_logprob` is deliberately **not** a
  condition.
- Because plaintext audio now leaves the pod, three texts that would otherwise be
  false are corrected: the "never leave the pod" sentence in `README.md`,
  `about_cog.about_text`, and `CONSENT_STRINGS["description"]`. The latter two name
  the configured host, never the API key.
- The chart loses `worker-pvc.yaml`, the `model-cache` volume and mount, and the
  memory headroom the model required; the worker pod becomes stateless.

## Capabilities

### New Capabilities

- `transcription`: how a recorded speaker track becomes timed segments — where the
  audio is sent, how a track too long for one request is divided, which returned
  segments are refused, how language is decided across windows, and what a person
  is told before consenting to be recorded.

This is the project's first spec. `openspec/specs/` is empty, so nothing is being
modified and no existing path is being reused. The disclosure requirements for
`/about` and `/consent` live in this capability rather than a separate `consent`
one: they exist only because transcription sends audio elsewhere, and splitting
them out would create a stub of a consent capability whose substance — the role,
the `Speak` denial, the packet-level check — is not written down anywhere yet.

### Modified Capabilities

None.

## Impact

**Added:** `sturnus/infrastructure/openai_transcription.py`,
`tests/infrastructure/test_openai_transcription.py` (run against a local HTTP
server the test starts, not a mocked SDK), the `openai` dependency.

**Deleted:** `sturnus/infrastructure/whisper.py`,
`tests/infrastructure/test_whisper.py`, `faster-whisper`,
`charts/sturnus/templates/worker-pvc.yaml`.

**Modified:** `WorkerSettings` and the worker's engine wiring
(`entrypoints/worker.py`, including `_WHISPER_COMPUTE_TYPE` and the `HF_HOME`
assignment), `infrastructure/discord/about_cog.py`,
`infrastructure/discord/views.py`, `charts/sturnus/values.yaml` and
`worker-deployment.yaml`, `Dockerfile`, `README.md`, `docs/operations.md`,
`docs/first-deployment.md`, `tests/entrypoints/test_worker_entrypoint.py`.

**Deliberately untouched:** the `TranscriptionEngine` port and
`TranscriptionResult` in `sturnus.application.transcription`,
`sturnus.application.worker`, and `sturnus.infrastructure.speech_gate`. Every test
under `tests/application/` must pass unchanged — that the port did not move is the
claim, and those tests passing is the evidence.

**Out of scope.** Making the model registry configurable
(`STURNUS_TRANSCRIPTION_MODELS`, a `ModelRegistry` injected into console, bot and
worker) is deferred to its own change; the list stays hardcoded with `whisper-1`
as its only entry. That keeps the *mechanism* inside the worker, but not quite
every line: `TranscriptionModel` loses `approximate_size` and `summary`, which
`console/routes_queue.py` reads into the re-queue dropdown's payload, so two
lines there go with them. `queue_cog.py` is untouched. Also not built: a second
backend, streaming, word-level timestamps, diarization, overlap between windows,
compression before upload, and vLLM's `vllm_xargs` extension field.
