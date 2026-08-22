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
from sturnus.application.worker import process_one, retry_pending_documents
from sturnus.domain import settings as domain_settings
from sturnus.domain.measurements import JobMeasurements
from sturnus.infrastructure.db.queue import ClaimedJob
from sturnus.infrastructure.documents.outline import PermanentDocumentError

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)

#: The guild every `FakeSessions` reports by default -- matches
#: `FakeConfig`'s default guild below, so a test that overrides neither
#: still exercises real guild-scoped config resolution rather than a
#: hardcoded shortcut.
GUILD = 1


class FakeQueue:
    def __init__(self, jobs: list[object]) -> None:
        self.jobs = list(jobs)
        self.completed: list[tuple[int, str]] = []
        self.failed: list[tuple[int, str]] = []
        self.last_is_final = False
        self.measured: list[JobMeasurements | None] = []

    async def claim(self) -> object | None:
        return self.jobs.pop(0) if self.jobs else None

    async def complete(
        self, job_id: int, transcript: str, measurements: JobMeasurements | None = None
    ) -> bool:
        self.completed.append((job_id, transcript))
        # Kept separately from `completed` so a test can assert on what was
        # measured without every existing assertion on that list having to
        # grow a third element it does not care about.
        self.measured.append(measurements)
        return self.last_is_final

    async def fail(self, job_id: int, error: str, _max_attempts: int) -> bool:
        self.failed.append((job_id, error))
        # Never dead: this fake counts no attempts, and the real
        # `JobQueue.fail` is where "out of attempts" is decided and tested
        # (`tests/infrastructure/test_queue.py`).
        return False


