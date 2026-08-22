"""The canary test: drive the real pipelines and grep the logs for payload.

`docs/verification/end-to-end-checklist.md` makes it a blocking legal gate
that no transcript, audio, token or key appears in any pod log. That gate is
executed by hand, once, after a deploy. This is the same check, run on every
commit, against the two code paths that actually touch the protected data:
`RecordingService.close()` and `process_one()`.

It deliberately does **not** know which fields those paths log. It plants
unique strings in the places a payload lives -- the transcript text, a
display name, the audio bytes -- and asserts none of them survives anywhere
in the output at DEBUG. That is what makes it catch a leak through a
variable no denylist happens to name, which is precisely the case the AST
test in `tests/test_logging_discipline.py` cannot see.

Extends the precedent already set by `tests/infrastructure/test_outline.py`,
which asserts the same property for one adapter with `caplog`.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sturnus.application.recording import RecordingService
from sturnus.application.worker import process_one
from sturnus.domain.session import EndReason, SessionTimeouts
from sturnus.observability.setup import configure_logging
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
    job,
)

#: Each is unique, so a failure message names exactly what escaped.
TRANSCRIPT_CANARY = "CANARYTRANSCRIPT-the-merger-closes-on-the-fourteenth"
DISPLAY_NAME_CANARY = "CANARYDISPLAYNAME-Dr-Alice-Example"
AUDIO_CANARY = b"CANARYAUDIO" * 64
MASTER_KEY_CANARY = "CANARYMASTERKEY-cGxlYXNlIGRvIG5vdCBsb2cgbWU"
#: A voice channel's name is free text an administrator chose, and since
#: `#34` it travels from the bot into the protocol header. It is not a
#: registered field, so it must not reach a log line either.
CHANNEL_NAME_CANARY = "CANARYCHANNELNAME-Vorstandssitzung-vertraulich"

T0 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Everything the process would write to stdout, at DEBUG."""
    buffer = io.StringIO()
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    configure_logging("worker", level="DEBUG", log_format="json", stream=buffer)
    yield buffer
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _assert_no_canaries(captured: io.StringIO) -> None:
    output = captured.getvalue()
    assert output, "nothing was logged; the test would pass vacuously"
    for name, canary in (
        ("transcript", TRANSCRIPT_CANARY),
        ("display name", DISPLAY_NAME_CANARY),
        ("master key", MASTER_KEY_CANARY),
        ("channel name", CHANNEL_NAME_CANARY),
    ):
        assert canary not in output, f"a {name} reached the log stream"
    assert "CANARYAUDIO" not in output, "audio bytes reached the log stream"
    # Every line must still be parseable -- a leak that also breaks the
    # format would otherwise hide behind a parse error.
    for line in output.splitlines():
        if line.strip():
            json.loads(line)


async def test_the_worker_pipeline_logs_no_payload(tmp_path: Path, captured: io.StringIO) -> None:
    """`process_one` end to end, with the transcript replaced by a canary."""
    queue = FakeQueue([job()])
    queue.last_is_final = True
    sessions = FakeSessions()
    sessions.names = {100: DISPLAY_NAME_CANARY}

    await process_one(
        queue=queue,
        engine=FakeEngine(TRANSCRIPT_CANARY),
        store=FakeStore(),
        crypto=FakeCrypto(),
        documents=FakeDocuments(),
        sessions=sessions,
        jobs=FakeJobs(),
        links=FakeLinks(),
        config=FakeConfig(),
        work_dir=tmp_path,
        max_attempts=3,
    )

    # The canary really did flow through the code under test.
    assert TRANSCRIPT_CANARY in queue.completed[0][1]
    _assert_no_canaries(captured)


async def test_a_failing_job_logs_no_payload(tmp_path: Path, captured: io.StringIO) -> None:
    """The failure path is where a message most wants to carry a payload."""

    class ExplodingEngine:
        # Three parameters, matching `application.transcription.
        # TranscriptionEngine` since main added the per-guild vocabulary
        # prompt (#44). A two-parameter fake would make `process_one` raise
        # `TypeError` before the engine ever ran, so the canary this test
        # plants would never be in the message it checks -- the test would
        # go on passing while measuring nothing.
        async def transcribe(
            self,
            path: Path,
            language: str | None,
            initial_prompt: str | None,
            model: str | None = None,
        ) -> object:
            del path, language, initial_prompt, model
            raise RuntimeError(f"decode failed on {TRANSCRIPT_CANARY}")

    queue = FakeQueue([job()])
    await process_one(
        queue=queue,
        engine=ExplodingEngine(),  # type: ignore[arg-type]
        store=FakeStore(),
        crypto=FakeCrypto(),
        documents=FakeDocuments(),
        sessions=FakeSessions(),
        jobs=FakeJobs(),
        links=FakeLinks(),
        config=FakeConfig(),
        work_dir=tmp_path,
        max_attempts=3,
    )

    assert len(queue.failed) == 1
    # The database column still gets the full message -- that is the point
    # of withholding it from the log rather than destroying it.
    assert TRANSCRIPT_CANARY in queue.failed[0][1]
    _assert_no_canaries(captured)


class _Writer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._chunks: list[bytes] = []

    def write(self, at: datetime, pcm: bytes) -> None:
        del at
        self._chunks.append(pcm)

    def close(self) -> None:
        self.path.write_bytes(b"".join(self._chunks))


