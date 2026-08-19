# Sturnus Plan 3: The Worker, Transcription and Outline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the encrypted recordings Plan 2 produces into a chronological protocol in Outline, and post its link back to the channel. At the end of this plan the MVP works end to end.

**Architecture:** A second process consumes the job queue, transcribes each speaker's recording with `faster-whisper`, and assembles the results using the transcript model from Plan 1. The document target is reached through a port with exactly one implementation, rendered by a sandboxed Jinja2 template. The bot publishes the finished link; the worker never touches Discord.

**Tech Stack:** Python 3.12, `faster-whisper` (CTranslate2), `jinja2` (sandboxed), `httpx`, SQLAlchemy 2.0 async.

**Spec:** `docs/superpowers/specs/2026-08-19-sturnus-design.md`

**Predecessors:** Plans 1 and 2. This plan consumes their output: the transcript model, the seven tables, envelope encryption, the S3 store, the repositories, and the jobs the bot enqueues.

## Global Constraints

- **Python `>=3.12`**, dependency management exclusively through `uv`.
- **The dependency rule:** `sturnus.domain` imports only the standard library — enforced by `tests/test_architecture.py`. `sturnus.application` may import `domain` and its own ports, never a concrete adapter.
- **One data access path:** SQLAlchemy 2.0 async ORM. No raw `asyncpg`.
- **Schema changes only through Alembic.**
- **All code, comments, docstrings and assertion messages in English.**
- **Timestamps are timezone-aware `datetime`.**
- **Conventional Commits**; no Claude attribution.
- **`mypy` `strict = true`** over `src` and `tests`; `ruff check` and `ruff format --check` clean.
- **Neither audio nor transcript content ever appears in a log line.** This is the one constraint whose violation is invisible in tests — a reviewer must check it by reading.

## Two things this plan must verify rather than assume

Spec 8.4 and 8.3 both carry explicit "verify during implementation" notes, and Plan 2's spike requirement exists for the same reason: a plausible-looking assumption about someone else's API is how a design fails late.

1. **Outline's API shape** — endpoint paths, request fields and the response containing the document URL. Task 8 verifies this against the running instance before the adapter is written.
2. **Outline's mention notification behaviour** — whether a mention notifies per occurrence or once per document and user. In a two-hour protocol one person may appear in hundreds of blocks. Task 7 settles it, because the fallback changes what the template renders.

---

### Task 1: The transcription port and a fake

Everything downstream is built against the port, so the fake comes first and the real engine second. That order keeps the assembly logic testable without a model file.

**Files:**
- Modify: `src/sturnus/application/ports.py`
- Create: `src/sturnus/application/transcription.py`
- Test: `tests/application/test_transcription_port.py`

**Interfaces:**
- Produces:
  - `TranscribedSegment(start: float, end: float, text: str)` — offsets in seconds from the start of the file
  - `TranscriptionResult(segments: tuple[TranscribedSegment, ...], language: str)`
  - `TranscriptionEngine` protocol with `transcribe(path: Path, language: str | None) -> TranscriptionResult`
  - `to_absolute(result, epoch, speaker) -> list[Segment]` converting offsets to `domain.transcript.Segment`

- [ ] **Step 1: Write the failing test**

```python
# tests/application/test_transcription_port.py
from datetime import UTC, datetime, timedelta

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
    to_absolute,
)
from sturnus.domain.transcript import SpeakerIdentity

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA = SpeakerIdentity(1, "anna")


def result(*pairs: tuple[float, float, str]) -> TranscriptionResult:
    return TranscriptionResult(
        segments=tuple(TranscribedSegment(s, e, t) for s, e, t in pairs),
        language="de",
    )


def test_offsets_are_anchored_to_the_audio_epoch() -> None:
    """The epoch is sample zero of the recording, not when the speaker joined."""
    epoch = T0 + timedelta(seconds=30)
    segments = to_absolute(result((0.0, 1.5, "hello")), epoch, ANNA)
    assert segments[0].start == epoch
    assert segments[0].end == epoch + timedelta(seconds=1.5)


def test_several_segments_keep_their_spacing() -> None:
    segments = to_absolute(result((0.0, 1.0, "a"), (10.0, 11.0, "b")), T0, ANNA)
    assert segments[1].start - segments[0].start == timedelta(seconds=10)


def test_the_speaker_is_attached_to_every_segment() -> None:
    segments = to_absolute(result((0.0, 1.0, "a"), (2.0, 3.0, "b")), T0, ANNA)
    assert all(s.speaker == ANNA for s in segments)


def test_an_empty_result_yields_no_segments() -> None:
    assert to_absolute(result(), T0, ANNA) == []


def test_sub_second_offsets_survive_the_conversion() -> None:
    """Whisper reports fractional seconds; rounding them would misorder speakers."""
    segments = to_absolute(result((0.12, 0.34, "x")), T0, ANNA)
    assert segments[0].start == T0 + timedelta(milliseconds=120)
```

- [ ] **Step 2: Run it, confirm it fails with `ModuleNotFoundError`, then implement**

