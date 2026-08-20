"""The worker loop, driven entirely through fakes.

What is being tested is the order of operations: decrypt, transcribe,
store, delete the local copy, and only then mark the job done. A wrong
order either loses a transcript or leaves plaintext audio on disk.

It also tests, from `test_a_multi_speaker_sessions_document_contains_every_speakers_text`
onward, that the document created for a session's last job is the real
*assembled* merge of every participant's stored transcript
(`sturnus.application.assembly.assemble`) rather than only the transcript
of whichever job happened to finish last.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sturnus.application.documents import CreatedDocument
from sturnus.application.transcription import TranscribedSegment, TranscriptionResult
from sturnus.application.worker import process_one
from sturnus.infrastructure.db.queue import ClaimedJob
from sturnus.infrastructure.documents.outline import PermanentDocumentError

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


class FakeQueue:
    def __init__(self, jobs: list[object]) -> None:
        self.jobs = list(jobs)
        self.completed: list[tuple[int, str]] = []
        self.failed: list[tuple[int, str]] = []
        self.last_is_final = False

    async def claim(self) -> object | None:
        return self.jobs.pop(0) if self.jobs else None

    async def complete(self, job_id: int, transcript: str) -> bool:
        self.completed.append((job_id, transcript))
        return self.last_is_final

    async def fail(self, job_id: int, error: str, _max_attempts: int) -> None:
        self.failed.append((job_id, error))


class FakeEngine:
    def __init__(self, text: str = "spoken words", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[tuple[Path, str | None]] = []

    async def transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        self.calls.append((path, language))
        if self.fail:
            raise RuntimeError("model exploded")
        return TranscriptionResult(
            segments=(TranscribedSegment(0.0, 1.0, self.text),), language="de"
        )


class FakeStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def get(self, _key: str, target: Path) -> None:
        target.write_bytes(b"encrypted")

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class FakeCrypto:
    """Stands in for unwrap-and-decrypt; writes a recognisable file."""

    def __init__(self) -> None:
        self.decrypted: list[Path] = []

    def decrypt_to(self, _source: Path, target: Path, _wrapped: bytes, _key_id: str) -> None:
        target.write_bytes(b"RIFFdecoded")
        self.decrypted.append(target)


class FakeDocuments:
    def __init__(self, permanent_error: bool = False) -> None:
        self.created: list[tuple[str, str]] = []
        self.permanent_error = permanent_error

    async def create(self, title: str, body: str) -> CreatedDocument:
        if self.permanent_error:
            raise PermanentDocumentError(404)  # collection is gone
        self.created.append((title, body))
        return CreatedDocument(id="doc-1", url="https://outline.example/doc/1")


class FakeSessions:
    """Also satisfies `sturnus.application.assembly.SessionReader`.

    Defaults describe a single participant (`discord_user_id=100`, matching
    the default `job()`), so every existing test -- none of which cares
    about the assembled document's content -- keeps working unchanged.
    """

    def __init__(self) -> None:
        self.languages: dict[int, str] = {}
        self.documented: list[tuple[int, str]] = []
        self.names: dict[int, str] = {100: "speaker-100"}
        self.epochs: dict[int, datetime] = {100: T0}
        self.bounds: tuple[datetime, datetime] = (T0, T0 + timedelta(hours=1))

    async def detected_language(self, _session_id: int, user_id: int) -> str | None:
        return self.languages.get(user_id)

    async def set_detected_language(self, _session_id: int, user_id: int, lang: str) -> None:
        self.languages.setdefault(user_id, lang)

    async def mark_documented(self, session_id: int, _doc_id: str, url: str) -> None:
        self.documented.append((session_id, url))

    async def participant_names(self, _session_id: int) -> dict[int, str]:
        return self.names

    async def audio_epoch(self, _session_id: int, user_id: int) -> datetime | None:
        return self.epochs.get(user_id)

    async def session_bounds(self, _session_id: int) -> tuple[datetime, datetime]:
        return self.bounds


class FakeJobs:
    """Satisfies `sturnus.application.assembly.JobReader`.

    Stands in for what is already persisted in `transcription_job` by the
    time `assemble` reads it -- independent of whatever `FakeQueue`
    records in the same test, exactly as `JobRepository.transcripts_for`
    (real DB rows) is independent of `JobQueue.complete` (a different
    repository over the same table) in production.
    """

    def __init__(self, per_speaker: dict[int, TranscriptionResult] | None = None) -> None:
        self._per_speaker = per_speaker or {}

    async def transcripts_for(self, _session_id: int) -> dict[int, TranscriptionResult]:
        return self._per_speaker


class FakeLinks:
    """Satisfies `sturnus.application.assembly.LinkReader`."""

    def __init__(self, linked: dict[int, tuple[str, str]] | None = None) -> None:
        self._linked = linked or {}

    async def external_identity(self, discord_user_id: int) -> tuple[str, str] | None:
        return self._linked.get(discord_user_id)


def job(job_id: int = 1, session_id: int = 1, user_id: int = 100) -> ClaimedJob:
    return ClaimedJob(
        id=job_id,
        session_id=session_id,
        discord_user_id=user_id,
        s3_key=f"sessions/{session_id}/speakers/{user_id}.enc",
        encryption_key_id="k1",
        wrapped_data_key=b"wrapped",
    )


def run(tmp_path: Path, **kw: Any) -> dict[str, Any]:
    """Assemble the call arguments; every collaborator defaults to a fake."""
    return {
        "queue": kw.get("queue") or FakeQueue([job()]),
        "engine": kw.get("engine") or FakeEngine(),
        "store": kw.get("store") or FakeStore(),
        "crypto": kw.get("crypto") or FakeCrypto(),
        "documents": kw.get("documents") or FakeDocuments(),
        "sessions": kw.get("sessions") or FakeSessions(),
        "jobs": kw.get("jobs") or FakeJobs(),
        "links": kw.get("links") or FakeLinks(),
        "work_dir": tmp_path,
        "max_attempts": 3,
    }


async def test_an_empty_queue_reports_no_work(tmp_path: Path) -> None:
    """The loop needs this to back off instead of spinning on an idle queue."""
    assert await process_one(**run(tmp_path, queue=FakeQueue([]))) is False


async def test_a_successful_job_is_completed_with_its_transcript(tmp_path: Path) -> None:
    queue = FakeQueue([job()])
    done = await process_one(**run(tmp_path, queue=queue, engine=FakeEngine("spoken words")))
    assert done is True
    assert len(queue.completed) == 1
    assert "spoken words" in queue.completed[0][1]
    assert queue.failed == []


async def test_no_plaintext_audio_survives_a_successful_job(tmp_path: Path) -> None:
    """Decrypted speech left on disk is what the encryption exists to prevent."""
    await process_one(**run(tmp_path))
    assert list(tmp_path.glob("**/*.wav")) == []
    assert list(tmp_path.glob("**/*.enc")) == []


async def test_no_plaintext_audio_survives_a_failed_job(tmp_path: Path) -> None:
    """The cleanup must sit in a finally, not on the happy path."""
    await process_one(**run(tmp_path, engine=FakeEngine(fail=True)))
    assert list(tmp_path.glob("**/*.wav")) == []
    assert list(tmp_path.glob("**/*.enc")) == []


async def test_a_transcription_error_fails_the_job_rather_than_completing_it(
    tmp_path: Path,
) -> None:
    queue = FakeQueue([job()])
    await process_one(**run(tmp_path, queue=queue, engine=FakeEngine(fail=True)))
    assert queue.completed == []
    assert len(queue.failed) == 1
    assert "model exploded" in queue.failed[0][1]


async def test_the_last_job_of_a_session_creates_the_document(tmp_path: Path) -> None:
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents, sessions = FakeDocuments(), FakeSessions()
    await process_one(**run(tmp_path, queue=queue, documents=documents, sessions=sessions))
    assert len(documents.created) == 1
    assert sessions.documented == [(1, "https://outline.example/doc/1")]


async def test_a_single_speakers_document_contains_their_transcript(tmp_path: Path) -> None:
    """The base case (Spec 8.3): a session with one speaker still gets a
    document containing that speaker's assembled words."""
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents = FakeDocuments()
    jobs = FakeJobs(
        {
            100: TranscriptionResult(
                segments=(TranscribedSegment(0.0, 1.0, "only speaker talking"),), language="de"
            )
        }
    )
    await process_one(**run(tmp_path, queue=queue, documents=documents, jobs=jobs))
    assert len(documents.created) == 1
    assert "only speaker talking" in documents.created[0][1]


