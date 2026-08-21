"""What `plan_requeue` decides, over plain dicts and without a database.

Every sentence `/queue requeue` says to an administrator is derived from a
`RequeuePlan`, so the rules are pinned here -- once -- rather than through
an `Interaction` and a Postgres container. `tests/infrastructure/discord/
test_queue_cog.py` covers the writes that follow from a plan; this file
covers the decision itself.
"""

from datetime import UTC, datetime

from sturnus.application.requeue import RequeuePlan, plan_requeue

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA, BEN, CLARA = 100, 200, 300


def job(
    job_id: int,
    user_id: int,
    status: str = "done",
    audio_deleted_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": job_id,
        "discord_user_id": user_id,
        "status": status,
        "audio_deleted_at": audio_deleted_at,
    }


def test_every_finished_job_with_audio_is_resettable() -> None:
    plan = plan_requeue([job(1, ANNA), job(2, BEN)])

    assert plan.resettable_job_ids == (1, 2)
    assert plan.erased_user_ids == ()
    assert plan.active_user_ids == ()
    assert plan.is_blocked is False
    assert plan.is_empty is False


def test_a_dead_job_is_terminal_and_therefore_resettable() -> None:
    """`dead` only means "gave up after `max_attempts`", never "unusable".

    A job that died because the *old* code path could not make sense of the
    audio is precisely the job an administrator wants re-run against the
    new one, so `dead` must not be mistaken for a reason to refuse.
    """
    plan = plan_requeue([job(1, ANNA, status="dead")])

    assert plan.resettable_job_ids == (1,)
    assert plan.is_blocked is False


def test_a_job_whose_audio_is_erased_is_skipped_rather_than_reset() -> None:
    """Re-queueing it would hand a worker an S3 key that no longer resolves.

    `queue.fail` would fire, `attempts` would climb, the job would go
    `dead` again, and the only product would be log noise -- so the plan
    names the speaker instead, and the reply tells the administrator their
    old transcript is carried into the new document unchanged.
    """
    plan = plan_requeue([job(1, ANNA), job(2, BEN, audio_deleted_at=T0)])

    assert plan.resettable_job_ids == (1,)
    assert plan.erased_user_ids == (BEN,)
    assert plan.is_blocked is False
    assert plan.is_empty is False


def test_a_running_job_blocks_the_whole_session() -> None:
    """Resetting a `running` job would be undone by the worker holding it.

    That worker still calls `complete()` when it finishes, which writes the
    old run's transcript and flips the row back to `done` -- silently
    reverting the reset while the administrator has been told it happened.
    """
    plan = plan_requeue([job(1, ANNA), job(2, BEN, status="running")])

    assert plan.active_user_ids == (BEN,)
    assert plan.is_blocked is True


def test_a_pending_job_blocks_the_whole_session() -> None:
    """It is already going to be transcribed; there is nothing to re-queue."""
    plan = plan_requeue([job(1, ANNA, status="pending")])

    assert plan.active_user_ids == (ANNA,)
    assert plan.is_blocked is True


def test_an_active_job_is_reported_as_active_even_if_its_audio_is_gone() -> None:
    """The blocking classification wins, because it is the one that refuses.

    A job can be `pending` with `audio_deleted_at` set -- the retention
    sweep does not consult job status. Reporting it as merely "skipped"
    would let the session through while a worker is still holding one of
    its jobs, which is the case the refusal exists for.
    """
    plan = plan_requeue([job(1, ANNA, status="running", audio_deleted_at=T0)])

    assert plan.active_user_ids == (ANNA,)
    assert plan.erased_user_ids == ()
    assert plan.is_blocked is True


def test_a_session_whose_audio_is_all_erased_has_nothing_to_reset() -> None:
    """Reporting success while changing nothing is the failure to avoid."""
    plan = plan_requeue([job(1, ANNA, audio_deleted_at=T0), job(2, BEN, audio_deleted_at=T0)])

    assert plan.resettable_job_ids == ()
    assert plan.erased_user_ids == (ANNA, BEN)
    assert plan.is_empty is True
    assert plan.is_blocked is False


def test_a_session_with_no_jobs_at_all_is_empty() -> None:
    """Nobody ever spoke, so there is no recording to transcribe again."""
    plan = plan_requeue([])

    assert plan == RequeuePlan(resettable_job_ids=(), erased_user_ids=(), active_user_ids=())
    assert plan.is_empty is True
    assert plan.is_blocked is False


def test_the_plan_is_ordered_by_job_id_whatever_order_the_rows_arrive_in() -> None:
    """The confirmation text is read by a human and must not reshuffle.

    Two runs of the same command against the same unchanged session have
    to produce the same sentence, or an administrator comparing them
    cannot tell a real change from row order.
    """
    plan = plan_requeue(
        [
            job(3, CLARA, audio_deleted_at=T0),
            job(1, ANNA),
            job(2, BEN, audio_deleted_at=T0),
        ]
    )

    assert plan.resettable_job_ids == (1,)
    assert plan.erased_user_ids == (BEN, CLARA)
