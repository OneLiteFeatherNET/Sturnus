from datetime import UTC, datetime, timedelta

from sturnus.application.retention import expired_jobs

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def job(job_id: int, until: datetime, deleted: datetime | None = None) -> dict[str, object]:
    return {"id": job_id, "retention_until": until, "audio_deleted_at": deleted}


def test_nothing_expires_before_its_time() -> None:
    assert expired_jobs([job(1, T0 + timedelta(days=1))], now=T0) == []


def test_an_expired_job_is_selected() -> None:
    assert [j["id"] for j in expired_jobs([job(1, T0 - timedelta(seconds=1))], now=T0)] == [1]


def test_an_already_deleted_job_is_not_selected_again() -> None:
    """Deleting twice is harmless but a re-run must not report work it did."""
    assert expired_jobs([job(1, T0 - timedelta(days=1), deleted=T0)], now=T0) == []


def test_the_boundary_is_inclusive_of_the_past_only() -> None:
    assert expired_jobs([job(1, T0)], now=T0) == []
