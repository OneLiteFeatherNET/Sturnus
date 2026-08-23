import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.domain import settings
from sturnus.domain.measurements import JobMeasurements, RecordedAudio
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base, GuildConfig, Session, TranscriptionJob
from sturnus.infrastructure.db.queue import DEFAULT_LEASE_SECONDS, JobQueue, claim_statement
from sturnus.infrastructure.db.repositories import JobRepository, SessionRepository
from sturnus.infrastructure.telemetry import JOB_OUTCOME, record

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200
#: Two more speakers and a second guild, for the parallel-tracks cap: the
#: cap is only observable on a session with more speakers than the cap,
#: and "per guild" is only observable against a second guild.
CARLA, DORA = 300, 400
OTHER_GUILD, OTHER_CHANNEL = 11, 12


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def seed(
    factory: async_sessionmaker[AsyncSession],
    speakers: list[int],
    guild: int = GUILD,
    channel: int = CHANNEL,
) -> int:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(guild, channel, "meeting-raum", T0)
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


async def _claim_one(factory: async_sessionmaker[AsyncSession], queue: JobQueue) -> Any:
    """Seeds one speaker's job and claims it, which is the state every
    measurement test below starts from."""
    await seed(factory, [ANNA])
    job = await queue.claim()
    assert job is not None
    return job


# ---------------------------------------------------------------------------
# What a finished job measured. The console's dashboard is built from these
# three numbers, and until now the worker computed all of them and kept
# none -- they went into a log line and a metric, both retained for weeks,
# while the job row lives as long as the guild does.
# ---------------------------------------------------------------------------


