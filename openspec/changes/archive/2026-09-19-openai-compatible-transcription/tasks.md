# Tasks

Ordered so the tree is deployable at every commit until 3.1, which is the point of
no return (design.md — Migration Plan). Layers follow the boundary
`tests/test_architecture.py` enforces.

## 1. The adapter, beside the engine that still runs (infrastructure)

- [x] 1.0 (domain) Point the registry at the endpoint's models: `KNOWN_MODELS`
      becomes `whisper-1` alone, `FALLBACK` follows it, and `TranscriptionModel`
      loses `approximate_size` and `summary` — for a model served by somebody
      else's endpoint nobody in this repository can write either truthfully, and
      a plausible-looking wrong answer in a dropdown is worse than a bare name.
      The module docstring's load-bearing argument ("an instruction to this worker
      to fetch and execute arbitrary weights") no longer holds and is rewritten;
      closedness stays, for `transcription_job.model`'s sake. Costs two lines in
      `console/routes_queue.py:796-797`, which read the removed fields. Verify
      `uv run pytest tests/domain/test_transcription_models.py tests/console/test_queue_routes.py`
      passes (design.md — Decision 6, the §6 fallback)
- [x] 1.1 Add the `openai` dependency and remove nothing yet; verify `uv sync`
      resolves and `uv run python scripts/check_lock_version.py` passes
- [x] 1.2 (infrastructure) Create `openai_transcription.py` implementing
      `TranscriptionEngine` unchanged, reading the WAV with `wave` into mono float
      in [-1.0, 1.0]; verify a new test asserts the conversion matches what
      `application/worker.py:427` already reads plus the /32768 division
- [x] 1.3 (infrastructure) Plan windows: at most the configured cap, every cut
      inside a gap reported by `speech_gate.speech_clips`, silence inside a window
      preserved, a window with no clip dropped; verify tests on a fixture with
      known speech and silence assert every cut lands in a gap and an interior
      silent window is skipped while its neighbours are still sent
- [x] 1.4 (infrastructure) Cut at the cap when a window contains no gap, and log
      it; verify a test on a gapless fixture asserts the chunk stays under the
      size limit and the cut is logged
- [x] 1.5 (infrastructure) Send one request per window through `AsyncOpenAI` with
      `verbose_json` and `timestamp_granularities=["segment"]`, add each window's
      start offset to the returned times and clamp to the window; verify tests
      against the local HTTP server assert offsets accumulate across three or more
      windows and an overrunning `end` is clamped rather than moved into the next
      window
- [x] 1.6 (infrastructure) Drop segments with `no_speech_prob > 0.6` and,
      independently, `compression_ratio > 2.4`, as module-level constants carrying
      their reasoning; verify the regression test for the measured hallucination
      shape — `no_speech_prob` 0.7 with `avg_logprob` −0.88 — asserts the segment
      is dropped, and that no rule reads `avg_logprob`
- [x] 1.7 (infrastructure) Treat a missing confidence field as the rule not
      applying; verify tests assert a response omitting `no_speech_prob` and one
      omitting `compression_ratio` keep their segments and the job succeeds
- [x] 1.8 (infrastructure) Normalise language names to ISO-639-1, fall back to the
      configured default on an unrecognised spelling, and pass the first window's
      language to every later window; verify tests cover all three
- [x] 1.9 (infrastructure) Make no request when the gate finds no clip in a track,
      returning an empty result; verify a test asserts zero requests reach the
      local server and `TRANSCRIPTION_SKIPPED` is emitted
- [x] 1.10 (infrastructure) Refuse a model outside the registry before uploading
      anything; verify a test asserts the refusal happens with no request made
- [x] 1.11 (infrastructure) Produce `JobMeasurements` from the gate, not from the
      response, and advance `TRANSCRIPTION_PROGRESS` once per completed window;
      verify a test asserts the measurements match what the gate saw and progress
      advances per window
- [x] 1.12 (infrastructure) Let a timeout and an HTTP 500 raise; verify tests
      assert both propagate so `worker.process_one` records an attempt
