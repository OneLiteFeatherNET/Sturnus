"""Traces and metrics over OTLP, built as a privacy control first.

A sibling of `sturnus.infrastructure.observability` rather than part of it:
that module is Sentry's privacy control and this is Tempo's, and keeping
them in separate files means two branches never edit the same control at
once. What they are *not* is two policies -- both defer to
`sturnus.observability` for the field registry and for the one question
"may this exception's message travel", so there is exactly one answer per
question across all three retained stores.

Spans go to Tempo, via `alloy-receiver.grafana.svc:4318`. They deliberately
do **not** go to Sentry. `sentry_sdk` can consume OTel spans, and
`observability.py` has locked that door twice on purpose
(`traces_sample_rate=0.0` *and* `before_send_transaction=drop_transaction`,
because `before_send` is never called for transactions and span data would
route around `scrub_event` entirely). Wiring the two together would open
exactly that path. The locks stay; the pod log line, carrying `trace_id`, is
the correlation point instead.

Three mechanisms keep content out of Tempo, each independent of the others.

**1. No auto-instrumentation packages, at all.** The same answer
`observability.py` gives with `auto_enabling_integrations=False`, to the
same threat, and every plausible instrumentor is disqualifying in this
codebase specifically:

- `opentelemetry-instrumentation-sqlalchemy` adds `db.statement`, and
  `queue.complete(job_id, transcript)` writes the serialised transcript
  through SQLAlchemy -- the statement and its parameters *are* the protected
  content.
- `opentelemetry-instrumentation-botocore` adds `aws.s3.key`, and the key
  format is `sessions/{id}/speakers/{discord_user_id}.enc`: a Discord user
  id on every download span, for free.
- `-httpx` / `-aiohttp-client` add `url.full`: presigned S3 URLs carrying
  `X-Amz-Signature`, exactly the `StdlibIntegration` risk their docstring
  names.
- `-aiohttp-server` would attach `link`'s only route,
  `/oauth/callback?code=...&state=...`, shipping an Outline authorization
  code to Tempo.

Only `opentelemetry-{api,sdk}`, `-semantic-conventions` and
`-exporter-otlp-proto-http` are installed, and none of them instruments
anything on its own.

**2. Both exception flags off, at one chokepoint.** Verified against
opentelemetry-sdk 1.44.0: `start_as_current_span` defaults to
`record_exception=True, set_status_on_exception=True`, and a
`RuntimeError("SECRET")` escaping such a span lands in *three* places --
`exception.message`, the full `exception.stacktrace`, and
`status.description`. Turning off only `record_exception` still leaves the
status description. `span()` below is the only way a span is opened here and
hard-codes both to `False`; `fail_span` records failure as a bare `ERROR`
status plus `error.type`, never a message.

**3. An allowlisting exporter.** `AllowlistingSpanExporter` rebuilds every
span from `fields.SAFE_SPAN_ATTRIBUTES` before OTLP sees it, drops all
events, and replaces the status with a bare code. This is the trace-side
analogue of `scrub_event`, with the same argued failure mode: an
unrecognised attribute is dropped, so a mistake costs a missing field in
Grafana rather than a transcript in Tempo. Mechanisms 2 and 3 are
independent on purpose -- the "stopped twice" reasoning `SAFE_FRAME_KEYS`
gives for `vars`.

`SpanLimits(max_attribute_length=256)` is a blunt third lock behind both: it
does not make a leak safe, but it caps one at 256 characters rather than a
whole conversation.

Verified against opentelemetry-sdk 1.44.0 and
opentelemetry-semantic-conventions 0.65b0.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from sturnus import __version__
from sturnus.config import OtelSettings
from sturnus.observability import events
from sturnus.observability.events import Event, log_event
from sturnus.observability.fields import (
    METRIC_LABEL_FIELDS,
    SAFE_SPAN_ATTRIBUTES,
    service_name,
    span_attribute,
)
from sturnus.observability.redaction import error_type, scrub_fields

log = logging.getLogger(__name__)

_INSTRUMENTATION_SCOPE: Final = "sturnus"

#: Buckets for every duration histogram, in **seconds**.
#:
#: Explicit rather than default, and this is not a nicety: the SDK's default
#: boundaries are `(0, 5, 10, 25, ... 10000)`, tuned for milliseconds. A
#: Whisper transcription of a 40-minute recording on CPU lands in the last
#: bucket of that set along with everything else over ten seconds, which is
#: to say the histogram would answer nothing at all.
_DURATION_BUCKETS: Final = (0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0)

#: Buckets for upload sizes, in bytes: 64 KiB to 1 GiB.
_BYTE_BUCKETS: Final = (
    65_536.0,
    262_144.0,
    1_048_576.0,
    4_194_304.0,
    16_777_216.0,
    67_108_864.0,
    268_435_456.0,
    1_073_741_824.0,
)

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


# ---------------------------------------------------------------------------
# The allowlisting exporter
# ---------------------------------------------------------------------------


class AllowlistingSpanExporter(SpanExporter):
    """Rebuilds every span from the allowlist before it leaves the process.

    Wraps the real OTLP exporter. For each span it keeps only attributes in
    `fields.SAFE_SPAN_ATTRIBUTES`, drops **all** events (the `exception`
    event is the leak `record_exception=True` produces, and no other event
    type is emitted here), and replaces the status with
    `Status(status_code)` -- discarding `description`, which is where
    `set_status_on_exception=True` puts `f"{type}: {exc}"`.

    Proven end to end in `tests/infrastructure/test_telemetry.py` rather
    than asserted: driving a span with deliberately leaky flags, a forbidden
    attribute and a transcript-shaped exception through this exporter yields
    a span carrying the registered attribute, a bare `ERROR` status, and no
    events.

    Read `fields.ALLOWED_FIELDS` before adding to the allowlist. Adding a
    key because a Grafana panel looks sparse is a decision about what leaves
    the cluster, not a formatting preference.
    """

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Any) -> SpanExportResult:
        return self._inner.export([self._rebuild(span) for span in spans])

    @staticmethod
    def _rebuild(span: ReadableSpan) -> ReadableSpan:
        attributes = {
            key: value
            for key, value in (span.attributes or {}).items()
            if key in SAFE_SPAN_ATTRIBUTES
        }
        status = (
            Status(span.status.status_code) if span.status is not None else Status(StatusCode.UNSET)
        )
        return ReadableSpan(
            name=span.name,
            context=span.get_span_context(),
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            events=(),
            links=span.links,
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


# ---------------------------------------------------------------------------
# Opening spans and recording failure
# ---------------------------------------------------------------------------


def _tracer() -> trace.Tracer:
    return trace.get_tracer(_INSTRUMENTATION_SCOPE)


def span_attributes(fields: Mapping[str, object]) -> dict[str, Any]:
    """Turns registered field names into span attribute keys.

    Goes through `redaction.scrub_fields` first -- the same call
    `events.log_event` makes -- so a span attribute and a log field are
    filtered by one implementation rather than two that agree today.
    """
    return {
        span_attribute(key): value
        for key, value in scrub_fields(fields).items()
        if span_attribute(key) in SAFE_SPAN_ATTRIBUTES
    }


@contextmanager
def span(name: str, kind: SpanKind = SpanKind.INTERNAL, **fields: object) -> Iterator[Span]:
    """Opens a span. The only way one is opened in this codebase.

    Hard-codes `record_exception=False, set_status_on_exception=False`, so
    the three leak paths verified in the module docstring are closed at
    every call site at once rather than at each one individually. A failing
    span is still marked: the exception is re-raised after `fail_span`
    records a bare `ERROR` status and the exception's class name.

    With no provider installed this is a `NonRecordingSpan` at roughly
    0.1 us, which is what makes "works with no collector" free rather than
    conditional.
    """
    with _tracer().start_as_current_span(
        name,
        kind=kind,
        attributes=span_attributes(fields),
        record_exception=False,
        set_status_on_exception=False,
    ) as active:
        try:
            yield active
        except BaseException as exc:
            fail_span(active, exc)
            raise


def fail_span(active: Span, exc: BaseException) -> None:
    """Marks a span failed without letting the message travel.

    `Status(StatusCode.ERROR)` with **no** description, plus
    `error.type = type(exc).__qualname__`. Never `str(exc)`: `worker.py`
    passes exactly that string to `queue.fail`, which is right for a
    database column an operator queries deliberately, and wrong for a store
    that indexes it and shows it to everyone with Grafana.
    `DiagnosticSafeError` remains the only marker that could ever change
    this, and it is `observability.py`'s contract, reused rather than
    reinvented.
    """
    active.set_status(Status(StatusCode.ERROR))
    active.set_attribute(span_attribute("error_type"), error_type(exc))


def set_span_fields(active: Span, **fields: object) -> None:
    """Sets registered fields on a specific span.

    The only way an attribute key is written in this codebase; nothing
    spells one as a string literal. That is what keeps every emitting call
    site and `fields.SAFE_SPAN_ATTRIBUTES` in step, and it is why a
    forbidden name is dropped here rather than merely at the exporter.
    """
    for key, value in span_attributes(fields).items():
        active.set_attribute(key, value)


def set_current_span_fields(**fields: object) -> None:
    """Stamps registered fields onto whatever span is currently active.

    How `job.claim` puts `session_id` on the root `job.process` span: the
    root is opened before a job has been claimed, so the id it most needs is
    not known yet. Order-dependent by nature -- if the enclosing span is
    ever closed before this runs, the attributes land on an invalid span and
    are silently discarded. Both ends carry a comment saying so.
    """
    set_span_fields(trace.get_current_span(), **fields)


def _metric_attributes(fields: Mapping[str, object]) -> dict[str, Any]:
    """Metric attributes: the registry, narrowed to what may be a label.

    Metrics multiply where logs and spans merely accumulate -- a new value
    of one attribute is a new time series forever -- so this is a strictly
    smaller set than `span_attributes` returns, holding only fixed source
    literals and `guild_id`. No user id can reach a metric attribute, which
    is the same decision that protects privacy paying twice: one rule, and
    the metric store cannot explode.
    """
    return {key: value for key, value in scrub_fields(fields).items() if key in METRIC_LABEL_FIELDS}


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------
#
# Created at import time off `metrics.get_meter`, which returns a proxy meter
# when no provider is installed; the proxy's instruments are real objects
# whose `add`/`record` are no-ops (measured: 0.10 us against 2.19 us with a
# provider), and they bind to the real provider the moment `init_telemetry`
# installs one. That is why nothing here is lazy or conditional.

_meter = metrics.get_meter(_INSTRUMENTATION_SCOPE)

JOB_STAGE_DURATION = _meter.create_histogram(
    "sturnus.job.stage.duration",
    unit="s",
    description="Wall time of one worker pipeline stage.",
)
JOB_OUTCOME = _meter.create_counter(
    "sturnus.job.outcome",
    unit="1",
    description="Transcription jobs by terminal outcome (done/failed/dead).",
)
QUEUE_DEPTH = _meter.create_gauge(
    "sturnus.queue.depth",
    unit="1",
    description="Transcription jobs per status, sampled once per worker poll.",
)
TRANSCRIPTION_AUDIO_DURATION = _meter.create_histogram(
    "sturnus.transcription.audio_duration",
    unit="s",
    description="Length of the audio handed to Whisper, for the realtime factor.",
)
SESSION_CLOSE_DURATION = _meter.create_histogram(
    "sturnus.session.close.duration",
    unit="s",
    description="Wall time of encrypt+upload+enqueue for a whole session.",
)
SESSION_DURATION = _meter.create_histogram(
    "sturnus.session.duration",
    unit="s",
    description="How long a recording session lasted, by end reason.",
)
SESSION_ACTIVE = _meter.create_up_down_counter(
    "sturnus.session.active",
    unit="1",
    description="Recording sessions currently open.",
)
RECORDING_UPLOAD_BYTES = _meter.create_histogram(
    "sturnus.recording.upload.bytes",
    unit="By",
    description="Size of one speaker's encrypted recording as uploaded.",
)
VOICE_PACKETS = _meter.create_counter(
    "sturnus.voice.packets",
    unit="1",
    description="Voice packets by what happened to them.",
)
VOICE_PACKET_ERRORS = _meter.create_counter(
    "sturnus.voice.packet_errors",
    unit="1",
    description="Voice packets whose handler raised.",
)
DOCUMENT_CREATE_DURATION = _meter.create_histogram(
    "sturnus.document.create.duration",
    unit="s",
    description="Wall time of one Outline document creation.",
)
OAUTH_CALLBACK = _meter.create_counter(
    "sturnus.oauth.callback",
    unit="1",
    description="Account-link OAuth callbacks by outcome.",
)


# ---------------------------------------------------------------------------
# Transcription progress
# ---------------------------------------------------------------------------


class TranscriptionProgress:
    """Where the transcription in flight has got to, and when it last moved.

    **The signal already existed and the code threw it away.**
    `WhisperModel.transcribe` returns `Tuple[Iterable[Segment],
    TranscriptionInfo]`; the segments are a *lazy generator* produced as
    decoding proceeds, each carrying `start` and `end`, and
    `TranscriptionInfo` carries the denominator. `WhisperEngine._transcribe`
    drained the generator inside a single tuple comprehension, so every
    intermediate observation was discarded and a job was observable only
    once it had already finished. A loop that reports as it goes costs
    nothing. (`log_progress=True` exists in the library and only drives a
    `tqdm` bar on stdout.)

    **Why this is worth the machinery.** A job that "finished" 100 minutes
    of audio in 43 seconds has a real-time factor of 140x -- physically
    impossible against the 1.94x `large-v3` manages on this hardware, and
    unmistakable. The symptom people actually saw was an empty transcript,
    which looks exactly like a participant who never spoke, and it was read
    as that for a day. Meanwhile a genuine job on the same recording has
    run for 98 minutes, so the honest range is very wide and this is the
    only instrument that can tell fast-because-broken from
    slow-because-working.

    **Observable instruments rather than a gauge somebody sets.** A
    synchronous gauge only changes when a call site sets it, so a decoder
    that wedges freezes the gauge at its last value and
    `seconds_since_progress` -- the actual alert condition -- could never
    grow. The SDK calls these callbacks once per export interval instead, so
    a stalled job's clock keeps running with no cooperation from the thread
    that is stuck. The same property is what lets the gauges emit *nothing*
    while the worker is idle: the series goes stale rather than reporting a
    finished job's numbers forever.

    **No id of any kind is a label**, and that is the same rule the rest of
    this module follows for the same two reasons: a session, job, guild or
    user id is unbounded cardinality, and a metric store keeps what it is
    given for a very long time. `model` and the resource's `service.name`
    are enough to read every number here.

    Process-global, because the worker transcribes exactly one job at a
    time (Spec 5.3) -- "the job in flight" is singular by construction. The
    lock is not decoration: `_transcribe` runs on an `asyncio.to_thread`
    worker thread and the metric reader calls these callbacks on its own.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        #: Cumulative decoded seconds per model, never reset -- this is what
        #: backs an observable *counter*, whose contract is a running total.
        self._decoded: dict[str, float] = {}
        self._model: str | None = None
        self._total = 0.0
        self._position = 0.0
        self._last_progress = 0.0

    def begin(self, model: str) -> None:
        """A job is now in flight. Called *before* the model call, not after it.

        The stall clock starts here rather than at the first segment
        deliberately: a job that wedges before producing anything -- during
        feature extraction or language detection, which is where the
        collapse this instrument exists for happened -- would otherwise be
        indistinguishable from a job that had only just started, forever.
        """
        with self._lock:
            self._model = model
            self._decoded.setdefault(model, 0.0)
            self._total = 0.0
            self._position = 0.0
            self._last_progress = self._now()

    def set_total(self, seconds: float) -> None:
        """The denominator: `TranscriptionInfo.duration_after_vad`.

        Known only once `transcribe()` has returned its info object, which
        is why it is separate from `begin`.

        This is the length of the array the model was handed, and since
        `WhisperEngine` hands over the gated speech concatenated rather than
        the padded track, it is the speech in the recording rather than the
        recording -- the more correct denominator for a real-time factor, and
        the one the positions reported to `advance` are on. Both numbers are
        on the *concatenated* timeline, which is why they are comparable;
        `whisper._on_the_original_timeline` puts the segments that reach the
        document back on the recording's, and those times are deliberately
        not what is reported here.
        """
        with self._lock:
            self._total = seconds

    def advance(self, position_seconds: float) -> None:
        """The decoder has reached `position_seconds` of the audio it was given.

        On the timeline of the array handed to the model -- the concatenated
        speech -- and not on the recording's, so that it and `set_total` are
        the same measure.

        Takes a position, not a delta, and only ever moves forward.
        faster-whisper's seek loop can emit a segment whose `end` is not
        past the previous one at a clip boundary, and a decrement here would
        be a counter reset -- which Prometheus turns into an enormous
        spurious rate rather than into a small correction.
        """
        with self._lock:
            if self._model is None:
                return
            gained = max(0.0, position_seconds - self._position)
            self._position += gained
            self._decoded[self._model] += gained
            self._last_progress = self._now()

    def end(self) -> None:
        """No job is in flight. Must run even when the decoder raised.

        Without it a failed job would look exactly like a wedged one:
        `seconds_since_progress` would climb past every threshold while the
        worker went cheerfully on to the next job.
        """
        with self._lock:
            self._model = None
            self._total = 0.0
            self._position = 0.0

    # -- the callbacks the SDK calls, once per export interval --

    def observe_decoded(self, options: CallbackOptions) -> Iterable[Observation]:
        """Cumulative decoded audio seconds. Reported whether idle or not.

        A counter's total does not disappear when the work stops, and the
        rate over a window is the whole point: divided by wall time this is
        the real-time factor.
        """
        del options
        with self._lock:
            totals = dict(self._decoded)
        return [
            Observation(seconds, _metric_attributes({"model": model}))
            for model, seconds in totals.items()
        ]

    def observe_position(self, options: CallbackOptions) -> Iterable[Observation]:
        """How far into the recording the job in flight has got, in seconds."""
        del options
        return self._in_flight(lambda: self._position)

    def observe_total(self, options: CallbackOptions) -> Iterable[Observation]:
        """How long the recording being decoded is, in seconds."""
        del options
        return self._in_flight(lambda: self._total)

    def observe_stall(self, options: CallbackOptions) -> Iterable[Observation]:
        """Seconds since the last segment -- or since the job began, if none.

        **The alert.** A position gauge cannot express it: a job stuck at
        zero looks like a job that has only just started, and every real
        job passes through that state.
        """
        del options
        return self._in_flight(lambda: self._now() - self._last_progress)

    def _in_flight(self, value: Callable[[], float]) -> list[Observation]:
        """One observation while a job is running, none at all otherwise.

        The empty list is load-bearing: it is what lets the series go stale
        on an idle worker instead of publishing the last job's numbers
        forever, and every alert expression in `docs/operations.md` section
        7.5 depends on it.
        """
        with self._lock:
            if self._model is None:
                return []
            return [Observation(value(), _metric_attributes({"model": self._model}))]