```python
# src/sturnus/application/transcription.py
"""The transcription port and the conversion into domain segments.

Whisper reports offsets relative to the start of the audio file. The file
begins at that speaker's audio epoch (Spec 6.3), so absolute time is the
epoch plus the offset — no in-memory state from the recording process is
needed, which is what lets the worker run in a different process entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from sturnus.domain.transcript import Segment, SpeakerIdentity


@dataclass(frozen=True)
class TranscribedSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    segments: tuple[TranscribedSegment, ...]
    language: str


class TranscriptionEngine(Protocol):
    async def transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        """Transcribe one speaker's recording.

        `language` pins the language; `None` asks the engine to detect it and
        report what it found.
        """
        ...


def to_absolute(
    result: TranscriptionResult, epoch: datetime, speaker: SpeakerIdentity
) -> list[Segment]:
    return [
        Segment(
            speaker=speaker,
            start=epoch + timedelta(seconds=segment.start),
            end=epoch + timedelta(seconds=segment.end),
            text=segment.text,
        )
        for segment in result.segments
    ]
```

Add `TranscriptionEngine` to the port table in `ports.py`'s docstring; leave the protocol itself here, next to the types it uses.

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add transcription port and offset conversion"
```

---

### Task 2: The Whisper adapter

**Files:**
- Create: `src/sturnus/infrastructure/whisper.py`
- Test: `tests/infrastructure/test_whisper.py`
- Create: `tests/fixtures/hello.wav` (a short real recording)

**Interfaces:**
- Consumes: `TranscriptionEngine`, `TranscriptionResult`
- Produces: `WhisperEngine(model_size: str, device: str, compute_type: str, default_language: str)`

- [ ] **Step 1: Add the dependency and record a fixture**

```bash
uv add "faster-whisper>=1.1"
```

Create `tests/fixtures/hello.wav`: two to three seconds of clearly spoken German, 16 kHz mono 16-bit — the format the audio writer produces. Record it or generate it; what matters is that it contains real speech, because the integration test asserts the transcript is non-empty.

- [ ] **Step 2: Write the test**

```python
# tests/infrastructure/test_whisper.py
from pathlib import Path

import pytest

from sturnus.infrastructure.whisper import WhisperEngine

FIXTURE = Path(__file__).parent.parent / "fixtures" / "hello.wav"


@pytest.fixture(scope="module")
def engine() -> WhisperEngine:
    # `tiny` keeps the test fast; production uses large-v3-turbo (Spec 7).
    return WhisperEngine(
        model_size="tiny", device="cpu", compute_type="int8", default_language="de"
    )


@pytest.mark.slow
async def test_transcribes_real_speech(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language="de")
    assert result.segments
    assert any(segment.text.strip() for segment in result.segments)


@pytest.mark.slow
async def test_offsets_are_within_the_recording(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language="de")
    for segment in result.segments:
        assert 0.0 <= segment.start <= segment.end


@pytest.mark.slow
async def test_detection_reports_a_language(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language=None)
    assert result.language


@pytest.mark.slow
async def test_silence_yields_no_segments(engine: WhisperEngine, tmp_path: Path) -> None:
    """A participant who never speaks must not produce hallucinated text.

    Whisper is known to invent text for silent input; `vad_filter` is what
    prevents it, and this test is what proves the filter is enabled.
    """
    import wave

    silent = tmp_path / "silence.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00" * 16_000 * 3)

    result = await engine.transcribe(silent, language="de")
    assert [s for s in result.segments if s.text.strip()] == []
