import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.queue import JobQueue
from sturnus.infrastructure.db.repositories import JobRepository, SessionRepository
from sturnus.infrastructure.telemetry import JOB_OUTCOME, record

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def seed(factory: async_sessionmaker[AsyncSession], speakers: list[int]) -> int:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    for user_id in speakers:
        await sessions.add_participant(session_id, user_id, f"user{user_id}", T0)
        await jobs.enqueue(
            session_id=session_id,
            discord_user_id=user_id,
            s3_key=f"sessions/{session_id}/speakers/{user_id}.enc",
            encryption_key_id="k1",
            wrapped_data_key=b"wrapped",
            retention_until=T0 + timedelta(days=30),
        )
    await sessions.close_session(session_id, T0 + timedelta(hours=1), "empty")
    return session_id


async def test_an_empty_queue_claims_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await JobQueue(factory).claim() is None


async def test_claiming_returns_what_the_worker_needs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory, [ANNA])
    job = await JobQueue(factory).claim()
    assert job is not None
    assert job.discord_user_id == ANNA
    assert job.encryption_key_id == "k1"
    assert job.wrapped_data_key == b"wrapped"


async def test_a_claimed_job_is_not_claimed_twice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two workers must never transcribe the same recording."""
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    first = await queue.claim()
    second = await queue.claim()
    assert first is not None
    assert second is None


async def test_completing_a_job_is_not_the_last_while_others_remain(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory, [ANNA, BEN])
    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None
    assert await queue.complete(job.id, "some text") is False


async def test_completing_the_final_job_reports_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The last completion is what triggers document creation (Spec 5.3)."""
    await seed(factory, [ANNA, BEN])
    queue = JobQueue(factory)
    for _ in range(2):
        job = await queue.claim()
        assert job is not None
        last = await queue.complete(job.id, "text")
    assert last is True


async def test_concurrent_completions_of_a_sessions_last_two_jobs_report_true_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drives two `complete()` calls for the two jobs of one session at the
    same time via `asyncio.gather`, the way two workers finishing at once
    really would -- every other test in this file awaits completions
    sequentially, which cannot exercise the race at all.

    Before the lock in `queue.py` was added, this returned `(False, False)`
    in 49 of 50 runs: each call's remaining-jobs count was computed against
    a snapshot that predated the other call's `done` update, so neither
    ever saw zero and the session's document was never created. Every
    iteration here must report the session done exactly once.
    """
    queue = JobQueue(factory)
    for _ in range(30):
        session_id = await seed(factory, [ANNA, BEN])
        first = await queue.claim()
        second = await queue.claim()
        assert first is not None
        assert second is not None
        assert first.session_id == session_id
        assert second.session_id == session_id

        results = await asyncio.gather(
            queue.complete(first.id, "text a"),
            queue.complete(second.id, "text b"),
        )
        assert sorted(results) == [False, True], (
            f"expected exactly one completion to report the session done, got {results}"
        )


async def test_a_failed_job_returns_to_the_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None
    await queue.fail(job.id, "boom", max_attempts=3)
    assert await queue.claim() is not None


async def test_a_job_that_keeps_failing_stops_being_claimed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Otherwise one broken recording spins forever and blocks its session."""
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    for _ in range(3):
        job = await queue.claim()
        assert job is not None
        await queue.fail(job.id, "boom", max_attempts=3)
    assert await queue.claim() is None


async def test_the_stored_error_is_readable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None
    await queue.fail(job.id, "decryption failed", max_attempts=3)
    assert "decryption failed" in (await queue.last_error(job.id) or "")


async def test_completing_a_job_is_not_last_while_the_session_is_still_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reproduces Defect 5 deterministically at the queue level.

    `sturnus.application.recording.RecordingService.close` uploads and
    enqueues speaker by speaker, so a worker can claim and complete the
    first speaker's job before a second speaker has even been enqueued
    yet, let alone before the session itself closes. Without gating on
    `session.status`, `complete` would report the session done from this
    one job alone -- exactly the failure the assembly fix was meant to
    end, one level up. This enqueues only one speaker and never closes
    the session, so a second speaker could still show up later.

    Against the pre-fix `complete` (remaining-jobs count only), this
    returns `True` -- confirmed by running it against that code directly.
    """
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await sessions.add_participant(session_id, ANNA, "anna", T0)
    await jobs.enqueue(
        session_id=session_id,
        discord_user_id=ANNA,
        s3_key=f"sessions/{session_id}/speakers/{ANNA}.enc",
        encryption_key_id="k1",
        wrapped_data_key=b"wrapped",
        retention_until=T0 + timedelta(days=30),
    )
    # Deliberately never closed.

    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None
    assert await queue.complete(job.id, "anna's words") is False


async def test_completing_the_final_job_is_last_once_the_session_closes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The counterpart to the test above: once every speaker is enqueued
    and the session is actually closed, the same job now correctly
    reports last.
    """
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, "meeting-raum", T0)
    await sessions.add_participant(session_id, ANNA, "anna", T0)
    await jobs.enqueue(
        session_id=session_id,
        discord_user_id=ANNA,
        s3_key=f"sessions/{session_id}/speakers/{ANNA}.enc",
        encryption_key_id="k1",
        wrapped_data_key=b"wrapped",
        retention_until=T0 + timedelta(days=30),
    )
    await sessions.close_session(session_id, T0 + timedelta(hours=1), "empty")

    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None
    assert await queue.complete(job.id, "anna's words") is True