#: The one progress object. See `TranscriptionProgress` for why it is
#: process-global rather than passed around.
TRANSCRIPTION_PROGRESS: Final = TranscriptionProgress()

TRANSCRIPTION_DECODED_SECONDS = _meter.create_observable_counter(
    "sturnus.transcription.decoded_seconds",
    callbacks=[TRANSCRIPTION_PROGRESS.observe_decoded],
    unit="s",
    description="Seconds of recording handed to the decoder, cumulative. Divide by wall "
    "time for the real-time factor.",
)
TRANSCRIPTION_POSITION_SECONDS = _meter.create_observable_gauge(
    "sturnus.transcription.position_seconds",
    callbacks=[TRANSCRIPTION_PROGRESS.observe_position],
    unit="s",
    description="How far into the recording the transcription in flight has got.",
)
TRANSCRIPTION_TOTAL_SECONDS = _meter.create_observable_gauge(
    "sturnus.transcription.total_seconds",
    callbacks=[TRANSCRIPTION_PROGRESS.observe_total],
    unit="s",
    description="How long the recording being transcribed is; the denominator for "
    "position_seconds.",
)
TRANSCRIPTION_SECONDS_SINCE_PROGRESS = _meter.create_observable_gauge(
    "sturnus.transcription.seconds_since_progress",
    callbacks=[TRANSCRIPTION_PROGRESS.observe_stall],
    unit="s",
    description="Seconds since the transcription in flight last produced a segment, or "
    "since it started. Absent when nothing is transcribing.",
)