```

Register the marker in `pyproject.toml` so the model download can be skipped in a fast loop:

```toml
[tool.pytest.ini_options]
markers = ["slow: needs a whisper model download"]
```

- [ ] **Step 3: Implement the adapter**

```python
# src/sturnus/infrastructure/whisper.py
"""faster-whisper behind the transcription port.

The library is synchronous and CPU-bound, so every call runs in a worker
thread. The model is loaded once and reused; jobs are processed one at a
time (Spec 5.3), so no locking is required around it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from faster_whisper import WhisperModel

from sturnus.application.transcription import (
    TranscribedSegment,
    TranscriptionResult,
)


class WhisperEngine:
    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        default_language: str,
    ) -> None:
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._default_language = default_language

    async def transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        return await asyncio.to_thread(self._transcribe, path, language)

    def _transcribe(self, path: Path, language: str | None) -> TranscriptionResult:
        segments, info = self._model.transcribe(
            str(path),
            language=language,
            # Skips the padded silence, which is most of a speaker's file and
            # would otherwise cost real time and invite hallucinated text.
            vad_filter=True,
            # Guards against the repetition cascades Whisper can fall into on
            # long audio (Spec 7).
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
        )
        collected = tuple(
            TranscribedSegment(start=s.start, end=s.end, text=s.text)
            for s in segments
        )
        detected = getattr(info, "language", None) or self._default_language
        return TranscriptionResult(segments=collected, language=detected)
```

> **Verify during implementation:** that `model.transcribe` returns segments with `.start`, `.end` and `.text`, and info carrying `.language`. The API has been stable across 1.x, but confirm against the installed version rather than trusting this snippet — and confirm that language detection still reports sensibly when `vad_filter` removes the opening silence, which is the case Spec 7 relies on.

- [ ] **Step 4: Run the tests, verify and commit**

```bash
uv run pytest tests/infrastructure/test_whisper.py -v -m slow
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -m "not slow"
git add -A && git commit -m "feat: add faster-whisper engine behind the transcription port"
```

---

### Task 3: The job queue

Implements Spec 9's queue and 5.3's completion rule. The rule matters: the document is created by whichever worker finishes the last job of a session, in the same transaction that marks it done, so two simultaneously finishing jobs cannot both create one.

**Files:**
- Create: `src/sturnus/infrastructure/db/queue.py`
- Test: `tests/infrastructure/test_queue.py`

**Interfaces:**
- Consumes: `TranscriptionJob`, `Session` models
- Produces: `JobQueue(session_factory)` with
  - `claim() -> ClaimedJob | None`
  - `complete(job_id, transcript) -> bool` — returns whether this was the session's last job
  - `fail(job_id, error, max_attempts) -> None`
  - `last_error(job_id) -> str | None`
  - `ClaimedJob(id, session_id, discord_user_id, s3_key, encryption_key_id, wrapped_data_key)`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_queue.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.queue import JobQueue
from sturnus.infrastructure.db.repositories import JobRepository, SessionRepository

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def seed(
    factory: async_sessionmaker[AsyncSession], speakers: list[int]
) -> int:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, T0)
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
```

- [ ] **Step 2: Run it, confirm it fails, then implement**

`claim()` selects one pending job whose attempt count is below the limit, using `select(...).with_for_update(skip_locked=True).limit(1)`, marks it `running`, and returns it — all in one transaction, which is what makes concurrent claiming safe.

`complete(job_id, transcript)` stores the transcript, sets the job `done`, and **in the same transaction** counts the session's jobs that are neither `done` nor dead. It returns `True` only when that count reaches zero. Doing the count in a separate transaction is the bug this design exists to avoid: two jobs finishing at once would both see work outstanding, or both see none.

`fail` increments `attempts`, stores the error, and returns the job to `pending` — or marks it `dead` once `attempts` reaches `max_attempts`. A dead job never blocks its session's completion count, so one unreadable recording still yields a document containing the other speakers.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/infrastructure/test_queue.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add the transcription job queue"
```

---

### Task 4: Rendering — the sandbox and the escaping

Implements Spec 8.2. Two safeguards, both load-bearing, and the second is the one that fires in normal operation.

**Files:**
- Create: `src/sturnus/infrastructure/templates/__init__.py`
- Create: `src/sturnus/infrastructure/templates/engine.py`
- Create: `src/sturnus/infrastructure/templates/markdown.py`
- Test: `tests/infrastructure/test_templates.py`

**Interfaces:**
- Produces:
  - `build_environment() -> SandboxedEnvironment`
  - `escape_markdown(value: str) -> str`
  - `render(template_source: str, **context: object) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_templates.py
import pytest
from jinja2.exceptions import SecurityError

from sturnus.infrastructure.templates.engine import render
from sturnus.infrastructure.templates.markdown import escape_markdown


def test_a_plain_template_renders() -> None:
    assert render("Hello {{ name }}", name="world") == "Hello world"


@pytest.mark.parametrize(
    "source",
    [
        "{{ ''.__class__ }}",
        "{{ ''.__class__.__mro__ }}",
        "{{ [].__class__.__base__.__subclasses__() }}",
        "{{ ().__class__.__bases__[0].__subclasses__() }}",
        "{{ config.__class__.__init__.__globals__ }}",
    ],
)
def test_sandbox_escapes_are_refused(source: str) -> None:
    """Templates become admin-settable in a later phase.

    An unguarded environment would then be a shell in the bot's pod for any
    guild administrator, so the sandbox goes in before the door opens.
    """
    with pytest.raises((SecurityError, Exception)):
        render(source)


def test_display_names_cannot_inject_a_link() -> None:
    """Discord display names are attacker-controlled input."""
    rendered = escape_markdown("[click here](https://evil.example)")
    assert "](" not in rendered


def test_escaping_neutralises_emphasis_and_code() -> None:
    for hostile in ["*bold*", "_italic_", "`code`", "# heading"]:
        assert escape_markdown(hostile) != hostile


def test_escaping_leaves_ordinary_text_alone() -> None:
    assert escape_markdown("Anna Example") == "Anna Example"


def test_escaping_survives_a_round_of_rendering() -> None:
    """The filter must be reachable from a template, not only from Python."""
    out = render("{{ name | md }}", name="a]b(c)")
    assert "](" not in out


def test_a_speaker_name_cannot_break_out_of_a_mention() -> None:
    """The exact shape used by the Outline adapter."""
    out = render(
        "@[{{ name | md }}](mention://user/{{ uid }})",
        name="x](mention://user/other) [y",
        uid="real-id",
    )
    assert out.count("mention://user/") == 1
```

- [ ] **Step 2: Run it, confirm it fails, then implement**

```python
# src/sturnus/infrastructure/templates/engine.py
"""The Jinja2 environment used for every rendered artefact.

