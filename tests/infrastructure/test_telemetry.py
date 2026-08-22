"""The trace-side privacy control, proven end to end rather than asserted.

The headline test is `test_a_leaky_span_still_exports_nothing_sensitive`: it
drives a span with OpenTelemetry's *own* leaky defaults, a forbidden
attribute and a transcript-bearing exception through the real allowlisting
exporter, and asserts the exported payload is clean. That is the difference
between a control and a comment.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, StatusCode

from sturnus.config import OtelSettings
from sturnus.domain.measurements import JobMeasurements
from sturnus.infrastructure import telemetry
from sturnus.infrastructure.telemetry import (
    AllowlistingSpanExporter,
    fail_span,
    init_telemetry,
    set_span_fields,
    span,
    span_attributes,
)
from sturnus.observability import events
from sturnus.observability.fields import SAFE_SPAN_ATTRIBUTES, SEMCONV_SPAN_ATTRIBUTES

TRANSCRIPT = "SECRET-TRANSCRIPT-what-they-actually-said-in-the-meeting"


@pytest.fixture
def exported() -> Iterator[InMemorySpanExporter]:
    """A provider whose spans pass through the real allowlisting exporter."""
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(AllowlistingSpanExporter(sink)))
    saved = trace.get_tracer_provider()
    trace._TRACER_PROVIDER = provider  # noqa: SLF001 - no public reset exists
    yield sink
    trace._TRACER_PROVIDER = saved  # noqa: SLF001
    provider.shutdown()


# ---------------------------------------------------------------------------
# Non-negotiable: works with no collector
# ---------------------------------------------------------------------------


def test_no_endpoint_installs_nothing_at_all() -> None:
    """The whole "works without Alloy" guarantee, in one assertion.

    Not `is_active()`-style introspection: the claim is that no provider is
    constructed, so nothing connects, nothing retries, and nothing logs an
    export failure.
    """
    settings = OtelSettings(otel_exporter_otlp_endpoint=None)
    assert init_telemetry("worker", settings) is False
    assert telemetry._tracer_provider is None  # noqa: SLF001
    assert telemetry._meter_provider is None  # noqa: SLF001
    assert events.current_trace_context() == {}


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_endpoint_is_absent_not_an_endpoint(blank: str) -> None:
    """The chart's default for an unconfigured cluster is `""`, not unset.

    `StrictSettings._reject_blank_required_values` does not apply to an
    optional field, so without this validator `""` would reach the exporter
    as a real endpoint and every export would fail forever.
    """
    settings = OtelSettings(otel_exporter_otlp_endpoint=blank)
    assert settings.otel_exporter_otlp_endpoint is None
    assert init_telemetry("bot", settings) is False


def test_spans_and_metrics_are_no_ops_with_no_provider() -> None:
    """Every instrumentation call in the codebase degrades to nothing."""
    telemetry.shutdown_telemetry()
    with span("job.process", job_id=1) as active:
        assert isinstance(active, NonRecordingSpan)
    # A counter with no provider must not raise, and must not require a
    # conditional at the call site.
    telemetry.record(telemetry.VOICE_PACKETS, 1, outcome="recorded", guild_id=42)
    telemetry.record(telemetry.JOB_STAGE_DURATION, 1.5, stage="decrypt", outcome="ok")


def test_the_whole_worker_pipeline_runs_with_no_collector() -> None:
    """The traced wrappers are inert too, not merely the raw API."""
    from sturnus.infrastructure.traced import TracedQueue

    class _Queue:
        """A whole `Queue`, not just `claim` -- mypy compares the wrapper
        against the full protocol at the call site."""

        async def claim(self) -> object | None:
            return None

        async def complete(
            self, job_id: int, transcript: str, measurements: JobMeasurements | None = None
        ) -> bool:
            del job_id, transcript, measurements
            raise AssertionError("not reached: the queue is empty")

        async def fail(self, job_id: int, error: str, max_attempts: int) -> bool:
            del job_id, error, max_attempts
            raise AssertionError("not reached: the queue is empty")

    import asyncio

    assert asyncio.run(TracedQueue(_Queue()).claim()) is None


def test_a_ratio_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match="TRACES_SAMPLE_RATIO"):
        OtelSettings(otel_traces_sample_ratio=1.5)


def test_the_environment_is_shared_with_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    """One environment string, so Tempo and Sentry can never disagree."""
    from sturnus.config import SentrySettings

    monkeypatch.setenv("STURNUS_SENTRY_ENVIRONMENT", "staging")
    assert OtelSettings().environment == "staging"
    assert SentrySettings().sentry_environment == "staging"


# ---------------------------------------------------------------------------
# Non-negotiable: nothing sensitive reaches a span
# ---------------------------------------------------------------------------


def test_a_leaky_span_still_exports_nothing_sensitive(
    exported: InMemorySpanExporter,
) -> None:
    """The control, demonstrated against deliberately wrong usage.

    Everything here is what a careless call site would do: OpenTelemetry's
    own default exception flags, an unregistered attribute holding a display
    name, and a transcript-bearing exception escaping the span. With
    `record_exception=True` the SDK writes the message into
    `exception.message`, the full `exception.stacktrace` **and**
    `status.description` -- three separate paths, verified against
    opentelemetry-sdk 1.44.0. The exporter must neutralise all of them.
    """
    tracer = trace.get_tracer("test")
    with pytest.raises(RuntimeError), tracer.start_as_current_span("job.process") as active:
        active.set_attribute("sturnus.job_id", 7)
        active.set_attribute("sturnus.speaker.display_name", "Alice Example")
        active.set_attribute("sturnus.transcript_text", TRANSCRIPT)
        raise RuntimeError(TRANSCRIPT)

    (out,) = exported.get_finished_spans()
    assert dict(out.attributes or {}) == {"sturnus.job_id": 7}
    assert out.events == ()
    assert out.status.status_code is StatusCode.ERROR
    assert out.status.description is None

    serialised = out.to_json()
    assert TRANSCRIPT not in serialised
    assert "Alice" not in serialised


def test_span_opened_through_the_helper_never_records_the_exception(
    exported: InMemorySpanExporter,
) -> None:
    """The first of the two independent locks: both flags off at the source."""
    with pytest.raises(RuntimeError), span("job.transcribe", job_id=7):
        raise RuntimeError(TRANSCRIPT)

    (out,) = exported.get_finished_spans()
    assert out.events == ()
    assert out.status.status_code is StatusCode.ERROR
    assert out.status.description is None
    assert out.attributes is not None
    assert out.attributes["error.type"] == "RuntimeError"
    assert TRANSCRIPT not in out.to_json()


def test_fail_span_records_a_class_name_never_a_message(
    exported: InMemorySpanExporter,
) -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("x") as active:
        fail_span(active, ValueError(TRANSCRIPT))
    (out,) = exported.get_finished_spans()
    assert out.attributes is not None
    assert out.attributes["error.type"] == "ValueError"
    assert TRANSCRIPT not in out.to_json()


def test_an_unregistered_field_never_becomes_an_attribute() -> None:
    """Dropped at the source as well as at the exporter -- two locks, not one."""
    attributes = span_attributes({"job_id": 7, "transcript": TRANSCRIPT, "display_name": "Alice"})
    assert attributes == {"sturnus.job_id": 7}


def test_log_only_fields_are_refused_as_span_attributes() -> None:
    """A user id is fine in `kubectl logs` and deliberately not in Tempo."""
    assert span_attributes({"discord_user_id": 12345, "job_id": 7}) == {"sturnus.job_id": 7}


def test_audio_bytes_offered_as_an_attribute_render_as_a_length(
    exported: InMemorySpanExporter,
) -> None:
    with span("recording.upload") as active:
        set_span_fields(active, bytes=b"\x00\x01" * 1024)
    (out,) = exported.get_finished_spans()
    assert out.attributes is not None
    assert out.attributes["sturnus.bytes"] == "<bytes len=2048>"


def test_metric_attributes_are_narrower_than_span_attributes() -> None:
    """Cardinality: a session id would be a new time series per session."""
    attributes = telemetry._metric_attributes(  # noqa: SLF001
        {"outcome": "done", "session_id": 4711, "job_id": 7, "guild_id": 42}
    )
    assert attributes == {"outcome": "done", "guild_id": 42}


# ---------------------------------------------------------------------------
# The literals in the stdlib-only registry match the real semantic conventions
# ---------------------------------------------------------------------------


def test_semconv_literals_match_the_installed_conventions() -> None:
    """`sturnus.observability.fields` is stdlib-only, so it spells these by hand.

    This is what stops the hand-written literal drifting from the convention
    it is claiming to follow.
    """
    from opentelemetry.semconv.attributes import (
        error_attributes,
        http_attributes,
        server_attributes,
        url_attributes,
    )

    assert SEMCONV_SPAN_ATTRIBUTES["error_type"] == error_attributes.ERROR_TYPE
    assert SEMCONV_SPAN_ATTRIBUTES["http_method"] == http_attributes.HTTP_REQUEST_METHOD
    assert SEMCONV_SPAN_ATTRIBUTES["http_status"] == http_attributes.HTTP_RESPONSE_STATUS_CODE
    assert SEMCONV_SPAN_ATTRIBUTES["server_address"] == server_attributes.SERVER_ADDRESS
    assert SEMCONV_SPAN_ATTRIBUTES["url_path"] == url_attributes.URL_PATH


def test_deployment_environment_literal_matches_the_incubating_convention() -> None:
    from opentelemetry.semconv._incubating.attributes import deployment_attributes

    assert deployment_attributes.DEPLOYMENT_ENVIRONMENT_NAME == "deployment.environment.name"


def test_every_semconv_attribute_is_in_the_span_allowlist() -> None:
    for name in SEMCONV_SPAN_ATTRIBUTES.values():
        assert name in SAFE_SPAN_ATTRIBUTES


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_init_telemetry_installs_providers_and_the_trace_context_hook() -> None:
    """With an endpoint, the Loki -> Tempo correlation field starts working."""
    settings = OtelSettings(
        otel_exporter_otlp_endpoint="http://alloy-receiver.grafana.svc:4318",
        otel_metric_export_interval_seconds=3600.0,
    )
    try:
        assert init_telemetry("worker", settings) is True
        assert telemetry._tracer_provider is not None  # noqa: SLF001
        assert telemetry._meter_provider is not None  # noqa: SLF001

        with span("job.process", job_id=1):
            context = events.current_trace_context()
            assert set(context) == {"trace_id", "span_id"}
            assert len(context["trace_id"]) == 32
            assert int(context["trace_id"], 16) != 0
    finally:
        telemetry.shutdown_telemetry()

    # Torn down cleanly: no stale hook left pointing at a dead provider.
    assert events.current_trace_context() == {}


def test_no_auto_instrumentation_package_is_installed() -> None:
    """Each one would ship a transcript, a user id or a credential.

    An install-time assertion because that is where the decision is made:
    `pyproject.toml` names four OpenTelemetry packages and none of them
    instruments anything. See the module docstring for what each
    instrumentor would attach.
    """
    import importlib.metadata

    installed = {dist.metadata["Name"] or "" for dist in importlib.metadata.distributions()}
    offenders = {name for name in installed if name.startswith("opentelemetry-instrumentation")}
    assert not offenders, f"auto-instrumentation must not be installed: {offenders}"


def test_the_otlp_export_logger_is_kept_out_of_sentry() -> None:
    """A verified cross-branch defect: every failed export would be an issue.

    The SDK logs export failures at ERROR and `init_sentry` configures
    `LoggingIntegration(event_level=logging.ERROR)`, so an unreachable Alloy
    would produce a Sentry event per retry per batch from all three pods.
    """
    import sentry_sdk.integrations.logging as sentry_logging

    telemetry._silence_sentry_export_storm()  # noqa: SLF001
    assert "opentelemetry" in sentry_logging._IGNORED_LOGGERS  # noqa: SLF001


def test_histogram_views_cover_every_histogram() -> None:
    """The SDK's default buckets top out at 10000 in milliseconds; ours are seconds."""
    names = {
        view._instrument_name  # noqa: SLF001
        for view in telemetry._views()  # noqa: SLF001
    }
    assert "sturnus.job.stage.duration" in names
    assert "sturnus.transcription.audio_duration" in names
    assert "sturnus.session.close.duration" in names
    assert "sturnus.recording.upload.bytes" in names


def _all_span_attribute_names(module: Any) -> set[str]:
    del module
    return set(SAFE_SPAN_ATTRIBUTES)


def test_the_allowlist_contains_no_forbidden_name() -> None:
    """A last, blunt check on the derived set itself."""
    forbidden = ("transcript", "display_name", "s3_key", "token", "secret", "key_material")
    for name in SAFE_SPAN_ATTRIBUTES:
        for bad in forbidden:
            assert bad not in name, name