class FakeEngine:
    """`detected` is what the engine *reports back*, which is not the same
    as what it was asked for: it is deliberately different from the
    language the transcription tests configure, so a test asserting that a
    configured language is never overwritten by detection cannot pass by
    the two happening to agree.
    """

    def __init__(
        self,
        text: str = "spoken words",
        fail: bool = False,
        detected: str = "de",
        measurements: JobMeasurements | None = None,
    ) -> None:
        self.text = text
        self.fail = fail
        self.detected = detected
        #: Defaults to `None`, which is the honest default for a double
        #: that decodes nothing: an engine reports what it measured, and
        #: this one measured nothing. Tests that care pass a value.
        self.measurements = measurements
        self.calls: list[tuple[Path, str | None]] = []
        #: `initial_prompt` from every call, recorded separately from
        #: `calls` so the existing `calls[i][1]` language assertions stay
        #: as they are.
        self.prompts: list[str | None] = []
        #: What each call was asked to run with. `None` is the worker's
        #: own default, which is every job nobody asked a question about.
        self.models: list[str | None] = []

    async def transcribe(
        self,
        path: Path,
        language: str | None,
        initial_prompt: str | None,
        model: str | None = None,
    ) -> TranscriptionResult:
        self.calls.append((path, language))
        self.prompts.append(initial_prompt)
        self.models.append(model)
        if self.fail:
            raise RuntimeError("model exploded")
        return TranscriptionResult(
            segments=(TranscribedSegment(0.0, 1.0, self.text),),
            language=self.detected,
            measurements=self.measurements,
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
        #: `target` from every `create` call, recorded separately from
        #: `created` so existing `created[i][1]`-style body assertions stay
        #: unchanged while still letting a test check what target was used.
        self.targets: list[str] = []
        self.permanent_error = permanent_error

    async def create(self, title: str, body: str, target: str) -> CreatedDocument:
        if self.permanent_error:
            raise PermanentDocumentError(404)  # collection is gone
        self.created.append((title, body))
        self.targets.append(target)
        return CreatedDocument(id="doc-1", url="https://outline.example/doc/1")


class FakeSessions:
    """Also satisfies `sturnus.application.assembly.SessionReader`.

    Defaults describe a single participant (`discord_user_id=100`, matching
    the default `job()`), so every existing test -- none of which cares
    about the assembled document's content -- keeps working unchanged.
    """

    def __init__(self) -> None:
        self.languages: dict[int, str] = {}
        self.documented: list[tuple[int, str, str]] = []
        self.names: dict[int, str] = {100: "speaker-100"}
        self.epochs: dict[int, datetime] = {100: T0}
        self.bounds: tuple[datetime, datetime] = (T0, T0 + timedelta(hours=1))
        #: What `closed_undocumented_sessions` reports -- empty by default,
        #: since most tests never exercise `retry_pending_documents`.
        self.pending_retry: list[int] = []
        #: What `guild_id` reports for every session id -- matches
        #: `FakeConfig`'s default guild (module-level `GUILD`), so
        #: guild-scoped configuration resolves the same way in both fakes
        #: unless a test deliberately points them at different guilds.
        self.guild = GUILD

    async def detected_language(self, _session_id: int, user_id: int) -> str | None:
        return self.languages.get(user_id)

    async def set_detected_language(self, _session_id: int, user_id: int, lang: str) -> None:
        self.languages.setdefault(user_id, lang)

    async def mark_documented(self, session_id: int, _doc_id: str, url: str, provider: str) -> None:
        self.documented.append((session_id, url, provider))

    async def participant_names(self, _session_id: int) -> dict[int, str]:
        return self.names

    async def audio_epoch(self, _session_id: int, user_id: int) -> datetime | None:
        return self.epochs.get(user_id)

    async def session_bounds(self, _session_id: int) -> tuple[datetime, datetime]:
        return self.bounds

    async def closed_undocumented_sessions(self) -> list[int]:
        return self.pending_retry

    async def channel_ref(self, _session_id: int) -> tuple[int, int, str | None]:
        return (self.guild, 4711, "meeting-raum")

    async def guild_id(self, _session_id: int) -> int:
        return self.guild


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
    """Satisfies `sturnus.application.worker.LinkRepository`.

    Unlike `sturnus.application.assembly.LinkReader` (one arg), `provider`
    is a parameter of `external_identity` here, matching production: the
    worker resolves it per guild, at document-creation time, from
    `document_provider` (Spec 11) rather than fixing it once. `requested`
    records every provider actually asked for, so a test can assert the
    *configured* provider reached this call rather than some default.
    """

    def __init__(self, linked: dict[int, tuple[str, str]] | None = None) -> None:
        self._linked = linked or {}
        self.requested: list[str] = []

    async def external_identity(
        self, discord_user_id: int, provider: str
    ) -> tuple[str, str] | None:
        self.requested.append(provider)
        return self._linked.get(discord_user_id)


class FakeConfig:
    """Satisfies `sturnus.application.worker.ConfigReader`.

    Defaults to exactly what a fully-configured guild (`GUILD`) would
    report for the three settings `_create_session_document` resolves:
    `document_target`, `document_provider`, `merge_gap_seconds`. A test
    that overrides one of these -- rather than the fakes' hardcoded return
    values `process_one` used to be exercised against -- is what proves the
    *configured* value reaches its use, not a default baked into the fake.
    """

    def __init__(self, values: dict[tuple[int, str], str] | None = None) -> None:
        self._values = values or {
            (GUILD, domain_settings.DOCUMENT_TARGET): "col-default",
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
            (GUILD, domain_settings.MERGE_GAP_SECONDS): "15",
        }

    async def get(self, guild_id: int, key: str) -> str | None:
        return self._values.get((guild_id, key))


def guild_config(extra: dict[str, str] | None = None) -> FakeConfig:
    """A `FakeConfig` for `GUILD` whose document settings are already right.

    The transcription keys (`transcription_language`, `transcription_prompt`)
    are absent unless a test names them, which is what keeps the
    unconfigured path -- detect once, then pin per speaker -- exercised by
    every test that does not care about them.
    """
    values: dict[tuple[int, str], str] = {
        (GUILD, domain_settings.DOCUMENT_TARGET): "col-default",
        (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
        (GUILD, domain_settings.MERGE_GAP_SECONDS): "15",
    }
    values.update({(GUILD, key): value for key, value in (extra or {}).items()})
    return FakeConfig(values)


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
        "config": kw.get("config") or FakeConfig(),
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
    assert sessions.documented == [(1, "https://outline.example/doc/1", "outline")]


async def test_document_target_from_configuration_reaches_the_sink(tmp_path: Path) -> None:
    """`documents.create`'s `target` must be the guild's configured
    `document_target` (Spec 11, read via `ConfigReader`), not a value
    baked in anywhere else -- there is no global default collection, and a
    test that only ever exercised `FakeConfig`'s own default would not
    catch this reaching the sink at all.
    """
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents = FakeDocuments()
    config = FakeConfig(
        {
            (GUILD, domain_settings.DOCUMENT_TARGET): "guild-1-collection",
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
            (GUILD, domain_settings.MERGE_GAP_SECONDS): "15",
        }
    )
    await process_one(**run(tmp_path, queue=queue, documents=documents, config=config))
    assert documents.targets == ["guild-1-collection"]


async def test_a_missing_document_target_does_not_crash_the_worker(tmp_path: Path) -> None:
    """A guild that has not configured `document_target` yet (it is a
    required key with no default, Spec 11) must not crash the worker or be
    silently skipped forever: the transcription job itself already
    succeeded, so this is a transient document-creation failure exactly
    like any other -- `retry_pending_documents` tries again later, once an
    administrator sets it.
    """
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents = FakeDocuments()
    config = FakeConfig(
        {
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
            (GUILD, domain_settings.MERGE_GAP_SECONDS): "15",
        }
    )
    done = await process_one(**run(tmp_path, queue=queue, documents=documents, config=config))
    assert done is True
    assert documents.created == []


async def test_document_provider_from_configuration_selects_the_link_lookup(
    tmp_path: Path,
) -> None:
    """`assemble` must read a speaker's external identity through the
    guild's configured `document_provider` (Spec 11), not a provider
    hardcoded anywhere along the way -- a later Confluence adapter reads
    its own account-link mapping only if this reaches all the way through
    to `AccountLinkRepository.external_identity`.
    """
    queue = FakeQueue([job()])
    queue.last_is_final = True
    links = FakeLinks({100: ("conf-1", "Anna Confluence")})
    # `assemble` only asks `links` about speakers it has a transcript for
    # (see `sturnus.application.assembly.assemble`); the default `run()`
    # jobs fake is empty, which would make this test pass vacuously.
    jobs = FakeJobs(
        {100: TranscriptionResult(segments=(TranscribedSegment(0.0, 1.0, "hi"),), language="de")}
    )
    config = FakeConfig(
        {
            (GUILD, domain_settings.DOCUMENT_TARGET): "col-default",
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "confluence",
            (GUILD, domain_settings.MERGE_GAP_SECONDS): "15",
        }
    )
    sessions = FakeSessions()
    await process_one(
        **run(tmp_path, queue=queue, links=links, jobs=jobs, config=config, sessions=sessions)
    )
    assert links.requested == ["confluence"]
    # The same configured provider must be stamped on the session row --
    # `session.document_provider` is what a later re-publish or migration
    # reads back to find out which sink owns `document_id`.
    assert [provider for _, _, provider in sessions.documented] == ["confluence"]


async def test_merge_gap_seconds_from_configuration_reaches_assemble(tmp_path: Path) -> None:
    """`process_one` must read `merge_gap_seconds` from configuration and
    pass it through `assemble` to `build_transcript` -- not silently keep
    using `sturnus.domain.transcript.DEFAULT_MERGE_GAP` (15s). Proven the
    same way `test_merge_gap_is_read_from_the_caller_not_the_domain_default`
    (`tests/application/test_assembly.py`) proves it one layer down: the
    same 4-second pause between two same-speaker segments renders as one
    block under the default and two under a guild-configured 1-second gap.
    """

    def two_segments_four_seconds_apart() -> FakeJobs:
        return FakeJobs(
            {
                100: TranscriptionResult(
                    segments=(
                        TranscribedSegment(0.0, 1.0, "first"),
                        TranscribedSegment(5.0, 6.0, "second"),
                    ),
                    language="de",
                )
            }
        )

    default_documents = FakeDocuments()
    default_queue = FakeQueue([job(job_id=1)])
    default_queue.last_is_final = True
    await process_one(
        **run(
            tmp_path,
            queue=default_queue,
            documents=default_documents,
            jobs=two_segments_four_seconds_apart(),
        )
    )
    assert default_documents.created[0][1].count("·") == 1  # one block header

    configured_documents = FakeDocuments()
    configured_queue = FakeQueue([job(job_id=2)])
    configured_queue.last_is_final = True
    configured_config = FakeConfig(
        {
            (GUILD, domain_settings.DOCUMENT_TARGET): "col-default",
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
            (GUILD, domain_settings.MERGE_GAP_SECONDS): "1",
        }
    )
    await process_one(
        **run(
            tmp_path,
            queue=configured_queue,
            documents=configured_documents,
            jobs=two_segments_four_seconds_apart(),
            config=configured_config,
        )
    )
    assert configured_documents.created[0][1].count("·") == 2  # two block headers


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


async def test_the_guilds_configured_language_is_what_gets_transcribed(
    tmp_path: Path,
) -> None:
    """`transcription_language` (Spec 11) is passed straight to the engine.

    Naming the language is what stops the engine detecting one, and
    detection on a per-speaker track is a coin flip whenever the speaker's
    first job is short.
    """
    engine = FakeEngine()
    config = guild_config({domain_settings.TRANSCRIPTION_LANGUAGE: "de"})
    await process_one(**run(tmp_path, engine=engine, config=config))
    assert engine.calls[0][1] == "de"


async def test_a_configured_language_is_never_pinned_as_a_detection(
    tmp_path: Path,
) -> None:
    """Writing the configured value into `detected_language` would make the
    column mean two different things and would freeze the configuration as
    it stood on a session's first job: a guild that corrects the setting
    mid-session would keep getting the old language until the session ends.
    """
    engine, sessions = FakeEngine(detected="nl"), FakeSessions()
    config = guild_config({domain_settings.TRANSCRIPTION_LANGUAGE: "de"})
    await process_one(**run(tmp_path, engine=engine, sessions=sessions, config=config))
    assert sessions.languages == {}


async def test_a_configured_language_wins_over_an_earlier_detection(
    tmp_path: Path,
) -> None:
    """The stored detection is a guess; the configured value is a decision.

    This is the ordering that stops one bad detection -- pinned by an
    earlier job of the same session, before an administrator noticed and
    configured the language -- from governing every job after it.
    """
    engine, sessions = FakeEngine(), FakeSessions()
    sessions.languages[100] = "nl"
    config = guild_config({domain_settings.TRANSCRIPTION_LANGUAGE: "de"})
    await process_one(**run(tmp_path, engine=engine, sessions=sessions, config=config))
    assert engine.calls[0][1] == "de"


async def test_auto_asks_for_detection_and_pins_what_came_back(tmp_path: Path) -> None:
    """`auto` is how a genuinely multilingual guild opts back in.

    Without it the detect-and-pin path would be unreachable in production,
    since `transcription_language` has a default and clearing the key
    restores it rather than removing it.
    """
    engine, sessions = FakeEngine(detected="nl"), FakeSessions()
    config = guild_config({domain_settings.TRANSCRIPTION_LANGUAGE: "auto"})
    await process_one(**run(tmp_path, engine=engine, sessions=sessions, config=config))
    assert engine.calls[0][1] is None
    assert sessions.languages[100] == "nl"


async def test_a_blank_configured_language_asks_for_detection(tmp_path: Path) -> None:
    """`guild_config` is a table operators are told they may edit with SQL,
    which `ConfigStore.set`'s validation never sees. A blank value has to
    mean something, and the alternative -- handing `""` to the engine,
    which rejects it -- turns one careless `UPDATE` into every job of that
    guild failing.
    """
    engine = FakeEngine()
    config = guild_config({domain_settings.TRANSCRIPTION_LANGUAGE: "  "})
    await process_one(**run(tmp_path, engine=engine, config=config))
    assert engine.calls[0][1] is None


async def test_a_configured_language_is_stripped_before_the_engine_sees_it(
    tmp_path: Path,
) -> None:
    """`" de "` is not a language code faster-whisper knows, and a value
    typed with a trailing space is not a decision to fail every job.
    """
    engine = FakeEngine()
    config = guild_config({domain_settings.TRANSCRIPTION_LANGUAGE: " de "})
    await process_one(**run(tmp_path, engine=engine, config=config))
    assert engine.calls[0][1] == "de"


async def test_the_guilds_vocabulary_prompt_reaches_the_engine(tmp_path: Path) -> None:
    """`transcription_prompt` (Spec 11) is per-guild for the same reason
    `document_target` is: one worker process serves every guild, and whose
    project names matter is not knowable until a session is in hand.
    """
    engine = FakeEngine()
    config = guild_config({domain_settings.TRANSCRIPTION_PROMPT: "Ducula, Guira, Minestom."})
    await process_one(**run(tmp_path, engine=engine, config=config))
    assert engine.prompts == ["Ducula, Guira, Minestom."]


async def test_a_guild_with_no_prompt_configured_biases_nothing(tmp_path: Path) -> None:
    engine = FakeEngine()
    await process_one(**run(tmp_path, engine=engine, config=guild_config()))
    assert engine.prompts == [None]


async def test_the_audio_object_is_not_deleted_after_transcription(tmp_path: Path) -> None:
    """Audio outlives its transcription (Spec 12); the retention sweep deletes it."""
    store = FakeStore()
    await process_one(**run(tmp_path, store=store))
    assert store.deleted == []


class FailingStore:
    """Stands in for a failed S3 download -- neither `get` nor `delete` ever succeeds."""

    async def get(self, _key: str, _target: Path) -> None:
        raise RuntimeError("S3 is unreachable")

    async def delete(self, _key: str) -> None:
        raise AssertionError("never called by process_one")


class FailingDocuments:
    """A document sink that always raises something other than `PermanentDocumentError`."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, _title: str, _body: str, _target: str) -> CreatedDocument:
        self.calls += 1
        raise RuntimeError("Outline is briefly returning 502")


async def test_a_download_failure_fails_the_job_instead_of_crashing_the_worker(
    tmp_path: Path,
) -> None:
    """Defect 4: before this fix, anything but a transcription error (a
    failed S3 download, a decrypt error, ...) propagated straight out of
    `process_one` with nothing to catch it, killing the worker process and
    leaving the job stuck `running` forever.
    """
    queue = FakeQueue([job()])
    done = await process_one(**run(tmp_path, queue=queue, store=FailingStore()))
    assert done is True
    assert queue.completed == []
    assert len(queue.failed) == 1
    assert "S3 is unreachable" in queue.failed[0][1]


async def test_a_transient_document_error_does_not_crash_the_worker(tmp_path: Path) -> None:
    """Defect 4: a transient Outline error (anything other than
    `PermanentDocumentError`) must not propagate out of `process_one` and
    kill the worker. The transcription job itself already succeeded, so it
    must not be requeued either -- `retry_pending_documents`, not
    `queue.fail`, is what retries the document.
    """
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents = FailingDocuments()
    done = await process_one(**run(tmp_path, queue=queue, documents=documents))
    assert done is True
    assert len(queue.completed) == 1  # the transcription job is still done
    assert queue.failed == []  # not requeued -- there is nothing wrong with it
    assert documents.calls == 1


async def test_retry_pending_documents_retries_closed_undocumented_sessions() -> None:
    sessions = FakeSessions()
    sessions.pending_retry = [1]
    documents = FakeDocuments()
    jobs = FakeJobs(
        {
            100: TranscriptionResult(
                segments=(TranscribedSegment(0.0, 1.0, "hello again"),), language="de"
            )
        }
    )
    await retry_pending_documents(documents, sessions, jobs, FakeLinks(), FakeConfig())
    assert len(documents.created) == 1
    assert "hello again" in documents.created[0][1]
    assert sessions.documented == [(1, "https://outline.example/doc/1", "outline")]


async def test_retry_pending_documents_survives_one_sessions_failure() -> None:
    """One session still failing (Outline still down) must not stop the
    sweep from trying every other session in the same pass.
    """
    sessions = FakeSessions()
    sessions.pending_retry = [1, 2]
    documents = FailingDocuments()
    jobs = FakeJobs(
        {100: TranscriptionResult(segments=(TranscribedSegment(0.0, 1.0, "hi"),), language="de")}
    )
    # must not raise
    await retry_pending_documents(documents, sessions, jobs, FakeLinks(), FakeConfig())
    assert documents.calls == 2  # both sessions were attempted despite failing


async def test_retry_pending_documents_does_nothing_when_nothing_is_pending() -> None:
    documents = FakeDocuments()
    await retry_pending_documents(documents, FakeSessions(), FakeJobs(), FakeLinks(), FakeConfig())
    assert documents.created == []


async def test_the_document_is_written_in_the_guilds_timezone(tmp_path: Path) -> None:
    """The protocol's title carries the local start, not the cluster's."""
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents = FakeDocuments()
    config = FakeConfig(
        {
            (GUILD, domain_settings.DOCUMENT_TARGET): "col-1",
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
            (GUILD, domain_settings.TIMEZONE): "Europe/Berlin",
        }
    )
    await process_one(**run(tmp_path, queue=queue, documents=documents, config=config))

    # FakeSessions' bounds start at 20:00 UTC, which is 22:00 in Berlin.
    assert documents.created, "no document was created"
    assert "22:00" in documents.created[0][0]


async def test_an_unusable_timezone_falls_back_to_utc_rather_than_failing(
    tmp_path: Path,
) -> None:
    """A typo in configuration must not cost the protocol entirely: a wrong
    offset is recoverable, a missing document is not. The warning names the
    guild so someone can fix it.
    """
    queue = FakeQueue([job()])
    queue.last_is_final = True
    documents = FakeDocuments()
    config = FakeConfig(
        {
            (GUILD, domain_settings.DOCUMENT_TARGET): "col-1",
            (GUILD, domain_settings.DOCUMENT_PROVIDER): "outline",
            (GUILD, domain_settings.TIMEZONE): "Mars/Olympus_Mons",
        }
    )
    await process_one(**run(tmp_path, queue=queue, documents=documents, config=config))

    assert documents.created, "an unusable timezone must not prevent the document"
    assert "20:00" in documents.created[0][0]


async def test_the_worker_persists_what_the_engine_measured(tmp_path: Path) -> None:
    """The numbers reach the queue exactly as the engine reported them.

    The worker neither recomputes nor sanity-checks them, and that is the
    design: the gate's figures are not derivable from the segments, and a
    worker that tried would arrive at `max(segment.end)` -- the end of the
    last thing said, which on a track whose speaker fell silent halfway
    through is nowhere near the length of the recording.
    """
    queue = FakeQueue([job()])
    measured = JobMeasurements(audio_seconds=521.0, speech_seconds=88.5, segment_count=2)

    await process_one(**run(tmp_path, queue=queue, engine=FakeEngine(measurements=measured)))

    assert queue.measured == [measured]


async def test_an_engine_that_measured_nothing_completes_the_job_anyway(
    tmp_path: Path,
) -> None:
    """Measurement is not a precondition for a transcript.

    A backend handed audio rather than a file never sees the recording's
    length, and refusing to store its transcript over that would trade a
    working transcription for a missing statistic.
    """
    queue = FakeQueue([job()])

    await process_one(**run(tmp_path, queue=queue, engine=FakeEngine()))

    assert queue.measured == [None]
    assert len(queue.completed) == 1
