# Sturnus — transcription moves to an OpenAI-compatible endpoint

**Status:** design, not yet implemented
**Date:** 2026-09-17
**Amends:** [2026-08-19-sturnus-design.md](2026-08-19-sturnus-design.md) §3.2, §7, §11,
§12.1, §13.4, §13.5
**Replaces:** `sturnus.infrastructure.whisper` in its entirety

## 1. What this round is about

Sturnus transcribes in-process. `WhisperEngine` loads a CTranslate2 conversion
of Whisper into the worker's own memory, keeps it there, and decodes each
speaker's track on a thread. That is why the worker's container image carries
CTranslate2, why `charts/sturnus/templates/worker-pvc.yaml` exists at all, and
why a deployment cannot be stood up by somebody who has a Discord server and no
GPU-adjacent hardware.

This round removes the in-process model. Transcription becomes an HTTP call to
an OpenAI-compatible `/audio/transcriptions` endpoint — OpenAI's own, or a
self-hosted vLLM, or anything else that speaks the same shape. The worker stops
owning a model and starts owning a client.

**The scope is the engine and nothing else.** `TranscriptionEngine`, the port in
`sturnus.application.transcription`, does not change by one character.
`sturnus.application.worker` does not change. There is exactly one call site —
`worker.py:835`, `await engine.transcribe(wav_path, pinned_language, prompt,
job.requested_model)` — and it will not notice. Every consequence below that
reaches outside the new adapter is listed explicitly in §6, §7 and §8, and each
one is there because leaving it out would leave the repository asserting
something untrue.

## 2. What is decided here, and by whom

Three decisions were taken by the repository owner on 2026-09-17 after being
shown the consequence. They are recorded with that provenance because each one
reverses a position this repository currently argues for at length, and a reader
a year from now deserves to know the reversal was deliberate.

### 2.1 faster-whisper is removed, not made optional

The obvious shape is a backend chosen by environment variable, with
`WhisperEngine` kept for deployments that would rather compute locally. **That
was offered and declined.** `faster-whisper` leaves the dependency list, and
`sturnus.infrastructure.whisper` — roughly seven hundred lines, most of them the
`Segment.seek` restore described in §3.3 — is deleted.

The reasoning is that two backends are two live code paths, two sets of tests,
and two sets of failure modes for a project with one deployment's worth of
operational attention. A self-hoster who wants local computation runs a vLLM or
Speaches container and points `STURNUS_TRANSCRIPTION_BASE_URL` at it; that is a
second container, not a second code path, and the second container is somebody
else's maintained software.

What this gives up, stated plainly so nobody has to rediscover it: Sturnus can
no longer transcribe with no network reachable at all. A deployment whose
transcription endpoint is down has a queue that drains into `attempts` and then
into `dead`, where before it had a worker that only needed its own disk.

### 2.2 Audio leaves the pod, and consent has to say so

`README.md` currently promises:

> Recordings are encrypted before they ever leave the pod and are kept only for
> a configured retention window.

With a remote endpoint that is no longer the whole truth. A chunk of plaintext
16 kHz PCM is sent, over TLS, to whatever host the operator configured — and the
people in that audio consented under a policy that described none of it. The
consent machinery this project is built around (`/consent`, the `Speak` denial,
the packet-level role check) protects against being recorded by Sturnus; it says
nothing about where the recording then goes.

**Decided:** the promise is narrowed rather than deleted, and three mechanisms
carry the narrowing.

1. **`STURNUS_TRANSCRIPTION_BASE_URL` has no default.** A worker started without
   it fails to construct its settings and does not run. There is no value of the
   configuration under which audio flows to `api.openai.com` because nobody
   chose otherwise — the operator either named a host or has no working
   deployment.
2. **`/about` names the configured host.** It is already the AGPL §13 network-use
   offer, i.e. the one command whose purpose is telling a user what this
   deployment actually is. Where the audio goes belongs in that answer.