async def test_a_running_jobs_lease_protects_it_from_a_second_claim(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    first = await queue.claim(T0)
    assert first is not None
    # Still well within the lease: must not be handed to a second worker.
    assert await queue.claim(T0 + timedelta(seconds=30)) is None


async def test_a_running_jobs_expired_lease_is_reclaimed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker killed mid-job (SIGKILL, an evicted pod) leaves its job
    `running` forever without this -- `claim` only ever selected `pending`
    jobs, so nothing would ever reclaim it (Defect 4).
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    first = await queue.claim(T0)
    assert first is not None

    reclaimed = await queue.claim(T0 + timedelta(seconds=61))
    assert reclaimed is not None
    assert reclaimed.id == first.id


async def test_a_reclaimed_job_gets_a_fresh_lease(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reclaiming worker must get the full lease again, not an
    already-expired one -- otherwise a third worker could reclaim it out
    from under the second before it has had any real time to work.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    first = await queue.claim(T0)
    assert first is not None
    reclaimed = await queue.claim(T0 + timedelta(seconds=61))
    assert reclaimed is not None

    assert await queue.claim(T0 + timedelta(seconds=90)) is None


# ---------------------------------------------------------------------------
# `sturnus.job.outcome`: what actually happened, not what was returned
# ---------------------------------------------------------------------------


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `record(...)` this module makes, captured on its way through.

    The real `record` is still called -- this observes the call, it does
    not replace the recorder -- so the production path, including
    `_metric_attributes`' allowlist, runs exactly as it does in the worker
    and a label that would be dropped there is dropped here too.

    A spy rather than a metric reader because installing a `MeterProvider`
    is a process-global, once-only operation: it would bind every
    module-level instrument in `sturnus.infrastructure.telemetry` for the
    rest of the session and silently invalidate
    `test_spans_and_metrics_are_no_ops_with_no_provider`, which asserts the
    opposite premise.
    """
    seen: list[dict[str, Any]] = []

    def spy(instrument: Any, value: float, **fields: object) -> None:
        seen.append({"instrument": instrument, "value": value, **fields})
        record(instrument, value, **fields)

    # Patched by dotted path rather than through the module object: `record`
    # is not re-exported from `sturnus.infrastructure.db.queue`, and mypy's
    # `no_implicit_reexport` is right to say so.
    monkeypatch.setattr("sturnus.infrastructure.db.queue.record", spy)
    return seen


def _job_outcomes(seen: list[dict[str, Any]]) -> list[str]:
    return [
        str(call["outcome"])
        for call in seen
        if call["instrument"] is JOB_OUTCOME and call["value"] == 1
    ]


async def test_a_completed_job_is_counted_as_done(
    factory: async_sessionmaker[AsyncSession], outcomes: list[dict[str, Any]]
) -> None:
    """The only path that may produce `done`, and it is the one that stored a
    transcript."""
    session_id = await seed(factory, [ANNA])
    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None

    await queue.complete(job.id, "the transcript")

    assert _job_outcomes(outcomes) == ["done"]
    assert session_id  # the seed really produced a job to complete


async def test_a_job_returned_for_another_attempt_is_counted_as_failed(
    factory: async_sessionmaker[AsyncSession], outcomes: list[dict[str, Any]]
) -> None:
    """The defect this test exists for.

    `process_one` returns `True` after `queue.fail(...)` just as it does
    after `queue.complete(...)` -- the boolean means "work was attempted",
    not "work succeeded" -- and the worker loop used to turn that boolean
    into `outcome="done"`. Every failed job was therefore counted as a
    success, which is worse than not counting at all: an operator would
    believe it.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None

    assert await queue.fail(job.id, "s3 timed out", 3) is False

    assert _job_outcomes(outcomes) == ["failed"]


async def test_a_job_out_of_attempts_is_counted_as_dead_and_says_so(
    factory: async_sessionmaker[AsyncSession], outcomes: list[dict[str, Any]]
) -> None:
    """`dead` is permanent loss and must be distinguishable from a retry.

    The return value is what lets the caller -- and the `job.process` span
    -- tell the two apart at all: `fail` is the only place that knows,
    because it is the only place that counts the attempts.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    job = await queue.claim()
    assert job is not None

    assert await queue.fail(job.id, "still broken", 1) is True

    assert _job_outcomes(outcomes) == ["dead"]


async def test_the_outcome_counter_never_reports_a_failure_as_a_success(
    factory: async_sessionmaker[AsyncSession], outcomes: list[dict[str, Any]]
) -> None:
    """The property, over one job's whole life: two retries, then death.

    Written as the sequence rather than as three separate assertions
    because the failure the counter had was precisely a *substitution* --
    the right number of measurements with the wrong label on them.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory)
    for _ in range(3):
        job = await queue.claim()
        assert job is not None
        await queue.fail(job.id, "broken", 3)

    assert _job_outcomes(outcomes) == ["failed", "failed", "dead"]
