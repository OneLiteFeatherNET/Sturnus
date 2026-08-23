"""Traced decorators for the ports `application` already receives.

`process_one` takes every pipeline stage as an injected narrow `Protocol` --
`Queue`, `AudioDownloader`, `Decryptor`, `TranscriptionEngine`,
`DocumentSink` -- because `sturnus.application.worker` may not import
`sturnus.infrastructure`. The rule that forbids instrumenting it is exactly
what makes instrumenting it trivial: the decorators below satisfy the same
protocols, `sturnus.entrypoints.worker` wraps the concrete adapters on the
way in, and `application/worker.py` gains full stage-level tracing with
**zero** lines changed and no risk to `tests/test_architecture.py`. The
twelve existing tests in `tests/application/test_worker.py` keep passing
untouched. `sturnus.application.recording` gets the same treatment through
its injected `AudioStore`, `Encryptor` and `JobQueue`.

None of them *subclasses* the protocol it satisfies, deliberately: a
`Protocol` subclass inherits the `...` method bodies, so a forgotten
override would type-check and silently return `None`. Structural conformance
is checked where it matters instead -- at the composition root in
`sturnus.entrypoints.worker` and `.bot`, where mypy `strict` compares the
whole wrapper against the parameter's declared protocol and names any
method that is missing or has drifted.

Every wrapper **passes values through unmodified and only observes.** That
is a correctness constraint, not a style note: `Queue.claim` returns
`object | None` and `process_one` immediately `cast`s the result to
`_ClaimedJobShape`, so a wrapper that returned anything but the original
object would break the pipeline silently. mypy `strict` catches signature
drift; `tests/infrastructure/test_traced_ports.py` runs the real
`process_one` against wrapped fakes and catches the rest.

A gap stated rather than hidden: `assemble` and `render_transcript` are
plain function calls inside `application`, not injected collaborators, so
they get no span. They appear as unaccounted time inside `job.process`
between `job.complete` and `document.create`, which is legible in a
waterfall and is the honest representation. Wrapping the repository reads
inside `assemble` would add noise for a pure-CPU merge over rows that have
already been fetched.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from opentelemetry.trace import SpanKind

from sturnus.application.documents import CreatedDocument, DocumentSink
from sturnus.application.ports import AudioStore, Encryptor, SessionKey
from sturnus.application.recording import JobQueue
from sturnus.application.transcription import TranscriptionEngine, TranscriptionResult
from sturnus.application.worker import AudioDownloader, Decryptor, Queue
from sturnus.domain.measurements import JobMeasurements, RecordedAudio
from sturnus.infrastructure.telemetry import (
    DOCUMENT_CREATE_DURATION,
    JOB_STAGE_DURATION,
    RECORDING_UPLOAD_BYTES,
    TRANSCRIPTION_AUDIO_DURATION,
    record,
    set_current_span_fields,
    set_span_fields,
    span,
)


class _StageTimer:
    """Times one pipeline stage into `sturnus.job.stage.duration`.

    The span answers "why was *this* job slow" by sitting next to the same
    job's other stages in one waterfall; the histogram answers "is the fleet
    getting slower" and is what a dashboard and an alert read. Neither
    substitutes for the other, which is why both exist for every stage.
    """

    def __init__(self, stage: str) -> None:
        self._stage = stage
        self._started = 0.0

    def __enter__(self) -> _StageTimer:
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        record(
            JOB_STAGE_DURATION,
            time.monotonic() - self._started,
            stage=self._stage,
            outcome="failed" if exc_type is not None else "ok",
        )


class TracedQueue:
    """`sturnus.application.worker.Queue`, traced."""

    def __init__(self, inner: Queue) -> None:
        self._inner = inner

    async def claim(self) -> object | None:
        with span("job.claim"), _StageTimer("claim"):
            claimed = await self._inner.claim()
        if claimed is not None:
            # Stamps the ids onto the *enclosing* `job.process` span, which
            # was opened before a job had been claimed and therefore before
            # its id could be known. Order-dependent: if `process_one` is
            # ever moved outside that root span, these land on an invalid
            # span and are silently discarded. The matching comment is in
            # `sturnus.entrypoints.worker._run`.
            set_current_span_fields(
                job_id=getattr(claimed, "id", None),
                session_id=getattr(claimed, "session_id", None),
            )
        return claimed

    async def complete(
        self,
        job_id: int,
        transcript: str,
        measurements: JobMeasurements | None = None,
        *,
        lease: datetime | None = None,
        audio: RecordedAudio | None = None,
    ) -> bool:
        # `transcript` is passed straight through and never observed: it is
        # the protected content itself, and the only thing worth recording
        # about it -- its length -- is already on the `job.transcribe` span.
        #
        # `measurements` is durations and a count, so unlike the transcript
        # it is safe to put on a span -- and worth putting there, since a
        # trace of a job that decoded nothing is otherwise a trace with no
        # sign of what went wrong.
        #
        # `audio` is forwarded and not observed. It is the file's own
        # description rather than the decoder's, so it says nothing about
        # why a job was slow -- and `object_bytes` is already on
        # `job.download`, measured on the same object.
        with span("job.complete", job_id=job_id), _StageTimer("complete"):
            if measurements is not None:
                set_current_span_fields(
                    audio_seconds=round(measurements.audio_seconds, 3),
                    speech_seconds=round(measurements.speech_seconds, 3),
                    segment_count=measurements.segment_count,
                )
            is_last = await self._inner.complete(
                job_id, transcript, measurements, lease=lease, audio=audio
            )
        # `outcome` lands on the enclosing `job.process` span, the same
        # place and for the same reason as `claim`'s ids: the root span is
        # opened before anything is known and cannot label itself
        # afterwards. It is set from the transition that happened rather
        # than from `process_one`'s return value, which is `True` for a
        # failed job too -- see `sturnus.infrastructure.db.queue.complete`.
        set_current_span_fields(is_last=is_last, outcome="done")
        return is_last

    async def fail(
        self, job_id: int, error: str, max_attempts: int, *, lease: datetime | None = None
    ) -> bool:
        # `error` is `str(exc)` from `process_one`. It goes to the database
        # column an operator queries deliberately, and it does **not** go on
        # the span -- see `telemetry.fail_span`.
        with span("job.fail", job_id=job_id, max_attempts=max_attempts):
            dead = await self._inner.fail(job_id, error, max_attempts, lease=lease)
        # `dead` and `failed` are different operational stories -- one is a
        # recording that will never exist, the other is a retry -- so the
        # root span distinguishes them exactly as the counter does.
        set_current_span_fields(outcome="dead" if dead else "failed")
        return dead


class TracedAudioDownloader:
    """`sturnus.application.worker.AudioDownloader`, traced.

    Note the absent attribute: the S3 key never becomes one. Its format is
    `sessions/{session_id}/speakers/{discord_user_id}.enc`, so it embeds a
    user id, and both halves are already registered separately.
    `object_bytes` carries all of the diagnostic value with none of that.
    """

    def __init__(self, inner: AudioDownloader) -> None:
        self._inner = inner

    async def get(self, key: str, target: Path) -> None:
        with span("job.download", SpanKind.CLIENT) as active, _StageTimer("download"):
            await self._inner.get(key, target)
            size = target.stat().st_size if target.exists() else 0
            set_span_fields(active, object_bytes=size)


class TracedDecryptor:
    """`sturnus.application.worker.Decryptor`, traced.

    Synchronous, and called by `process_one` through `asyncio.to_thread`.
    That is safe for the span: `contextvars` are copied into the worker
    thread, so the span opened here parents correctly under `job.process`
    rather than becoming an orphaned root.
    """

    def __init__(self, inner: Decryptor) -> None:
        self._inner = inner

    def decrypt_to(self, source: Path, target: Path, wrapped: bytes, key_id: str) -> None:
        with span("job.decrypt", key_id=key_id) as active, _StageTimer("decrypt"):
            self._inner.decrypt_to(source, target, wrapped, key_id)
            if target.exists():
                set_span_fields(active, plaintext_bytes=target.stat().st_size)


class TracedTranscriptionEngine:
    """`sturnus.application.transcription.TranscriptionEngine`, traced.

    The span that is almost always the answer to "which stage is slow".

    `segment_count` and `char_count` are counts of the transcript, never the
    transcript, and they are the only way to distinguish "Whisper returned
    nothing" from "Whisper produced a repetition cascade" -- the two failure
    modes `infrastructure/whisper.py` sets `compression_ratio_threshold` and
    `no_speech_threshold` to guard against. A length is a weak side channel
    and that is accepted deliberately, with the precedent that
    `documents/outline.py` already logs `len(title)` and `len(body)` at
    DEBUG. Removing them is a one-line edit to `fields.ALLOWED_FIELDS`,
    which is the point of having a registry.
    """

    def __init__(self, inner: TranscriptionEngine) -> None:
        self._inner = inner

    async def transcribe(
        self,
        path: Path,
        language: str | None,
        initial_prompt: str | None,
        model: str | None = None,
    ) -> TranscriptionResult:
        """Forwards every argument, and puts `initial_prompt` on no span.

        The third parameter has no default on purpose, mirroring the port
        it wraps: a default here would silently drop every guild's
        configured vocabulary (Spec 11) the moment this wrapper is applied,
        while the untraced path kept it -- an observability decorator
        changing what is transcribed, which is exactly what a decorator
        must never do.

        It is also not a span attribute. `language` is a bounded literal
        and belongs on the span; `initial_prompt` is guild-configured free
        text and is therefore content, not metadata. It is not in
        `fields.ALLOWED_FIELDS` and must not be added to it.
        """
        with span("job.transcribe") as active, _StageTimer("transcribe"):
            result = await self._inner.transcribe(path, language, initial_prompt, model)
            audio_seconds = max((segment.end for segment in result.segments), default=0.0)
            set_span_fields(
                active,
                language=result.language,
                segment_count=len(result.segments),
                char_count=sum(len(segment.text) for segment in result.segments),
                audio_seconds=audio_seconds,
            )
            # Paired with the `transcribe` stage histogram, this is the
            # realtime factor Spec 15 flags as an unmeasured risk:
            # `rate(stage_sum{stage="transcribe"}) / rate(audio_sum)`,
            # measured continuously against real material instead of
            # estimated once.
            record(TRANSCRIPTION_AUDIO_DURATION, audio_seconds)
            return result


class TracedDocumentSink:
    """`sturnus.application.documents.DocumentSink`: the duration metric only.

    **Deliberately opens no span.** Unlike every other port here, the
    concrete adapter behind this one is in `infrastructure` already and
    carries its own `document.create` CLIENT span
    (`sturnus.infrastructure.documents.outline.OutlineSink.create`) with
    attributes this wrapper could not produce: the HTTP status, whether the
    rejection was permanent, the server address. Opening a second span of
    the same name here would put two identical, nested entries in every
    waterfall and tell the reader nothing.

    What is left is the histogram, which genuinely belongs here rather than
    in the adapter: it must cover *every* `DocumentSink`, so it keeps
    working if Outline is ever swapped for another provider.
    """

    def __init__(self, inner: DocumentSink) -> None:
        self._inner = inner

    async def create(self, title: str, body: str, target: str) -> CreatedDocument:
        started = time.monotonic()
        outcome = "failed"
        try:
            created = await self._inner.create(title, body, target)
            outcome = "ok"
            return created
        finally:
            # In a `finally` so a failed creation is timed too: "Outline is
            # slow" and "Outline is refusing us" are different incidents,
            # and the latency of the second is the evidence that tells them
            # apart.
            record(DOCUMENT_CREATE_DURATION, time.monotonic() - started, outcome=outcome)


class TracedAudioStore:
    """`sturnus.application.ports.AudioStore`, traced.

    `sturnus.recording.upload.bytes` is capacity planning against the
    retention window Spec 15 names as the top operational risk.
    """

    def __init__(self, inner: AudioStore) -> None:
        self._inner = inner

    async def put(self, key: str, source: Path) -> None:
        size = source.stat().st_size if source.exists() else 0
        with span("recording.upload", SpanKind.CLIENT, object_bytes=size):
            await self._inner.put(key, source)
        record(RECORDING_UPLOAD_BYTES, size)

    async def delete(self, key: str) -> None:
        with span("recording.delete", SpanKind.CLIENT):
            await self._inner.delete(key)


class TracedEncryptor:
    """`sturnus.application.ports.Encryptor`, traced.

    `new_session_key` is deliberately not traced: it is a pure in-memory key
    generation whose only interesting values are the key material itself.
    """

    def __init__(self, inner: Encryptor) -> None:
        self._inner = inner

    @property
    def key_id(self) -> str:
        return self._inner.key_id

    def new_session_key(self) -> SessionKey:
        return self._inner.new_session_key()

    def encrypt(self, source: Path, target: Path, key: bytes) -> None:
        with span("recording.encrypt", key_id=self._inner.key_id) as active:
            self._inner.encrypt(source, target, key)
            if target.exists():
                set_span_fields(active, object_bytes=target.stat().st_size)


class TracedJobQueue:
    """`sturnus.application.recording.JobQueue`, traced.

    The enqueue that closes the loop between the bot and the worker: a
    session that recorded audio but enqueued nothing is the failure this
    span, and `session.closed`'s `jobs_enqueued` field, exist to make
    visible.
    """

    def __init__(self, inner: JobQueue) -> None:
        self._inner = inner

    async def enqueue(
        self,
        *,
        session_id: int,
        discord_user_id: int,
        s3_key: str,
        encryption_key_id: str,
        wrapped_data_key: bytes,
        retention_until: datetime,
    ) -> int:
        with span("job.enqueue", session_id=session_id, key_id=encryption_key_id) as active:
            job_id = await self._inner.enqueue(
                session_id=session_id,
                discord_user_id=discord_user_id,
                s3_key=s3_key,
                encryption_key_id=encryption_key_id,
                wrapped_data_key=wrapped_data_key,
                retention_until=retention_until,
            )
            set_span_fields(active, job_id=job_id)
            return job_id