class _WriterFactory:
    def __init__(self, root: Path) -> None:
        self._root = root

    def open(self, session_id: int, discord_user_id: int, epoch: datetime) -> _Writer:
        del epoch
        directory = self._root / str(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        return _Writer(directory / f"{discord_user_id}.wav")


class _Encryptor:
    key_id = "canary-key-1"

    def new_session_key(self) -> object:
        from sturnus.application.ports import SessionKey

        return SessionKey(plaintext=MASTER_KEY_CANARY.encode(), wrapped=MASTER_KEY_CANARY.encode())

    def encrypt(self, source: Path, target: Path, key: bytes) -> None:
        del key
        target.write_bytes(b"encrypted:" + source.read_bytes())


class _Sessions:
    def __init__(self) -> None:
        self.opened = False

    async def open_session(
        self, guild_id: int, channel_id: int, channel_name: str | None, now: datetime
    ) -> int:
        # `channel_name` arrived with the protocol header work on main
        # (#34): the worker writes the room's name into the document and
        # only the bot can resolve it. The callers below pass
        # `CHANNEL_NAME_CANARY` rather than `None`, because a fake that
        # never sees a real name would not be exercising the case that
        # matters.
        del guild_id, channel_id, channel_name, now
        self.opened = True
        return 4711

    async def add_participant(
        self, session_id: int, discord_user_id: int, display_name: str, now: datetime
    ) -> None: ...

    async def set_audio_epoch(
        self, session_id: int, discord_user_id: int, at: datetime
    ) -> None: ...

    async def record_silent_audio(
        self, session_id: int, discord_user_id: int, at: datetime
    ) -> None: ...

    async def close_session(self, session_id: int, ended_at: datetime, reason: str) -> None: ...

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None: ...

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:
        del session_id
        return None

    async def session_status(self, session_id: int) -> str | None:
        del session_id
        return "open"


class _Announcer:
    """The `Announcer` port, which is not a log sink and must not become one.

    `RecordingService` speaks into the voice channel once per session, about
    a speaker whose audio carries no level. That message names a Discord user
    id -- deliberately, because the room has to know whose microphone it is --
    and the channel is the only place it is allowed to appear. Recording the
    posts here rather than dropping them is what lets the canary sweep below
    see everything the service produced, log line or not.
    """

    def __init__(self) -> None:
        self.posted: list[tuple[int, str]] = []

    async def post(self, channel_id: int, text: str) -> None:
        self.posted.append((channel_id, text))


class _Jobs:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    async def enqueue(self, **kwargs: object) -> int:
        self.enqueued.append(kwargs)
        return len(self.enqueued)


class _Store:
    async def put(self, key: str, source: Path) -> None: ...

    async def delete(self, key: str) -> None: ...


async def test_the_recording_pipeline_logs_no_payload(
    tmp_path: Path, captured: io.StringIO
) -> None:
    """`RecordingService` from first packet to `close()`.

    Audio bytes, a display name and a wrapped key all pass through, and
    `session.opened` / `session.speaker_first_packet` /
    `session.speaker_finalized` / `session.closed` are all emitted along the
    way -- so this is a real run of the narrative, not a smoke test.
    """
    jobs = _Jobs()
    service = RecordingService(
        guild_id=1,
        channel_id=2,
        channel_name=CHANNEL_NAME_CANARY,
        timeouts=SessionTimeouts(),
        sessions=_Sessions(),
        jobs=jobs,
        store=_Store(),
        writers=_WriterFactory(tmp_path),
        encryptor=_Encryptor(),  # type: ignore[arg-type]
        announcer=_Announcer(),
        retention_days=30,
    )

    await service.participants_changed(2, T0)
    assert service.is_recording
    await service.voice_packet(100, DISPLAY_NAME_CANARY, 1, 0, AUDIO_CANARY, T0)
    await service.close(EndReason.EMPTY, T0)

    assert len(jobs.enqueued) == 1, "the pipeline must actually have run"
    _assert_no_canaries(captured)


async def test_a_session_that_recorded_nothing_is_an_error_line(
    tmp_path: Path, captured: io.StringIO
) -> None:
    """The incident's outcome, as one alertable line rather than as silence.

    A session with consenting participants that enqueues no job recorded
    nothing. Before this branch that produced no document, no announcement,
    and not one log line -- the failure was expressed entirely as absence.
    """
    service = RecordingService(
        guild_id=1,
        channel_id=2,
        channel_name=CHANNEL_NAME_CANARY,
        timeouts=SessionTimeouts(),
        sessions=_Sessions(),
        jobs=_Jobs(),
        store=_Store(),
        writers=_WriterFactory(tmp_path),
        encryptor=_Encryptor(),  # type: ignore[arg-type]
        announcer=_Announcer(),
        retention_days=30,
    )

    await service.participants_changed(3, T0)
    # No `voice_packet` at all: people were present and nothing arrived.
    await service.close(EndReason.EMPTY, T0)

    closed = [
        json.loads(line)
        for line in captured.getvalue().splitlines()
        if line.strip() and json.loads(line)["event"] == "session.closed"
    ]
    assert len(closed) == 1
    assert closed[0]["level"] == "ERROR"
    assert closed[0]["jobs_enqueued"] == 0
    assert closed[0]["session_id"] == 4711