Sandboxed from the outset. In this phase all templates ship inside the
image, so nothing untrusted is executed yet — but the moment templates
become settable through a command, an ordinary environment would be
arbitrary code execution in the bot's pod. Adding the sandbox now costs
nothing and means that later change does not land on a foundation that
cannot carry it.
"""

from __future__ import annotations

from jinja2.sandbox import SandboxedEnvironment

from sturnus.infrastructure.templates.markdown import escape_markdown


def build_environment() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    # autoescape is off deliberately: the output is Markdown, not HTML, and
    # HTML escaping would corrupt it. Escaping is explicit through this filter
    # instead, applied to every value that comes from outside.
    env.filters["md"] = escape_markdown
    return env


def render(template_source: str, **context: object) -> str:
    return build_environment().from_string(template_source).render(**context)
```

```python
# src/sturnus/infrastructure/templates/markdown.py
"""Escaping for values interpolated into Markdown.

Discord display names and transcript text are not trustworthy. Someone
calling themselves `[click here](https://…)` would otherwise place a link
into every protocol they appear in, and a name containing `](` can close
the surrounding construct and start something else.
"""

from __future__ import annotations

_SPECIAL = "\\`*_{}[]()#+-.!|>~"


def escape_markdown(value: str) -> str:
    return "".join("\\" + ch if ch in _SPECIAL else ch for ch in value)
```

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/infrastructure/test_templates.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add sandboxed template engine with markdown escaping"
```

---

### Task 5: The document sink port and the transcript renderer

Implements Spec 8.1. The model stays target-neutral; the adapter's template decides what a speaker looks like.

**Files:**
- Create: `src/sturnus/application/documents.py`
- Create: `src/sturnus/infrastructure/documents/__init__.py`
- Create: `src/sturnus/infrastructure/documents/outline_template.md.j2`
- Test: `tests/application/test_document_rendering.py`

**Interfaces:**
- Consumes: `Transcript`, `SpeakerIdentity` from `domain.transcript`; `render` from Task 4
- Produces:
  - `DocumentSink` protocol — `create(title: str, body: str) -> CreatedDocument`
  - `CreatedDocument(id: str, url: str)`
  - `render_transcript(transcript, template_source, tz) -> str`
  - `document_title(transcript, tz) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/application/test_document_rendering.py
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sturnus.application.documents import document_title, render_transcript
from sturnus.domain.transcript import SpeakerIdentity, Transcript, TranscriptBlock

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
TEMPLATE = (
    Path(__file__).parent.parent.parent
    / "src/sturnus/infrastructure/documents/outline_template.md.j2"
).read_text(encoding="utf-8")

LINKED = SpeakerIdentity(1234, "maxm", external_user_id="9c8b", external_display_name="Max Example")
GUEST = SpeakerIdentity(9876, "guestuser")


def transcript(*blocks: TranscriptBlock) -> Transcript:
    speakers: list[SpeakerIdentity] = []
    for b in blocks:
        if b.speaker not in speakers:
            speakers.append(b.speaker)
    return Transcript(
        session_started_at=T0,
        session_ended_at=T0 + timedelta(hours=1),
        participants=tuple(speakers),
        blocks=blocks,
    )


def block(speaker: SpeakerIdentity, offset: int, text: str) -> TranscriptBlock:
    return TranscriptBlock(speaker=speaker, start=T0 + timedelta(seconds=offset), text=text)


def render(*blocks: TranscriptBlock) -> str:
    return render_transcript(transcript(*blocks), TEMPLATE, tz=UTC)


def test_a_linked_speaker_is_rendered_as_a_mention() -> None:
    out = render(block(LINKED, 0, "hello"))
    assert "@[Max Example](mention://user/9c8b)" in out


def test_a_linked_speaker_also_carries_the_discord_link() -> None:
    out = render(block(LINKED, 0, "hello"))
    assert "https://discord.com/users/1234" in out


def test_an_unlinked_speaker_gets_no_mention_but_keeps_the_link() -> None:
    out = render(block(GUEST, 0, "hello"))
    assert "mention://user/" not in out
    assert "https://discord.com/users/9876" in out


def test_the_spoken_text_appears() -> None:
    assert "hello there" in render(block(GUEST, 0, "hello there"))


def test_blocks_appear_in_order() -> None:
    out = render(block(GUEST, 0, "first"), block(LINKED, 60, "second"))
    assert out.index("first") < out.index("second")


def test_the_document_does_not_start_with_a_heading() -> None:
    """Outline keeps the title in its own field (Spec 8.3)."""
    assert not render(block(GUEST, 0, "x")).lstrip().startswith("# ")


def test_a_hostile_display_name_cannot_inject_a_link() -> None:
    hostile = SpeakerIdentity(5, "x](https://evil.example) [y")
    out = render(block(hostile, 0, "text"))
    assert "evil.example" not in out or "\\]" in out


def test_hostile_transcript_text_is_escaped() -> None:
    out = render(block(GUEST, 0, "[click](https://evil.example)"))
    assert "](https://evil.example)" not in out


def test_the_title_carries_date_and_time() -> None:
    title = document_title(transcript(block(GUEST, 0, "x")), tz=UTC)
    assert "2026-08-19" in title


def test_a_participant_list_is_present() -> None:
    out = render(block(LINKED, 0, "a"), block(GUEST, 10, "b"))
    assert "Max Example" in out
    assert "guestuser" in out
```

- [ ] **Step 2: Run it, confirm it fails, then write the template and the renderer**

The template is the readable specification of the output. It must produce, per Spec 8.3:

```markdown
**14:32:05** · @[Max Example](mention://user/9c8b…) ([maxm](https://discord.com/users/1234…))

The spoken text of this block.
```

and for an unlinked speaker only the bracketed Discord identity. Every value coming from the transcript passes through the `md` filter — names and spoken text alike.

`render_transcript` renders the template with the transcript and a timezone; `document_title` builds the title from the session's start. The timezone is a parameter rather than a constant because a protocol read by people in one place should carry local times, and hardcoding UTC would make every timestamp subtly wrong for its readers.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/application/test_document_rendering.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: render transcripts through a template"
```

---

### Task 6: Session assembly

Puts a whole session together: fetch every job's transcript, attach identities, merge, render.

**Files:**
- Create: `src/sturnus/application/assembly.py`
- Test: `tests/application/test_assembly.py`

**Interfaces:**
- Consumes: `build_transcript`, `SpeakerIdentity`, `to_absolute`
- Produces: `assemble(session_id, sessions, jobs, links, tz) -> Transcript`
- **Extends** (these do not exist yet — Plan 2 built only what the bot needed):
  - `SessionRepository.session_bounds(session_id) -> tuple[datetime, datetime]`
  - `JobRepository.transcripts_for(session_id) -> dict[int, TranscriptionResult]`
  - new `AccountLinkRepository.external_identity(discord_user_id) -> tuple[str, str] | None`, returning the external id and display name for the configured provider, or `None`

- [ ] **Step 1: Write the failing test**

```python
# tests/application/test_assembly.py
from datetime import UTC, datetime, timedelta

from sturnus.application.assembly import assemble
from sturnus.application.transcription import TranscribedSegment, TranscriptionResult

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA, BEN = 100, 200


class FakeSessions:
    def __init__(self) -> None:
        self.names = {ANNA: "anna", BEN: "ben"}
        self.epochs = {ANNA: T0, BEN: T0 + timedelta(seconds=10)}
        self.bounds = (T0, T0 + timedelta(hours=1))

    async def participant_names(self, session_id: int) -> dict[int, str]:
        return self.names

    async def audio_epoch(self, session_id: int, user_id: int) -> datetime | None:
        return self.epochs.get(user_id)

    async def session_bounds(self, session_id: int) -> tuple[datetime, datetime]:
        return self.bounds


class FakeJobs:
    def __init__(self, per_speaker: dict[int, TranscriptionResult]) -> None:
        self._per_speaker = per_speaker

    async def transcripts_for(self, session_id: int) -> dict[int, TranscriptionResult]:
        return self._per_speaker


class FakeLinks:
    def __init__(self, linked: dict[int, tuple[str, str]] | None = None) -> None:
        self._linked = linked or {}

    async def external_identity(self, discord_user_id: int) -> tuple[str, str] | None:
        return self._linked.get(discord_user_id)


def result(*pairs: tuple[float, float, str]) -> TranscriptionResult:
    return TranscriptionResult(
        segments=tuple(TranscribedSegment(s, e, t) for s, e, t in pairs), language="de"
    )


async def test_segments_from_both_speakers_are_interleaved_by_time() -> None:
    """Each speaker's offsets are relative to their own epoch, ten seconds apart."""
    transcript = await assemble(
        1,
        FakeSessions(),
        FakeJobs({ANNA: result((0.0, 2.0, "anna first")), BEN: result((0.0, 2.0, "ben second"))}),
        FakeLinks(),
        tz=UTC,
    )
    assert [b.text for b in transcript.blocks] == ["anna first", "ben second"]