3. **The `/consent` prompt names it too**, in `CONSENT_STRINGS["description"]`
   (`sturnus/infrastructure/discord/views.py:21`), beside the existing
   `policy_url`. A person deciding whether to be recorded sees the host before
   they press the button, not in a document they will not open.

Only the host is shown, never the API key, and the key never reaches a log
field, a span attribute or a metric label.

### 2.3 The registry's list moves from code to configuration

`sturnus.domain.transcription_models` argues across forty lines that the list of
runnable models must be closed **and** hardcoded. The load-bearing half of that
argument is this:

> a free string is not "a model name", it is *an instruction to this worker to
> fetch and execute arbitrary weights from the internet*, issued through a
> re-queue button.

**That argument does not survive the change.** The worker no longer fetches or
executes anything. A model name is now a string handed to somebody else's
server, which serves the models it serves and refuses the rest; the worst a
wrong name achieves is a 400 and a failed job. The remaining reason for a closed
list is much weaker and purely local: the console's re-queue dropdown
(`routes_queue.py:799`) needs a finite set of things to render.

**Decided:** the list becomes configuration, the closedness stays. An unknown
name is still refused rather than substituted, for the reason the module already
gives and which is untouched by any of this — `transcription_job.model` records
what ran, and a silent fallback would make that column a lie.

`TranscriptionModel` also loses `approximate_size` and `summary`. Both were
honest when the worker downloaded the weights and could describe the trade; for
a model served by somebody else's endpoint nobody in this repository can write
either field truthfully, and a plausible-looking wrong answer in a dropdown is
worse than a bare name.

## 3. The adapter

One new module, `sturnus/infrastructure/openai_transcription.py`. Everything in
this section happens inside it.

### 3.1 The port does not change

```python
async def transcribe(
    self,
    path: Path,
    language: str | None,
    initial_prompt: str | None,
    model: str | None = None,
) -> TranscriptionResult: ...
```

`TranscriptionResult`, `TranscribedSegment` and `to_absolute` in
`sturnus.application.transcription` are untouched, as is the `JobMeasurements`
that rides on the result. The chunking in §3.2 is an implementation detail of
this class and is visible to nobody.

### 3.2 Chunking: cut in silence, never compress it

An OpenAI-compatible endpoint takes a file with a size limit — 25 MB on
OpenAI's. One speaker's track from a 100-minute session is about 192 MB, because
`sturnus.infrastructure.audio` writes 16 kHz mono 16-bit PCM and inserts real
silence for every gap Discord did not send packets for. Chunking is therefore
not an optimisation; without it a long session cannot be transcribed at all.

Four steps:

1. **Read.** The WAV is read with the standard library's `wave` and converted to
   mono float in `[-1.0, 1.0]`. `sturnus.application.worker` already reads these
   files exactly this way (`worker.py:427`); the only addition is the division
   by 32768 that `faster_whisper.audio.decode_audio` used to perform.
2. **Gate.** `speech_clips(audio)` from `sturnus.infrastructure.speech_gate`,
   unchanged. It stays because the problem it was written for is
   Sturnus's own: the bit-exact zero padding between packets is the single most
   reliable way to make Whisper invent text, and an HTTP endpoint exposes no
   decoding parameter that would defend against it. **No clips at all means the
   endpoint is not called** and an empty result is returned, for the reason
   `whisper.py` already gives — a silent participant must produce no segments,
   and a whole recording of padding pushed through a decoder is both slow and
   the worst case for hallucination.
3. **Plan the cuts.** Windows of at most **twelve minutes**, and every cut lands
   inside a gap the gate reported as silence. Silence *inside* a window stays
   where it is. A window containing no clip at all is dropped rather than sent.
   Twelve minutes at 32 kB/s is about 23 MB, inside the 25 MB limit with room for
   the multipart envelope.

   **When no gap exists inside the window, the cut happens at the cap anyway**,
   through the middle of whatever is being said. This is the one case where the
   rule above cannot be honoured, and the order of the two constraints is
   deliberate: a cut word costs one word's accuracy at one boundary, while a
   window over the limit is a rejected request and a failed job. The gate's
   hangover means a twelve-minute stretch with no gap at all requires twelve
   minutes of continuous speech from one participant, so this is the rare case
   and is allowed to be the worse one. It is logged, because a transcript with an
   unexplained broken sentence should be explicable from the worker's output.