def record(instrument: Any, value: float, **fields: object) -> None:
    """Records one measurement with metric-safe attributes.

    One helper for counters, histograms, gauges and up/down counters alike
    so no call site picks the method name *and* builds the attribute dict
    itself -- `_metric_attributes` is not optional, and making it the only
    path is cheaper than remembering.
    """
    attributes = _metric_attributes(fields)
    if hasattr(instrument, "record"):
        instrument.record(value, attributes)
    elif hasattr(instrument, "set"):
        instrument.set(value, attributes)
    else:
        instrument.add(value, attributes)


# ---------------------------------------------------------------------------
# Wiring it up
# ---------------------------------------------------------------------------


def _views() -> list[View]:
    """One `View` per histogram, carrying explicit buckets. See `_DURATION_BUCKETS`."""
    seconds = [
        "sturnus.job.stage.duration",
        "sturnus.transcription.audio_duration",
        "sturnus.session.close.duration",
        "sturnus.session.duration",
        "sturnus.document.create.duration",
    ]
    views = [
        View(
            instrument_name=name,
            aggregation=ExplicitBucketHistogramAggregation(boundaries=_DURATION_BUCKETS),
        )
        for name in seconds
    ]
    views.append(
        View(
            instrument_name="sturnus.recording.upload.bytes",
            aggregation=ExplicitBucketHistogramAggregation(boundaries=_BYTE_BUCKETS),
        )
    )
    return views