async def test_a_linked_account_reaches_the_transcript() -> None:
    transcript = await assemble(
        1,
        FakeSessions(),
        FakeJobs({ANNA: result((0.0, 1.0, "hello"))}),
        FakeLinks({ANNA: ("out-1", "Anna Example")}),
        tz=UTC,
    )
    speaker = transcript.blocks[0].speaker
    assert speaker.external_user_id == "out-1"
    assert speaker.external_display_name == "Anna Example"


async def test_an_unlinked_speaker_keeps_only_their_discord_identity() -> None:
    transcript = await assemble(
        1, FakeSessions(), FakeJobs({BEN: result((0.0, 1.0, "hi"))}), FakeLinks(), tz=UTC
    )
    assert transcript.blocks[0].speaker.external_user_id is None


async def test_a_speaker_without_an_epoch_is_skipped() -> None:
    """No epoch means no audio was ever recorded for them."""
    sessions = FakeSessions()
    sessions.epochs.pop(BEN)
    transcript = await assemble(
        1,
        sessions,
        FakeJobs({ANNA: result((0.0, 1.0, "a")), BEN: result((0.0, 1.0, "b"))}),
        FakeLinks(),
        tz=UTC,
    )
    assert [b.text for b in transcript.blocks] == ["a"]


async def test_a_session_with_no_transcripts_yields_an_empty_transcript() -> None:
    transcript = await assemble(1, FakeSessions(), FakeJobs({}), FakeLinks(), tz=UTC)
    assert transcript.blocks == ()