4. **Send and reassemble.** One request per window. Each returned segment's
   `start` and `end` get the window's own start offset added, and are clamped to
   the window's extent. That is the entire restore: one window, one constant
   offset.

### 3.3 Why the cuts must land in silence

The temptation is to concatenate the speech and drop the gaps, which is what
`WhisperEngine` does today — it is cheaper and it is what the gate's output
invites. **It cannot be done here, and the reason is written down in
`whisper.py` before this design existed.**

`whisper.py` attributes a decoded segment to a clip by `Segment.seek`, the first
log-mel frame of the encoder window it came out of, and its docstring states
explicitly that reading the clip from the segment's reported times *cannot be
made safe*: a decoded `end` routinely overruns the window it came from, so
resolving the clip from that `end` adds the next clip's offset and moves the text
across an entire removed silence. The measurement is in that docstring — on a
100-minute recording with 187 clips, 57 of 462 segments overran, 5 of them by
more than half their own length, landing 20 to 31 seconds from where they were
spoken. The error is always exactly one gap wide, so on a session with three
utterances forty minutes apart it is forty minutes.

**An HTTP response contains no `seek`.** The only safe attribution is therefore
to make attribution unnecessary: if nothing inside a window was removed, every
segment in it maps by one addition, and an overrunning `end` can do no worse
than claim a few tenths of a second of the silence next to it — which the clamp
then takes back.

This is the load-bearing reason twelve-minute windows are cut at silence
boundaries rather than at twelve minutes exactly. A cut through the middle of a
word is a lesser problem than the one above but still a real one; a cut in
silence costs nothing.

### 3.4 The library: the official `openai` SDK

The call is made through `AsyncOpenAI`, not through a hand-rolled `httpx` POST.
vLLM documents this client against its own transcription endpoint, so
"OpenAI-compatible" is tested by somebody other than us.

```python
await client.audio.transcriptions.create(
    file=chunk_path,
    model=model_name,
    response_format="verbose_json",
    timestamp_granularities=["segment"],
    language=language,
    prompt=initial_prompt,
    temperature=0,
)
```

Four things it provides that would otherwise be written here:

- **A typed response.** `TranscriptionVerbose` with `.segments`, `.language` and
  `.duration`, and `TranscriptionSegment` with `.start`, `.end`, `.text`. This
  repository runs `mypy`; the alternative is `dict[str, Any]` and hand-written
  narrowing.
- **Retries with backoff.** Two attempts, 0.5 s rising to 8 s, honouring
  `Retry-After`. Without it a rate-limited request burns one of
  `max_job_attempts` and re-uploads twelve minutes of audio from the start.
- **Timeouts** per client and per request.
- **Native `async`**, so `transcribe` is genuinely awaited rather than handed to
  `asyncio.to_thread` as the CPU-bound engine had to be.

Its added weight is `distro`, `jiter` and `sniffio`; `httpx`, `pydantic`, `anyio`
and `tqdm` are already in `uv.lock`. Against the departure of `faster-whisper`
and CTranslate2 this is a rounding error.

**Rejected:** `litellm`, an abstraction across providers that buys nothing when
the target is OpenAI-compatible by definition. **Rejected:** `pydub` for the
chunking — it needs ffmpeg in the image, it depends on the `audioop` module that
Python 3.13 removed (this project runs 3.14), and it decodes and re-encodes
container formats when the input is already raw PCM that a numpy slice and
`wave.writeframes` handle in three lines.

### 3.5 Language

`verbose_json` returns a language, but `whisper-1` returns it as an English
name (`"german"`), while every language in this codebase is an ISO-639-1 code —
`transcription_language`, `DETECT_LANGUAGE`, `session_participant.detected_language`,
`transcription_default_language`. The adapter normalises names to codes and
passes codes through unchanged, so nothing downstream learns that two spellings
exist. An unrecognised spelling falls back to the configured default rather than
failing the job.

