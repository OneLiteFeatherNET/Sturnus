from datetime import UTC, datetime, timedelta

from sturnus.application.retention import expired_jobs, sweep_expired_audio
from sturnus.infrastructure.crypto import KeyWrapper
from sturnus.infrastructure.documents.artefacts import SealedArtefacts

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def job(
    job_id: int,
    until: datetime,
    deleted: datetime | None = None,
    s3_key: str | None = None,
    spectrogram_key: str | None = None,
) -> dict[str, object]:
    """One row as `candidates_for_retention` shapes it.

    `spectrogram_key` defaults to `None`, which is what the column holds
    for every job whose guild never asked for spectrograms -- so the
    sweep's ordinary case stays the one most of these tests exercise.
    """
    return {
        "id": job_id,
        "retention_until": until,
        "audio_deleted_at": deleted,
        "s3_key": s3_key or f"sessions/1/speakers/{job_id}.enc",
        "spectrogram_key": spectrogram_key,
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


# ---------------------------------------------------------------------------
# The rule a stored spectrogram exists under
#
# A spectrogram is a rendering of when somebody spoke and for how long. It
# is less than the audio and it is not nothing, and this sweep is the only
# thing that ends a recording's life -- so it has to end the picture's too,
# or `spectrograms_by_default` becomes a switch that quietly makes
# something about a person's voice outlive their recording's retention.
# ---------------------------------------------------------------------------


async def test_a_swept_job_loses_its_spectrogram_in_the_same_pass() -> None:
    """Both objects, one pass. Not a second sweep that could be forgotten."""
    jobs = FakeJobs([job(1, T0 - timedelta(seconds=1), s3_key="a", spectrogram_key="a.spec")])
    store = FakeStore()

    await sweep_expired_audio(jobs, store, T0)

    assert store.deleted == ["a", "a.spec"]
    assert jobs.deleted == [1]


async def test_a_job_with_no_spectrogram_is_swept_exactly_as_before() -> None:
    """Most jobs have no picture, and the sweep must not invent a key for
    them: a derived key would send a `DELETE` for an object that was never
    written, on every job, for ever."""
    jobs = FakeJobs([job(1, T0 - timedelta(seconds=1), s3_key="a")])
    store = FakeStore()

    await sweep_expired_audio(jobs, store, T0)

    assert store.deleted == ["a"]
    assert jobs.deleted == [1]


async def test_a_spectrogram_that_would_not_delete_leaves_the_job_unstamped() -> None:
    """The stamp is the claim that this recording is gone -- all of it.

    A stamp written while the picture is still in the bucket would end the
    job's candidacy for ever and leave the artefact behind with nothing
    left that knows to delete it. Leaving the row unstamped costs one
    repeated `DELETE` of an object already gone, which S3 answers
    successfully.
    """
    jobs = FakeJobs([job(1, T0 - timedelta(seconds=1), s3_key="a", spectrogram_key="fails")])
    store = FakeStore(fail_keys={"fails"})

    await sweep_expired_audio(jobs, store, T0)  # must not raise

    assert store.deleted == ["a"]
    assert jobs.deleted == []


# ---------------------------------------------------------------------------
# The rule a stored *protocol* exists under, which is the opposite one
#
# A protocol is not audio. `audio_retention_days` governs the recording,
# and the record of the meeting deliberately outlives it: a transcript
# answers `200` with `audio_available: false` rather than `404` for exactly
# that reason. So this sweep must not touch a session's stored protocol,
# and -- the half that is invisible from the sweep -- a stored protocol
# must not be sealed under anything the sweep destroys.
#
# Sealing an export artefact under the session data key would satisfy every
# other test in this file and make every stored Markdown and HTML export
# unreadable on day thirty-one, silently, noticed by whoever next opened an
# old document. These two tests are what that mistake has to get past.
# ---------------------------------------------------------------------------

MASTER = b"0" * 32
GUILD = 4711
PROTOCOL_KEY = "protocols/1/7.md"


class FakeObjects:
    """The bucket, as a dictionary, for the two things stored in it."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, body: bytes, _content_type: str) -> None:
        self.objects[key] = body

    async def get(self, key: str) -> bytes:
        return self.objects[key]


async def test_the_sweep_does_not_delete_a_sessions_stored_protocol() -> None:
    """The sweep deletes what the job names -- its audio and its picture --
    and nothing else in the bucket.

    Adding the protocol to this list would be deleting the record of a
    meeting because the recording of it expired, which is not what
    `audio_retention_days` is."""
    jobs = FakeJobs([job(1, T0 - timedelta(seconds=1), s3_key="a", spectrogram_key="a.spec")])
    store = FakeStore()

    await sweep_expired_audio(jobs, store, T0)

    assert PROTOCOL_KEY not in store.deleted
    assert store.deleted == ["a", "a.spec"]


async def test_a_stored_protocol_still_opens_after_its_recording_is_swept() -> None:
    """The failure this pins is a data-loss bug wearing the costume of a
    security fix.

    An export artefact is sealed under a data key of its own, wrapped under
    the master key and bound to the guild and the purpose -- never under
    the session data key the sweep's job row carries. Everything that key
    depends on is destroyed here first: the audio object, the picture, and
    the row's own key material. The protocol still opens, with the master
    key and the guild and nothing else."""
    artefacts = SealedArtefacts(FakeObjects(), KeyWrapper(master_key=MASTER, key_id="k1"))
    await artefacts.put_sealed(PROTOCOL_KEY, b"# Minutes\n", guild_id=GUILD)

    expired = job(1, T0 - timedelta(seconds=1), s3_key="a", spectrogram_key="a.spec")
    jobs = FakeJobs([expired])
    await sweep_expired_audio(jobs, FakeStore(), T0)
    # What the sweep leaves behind today, plus the obvious hardening of it
    # that a later change would make: the row keeps no key material for a
    # recording that no longer exists. Either way the protocol is not
    # holding on to it.
    expired["wrapped_data_key"] = None
    expired["encryption_key_id"] = None

    assert await artefacts.get(PROTOCOL_KEY, guild_id=GUILD) == b"# Minutes\n"