async def test_display_names_come_from_the_session_not_from_now() -> None:
    """Names are frozen at recording time (Spec 8.3)."""
    transcript = await assemble(
        1, FakeSessions(), FakeJobs({ANNA: result((0.0, 1.0, "x"))}), FakeLinks(), tz=UTC
    )
    assert transcript.blocks[0].speaker.discord_display_name == "anna"
```

- [ ] **Step 2: Extend the repositories**

The fakes above pin three methods the real repositories lack, because Plan 2
built only what the bot needed. Add them alongside their existing tests in
`tests/infrastructure/test_repositories.py`:

- `SessionRepository.session_bounds` returns `(started_at, ended_at)`; a session
  still open raises rather than inventing an end.
- `JobRepository.transcripts_for` returns each speaker's stored transcript for a
  session, skipping jobs that are dead or unfinished — a dead job must not stop
  the remaining speakers from appearing in the document.
- `AccountLinkRepository.external_identity` looks up `account_link` by
  `(discord_user_id, provider)`. The provider comes from configuration, so a
  later Confluence adapter reads its own mapping rather than Outline's.

Write a test for each before implementing it.

- [ ] **Step 3: Run the assembly test, confirm it fails, then implement**

`assemble` reads the participants and their epochs, converts each speaker's transcription into absolute segments with `to_absolute`, attaches the external identity where a link exists, and hands everything to `build_transcript` from Plan 1 — which does the ordering and block merging. A speaker without an epoch contributed no audio and is skipped rather than defaulting to the session start, which would place their words at a time they did not speak.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/application/test_assembly.py tests/infrastructure/test_repositories.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: assemble a session transcript from its jobs"
```

---

### Task 7: Settle the mention question

**Files:**
- Create: `docs/verification/outline-mentions.md`
- Possibly modify: `src/sturnus/infrastructure/documents/outline_template.md.j2`

Spec 8.3 leaves this open, and the answer changes the template. Settle it before the adapter exists, because retrofitting it means regenerating documents.

- [ ] **Step 1: Test the behaviour against the running instance**

Create a document in a scratch collection containing the same user mentioned twenty times. Then check whether that user received twenty notifications or one. Record in `docs/verification/outline-mentions.md`: the Outline version, what you created, and what arrived.

- [ ] **Step 2: Act on the answer**

If notification is **per document and user**: nothing changes, note it and move on.

If notification is **per mention**: a two-hour protocol would make the bot a spam source, and the fallback from Spec 8.3 applies — only a speaker's **first** block renders as a mention, all later ones as plain text with the Discord link. Change the template accordingly and add a test asserting that a speaker appearing three times produces exactly one `mention://user/`.

- [ ] **Step 3: Commit the finding**

```bash
git add -A && git commit -m "docs: record outline mention notification behaviour"
```

The document is part of the deliverable — without it the next reader cannot tell whether the template is shaped by evidence or by guesswork.

---

### Task 8: The Outline adapter

**Files:**
- Create: `docs/verification/outline-api.md`
- Create: `src/sturnus/infrastructure/documents/outline.py`
- Test: `tests/infrastructure/test_outline.py`

**Interfaces:**
- Consumes: `DocumentSink`, `CreatedDocument`
- Produces: `OutlineSink(base_url, api_token, collection_id)`

- [ ] **Step 1: Verify the API before writing the client**

**Do not write this adapter from documentation alone.** Spec 8.4 flags the endpoint paths, field names and response shape as unverified, and Plan 2's spike exists because guessing another system's API is how a design fails late.

Against the running instance, with a scratch collection and a token that can be revoked afterwards, establish and record in `docs/verification/outline-api.md`:

- The exact endpoint that creates a document, and its request fields — at minimum title, body, target collection, and whether it publishes immediately or leaves a draft.
- The exact response, and **where the document's URL appears** — the bot posts that URL, so a wrong field means a broken link rather than an error.
- How authentication is passed.
- What happens on failure: status code and body for an invalid token and for a non-existent collection. The adapter must distinguish "retry this" from "this will never work", and a job retried forever against a deleted collection is a queue that never drains.
- Whether the body has a size limit. A long protocol can run to hundreds of kilobytes.

Paste the actual requests and responses into the document, with the token redacted.

- [ ] **Step 2: Write the test against the verified shape**