The multi-window case needs one rule beyond that: **the first window's language
governs the rest.** Its result is passed as the `language` argument to every
later window of the same track. This is the same pinning
`set_detected_language` performs across a session's jobs, applied one level down,
and for the same reason — one speaker's track must not change language halfway
through because a detector guessed differently on a quieter stretch.

### 3.6 Measurements and telemetry

`JobMeasurements(audio_seconds, speech_seconds, segment_count)` is produced
exactly as it is today: the first two come from the gate, which still runs, and
the third from the assembled segments. Nothing about
`transcription.decoded` changes shape.

`TRANSCRIPTION_PROGRESS` keeps its contract but coarsens.
`begin(model)` and `end()` are unchanged; `set_total` receives the summed
duration of the windows that will actually be sent, and `advance` is called once
per completed window rather than once per decoded segment.

**This makes the stall clock weaker and the spec says so rather than letting an
operator discover it.** A twelve-minute window is one HTTP request with no
intermediate signal, so the interval between two `advance` calls is now a whole
window's round trip. Whatever threshold reads the stall gauge has to tolerate
that, and a real hang is no longer distinguishable from a slow window until a
window's worth of time has passed.

`Event.WORKER_STARTED` loses `device` and `compute_type` — there is no local
device — and gains the endpoint's host. `Event.TRANSCRIPTION_DECODED` gains the
window count, because "one window" and "nine windows" are different jobs and
the realtime factor alone does not distinguish them.

## 4. What is lost, and what is recovered

`whisper.py` sets every decoding parameter explicitly, each against a named
failure, and its docstring carries the mechanism for each. **The HTTP interface
exposes almost none of them.** It takes `prompt` and `temperature`, and that is
the whole of it — no `log_prob_threshold`, no `condition_on_previous_text`, no
`compression_ratio_threshold`, no `no_speech_threshold`, no `beam_size`. What
follows is which of them still hold anyway, and how.

What still defends against invented speech:

| Defence | Status |
|---|---|
| The speech gate | **Kept.** Ours, not the library's, and it addresses the failure that actually reached production here — a subtitle credit invented on room tone. |
| All-silent windows never sent | **Kept**, and strengthened: it is now per window, not only per track. |
| `temperature=0` | **Kept**, expressible over the API. |
| `condition_on_previous_text=False` | **Kept structurally**, not as a parameter — see the last paragraph of this section. |
| `no_speech_threshold`, `compression_ratio_threshold` | **Moved**, from decode time to after the response. Below. |
| A beam wider than the library default | **Gone.** `whisper.py:597` raises it above 5 by spending CPU the worker has and latency nobody is waiting on; OpenAI's endpoint exposes no such knob, and the trade is no longer ours to make. |

**Moved, rather than lost.** `TranscriptionSegment` carries `avg_logprob`,
`compression_ratio` and `no_speech_prob` — the same three numbers the decode-time
vetoes were computed from. The vetoes themselves cannot be reinstated, but two of
the three rules can be applied to the returned segments instead, and that is
arguably the more honest place for them: the thresholds become visible data in
our own code rather than arguments to somebody else's decoder.

**Two independent rules, and `avg_logprob` is not one of them.** A segment is
dropped when `no_speech_prob > 0.6`, and separately when
`compression_ratio > 2.4`. Both numbers are carried over from
`whisper.py:517,518` unchanged, and both are measured on this project's own
material.

**`avg_logprob` must not be added as a condition, and this is the one place in
the change where getting it wrong reintroduces a defect this project has already
paid for.** `whisper.py:519-537` records the mechanism: faster-whisper's silence
skip is

    should_skip = result.no_speech_prob > no_speech_threshold
    if log_prob_threshold is not None and avg_logprob > log_prob_threshold:
        should_skip = False