async def test_a_multi_speaker_sessions_document_contains_both_speakers_text(
    tmp_path: Path,
) -> None:
    """Regression test for the defect this task fixes.

    `_create_session_document` must call `sturnus.application.assembly.
    assemble` to merge every participant's stored transcript, not build
    the document from only the one job that happened to finish the
    session. Multi-speaker sessions are the entire reason speakers are
    recorded separately -- a document with only one person's words is
    wrong even though nothing raises.
    """
    queue = FakeQueue([job(job_id=2, session_id=1, user_id=200)])
    queue.last_is_final = True
    sessions = FakeSessions()
    sessions.names = {100: "anna", 200: "ben"}
    sessions.epochs = {100: T0, 200: T0 + timedelta(seconds=30)}
    documents = FakeDocuments()
    jobs = FakeJobs(
        {
            100: TranscriptionResult(
                segments=(TranscribedSegment(0.0, 1.0, "anna spoke first"),), language="de"
            ),
            200: TranscriptionResult(
                segments=(TranscribedSegment(0.0, 1.0, "ben spoke second"),), language="de"
            ),
        }
    )
    await process_one(
        **run(tmp_path, queue=queue, documents=documents, sessions=sessions, jobs=jobs)
    )
    assert len(documents.created) == 1
    body = documents.created[0][1]
    assert "anna spoke first" in body
    assert "ben spoke second" in body
    # Chronological order: anna's epoch is 30 seconds before ben's.
    assert body.index("anna spoke first") < body.index("ben spoke second")


