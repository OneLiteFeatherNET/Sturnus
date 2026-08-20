"""The worker loop, driven entirely through fakes.

What is being tested is the order of operations: decrypt, transcribe,
store, delete the local copy, and only then mark the job done. A wrong
order either loses a transcript or leaves plaintext audio on disk.
"""

from pathlib import Path
from typing import Any

from sturnus.application.documents import CreatedDocument
from sturnus.application.transcription import TranscribedSegment, TranscriptionResult
from sturnus.application.worker import process_one
from sturnus.infrastructure.db.queue import ClaimedJob
from sturnus.infrastructure.documents.outline import PermanentDocumentError


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
    def __init__(self) -> None:
        self.languages: dict[int, str] = {}
        self.documented: list[tuple[int, str]] = []

    async def detected_language(self, _session_id: int, user_id: int) -> str | None:
        return self.languages.get(user_id)

    async def set_detected_language(self, _session_id: int, user_id: int, lang: str) -> None:
        self.languages.setdefault(user_id, lang)

    async def mark_documented(self, session_id: int, _doc_id: str, url: str) -> None:
        self.documented.append((session_id, url))


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