An invented subtitle credit is a *fluent, high-probability* token sequence — that
is why the model reaches for one when there is nothing to transcribe — so its
`avg_logprob` of about −0.88 sits above the library's −1.0 default and switches
off the guard aimed at it. That is how `" Untertitelung des ZDF, 2020"` reached a
production protocol with `no_speech_threshold` already set, and it is why
`log_prob_threshold` is `None` in that file rather than a number. The same
measurement notes that lowering `no_speech_threshold` to 0.4 changed nothing
while the veto was live, so do not reach for that either.

0.6 is not a margin and the spec should not pretend otherwise. On the measured
set — `tiny`, German, 111 fragments — real 0.4 s fragments reached 0.804 while
invented lines went as low as 0.639: the two classes overlap and no threshold
separates them. 0.6 rejects every invented line at the cost of four 0.4 s
fragments of a real word. Whoever moves it should move it against a second
speaker and a second fixture, and should expect to trade rather than to find a
gap.

`compression_ratio_threshold` stays at 2.4 and is kept for what it does catch,
which is repetition — the same measurement is explicit that it does *not* catch
the credit (0.69, against 0.43 for real speech, so the invented line is less
repetitive than the thing worth keeping).

Both live as module-level constants with this reasoning attached, not as
configuration. One thing genuinely improves: `log_prob_threshold=None` makes
faster-whisper discard the window *inside the library*, leaving no trace but a
DEBUG line on a logger nothing here configures, and `whisper.py:666-675` says so.
A client-side drop is ours to log, with the segment's own numbers, at the
severity we choose.

**`condition_on_previous_text=False` survives the move for free**, which is
worth stating because it looks like another loss. It exists so one hallucinated
segment cannot become the context every later segment is decoded against. Each
window here is a separate request carrying the guild's configured
`initial_prompt` and nothing else — no returned text is ever fed back — so the
cascade it guards against is structurally impossible rather than switched off.

It must degrade rather than depend: **vLLM's `verbose_json` omits
`no_speech_prob`**, and other implementations will omit other fields. A missing
field means that rule does not apply, never that the segment is dropped and never
that the job fails. A deployment against such an endpoint therefore keeps only
the speech gate and `temperature=0` — which is the configuration that the credit
measurement above was taken *without*, so `docs/operations.md` says plainly that
an endpoint reporting `no_speech_prob` is the safer one to point at.

## 5. Configuration

| Variable | Default | Notes |
|---|---|---|
| `STURNUS_TRANSCRIPTION_BASE_URL` | **none** | Absent means the worker does not start. See §2.2. |
| `STURNUS_TRANSCRIPTION_API_KEY` | `""` | `SecretStr`. Empty is valid — a local vLLM needs no key. |
| `STURNUS_TRANSCRIPTION_MODEL` | `whisper-1` | What a job with no requested model runs. |
| `STURNUS_TRANSCRIPTION_MODELS` | `whisper-1` | Comma-separated. What the re-queue dropdown offers. See §6. |
| `STURNUS_TRANSCRIPTION_TIMEOUT` | `600` | Seconds, per request. |
| `STURNUS_TRANSCRIPTION_MAX_RETRIES` | `2` | The SDK's own retries, below `max_job_attempts`. |
| `STURNUS_TRANSCRIPTION_DEFAULT_LANGUAGE` | `de` | Renamed from `STURNUS_WHISPER_DEFAULT_LANGUAGE`; same meaning, same reasoning. |
| `STURNUS_TRANSCRIPTION_MAX_CHUNK_SECONDS` | `720` | The window cap of §3.2. Configurable because a 25 MB limit is OpenAI's, not the protocol's. |

Removed outright: `STURNUS_WHISPER_MODEL` and `STURNUS_MODEL_CACHE_DIR`.
`STURNUS_WHISPER_DEFAULT_LANGUAGE` is not removed but renamed, as the table
above records — same meaning, same `de`, same reasoning on the field. `_WHISPER_COMPUTE_TYPE` (`entrypoints/worker.py:174`)
and the `HF_HOME` assignment that precedes the engine's construction go with
them.

