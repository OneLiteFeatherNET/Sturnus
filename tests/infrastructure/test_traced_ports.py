"""The traced wrappers, driven through the *real* `process_one`.

Two properties are being checked, and the first matters more than the
second.

**Behaviour is unchanged.** `Queue.claim` returns `object | None` and
`process_one` immediately `cast`s it to `_ClaimedJobShape`; a wrapper that
returned anything but the original object would break the pipeline silently
and no type checker would notice, because the declared type is `object`.
So the wrappers are exercised against the same fakes
`tests/application/test_worker.py` uses, and the same assertions are made.

**The expected span tree appears**, with no forbidden attribute in it -- run
against a transcript-shaped canary so a leak fails the test rather than
being reasoned about.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sturnus.application.worker import process_one
from sturnus.infrastructure.telemetry import AllowlistingSpanExporter, span
from sturnus.infrastructure.traced import (
    TracedAudioDownloader,
    TracedDecryptor,
    TracedDocumentSink,
    TracedQueue,
    TracedTranscriptionEngine,
)
from tests.application.test_worker import (
    FakeConfig,
    FakeCrypto,
    FakeDocuments,
    FakeEngine,
    FakeJobs,
    FakeLinks,
    FakeQueue,
    FakeSessions,
    FakeStore,
    exports,
    job,
)

#: A canary that must never appear in any exported span. It is what the
#: engine "transcribes", so it flows through `complete`, the assembled
#: document body, and the document title.
CANARY = "CANARY-they-discussed-the-acquisition-in-confidence"


@pytest.fixture
def exported() -> Iterator[InMemorySpanExporter]:
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(AllowlistingSpanExporter(sink)))
    saved = trace.get_tracer_provider()
    trace._TRACER_PROVIDER = provider  # noqa: SLF001 - no public reset exists
    yield sink
    trace._TRACER_PROVIDER = saved  # noqa: SLF001
    provider.shutdown()


async def _run_one(tmp_path: Path, *, is_last: bool = True) -> tuple[FakeQueue, FakeDocuments]:
    queue = FakeQueue([job()])
    queue.last_is_final = is_last
    documents = FakeDocuments()
    sessions = FakeSessions()
    with span("job.process"):
        await process_one(
            queue=TracedQueue(queue),
            engine=TracedTranscriptionEngine(FakeEngine(CANARY)),
            store=TracedAudioDownloader(FakeStore()),
            crypto=TracedDecryptor(FakeCrypto()),
            exports=exports(TracedDocumentSink(documents)),
            sessions=sessions,
            jobs=FakeJobs(),
            links=FakeLinks(),
            config=FakeConfig(),
            work_dir=tmp_path,
            max_attempts=3,
        )
    return queue, documents


async def test_wrapping_does_not_change_what_process_one_does(tmp_path: Path) -> None:
    """The pass-through property: same completions, same document, same body."""
    queue, documents = await _run_one(tmp_path)

    assert len(queue.completed) == 1
    job_id, transcript = queue.completed[0]
    assert job_id == 1
    # The transcript reached the queue untouched -- the wrapper observes,
    # it does not transform.
    assert CANARY in transcript
    assert queue.failed == []
    assert len(documents.created) == 1


async def test_the_expected_span_tree_appears(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    await _run_one(tmp_path)

    names = [s.name for s in exported.get_finished_spans()]
    assert "job.claim" in names
    assert "job.download" in names
    assert "job.decrypt" in names
    assert "job.transcribe" in names
    assert "job.complete" in names
    # Deliberately absent: `TracedDocumentSink` opens no span, because the
    # concrete `OutlineSink` behind it carries its own richer
    # `document.create` CLIENT span. A wrapper span here would be a second,
    # identical, nested entry in every waterfall. `FakeDocuments` stands in
    # for that adapter, so nothing emits one in this test.
    assert "document.create" not in names
    # The root closes last, so every stage above is a child of it.
    assert names[-1] == "job.process"


async def test_no_exported_span_carries_the_transcript(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    """The canary assertion. A leak fails here rather than in production."""
    await _run_one(tmp_path)

    for finished in exported.get_finished_spans():
        serialised = finished.to_json()
        assert CANARY not in serialised, f"{finished.name} leaked the transcript"
        assert "speaker-100" not in serialised, f"{finished.name} leaked a display name"
        # The S3 key embeds a Discord user id, so it is out of spans
        # entirely -- see `fields.DENIED_NAMES`.
        assert "sessions/1/speakers/100.enc" not in serialised


async def test_counts_of_the_transcript_are_recorded_but_not_its_text(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    """The judgement call, made explicit: a length travels, the words do not."""
    await _run_one(tmp_path)

    (transcribe,) = [s for s in exported.get_finished_spans() if s.name == "job.transcribe"]
    attributes = dict(transcribe.attributes or {})
    assert attributes["sturnus.segment_count"] == 1
    assert attributes["sturnus.char_count"] == len(CANARY)
    assert attributes["sturnus.language"] == "de"
    assert CANARY not in transcribe.to_json()


async def test_the_claim_stamps_ids_onto_the_enclosing_root_span(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    """The order-dependent bit, pinned so a later refactor cannot break it quietly."""
    await _run_one(tmp_path)

    (root,) = [s for s in exported.get_finished_spans() if s.name == "job.process"]
    attributes = dict(root.attributes or {})
    assert attributes["sturnus.job_id"] == 1
    assert attributes["sturnus.session_id"] == 1


async def test_a_failing_stage_marks_its_span_without_a_message(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    """`process_one` catches the failure; the span still records that it happened."""
    queue = FakeQueue([job()])
    with span("job.process"):
        await process_one(
            queue=TracedQueue(queue),
            engine=TracedTranscriptionEngine(FakeEngine(CANARY, fail=True)),
            store=TracedAudioDownloader(FakeStore()),
            crypto=TracedDecryptor(FakeCrypto()),
            exports=exports(TracedDocumentSink(FakeDocuments())),
            sessions=FakeSessions(),
            jobs=FakeJobs(),
            links=FakeLinks(),
            config=FakeConfig(),
            work_dir=tmp_path,
            max_attempts=3,
        )

    assert len(queue.failed) == 1
    (transcribe,) = [s for s in exported.get_finished_spans() if s.name == "job.transcribe"]
    assert transcribe.status.status_code.name == "ERROR"
    assert transcribe.status.description is None
    assert dict(transcribe.attributes or {})["error.type"] == "RuntimeError"
    # "model exploded" is the message; it belongs in the database column,
    # not in Tempo.
    assert "model exploded" not in transcribe.to_json()


async def test_the_root_span_of_a_failed_job_does_not_say_it_was_done(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    """The lie, at the span end of it.

    `process_one` returns `True` here -- the job was attempted, the engine
    raised, `queue.fail` ran -- and the worker loop turned that boolean
    into `outcome="done"`. Driven through the *real* `process_one` so the
    return value really is `True` on this path rather than being asserted
    to be.
    """
    queue = FakeQueue([job()])
    with span("job.process"):
        attempted = await process_one(
            queue=TracedQueue(queue),
            engine=TracedTranscriptionEngine(FakeEngine(CANARY, fail=True)),
            store=TracedAudioDownloader(FakeStore()),
            crypto=TracedDecryptor(FakeCrypto()),
            exports=exports(TracedDocumentSink(FakeDocuments())),
            sessions=FakeSessions(),
            jobs=FakeJobs(),
            links=FakeLinks(),
            config=FakeConfig(),
            work_dir=tmp_path,
            max_attempts=3,
        )

    assert attempted is True, "the premise: the return value cannot tell these apart"
    (root,) = [s for s in exported.get_finished_spans() if s.name == "job.process"]
    assert dict(root.attributes or {})["sturnus.outcome"] == "failed"


async def test_the_root_span_of_a_completed_job_says_done(
    tmp_path: Path, exported: InMemorySpanExporter
) -> None:
    """The control on the test above: `done` still reaches the span it belongs on.

    Without this, deleting the `outcome` stamp altogether would satisfy the
    "not done" assertion above and lose the label entirely.
    """
    await _run_one(tmp_path)

    (root,) = [s for s in exported.get_finished_spans() if s.name == "job.process"]
    assert dict(root.attributes or {})["sturnus.outcome"] == "done"