```python
# tests/infrastructure/test_outline.py
import httpx
import pytest

from sturnus.infrastructure.documents.outline import OutlineSink, PermanentDocumentError

BASE = "https://outline.example"
COLLECTION = "col-1"


def sink(handler: httpx.MockTransport) -> OutlineSink:
    return OutlineSink(
        base_url=BASE,
        api_token="secret-token",
        collection_id=COLLECTION,
        transport=handler,
    )


async def test_a_created_document_returns_its_id_and_url() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"id": "doc-1", "url": "/doc/protocol-abc"}}
        )

    created = await sink(httpx.MockTransport(handle)).create("Title", "Body")
    assert created.id == "doc-1"
    assert created.url.endswith("/doc/protocol-abc")


async def test_the_url_is_absolute() -> None:
    """Outline returns a relative path; the bot posts this into Discord."""
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    created = await sink(httpx.MockTransport(handle)).create("T", "B")
    assert created.url.startswith(BASE)


async def test_the_token_is_sent_and_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    with caplog.at_level("DEBUG"):
        await sink(httpx.MockTransport(handle)).create("T", "B")

    assert "secret-token" in seen.get("authorization", "")
    assert "secret-token" not in caplog.text


async def test_the_body_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Transcript content must never reach a log line."""
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "d", "url": "/doc/x"}})

    with caplog.at_level("DEBUG"):
        await sink(httpx.MockTransport(handle)).create("T", "CONFIDENTIAL-SPEECH")

    assert "CONFIDENTIAL-SPEECH" not in caplog.text


async def test_a_server_error_is_retryable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(Exception) as excinfo:
        await sink(httpx.MockTransport(handle)).create("T", "B")
    assert not isinstance(excinfo.value, PermanentDocumentError)


async def test_a_rejected_token_is_permanent() -> None:
    """Retrying an unauthorised call forever would never drain the queue."""
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(PermanentDocumentError):
        await sink(httpx.MockTransport(handle)).create("T", "B")


async def test_a_missing_collection_is_permanent() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with pytest.raises(PermanentDocumentError):
        await sink(httpx.MockTransport(handle)).create("T", "B")
```

> Adjust the mocked paths, fields and status codes to whatever Step 1 actually found. The assertions about behaviour — absolute URL, no secrets or content in logs, retryable versus permanent — stay as they are regardless of what the API looks like.

- [ ] **Step 3: Implement the adapter, then verify and commit**

```bash
uv add "httpx>=0.28"
uv run pytest tests/infrastructure/test_outline.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add the outline document adapter"
```

---

### Task 9: The worker process

**Files:**
- Create: `src/sturnus/application/worker.py`
- Create: `src/sturnus/entrypoints/worker.py`
- Test: `tests/application/test_worker.py`

**Interfaces:**
- Consumes: everything above
- Produces: `process_one(...) -> bool`, `main()`

- [ ] **Step 1: Write the failing test**

```python
# tests/application/test_worker.py
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

    async def fail(self, job_id: int, error: str, max_attempts: int) -> None:
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

    async def get(self, key: str, target: Path) -> None:
        target.write_bytes(b"encrypted")

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class FakeCrypto:
    """Stands in for unwrap-and-decrypt; writes a recognisable file."""

    def __init__(self) -> None:
        self.decrypted: list[Path] = []

    def decrypt_to(self, source: Path, target: Path, wrapped: bytes, key_id: str) -> None:
        target.write_bytes(b"RIFFdecoded")
        self.decrypted.append(target)


class FakeDocuments:
    def __init__(self, permanent_error: bool = False) -> None:
        self.created: list[tuple[str, str]] = []
        self.permanent_error = permanent_error

    async def create(self, title: str, body: str) -> CreatedDocument:
        if self.permanent_error:
            raise PermanentDocumentError("collection is gone")
        self.created.append((title, body))
        return CreatedDocument(id="doc-1", url="https://outline.example/doc/1")


class FakeSessions:
    def __init__(self) -> None:
        self.languages: dict[int, str] = {}
        self.documented: list[tuple[int, str]] = []

    async def detected_language(self, session_id: int, user_id: int) -> str | None:
        return self.languages.get(user_id)

    async def set_detected_language(self, session_id: int, user_id: int, lang: str) -> None:
        self.languages.setdefault(user_id, lang)

    async def mark_documented(self, session_id: int, doc_id: str, url: str) -> None:
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
    await process_one(
        **run(tmp_path, queue=queue, documents=FakeDocuments(permanent_error=True))
    )
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
```


- [ ] **Step 2: Implement `process_one`**

One job, start to finish:

1. Claim a job. Nothing claimed → return `False`.
2. Download the encrypted object to a temporary directory.
3. Unwrap the data key with the master key named by `encryption_key_id`, decrypt to a temporary WAV.
4. Transcribe. First job for a speaker: no pinned language, then persist what was detected. Later jobs: pin the stored language (Spec 7).
5. Store the transcript on the job and ask whether it was the session's last.
6. If it was: assemble, render, create the document, store its id and URL on the session, set the session `documented`.
7. Delete the audio object from S3 and stamp `audio_deleted_at`.
8. Remove every temporary file — in a `finally`, so a failure anywhere above does not leave decrypted speech on disk.

Point 8 is the one to get right. Everything else fails loudly; leftover plaintext audio fails silently and is exactly what the encryption was for.