async def test_a_dead_speakers_job_does_not_prevent_others_from_appearing(
    tmp_path: Path,
) -> None:
    """A speaker whose job exhausted its retries and went `dead`
    (`JobQueue.fail`) has no transcript for `JobRepository.transcripts_for`
    to read -- it only reads `done` jobs -- but that must not stop the
    session's other speakers from appearing in the document.
    """
    queue = FakeQueue([job()])  # anna (100) completes normally and is last
    queue.last_is_final = True
    sessions = FakeSessions()
    sessions.names = {100: "anna", 200: "ben"}
    # Ben is a known participant (he has an audio epoch) but his job never
    # reached `done`, so he has no entry in `jobs` -- exactly what a `dead`
    # job looks like to `assemble`.
    sessions.epochs = {100: T0, 200: T0 + timedelta(seconds=30)}
    documents = FakeDocuments()
    jobs = FakeJobs(
        {
            100: TranscriptionResult(
                segments=(TranscribedSegment(0.0, 1.0, "anna is still here"),), language="de"
            )
        }
    )
    await process_one(
        **run(tmp_path, queue=queue, documents=documents, sessions=sessions, jobs=jobs)
    )
    assert len(documents.created) == 1
    assert "anna is still here" in documents.created[0][1]


async def test_a_job_that_is_not_the_last_creates_nothing(tmp_path: Path) -> None:
    queue = FakeQueue([job()])
    queue.last_is_final = False
    documents = FakeDocuments()
    await process_one(**run(tmp_path, queue=queue, documents=documents))
    assert documents.created == []


async def test_a_permanent_document_error_does_not_requeue_the_job(tmp_path: Path) -> None:
    """Retrying against a deleted collection forever would never drain the queue."""
    queue = FakeQueue([job()])
    queue.last_is_final = True
    await process_one(**run(tmp_path, queue=queue, documents=FakeDocuments(permanent_error=True)))
    assert queue.failed == [] or "permanent" in queue.failed[0][1].lower()


async def test_the_first_job_of_a_speaker_detects_and_stores_the_language(
    tmp_path: Path,
) -> None:
    engine, sessions = FakeEngine(), FakeSessions()
    await process_one(**run(tmp_path, engine=engine, sessions=sessions))
    assert engine.calls[0][1] is None  # nothing pinned yet
    assert sessions.languages[100] == "de"


async def test_a_later_job_pins_the_stored_language(tmp_path: Path) -> None:
    """Detecting per job would let one speaker change language mid-protocol."""
    engine, sessions = FakeEngine(), FakeSessions()
    sessions.languages[100] = "de"
    await process_one(**run(tmp_path, engine=engine, sessions=sessions))
    assert engine.calls[0][1] == "de"


async def test_the_audio_object_is_not_deleted_after_transcription(tmp_path: Path) -> None:
    """Audio outlives its transcription (Spec 12); the retention sweep deletes it."""
    store = FakeStore()
    await process_one(**run(tmp_path, store=store))
    assert store.deleted == []