async def test_completing_a_job_stores_what_it_measured(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    queue = JobQueue(factory)
    job = await _claim_one(factory, queue)

    await queue.complete(
        job.id,
        "the transcript",
        JobMeasurements(audio_seconds=521.0, speech_seconds=88.5, segment_count=42),
    )

    async with factory() as session:
        stored = await session.get(TranscriptionJob, job.id)
        assert stored is not None
        assert stored.audio_seconds == 521.0
        assert stored.speech_seconds == 88.5
        assert stored.segment_count == 42


async def test_a_job_completed_without_measurements_stores_null_not_zero(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Null means "never measured"; zero would mean "said nothing".

    They are different facts about a recording, and the console renders
    them differently -- one as an absence, the other as a silent
    participant. Collapsing them here would make that distinction
    unrecoverable.
    """
    queue = JobQueue(factory)
    job = await _claim_one(factory, queue)

    await queue.complete(job.id, "the transcript")

    async with factory() as session:
        stored = await session.get(TranscriptionJob, job.id)
        assert stored is not None
        assert stored.audio_seconds is None
        assert stored.segment_count is None


async def test_a_track_that_produced_nothing_still_records_its_length(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case the whole column set exists for.

    A recording of real length whose decoder returned no segments is the
    signature of the defect that cost this project two days. Storing the
    length beside a segment count of zero is what makes it visible
    afterwards, rather than indistinguishable from a participant who
    never spoke.
    """
    queue = JobQueue(factory)
    job = await _claim_one(factory, queue)

    await queue.complete(
        job.id, "", JobMeasurements(audio_seconds=6000.0, speech_seconds=0.9, segment_count=0)
    )

    async with factory() as session:
        stored = await session.get(TranscriptionJob, job.id)
        assert stored is not None
        assert stored.audio_seconds == 6000.0
        assert stored.segment_count == 0


async def test_measurements_do_not_change_whether_a_job_was_the_last(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`complete` answers one question -- was this the session's last job,
    which is what triggers document creation -- and adding a second
    responsibility to it must not disturb the first. Two speakers, so both
    answers are exercised rather than only the one a single job can give.
    """
    await seed(factory, [ANNA, BEN])
    queue = JobQueue(factory)

    first = await queue.claim()
    assert first is not None
    assert await queue.complete(first.id, "a", JobMeasurements(10.0, 5.0, 1)) is False

    second = await queue.claim()
    assert second is not None
    assert await queue.complete(second.id, "b", JobMeasurements(20.0, 6.0, 2)) is True


# ---------------------------------------------------------------------------
# `max_parallel_tracks`: how much of the worker pool one meeting may take
#
# A four-speaker meeting is four independent jobs. Two workers may take two
# of them safely -- `claim` has always been serialised by `FOR UPDATE SKIP
# LOCKED` -- but nothing stopped one session from occupying *every* worker
# while a second guild's meeting waited behind all of it. These tests are
# the cap, and the last of them is the only one that can prove it: real
# claimers, running at the same time, against a real PostgreSQL.
# ---------------------------------------------------------------------------


async def set_cap(factory: async_sessionmaker[AsyncSession], guild: int, value: str) -> None:
    """Sets `max_parallel_tracks` for a guild the way an administrator does."""
    await ConfigStore(factory).set(guild, settings.MAX_PARALLEL_TRACKS, value, T0)


async def store_raw(factory: async_sessionmaker[AsyncSession], guild: int, value: str) -> None:
    """Puts a value in `guild_config` that `ConfigStore.set` would refuse.

    `docs/operations.md` section 4.1 tells operators they may edit
    `guild_config` with SQL, so the read path has to survive what that
    lets in -- and the read path here is the claim statement itself.
    """
    async with factory() as session:
        session.add(
            GuildConfig(
                guild_id=guild, key=settings.MAX_PARALLEL_TRACKS, value=value, updated_at=T0
            )
        )
        await session.commit()


async def running_tracks(factory: async_sessionmaker[AsyncSession], session_id: int) -> int:
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(TranscriptionJob)
            .where(
                TranscriptionJob.session_id == session_id,
                TranscriptionJob.status == "running",
            )
        )
    return int(count or 0)


async def forget_everything(factory: async_sessionmaker[AsyncSession]) -> None:
    """Drops every session and, by cascade, every job.

    The concurrency test below runs its scenario many times over, and a
    claim is now ordered oldest-job-first across the whole queue: without
    this, the second round would spend its claimers on the first round's
    leftovers instead of on the session it just seeded.
    """
    async with factory() as session:
        await session.execute(delete(Session))
        await session.commit()


async def test_a_session_is_claimed_only_up_to_its_guilds_parallel_track_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Four speakers, a limit of two: the third claim finds nothing.

    Not because the queue is empty -- two of this session's jobs are still
    `pending` -- but because this meeting has already taken as much of the
    pool as its guild allows it to.
    """
    await seed(factory, [ANNA, BEN, CARLA, DORA])
    await set_cap(factory, GUILD, "2")
    queue = JobQueue(factory)

    assert await queue.claim() is not None
    assert await queue.claim() is not None
    assert await queue.claim() is None


async def test_a_guild_that_has_named_no_limit_gets_the_conservative_default(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing configured is the state of every guild that has never been
    asked, so the default is what actually governs the deployment."""
    await seed(factory, [ANNA, BEN, CARLA, DORA])
    queue = JobQueue(factory)

    claimed = [await queue.claim() for _ in range(4)]

    assert sum(job is not None for job in claimed) == int(
        settings.DEFAULTS[settings.MAX_PARALLEL_TRACKS]
    )


async def test_finishing_a_track_hands_the_slot_to_the_next_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The cap bounds how many run at once, never how many run in total."""
    await seed(factory, [ANNA, BEN, CARLA, DORA])
    await set_cap(factory, GUILD, "2")
    queue = JobQueue(factory)

    first = await queue.claim()
    second = await queue.claim()
    assert first is not None
    assert second is not None
    assert await queue.claim() is None

    await queue.complete(first.id, "anna's words", lease=first.claimed_at)

    third = await queue.claim()
    assert third is not None
    assert third.id not in {first.id, second.id}


async def test_each_guild_is_capped_by_its_own_setting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The setting is per guild and the claim statement is guild-agnostic,
    so the one thing worth proving is that a candidate is measured against
    *its own* guild's value rather than against whatever row of
    `guild_config` happened to be found first."""
    strict = await seed(factory, [ANNA, BEN, CARLA], guild=GUILD, channel=CHANNEL)
    relaxed = await seed(factory, [ANNA, BEN, CARLA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)
    await set_cap(factory, GUILD, "1")
    await set_cap(factory, OTHER_GUILD, "3")
    queue = JobQueue(factory)

    while await queue.claim() is not None:
        pass

    assert await running_tracks(factory, strict) == 1
    assert await running_tracks(factory, relaxed) == 3


async def test_a_hand_edited_limit_that_is_not_a_number_is_ignored_rather_than_fatal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One bad row must not stop the worker.

    `ConfigStore.set` refuses a non-integer, but a direct `UPDATE` does
    not -- and this value is read inside the claim statement, so a value
    the database cannot cast would fail *every* claim for *every* guild,
    not just this one's. The queue falls back to the default instead.
    """
    await seed(factory, [ANNA, BEN, CARLA])
    await store_raw(factory, GUILD, "as many as possible")
    queue = JobQueue(factory)

    claimed = [await queue.claim() for _ in range(3)]

    assert sum(job is not None for job in claimed) == int(
        settings.DEFAULTS[settings.MAX_PARALLEL_TRACKS]
    )


async def test_a_meeting_at_its_limit_does_not_block_another_guilds_meeting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The cap must filter candidates, never stop the scan.

    A capped session's jobs are the oldest in the queue here, so a claim
    that gave up at the first unclaimable row would report an empty queue
    while another guild's meeting sat behind it untranscribed.
    """
    busy = await seed(factory, [ANNA, BEN, CARLA], guild=GUILD, channel=CHANNEL)
    waiting = await seed(factory, [ANNA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)
    await set_cap(factory, GUILD, "1")
    queue = JobQueue(factory)

    claimed = [job for _ in range(3) if (job := await queue.claim()) is not None]

    assert [job.session_id for job in claimed] == [busy, waiting]


async def test_the_oldest_outstanding_track_is_claimed_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Oldest-first is the anti-starvation guarantee.

    A session that keeps enqueuing gets *newer* job ids, which sort behind
    everything already waiting -- so a busy guild can never push a quiet
    one's meeting back indefinitely. Before this change `claim` had no
    `ORDER BY` at all and took whatever the scan reached first.
    """
    await seed(factory, [ANNA, BEN], guild=GUILD, channel=CHANNEL)
    await seed(factory, [CARLA, DORA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)
    queue = JobQueue(factory)

    claimed = [job for _ in range(4) if (job := await queue.claim()) is not None]

    assert [job.id for job in claimed] == sorted(job.id for job in claimed)


async def test_a_second_meeting_is_served_before_a_capped_ones_remaining_tracks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Oldest-first *and* the cap together, which is the whole point.

    The four-speaker meeting is older and would take the entire pool
    without a cap; the cap gives it two slots and the meeting behind it
    gets served rather than waiting for two more transcriptions.
    """
    crowded = await seed(factory, [ANNA, BEN, CARLA, DORA], guild=GUILD, channel=CHANNEL)
    later = await seed(factory, [ANNA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)
    await set_cap(factory, GUILD, "2")
    queue = JobQueue(factory)

    claimed = [job for _ in range(4) if (job := await queue.claim()) is not None]

    assert [job.session_id for job in claimed] == [crowded, crowded, later]


async def test_workers_claiming_at_the_same_instant_never_exceed_the_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The test the cap exists for, and the only one that can prove it.

    Six claimers, each on its own connection, released together against a
    real PostgreSQL: `SKIP LOCKED` semantics cannot be faked, and a cap
    that holds when claims are awaited one after another says nothing
    about a cap under a pool of workers all polling the same queue.

    Repeated, because a race that fires once in twenty runs is a race that
    fires in production. Each round starts from an empty queue -- claims
    are oldest-first, so the previous round's leftovers would otherwise be
    what the next round's claimers found.
    """
    queue = JobQueue(factory)
    cap = 2
    for _ in range(20):
        await forget_everything(factory)
        session_id = await seed(factory, [ANNA, BEN, CARLA, DORA])
        await set_cap(factory, GUILD, str(cap))

        claimed = await asyncio.gather(*(queue.claim() for _ in range(6)))

        taken = [job for job in claimed if job is not None]
        assert len(taken) == cap, f"expected {cap} claims, got {len(taken)}"
        assert len({job.id for job in taken}) == len(taken), "a job was claimed twice"
        assert await running_tracks(factory, session_id) == cap


# ---------------------------------------------------------------------------
# The lease as a fencing token: what a worker may still do to a job that has
# been taken away from it
#
# Nothing renews a lease mid-job, so a job that outruns `lease_seconds` is
# claimable by a second worker while the first is still transcribing it.
# With one worker that was a latent hazard; with several it is reachable,
# and both workers then call `complete` on the same job.
# ---------------------------------------------------------------------------


async def test_a_reclaimed_job_may_not_be_completed_by_the_worker_that_lost_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two workers on one track: only the one that holds the claim may finish it.

    Without the fence both completions apply, both count zero remaining
    jobs, and both report the session done -- so the session's protocol is
    created twice, from whichever transcript landed last.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    lost = await queue.claim(T0)
    assert lost is not None
    holder = await queue.claim(T0 + timedelta(seconds=61))
    assert holder is not None
    assert holder.id == lost.id

    assert await queue.complete(lost.id, "the slow worker", lease=lost.claimed_at) is False
    assert await queue.complete(holder.id, "the holder", lease=holder.claimed_at) is True

    async with factory() as session:
        stored = await session.get(TranscriptionJob, holder.id)
        assert stored is not None
        assert stored.transcript == "the holder"


async def test_a_job_already_finished_is_never_finished_a_second_time(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other order of the same race, and the one that creates two documents.

    The worker that lost its claim finishes *after* the holder has already
    completed the job. Its completion must not report the session done a
    second time.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    lost = await queue.claim(T0)
    assert lost is not None
    holder = await queue.claim(T0 + timedelta(seconds=61))
    assert holder is not None

    assert await queue.complete(holder.id, "the holder", lease=holder.claimed_at) is True
    assert await queue.complete(lost.id, "the slow worker", lease=lost.claimed_at) is False

    async with factory() as session:
        stored = await session.get(TranscriptionJob, lost.id)
        assert stored is not None
        assert stored.transcript == "the holder"


async def test_a_worker_that_lost_its_claim_may_not_return_the_job_to_the_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`fail` needs the same fence as `complete`, for a worse reason.

    A stale `fail` would yank a job out from under the worker that is
    transcribing it right now -- back to `pending` for a third worker to
    claim, or, once the attempts run out, straight to `dead`: a recording
    written off while a healthy worker was busy producing its transcript.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    lost = await queue.claim(T0)
    assert lost is not None
    holder = await queue.claim(T0 + timedelta(seconds=61))
    assert holder is not None

    assert await queue.fail(lost.id, "boom", max_attempts=1, lease=lost.claimed_at) is False

    async with factory() as session:
        stored = await session.get(TranscriptionJob, holder.id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.attempts == 0


async def test_a_completion_from_a_lost_claim_is_counted_as_stale_not_as_done(
    factory: async_sessionmaker[AsyncSession], outcomes: list[dict[str, Any]]
) -> None:
    """A transcription was spent and thrown away, and the counter says so.

    Counting it as `done` would be the same lie the outcome metric was
    built to end: work that did not become a transcript, reported as work
    that did.
    """
    await seed(factory, [ANNA])
    queue = JobQueue(factory, lease_seconds=60)
    lost = await queue.claim(T0)
    assert lost is not None
    assert await queue.claim(T0 + timedelta(seconds=61)) is not None

    await queue.complete(lost.id, "thrown away", lease=lost.claimed_at)

    assert _job_outcomes(outcomes) == ["stale"]


# ---------------------------------------------------------------------------
# What the recording is, as a file
#
# `sample_rate`, `channels` and `stored_bytes` are re-read out of the
# object store on every request that wants them -- a ranged GET and a
# chunk decrypt to answer "how many channels". The worker holds both
# copies on disk at the moment it completes a job, and this is where it
# writes them down.
# ---------------------------------------------------------------------------


async def test_completing_a_job_stores_what_the_recording_is(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    queue = JobQueue(factory)
    job = await _claim_one(factory, queue)

    await queue.complete(
        job.id,
        "the transcript",
        audio=RecordedAudio(sample_rate=16_000, channels=1, stored_bytes=1_048_576),
    )

    async with factory() as session:
        stored = await session.get(TranscriptionJob, job.id)
        assert stored is not None
        assert stored.sample_rate == 16_000
        assert stored.channels == 1
        assert stored.stored_bytes == 1_048_576


async def test_a_job_completed_without_a_readable_header_stores_null_not_zero(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Null means "nobody could look"; nought hertz would be a claim about
    a recording. The same rule the measurements follow, one column set
    over."""
    queue = JobQueue(factory)
    job = await _claim_one(factory, queue)

    await queue.complete(job.id, "the transcript")

    async with factory() as session:
        stored = await session.get(TranscriptionJob, job.id)
        assert stored is not None
        assert stored.sample_rate is None
        assert stored.channels is None
        assert stored.stored_bytes is None


async def test_a_worker_that_lost_its_job_does_not_stamp_the_file_it_measured(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fenced by the same lease as the transcript.

    A worker whose lease expired mid-job is measuring a copy nobody is
    waiting for, and the row belongs to whoever holds it now.
    """
    queue = JobQueue(factory)
    job = await _claim_one(factory, queue)
    stale = job.claimed_at - timedelta(hours=1)

    assert (
        await queue.complete(
            job.id,
            "the transcript",
            lease=stale,
            audio=RecordedAudio(sample_rate=48_000, channels=2, stored_bytes=1),
        )
        is False
    )

    async with factory() as session:
        stored = await session.get(TranscriptionJob, job.id)
        assert stored is not None
        assert stored.sample_rate is None
        assert stored.stored_bytes is None


# ---------------------------------------------------------------------------
# `priority`: what an administrator said should run first
#
# The column and its index landed in migration 0013 with nothing reading
# them. These are the tests that make the claim read them -- lower first,
# ties still broken by id, and the plan still one forward scan of
# `ix_job_claim_order` rather than a sort over the whole queue.
# ---------------------------------------------------------------------------


async def set_priority(
    factory: async_sessionmaker[AsyncSession], session_id: int, priority: int
) -> None:
    """Puts one session's jobs at a priority, the way the console's write does."""
    async with factory() as session:
        await session.execute(
            update(TranscriptionJob)
            .where(TranscriptionJob.session_id == session_id)
            .values(priority=priority)
        )
        await session.commit()


async def test_a_raised_session_is_claimed_before_an_older_ordinary_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of the column, and the story it was asked for.

    The retrospective was enqueued second, so oldest-first alone would put
    it behind the recording nobody is waiting on. Lower first: the
    ordinary session is held back to `1` and the retrospective, still at
    the ordinary `0`, is claimed first.
    """
    ordinary = await seed(factory, [ANNA], guild=GUILD, channel=CHANNEL)
    retrospective = await seed(factory, [BEN], guild=GUILD, channel=CHANNEL)
    await set_priority(factory, ordinary, 1)
    queue = JobQueue(factory)

    claimed = [job for _ in range(2) if (job := await queue.claim()) is not None]

    assert [job.session_id for job in claimed] == [retrospective, ordinary]


async def test_a_session_held_back_runs_after_a_meeting_that_ended_later(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Holding one back is how going first is expressed; it must actually work.

    The held-back session's jobs are the *oldest* in the queue, which is
    the case a claim that merely ordered by id would get wrong.
    """
    held_back = await seed(factory, [ANNA, BEN], guild=GUILD, channel=CHANNEL)
    await set_priority(factory, held_back, 2)
    later = await seed(factory, [CARLA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)
    queue = JobQueue(factory)

    claimed = [job for _ in range(3) if (job := await queue.claim()) is not None]

    assert [job.session_id for job in claimed] == [later, held_back, held_back]


async def test_two_sessions_at_the_same_priority_still_run_oldest_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """First-in-first-out within a priority, which the index gives for free."""
    first = await seed(factory, [ANNA], guild=GUILD, channel=CHANNEL)
    second = await seed(factory, [BEN], guild=GUILD, channel=CHANNEL)
    await set_priority(factory, first, 5)
    await set_priority(factory, second, 5)
    queue = JobQueue(factory)

    claimed = [job for _ in range(2) if (job := await queue.claim()) is not None]

    assert [job.session_id for job in claimed] == [first, second]


async def test_no_index_this_schema_has_can_order_a_claim_by_priority(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The plan, asked rather than assumed -- and it is not the one 0013 said.

    Migration 0013 added `ix_job_claim_order (status, priority, id)` and
    said `ORDER BY priority, id` would be one forward scan of it. It is
    not: `status` leads that index and a claim matches two values of it,
    so the scan yields two ordered runs and PostgreSQL sorts them. The
    ordering is correct and costs exactly what ordering by `id` alone
    already cost; the index is simply not earning it.

    Sequential *and* bitmap scans are switched off here, which leaves the
    ordered index scan as the only plan available -- so a `Sort` under
    those conditions is not the planner preferring something on a
    twelve-row table, it is the planner having no way to avoid one.

    The assertion is deliberately about the sort and not about which index
    is chosen. With a handful of rows and a predicate on `status` alone,
    `ix_job_status` and `ix_job_claim_order` are equally good and the
    cheaper one wins by size; that choice is noise and a test asserting it
    would fail on the next row inserted. The sort is not noise.

    **This test fails the day somebody adds the partial index
    `claim_statement` recommends, and that is the point.** Read that
    docstring before changing anything here: ordering by `status` first
    would also make it pass, and would silently restore Defect 4.
    """
    await seed(factory, [ANNA, BEN], guild=GUILD, channel=CHANNEL)
    await seed(factory, [CARLA], guild=OTHER_GUILD, channel=OTHER_CHANNEL)

    plan = await explain_a_claim(factory)

    assert "Sort Key: transcription_job.priority, transcription_job.id" in plan, plan


async def explain_a_claim(factory: async_sessionmaker[AsyncSession]) -> str:
    """PostgreSQL's plan for the statement `claim` runs, as text.

    The statement comes from `JobQueue` itself rather than being rewritten
    here, because a plan for a hand-copied query would settle something
    about the copy.
    """
    statement = claim_statement(T0 - timedelta(seconds=DEFAULT_LEASE_SECONDS))
    async with factory() as session:
        connection = await session.connection()
        # Compiled against the dialect that will run it, with the values
        # written in: `EXPLAIN` takes a statement, not a statement and a
        # bag of parameters, and a plan for the wrong dialect's rendering
        # of `FOR UPDATE SKIP LOCKED` would be a plan for another query.
        compiled = statement.compile(
            dialect=connection.dialect, compile_kwargs={"literal_binds": True}
        )
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
        rows = await session.execute(text(f"EXPLAIN {compiled}"))
        return "\n".join(str(row[0]) for row in rows)