**Segment timestamps constrain the model choice, and the docs must say why.**
On OpenAI, only `whisper-1` supports `verbose_json` and
`timestamp_granularities`; `gpt-4o-transcribe` and `gpt-4o-mini-transcribe`
support neither. Sturnus cannot use them at all — the protocol interleaves
several speakers chronologically, so a transcript without segment times is not a
partial result, it is no result. `docs/operations.md` states this beside the
variable, because "use the newer model" is the obvious wrong move and the API
error it produces will not explain itself.

## 6. The registry, configurable — and the one place this reaches outside the worker

This is the only part of the change that touches processes other than the worker,
and it is listed separately so it can be cut without touching anything else.

`KNOWN_MODELS`, `FALLBACK`, `is_known`, `describe` and `resolve` are module-level
names over a module-level constant, consumed by three processes:

| Consumer | Uses |
|---|---|
| `console/routes_queue.py:574` | `resolve` on a re-queue request |
| `console/routes_queue.py:792,799` | `FALLBACK` and `KNOWN_MODELS` for the dropdown |
| `infrastructure/discord/queue_cog.py:562` | `FALLBACK` |
| `infrastructure/openai_transcription.py` | the second check, as `whisper.py` does today |

A list that comes from the environment cannot stay a module-level constant
without a mutable global. The shape instead: a frozen `ModelRegistry` value
object in the same domain module, carrying the tuple and the fallback, with the
three functions as methods and one `from_configuration(raw: str | None)` parser.
Each process builds one at startup and injects it — the console through the
dependency mechanism `routes_queue` already uses for its adapters, the bot into
`queue_cog`, the worker into the adapter. One parser, so the three processes
cannot disagree about the format; the concern the module's own docstring raises
about three processes sharing a format rather than a module.

**If this is more than the round should carry**, the fallback is to keep the
tuple hardcoded with `whisper-1` as its only entry and defer the configurable
list to its own change. That costs a self-hoster with a vLLM-served model a
fork, which is the whole reason §2.3 was decided the other way — but it is a
clean cut, and nothing else in this document depends on it.

## 7. Consent, `/about`, and the README

Three text changes, each one because the existing text would otherwise be false.

- **`README.md`.** The "never leave the pod" sentence becomes: recordings are
  encrypted at rest in the object store and for the retention window, and a
  chunk of plaintext audio is sent to the configured transcription endpoint at
  the moment of transcription. Self-hosting instructions gain the endpoint as a
  prerequisite alongside PostgreSQL and the object store.
- **`about_cog.about_text`.** Gains the configured host beside name, version,
  licence and source offer.
- **`CONSENT_STRINGS["description"]`** (`views.py:21`). Gains the host, beside
  the existing `policy_url`. `consent_cog.py:91` already formats that string
  with a value; this adds a second.

`docs/operations.md` and `docs/first-deployment.md` gain the endpoint as a
first-class prerequisite. `first-deployment.md` is ordered by what depends on
what, and the transcription endpoint now precedes the first recording.

## 8. What the deployment loses

All of it good news, and all of it a reason the change is worth making beyond
the self-hosting one.

- `charts/sturnus/templates/worker-pvc.yaml` is deleted, along with the
  `model-cache` volume and mount (`worker-deployment.yaml:92,102`) and
  `STURNUS_MODEL_CACHE_DIR` / `STURNUS_WHISPER_MODEL`
  (`values.yaml:551,558`).
- The worker's memory request and limit drop by roughly the size of the model it
  no longer holds — `large-v3` is about 3.1 GB as an `int8_float32` conversion.
- The image loses CTranslate2 and `faster-whisper`, and the worker's first start
  no longer includes a multi-gigabyte model download.
- A worker pod becomes stateless, which removes the reason its rollout strategy
  had to care about a volume.

## 9. Tests

`tests/infrastructure/test_whisper.py` is deleted rather than adapted. About
half of it pins decoding parameters that no longer exist and the rest pins the
`seek` restore that §3.3 removes; adapting it would produce a file that tests a
design nobody chose.

