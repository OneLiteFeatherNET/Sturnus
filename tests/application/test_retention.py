from datetime import UTC, datetime, timedelta

from sturnus.application.retention import expired_jobs, sweep_expired_audio

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def job(
    job_id: int,
    until: datetime,
    deleted: datetime | None = None,
    s3_key: str | None = None,
) -> dict[str, object]:
    return {
        "id": job_id,
        "retention_until": until,
        "audio_deleted_at": deleted,
        "s3_key": s3_key or f"sessions/1/speakers/{job_id}.enc",
    }


def test_nothing_expires_before_its_time() -> None:
    assert expired_jobs([job(1, T0 + timedelta(days=1))], now=T0) == []


def test_an_expired_job_is_selected() -> None:
    assert [j["id"] for j in expired_jobs([job(1, T0 - timedelta(seconds=1))], now=T0)] == [1]


def test_an_already_deleted_job_is_not_selected_again() -> None:
    """Deleting twice is harmless but a re-run must not report work it did."""
    assert expired_jobs([job(1, T0 - timedelta(days=1), deleted=T0)], now=T0) == []


def test_the_boundary_is_inclusive_of_the_past_only() -> None:
    assert expired_jobs([job(1, T0)], now=T0) == []


# ---------------------------------------------------------------------------
# `sweep_expired_audio` -- the periodic sweep that actually calls
# `expired_jobs`, deletes each object, and stamps `audio_deleted_at`.
# ---------------------------------------------------------------------------


class FakeJobs:
    def __init__(self, candidates: list[dict[str, object]] | None = None) -> None:
        self._candidates = candidates or []
        self.deleted: list[int] = []

    async def candidates_for_retention(self) -> list[dict[str, object]]:
        return self._candidates

    async def mark_audio_deleted(self, job_id: int, _now: datetime) -> None:
        self.deleted.append(job_id)


class FakeStore:
    def __init__(self, fail_keys: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self._fail_keys = fail_keys or set()

    async def delete(self, key: str) -> None:
        if key in self._fail_keys:
            raise RuntimeError("S3 is briefly unreachable")
        self.deleted.append(key)


async def test_sweep_expired_audio_deletes_and_stamps_an_expired_job() -> None:
    jobs = FakeJobs([job(1, T0 - timedelta(seconds=1))])
    store = FakeStore()
    await sweep_expired_audio(jobs, store, T0)
    assert store.deleted == ["sessions/1/speakers/1.enc"]
    assert jobs.deleted == [1]


async def test_sweep_expired_audio_skips_a_job_not_yet_expired() -> None:
    jobs = FakeJobs([job(1, T0 + timedelta(days=1))])
    store = FakeStore()
    await sweep_expired_audio(jobs, store, T0)
    assert store.deleted == []
    assert jobs.deleted == []


async def test_sweep_expired_audio_does_not_stamp_a_job_whose_delete_failed() -> None:
    """A failed delete must be retried on the next sweep, not silently
    recorded as done."""
    jobs = FakeJobs([job(1, T0 - timedelta(seconds=1), s3_key="k1")])
    store = FakeStore(fail_keys={"k1"})
    await sweep_expired_audio(jobs, store, T0)  # must not raise
    assert store.deleted == []
    assert jobs.deleted == []


async def test_sweep_expired_audio_survives_one_jobs_failure() -> None:
    """One unreachable object must not stop every other job's retention
    from being enforced in the same sweep.
    """
    jobs = FakeJobs(
        [
            job(1, T0 - timedelta(seconds=1), s3_key="fails"),
            job(2, T0 - timedelta(seconds=1), s3_key="succeeds"),
        ]
    )
    store = FakeStore(fail_keys={"fails"})
    await sweep_expired_audio(jobs, store, T0)  # must not raise
    assert store.deleted == ["succeeds"]
    assert jobs.deleted == [2]