def _trace_context() -> dict[str, str]:
    """`events.current_trace_context`'s implementation, injected not imported.

    `sturnus.observability` is standard-library only so that
    `sturnus.application` can import it; it therefore cannot read a span
    itself. `init_telemetry` hands it this function, which is what puts
    `trace_id` in every JSON log line and turns a Loki row into a click
    through to the Tempo waterfall for the same job.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


def init_telemetry(component: str, settings: OtelSettings | None = None) -> bool:
    """Installs traces and metrics for one process. Returns whether it did.

    Call immediately after `init_sentry(component)`, with the same literal
    component name, as the second statement of `main()`.

    **With no endpoint configured this returns `False` having installed
    nothing** -- no provider, no exporter, no background thread, no
    connection attempt, no error. Every `span()` in the codebase is then a
    `NonRecordingSpan` and every `record()` a no-op, so an operator running
    Sturnus outside this cluster needs no Alloy, no flag and no code path of
    their own. That is asserted directly in
    `tests/infrastructure/test_telemetry.py`.

    OTLP over **HTTP**, not gRPC. Alloy accepts both 4317 and 4318, so the
    choice is free, and the gRPC exporter drags in `grpcio` -- a large
    per-arch binary wheel and a known source of fork/thread hangs. Both
    exporters do their blocking I/O on their own daemon threads, so neither
    the asyncio loop nor the voice router thread is ever blocked by an
    export.

    Sampling is 100%. The arithmetic, rather than a reflexive ratio: the
    worker processes strictly one job at a time and each is minutes of CPU
    Whisper work, the bot opens a handful of sessions per guild per day,
    `link` sees a handful of callbacks, and the packet path emits no spans
    at all by construction. Total volume is a rounding error against Tempo.
    `STURNUS_OTEL_TRACES_SAMPLE_RATIO` exists as a valve for a future
    high-volume path, and the corollary matters more than the ratio:
    sampling must never be why a failed job is invisible, which is why job
    outcome is *also* an unsampled counter.
    """
    global _tracer_provider, _meter_provider

    settings = settings or OtelSettings()
    if settings.otel_exporter_otlp_endpoint is None:
        return False

    endpoint = settings.otel_exporter_otlp_endpoint
    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: service_name(component),
            # Groups the three deployments back together after
            # `service.name` has split them for Tempo's search and
            # Grafana's service graph.
            ResourceAttributes.SERVICE_NAMESPACE: "sturnus",
            # The same `__version__` `init_sentry` uses for its release tag,
            # which release-please keeps in lockstep with the chart's
            # appVersion.
            ResourceAttributes.SERVICE_VERSION: __version__,
            # The pod name, exactly as `init_sentry` relies on the SDK
            # defaulting `server_name` to it.
            ResourceAttributes.SERVICE_INSTANCE_ID: socket.gethostname(),
            # Literal rather than the semconv constant: the incubating
            # module's import path is not stable across releases, and
            # `tests/infrastructure/test_telemetry.py` pins the string
            # against what the installed package exports.
            "deployment.environment.name": settings.environment,
        }
    )

    _tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_ratio)),
        # A third lock behind `span()`'s flags and the allowlisting
        # exporter: caps any string attribute at the SDK boundary.
        span_limits=SpanLimits(max_attribute_length=256),
    )
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(
            AllowlistingSpanExporter(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
    )
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                export_interval_millis=settings.otel_metric_export_interval_seconds * 1000,
            )
        ],
        views=_views(),
    )
    metrics.set_meter_provider(_meter_provider)

    events.set_trace_context_provider(_trace_context)
    _silence_sentry_export_storm()

    # Announced at INFO, the way `init_sentry` announces itself, and for a
    # sharper reason than symmetry: if the endpoint is wrong, spans and
    # metrics vanish and every dashboard shows a flat, healthy-looking zero.
    # This line and the deploy checklist's "confirm one trace and one metric
    # arrive in Grafana" step are the only two detectors of that. An OTLP
    # endpoint is a service address, not a credential.
    log_event(
        log,
        logging.INFO,
        Event.TELEMETRY_ENABLED,
        "OpenTelemetry traces and metrics enabled",
        component=component,
        server_address=endpoint,
    )
    return True


def _silence_sentry_export_storm() -> None:
    """Stops a failed OTLP export from becoming a Sentry issue, forever.

    A verified cross-branch defect rather than a precaution. The SDK logs
    export failures with `logger.exception` (`sdk/trace/export/__init__.py`)
    and `_logger.error` (the OTLP HTTP exporter), and
    `observability.init_sentry` configures
    `LoggingIntegration(event_level=logging.ERROR)`. So with Alloy
    unreachable -- a NetworkPolicy, a namespace move, a rollout -- *every
    retry of every failed batch* becomes a Sentry event, from all three
    pods, until someone notices the bill.

    `ignore_logger` is confirmed present in the pinned sentry-sdk 2.68.0.
    It is called from here rather than added to `init_sentry` so that the
    branch owning that privacy control is not edited from this one; the
    matching `NEVER_BELOW` entry in `sturnus.observability.setup` keeps the
    same storm out of Loki. Doing it here also means the suppression exists
    only when OTLP export is actually configured.

    Note the consequence, stated because it is load-bearing: once this
    runs, a broken exporter is visible in neither Sentry nor Loki, so the
    post-deploy smoke check is the only remaining detector. That is what
    makes it a real checklist item rather than a nicety.
    """
    try:
        from sentry_sdk.integrations.logging import ignore_logger
    except ImportError:  # pragma: no cover - sentry-sdk is a hard dependency
        return
    ignore_logger("opentelemetry")


def shutdown_telemetry() -> None:
    """Flushes and tears down both providers. Call in each `_run`'s `finally`.

    Without it the last batch of spans -- which, during a SIGTERM, is
    exactly the batch describing the shutdown that is about to be
    investigated -- dies with the process.
    """
    global _tracer_provider, _meter_provider
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None
    events.set_trace_context_provider(None)