- [x] 1.13 Verify `uv run pytest tests/test_architecture.py` still passes — the
      adapter must not have pulled anything into `domain` or `application`

## 2. Configuration (entrypoints)

- [x] 2.1 (entrypoints) Add `STURNUS_TRANSCRIPTION_BASE_URL` (no default),
      `_API_KEY` (`SecretStr`, empty valid), `_MODEL`, `_TIMEOUT`,
      `_MAX_RETRIES`, `_MAX_CHUNK_SECONDS` to `WorkerSettings`; verify a test
      asserts a worker without a base URL fails to construct its settings
- [x] 2.2 (entrypoints) Rename `STURNUS_WHISPER_DEFAULT_LANGUAGE` to
      `STURNUS_TRANSCRIPTION_DEFAULT_LANGUAGE`, same meaning and same `de`; verify
      the settings test covers the new name and no test still references the old

## 3. Point of no return — one commit (entrypoints + infrastructure)

- [x] 3.1 (entrypoints) Swap the engine at the single call site's construction,
      and in the same commit delete `infrastructure/whisper.py`,
      `tests/infrastructure/test_whisper.py`, `faster-whisper`,
      `_WHISPER_COMPUTE_TYPE` and the `HF_HOME` assignment; verify
      `uv run pytest` is green with no reference to `whisper` outside the
      adapter's own name
- [x] 3.2 (application) Verify every test under `tests/application/` passes
      **unchanged** — that the port did not move is the claim, and these tests
      passing untouched is the evidence
- [x] 3.3 (entrypoints) Replace the assertion in
      `tests/entrypoints/test_worker_entrypoint.py` that pins `whisper_model` to
      `FALLBACK` with its equivalent for the new default; verify that file passes

## 4. Deployment and the texts that would otherwise be false

- [x] 4.1 Delete `charts/sturnus/templates/worker-pvc.yaml`, the `model-cache`
      volume and mount, and `STURNUS_MODEL_CACHE_DIR` / `STURNUS_WHISPER_MODEL`
      from `values.yaml`; lower the worker's memory request and limit; verify
      `helm template` renders and the chart CI job passes
- [x] 4.2 Remove CTranslate2 and the model download from the `Dockerfile`; verify
      the image build job succeeds and the worker starts without a cache volume
- [x] 4.3 (infrastructure) Add the configured host to `about_cog.about_text` and
      to `CONSENT_STRINGS["description"]` beside `policy_url`; verify tests assert
      the host appears in both and that the API key appears in neither
- [x] 4.4 Raise the stall alert in `docs/operations.md` section 7.5 — the
      `sturnus_transcription_seconds_since_progress > 600` rule pages on every
      healthy multi-window job once the 720 s window cap applies — and document
      the threshold as a function of `STURNUS_TRANSCRIPTION_MAX_CHUNK_SECONDS`
      plus endpoint processing and retry budget, beside both variables; verify the
      markdown-lint job passes and that no number below the default window cap
      remains in that section (design.md — Decision 8)
- [x] 4.5 Narrow the "never leave the pod" sentence in `README.md`, and add the
      endpoint as a first-class prerequisite to `docs/operations.md` and
      `docs/first-deployment.md` — ahead of the first recording, with the
      `whisper-1`-only timestamp constraint and the coarser stall clock stated;
      verify the markdown-lint job passes

## 5. Whole-system verification

- [x] 5.1 Verify `uv run ruff check`, `uv run ruff format --check`,
      `uv run mypy` and `uv run pytest` all pass on the finished tree
- [x] 5.2 Verify no log line, span attribute or metric label carries the API key —
      run `uv run pytest tests/test_logging_discipline.py` and grep the adapter for
      non-`%s` logging
- [ ] 5.3 Walk `docs/verification/end-to-end-checklist.md` against a deployment
      pointed at a real endpoint: one short session and one over twelve minutes,
      confirming the long one produces continuous timestamps across window
      boundaries