`tests/infrastructure/test_openai_transcription.py` replaces it, run against a
**local HTTP server** that the test starts and that `base_url` points at — not
against a mocked SDK. Mocking `AsyncOpenAI` would test our mock's idea of the
protocol, which is exactly the thing the SDK was chosen to stop us inventing.

What it pins:

- every cut lands inside a gap the gate reported, on a fixture whose speech and
  silence are known;
- offsets accumulate correctly across three or more windows, and a segment whose
  `end` overruns its window is clamped rather than moved into the next one;
- a track the gate finds nothing in makes no request at all, and emits
  `TRANSCRIPTION_SKIPPED`;
- an all-silent window in the middle of a track is skipped while the windows
  around it are still sent, and the offsets after it stay right;
- a track with no gap at all inside a window is cut at the cap, stays under the
  size limit, and logs the cut;
- the first window's language is passed to every later window;
- an English language name comes back as an ISO code, and an unrecognised one
  falls back to the default;
- a segment with `no_speech_prob > 0.6` is dropped **whatever its `avg_logprob`**
  — including the measured hallucination shape, `no_speech_prob` 0.7 with
  `avg_logprob` −0.88, which is the regression test for §4;
- a segment with `compression_ratio > 2.4` is dropped independently of the above;
- a response omitting `no_speech_prob` keeps its segments, and one omitting
  `compression_ratio` keeps them too;
- a model outside the registry is refused before any audio is uploaded;
- a timeout and an HTTP 500 both raise, so `worker.process_one` records
  `attempts` and retries;
- `JobMeasurements` matches what the gate saw, not what the endpoint returned.

`tests/domain/test_transcription_models.py` is rewritten around
`ModelRegistry.from_configuration`, keeping the existing refusal tests and adding
parsing ones. `tests/entrypoints/test_worker_entrypoint.py` loses the assertion
that pins `whisper_model` to `FALLBACK` and gains the equivalent for the new
default. Every test under `tests/application/` stays as it is — that the port did
not change is the claim, and those tests passing untouched are the evidence.

## 10. What is deliberately not built

- **No second backend.** §2.1.
- **No streaming.** The SDK supports it and `whisper-1` does not, and there is
  nothing to stream to: transcription runs after the meeting, into a document.
- **No word-level timestamps.** They cost latency and the protocol renders
  `%H:%M:%S` with a fifteen-second merge window.
- **No diarization.** Sturnus records one stream per speaker, so speaker
  identity is structural and already exact. A model's guess at it could only be
  worse.
- **No overlap between windows.** The usual advice is one or two seconds of
  overlap to preserve sentence context across a cut, and it is advice for cuts at
  arbitrary times. Cutting in silence is what that advice is trying to
  approximate, and overlap would duplicate segments that then need
  de-duplicating by text — a worse problem than the one it solves.
- **No compression before upload.** FLAC would halve the bytes and Opus would
  cut them by an order of magnitude, and both mean a codec dependency
  (`soundfile`, libsndfile) to solve a problem the twelve-minute window already
  solves. Worth revisiting only if a deployment's egress cost says so.
- **No `vllm_xargs`.** Some of the lost decoding parameters could be passed back
  through vLLM's extension field. That would make the adapter's behaviour depend
  on which implementation it is talking to, which is the opposite of what
  "OpenAI-compatible" is being relied on for.

## 11. Delivery

1. `sturnus/infrastructure/openai_transcription.py` with its tests, against the
   port as it stands, while `WhisperEngine` still exists and still runs.
2. The registry becomes `ModelRegistry`; the three consumers in §6 are injected.
   Cut this step and its §6 fallback applies.
3. `WorkerSettings` and the worker's wiring swap engines. `whisper.py`,
   `test_whisper.py` and `faster-whisper` are deleted.
4. Chart, `Dockerfile`, `README.md`, `docs/operations.md`,
   `docs/first-deployment.md`, `/about` and `CONSENT_STRINGS`.

Steps 1 and 2 are independent of each other. Step 3 is the point of no return
and should be one commit, because a tree with both engines and neither wired is
not a state anybody should bisect through.