> **Retention conflict to resolve while implementing.** Step 7 deletes the audio immediately, but Spec 12 keeps it for `audio_retention_days` so a bad transcription can be redone. These contradict. The spec is the authority: **do not delete the object here.** Leave `retention_until` to the sweep in Task 10, and delete only the temporary local files. If you conclude the spec is wrong, report it rather than deciding for it.

- [ ] **Step 3: Write the entrypoint**

`sturnus-worker` loads settings, runs Alembic migrations to head (the worker owns migrations per Spec 13.1), builds the dependency graph, and loops `process_one` with a short sleep when the queue is empty. It serves the same health endpoints as the bot and shuts down cleanly on `SIGTERM`, finishing the job in flight rather than abandoning it.

Restore its console script in `pyproject.toml`.

- [ ] **Step 4: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add the transcription worker"
```

---

### Task 10: Retention, erasure and the link back to the channel

The remaining obligations from Spec 12.2, 12.3 and 8.5.

**Files:**
- Create: `src/sturnus/application/retention.py`
- Create: `src/sturnus/infrastructure/discord/audio_cog.py`
- Create: `src/sturnus/application/publishing.py`
- Test: `tests/application/test_retention.py`, `tests/application/test_publishing.py`

- [ ] **Step 1: Write the retention tests**

```python
# tests/application/test_retention.py
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
```

- [ ] **Step 2: Implement the sweep**

A periodic pass in the worker: select jobs whose `retention_until` has passed and whose `audio_deleted_at` is null, delete each object from S3, stamp the timestamp. The S3 lifecycle rule from Spec 12.2 is the second line of defence, not a replacement — deletion must be recorded in the database as well, because that record is the evidence that it happened.

- [ ] **Step 3: Implement the erasure commands**

`/audio delete` removes the caller's own recordings across all sessions, immediately, ignoring the retention period. `/audio purge <user>` does the same for a named user and is admin-only, so erasure requests under Art. 17 GDPR can be served. Both delete the S3 objects and stamp `audio_deleted_at`; both reply ephemerally with a count of what was removed. Existing transcripts are untouched and the reply says so — they are a separate processing result and live in the document system, not here.

- [ ] **Step 4: Implement link publishing**

Spec 8.5: the worker sets the session `documented` with its URL; the bot polls every `publish_poll_seconds` for sessions that are `documented` with `announced_at` still null, posts the link into the channel's text area, and stamps `announced_at`.

Test the selection logic as a pure function: given sessions in various states, which should be announced. Cover that an already-announced session is never announced twice — that field is what protects against a restart re-posting every link the bot ever published.

- [ ] **Step 5: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add retention sweep, erasure commands and link publishing"
```

---

### Task 11: End-to-end verification

No new code. This is where the MVP is either real or not.

- [ ] **Step 1: Run a full session against a test guild**

With informed participants in a test guild, not a production server:

1. Two people join the recording channel, both consenting, and hold a short conversation with clear pauses and at least one interruption.
2. Both leave; the grace period expires; the bot closes the session.
3. The worker transcribes both recordings and creates the document.
4. The bot posts the link.

Record in the report: wall-clock time from session end to link posted, and the ratio of that to the speaking time.

- [ ] **Step 2: Check what the document actually says**

- Is the order right — including across speakers, and around the interruption?
- Does a pause appear as a pause, or did segments slide together?
- Are linked participants rendered as mentions and unlinked ones as plain names with a Discord link?
- Are the timestamps plausible against what actually happened?

Chronology is the thing most likely to be subtly wrong and least likely to look wrong. Check it against a real recollection of the conversation, not against itself.

- [ ] **Step 3: Check the obligations**

- Audio still in S3 after transcription, with `retention_until` set (Spec 12).
- `/audio delete` removes it and the reply matches what was deleted.
- An administrator without the consent role who speaks contributes **nothing** to the document.
- No transcript content, audio, or token in any log line from either process.

The last two are legal gates. If either fails, stop and fix before this runs anywhere near real users.

- [ ] **Step 4: Write up the findings**

Record measured latency, transcription quality on real German speech, memory and CPU under load, and anything that surprised you. That document tells Plan 4 what to size the deployment for — the numbers in the spec are estimates and this is the first time reality is available.

---

## What exists after this plan

The MVP, end to end: a conversation in a Discord voice channel becomes a chronological protocol in Outline, with a link posted back to the channel, consent enforced on two layers, and audio encrypted and expiring on schedule.

What remains for Plan 4: the OAuth link service that lets participants connect their Outline account — until then everyone appears under their Discord name — plus the Helm chart, the Flux manifests and the deployment itself.

## Risks carried into Plan 4

- **Latency is unknown until Task 11 measures it.** The estimate is roughly the speaking time; if it turns out far worse, the choice is a smaller model or splitting recordings after all, and both change the spec.
- **Transcription quality on real German speech with overlapping speakers is unmeasured.** `large-v3-turbo` is the reasoned choice, not a tested one.
- **No account is linked yet**, so the mention path in the template is exercised only by test data until Plan 4 lands.
