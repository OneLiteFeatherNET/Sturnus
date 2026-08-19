# Sturnus Plan 1: Foundation, Domain, and Persistence

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working Python repository with CI, fully tested domain logic (session states, time reconstruction, transcript assembly, consent) and the database layer including migrations.

**Architecture:** Layering with an inward-facing dependency rule. `domain/` contains pure logic with no I/O and no third-party libraries; `infrastructure/db/` encapsulates SQLAlchemy. The rule is enforced by a test that applies from Task 2 onward — any later violation fails in CI.

**Tech Stack:** Python 3.12, uv, SQLAlchemy 2.0 (async, `asyncpg`), Alembic, pytest, pytest-asyncio, testcontainers, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-19-sturnus-design.md`

## Global Constraints

- **Python `>=3.12`**, package management exclusively via `uv`.
- **Dependency rule:** `sturnus.domain` imports neither `sturnus.application` nor `sturnus.infrastructure` nor any library with I/O (`discord`, `sqlalchemy`, `boto3`, `jinja2`, `faster_whisper`). Enforced by `tests/test_architecture.py`.
- **One data access path:** exclusively the SQLAlchemy 2.0 ORM in async mode. Raw `asyncpg` alongside the ORM is excluded (Spec 9).
- **Schema changes exclusively via Alembic.** No `create_all()` in the production path.
- **Conventional Commits** — input for Release Please. Commit prefixes: `feat:`, `fix:`, `chore:`, `test:`, `docs:`, `refactor:`.
- **No Claude attribution** in commits (org requirement).
- **Timestamps are `datetime` with `timezone.utc`**, never naive.
- The version lives **only** in `pyproject.toml` with the marker `# x-release-please-version`.

---

### Task 1: Repository Scaffolding, Tooling, and CI

**Files:**
- Create: `pyproject.toml`, `.python-version`, `README.md`, `CHANGELOG.md`
- Create: `release-please-config.json`, `.release-please-manifest.json`
- Create: `src/sturnus/__init__.py`, `src/sturnus/domain/__init__.py`, `src/sturnus/application/__init__.py`, `src/sturnus/infrastructure/__init__.py`
- Create: `.github/workflows/build.yml`, `.github/workflows/release-please.yml`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing
- Produces: package `sturnus` importable; `sturnus.__version__` as `str`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "sturnus"
version = "0.1.0" # x-release-please-version
description = "Discord voice transcription with Outline document output"
readme = "README.md"
requires-python = ">=3.12"
authors = [{ name = "OneLiteFeather" }]
dependencies = [
    "sqlalchemy>=2.0.44",
    "asyncpg>=0.31.0",
    "alembic>=1.14.0",
    "pydantic-settings>=2.12.0",
]

[project.scripts]
sturnus-bot = "sturnus.entrypoints.bot:main"
sturnus-link = "sturnus.entrypoints.link:main"
sturnus-worker = "sturnus.entrypoints.worker:main"

[build-system]
requires = ["uv_build>=0.12.1,<0.13.0"]
build-backend = "uv_build"

[dependency-groups]
lint = ["mypy>=1.19.0", "ruff>=0.14.7"]
test = [
    "pytest>=9.0.1",
    "pytest-asyncio>=1.3.0",
    "testcontainers[postgres]>=4.9.0",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

# These three ship no type information. Strictness for our own code
# stays untouched; without this block the test run fails on
# import-untyped even though there's no actual error in the code.
[[tool.mypy.overrides]]
module = ["testcontainers.*", "alembic.*", "psycopg.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

The entry points under `[project.scripts]` point to modules that don't
exist until later plans. That's intentional — `uv sync` doesn't check
them; only an actual invocation would fail.

- [ ] **Step 2: Create package structure and version**

```bash
mkdir -p src/sturnus/{domain,application,infrastructure} tests
echo "3.12" > .python-version
printf '__version__ = "0.1.0"\n' > src/sturnus/__init__.py
touch src/sturnus/domain/__init__.py src/sturnus/application/__init__.py src/sturnus/infrastructure/__init__.py
printf '# Changelog\n' > CHANGELOG.md
printf '# Sturnus\n\nDiscord voice transcription with Outline document output.\n' > README.md
```

- [ ] **Step 3: Write smoke test**

```python
# tests/test_smoke.py
import sturnus


def test_package_exposes_version() -> None:
    assert isinstance(sturnus.__version__, str)
    assert sturnus.__version__.count(".") == 2
```

- [ ] **Step 4: Install dependencies and run the test**

Run: `uv sync --all-groups && uv run pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Run linter and type check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: both clean. If formatting flags something, run `uv run ruff format .` and check again.

- [ ] **Step 6: Create Release Please files**

```json
// release-please-config.json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "release-type": "simple",
  "include-component-in-tag": false,
  "include-v-in-tag": true,
  "bootstrap-sha": "HEAD",
  "pull-request-header": "",
  "packages": {
    ".": {
      "package-name": "sturnus",
      "changelog-path": "CHANGELOG.md",
      "extra-files": [
        { "type": "generic", "path": "pyproject.toml" }
      ]
    }
  }
}
```

```json
// .release-please-manifest.json
{ ".": "0.1.0" }
```

The entry for `charts/sturnus/Chart.yaml` in `extra-files` gets added in
Plan 4, once the chart exists — a reference to a missing file would make
Release Please fail.

- [ ] **Step 7: Create PR workflow**

```yaml
# .github/workflows/build.yml
name: build

on:
  pull_request:
  push:
    branches: [main]

concurrency:
  group: build-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - run: uv sync --all-groups
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy
      - run: uv run pytest -v
```

This workflow is deliberately repo-local: the central catalog has no
Python counterpart to `gradle-build-pr.yml` (Spec 13.3).

- [ ] **Step 8: Create Release Please workflow**

```yaml
# .github/workflows/release-please.yml
name: release-please

on:
  push:
    branches: [main]

permissions:
  contents: write
  pull-requests: write

jobs:
  release-please:
    runs-on: ubuntu-latest
    outputs:
      release_created: ${{ steps.release.outputs.release_created }}
      version: ${{ steps.release.outputs.version }}
    steps:
      - id: release
        uses: googleapis/release-please-action@v5
        with:
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json
```

The `publish` job that hooks in `docker-publish.yml@v2.4.0` gets added in
Plan 4 — without a Dockerfile it would have nothing to build. A
tag-triggered workflow is not created (Spec 13.1).

- [ ] **Step 9: Add `.gitignore` and commit**

```bash
printf '.venv/\n__pycache__/\n*.pyc\n.env\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n' > .gitignore
git add -A
git commit -m "chore: scaffold python project with ci and release-please"
```

---

### Task 2: Architecture Test for the Dependency Rule

This test comes before the domain logic, so the rule applies from the
first line of domain code instead of being enforced after the fact.

**Files:**
- Test: `tests/test_architecture.py`

**Interfaces:**
- Consumes: package structure from Task 1
- Produces: nothing (pure test)

- [ ] **Step 1: Write the test**

```python
# tests/test_architecture.py
import ast
from pathlib import Path

DOMAIN = Path(__file__).parent.parent / "src" / "sturnus" / "domain"

FORBIDDEN_PREFIXES = (
    "sturnus.application",
    "sturnus.infrastructure",
    "discord",
    "sqlalchemy",
    "alembic",
    "asyncpg",
    "boto3",
    "botocore",
    "jinja2",
    "faster_whisper",
    "aiohttp",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import and thus stays within the package
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def test_domain_has_no_outward_imports() -> None:
    violations: list[str] = []
    for path in DOMAIN.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")
    assert not violations, "domain must not import outward:\n" + "\n".join(violations)


def test_application_does_not_import_infrastructure() -> None:
    app = DOMAIN.parent / "application"
    violations: list[str] = []
    for path in app.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith("sturnus.infrastructure"):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")
    assert not violations, "application must not import infrastructure:\n" + "\n".join(violations)
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_architecture.py -v`
Expected: PASS (both packages are still empty, there's nothing to violate)

- [ ] **Step 3: Verify the test against an actual violation**

```bash
printf 'import sqlalchemy\n' > src/sturnus/domain/_probe.py
uv run pytest tests/test_architecture.py -v
```
Expected: FAIL with `domain must not import outward: domain/_probe.py: sqlalchemy`

A test that never fails doesn't verify anything — this step proves it actually triggers.

- [ ] **Step 4: Remove the probe and verify again**

```bash
rm src/sturnus/domain/_probe.py
uv run pytest tests/test_architecture.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_architecture.py
git commit -m "test: enforce inward dependency rule for domain layer"
```

---

### Task 3: Session State Machine

Implements Spec 5.1. The machine knows nothing about Discord or the
database and receives time from outside.

**Files:**
- Create: `src/sturnus/domain/session.py`
- Test: `tests/domain/test_session.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `SessionState` (StrEnum): `IDLE`, `RECORDING`, `GRACE`, `CLOSING`
  - `EndReason` (StrEnum): `EMPTY`, `IDLE_TIMEOUT`, `MAX_DURATION`
  - `SessionTimeouts(empty_grace_seconds: int, idle_timeout_minutes: int, max_session_hours: int)`
  - `SessionMachine(timeouts: SessionTimeouts)` with
    `state: SessionState`, `started_at: datetime | None`, `end_reason: EndReason | None`,
    `participants_changed(consented_count: int, now: datetime) -> None`,
    `audio_received(now: datetime) -> None`,
    `tick(now: datetime) -> EndReason | None`

- [ ] **Step 1: Write the test**

```python
# tests/domain/test_session.py
from datetime import datetime, timedelta, timezone

import pytest

from sturnus.domain.session import EndReason, SessionMachine, SessionState, SessionTimeouts

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)


def machine() -> SessionMachine:
    return SessionMachine(SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4))


def test_starts_idle() -> None:
    assert machine().state is SessionState.IDLE


def test_first_consented_participant_starts_recording() -> None:
    m = machine()
    m.participants_changed(1, T0)
    assert m.state is SessionState.RECORDING
    assert m.started_at == T0


def test_participant_without_consent_does_not_start() -> None:
    m = machine()
    m.participants_changed(0, T0)
    assert m.state is SessionState.IDLE
    assert m.started_at is None


def test_last_participant_leaving_enters_grace() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=5))
    assert m.state is SessionState.GRACE


def test_return_within_grace_resumes_same_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=5))
    m.participants_changed(1, T0 + timedelta(minutes=5, seconds=30))
    assert m.state is SessionState.RECORDING
    assert m.started_at == T0  # same session, no restart


def test_grace_expiry_closes_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=5))
    assert m.tick(T0 + timedelta(minutes=5, seconds=59)) is None
    assert m.tick(T0 + timedelta(minutes=6, seconds=1)) is EndReason.EMPTY
    assert m.state is SessionState.CLOSING


def test_idle_timeout_closes_session() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(minutes=1))
    assert m.tick(T0 + timedelta(minutes=15)) is None
    assert m.tick(T0 + timedelta(minutes=16, seconds=1)) is EndReason.IDLE_TIMEOUT


def test_audio_resets_idle_timer() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(minutes=14))
    assert m.tick(T0 + timedelta(minutes=20)) is None


def test_max_duration_closes_even_while_speaking() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(hours=3, minutes=59))
    assert m.tick(T0 + timedelta(hours=4, seconds=1)) is EndReason.MAX_DURATION


def test_tick_is_idempotent_after_closing() -> None:
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0)
    assert m.tick(T0 + timedelta(seconds=61)) is EndReason.EMPTY
    assert m.tick(T0 + timedelta(seconds=62)) is None  # doesn't report twice
    assert m.end_reason is EndReason.EMPTY


def test_tick_before_start_does_nothing() -> None:
    assert machine().tick(T0 + timedelta(hours=10)) is None


def test_naive_datetime_is_rejected() -> None:
    m = machine()
    with pytest.raises(ValueError, match="timezone-aware"):
        m.participants_changed(1, datetime(2026, 8, 19, 20, 0, 0))
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/domain/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sturnus.domain.session'`

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/domain/session.py
"""State machine for a recording session.

Knows nothing about Discord or the database; time is passed in on every
call so that all transitions are deterministically testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class SessionState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    GRACE = "grace"
    CLOSING = "closing"


class EndReason(StrEnum):
    EMPTY = "empty"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"


@dataclass(frozen=True)
class SessionTimeouts:
    empty_grace_seconds: int = 60
    idle_timeout_minutes: int = 15
    max_session_hours: int = 4


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value


class SessionMachine:
    def __init__(self, timeouts: SessionTimeouts) -> None:
        self._timeouts = timeouts
        self.state: SessionState = SessionState.IDLE
        self.started_at: datetime | None = None
        self.end_reason: EndReason | None = None
        self._last_audio_at: datetime | None = None
        self._grace_since: datetime | None = None

    def participants_changed(self, consented_count: int, now: datetime) -> None:
        """Reports how many consenting participants are in the channel."""
        _require_aware(now)
        if self.state is SessionState.CLOSING:
            return
        if consented_count > 0:
            if self.state is SessionState.IDLE:
                self.started_at = now
                self._last_audio_at = now
            self.state = SessionState.RECORDING
            self._grace_since = None
        elif self.state is SessionState.RECORDING:
            self.state = SessionState.GRACE
            self._grace_since = now

    def audio_received(self, now: datetime) -> None:
        _require_aware(now)
        self._last_audio_at = now

    def tick(self, now: datetime) -> EndReason | None:
        """Checks the time conditions. Returns the reason once the session closes.

        Reports each closure exactly once; further calls return None.
        """
        _require_aware(now)
        if self.state in (SessionState.IDLE, SessionState.CLOSING):
            return None
        assert self.started_at is not None

        reason = self._due_reason(now)
        if reason is None:
            return None
        self.state = SessionState.CLOSING
        self.end_reason = reason
        return reason

    def _due_reason(self, now: datetime) -> EndReason | None:
        assert self.started_at is not None
        if now - self.started_at > timedelta(hours=self._timeouts.max_session_hours):
            return EndReason.MAX_DURATION
        if self._grace_since is not None:
            if now - self._grace_since > timedelta(seconds=self._timeouts.empty_grace_seconds):
                return EndReason.EMPTY
        if self._last_audio_at is not None:
            if now - self._last_audio_at > timedelta(minutes=self._timeouts.idle_timeout_minutes):
                return EndReason.IDLE_TIMEOUT
        return None
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/domain/test_session.py -v`
Expected: all PASS

- [ ] **Step 5: Linter, type check, and architecture test**

Run: `uv run ruff check . && uv run mypy && uv run pytest -v`
Expected: everything clean

- [ ] **Step 6: Commit**

```bash
git add src/sturnus/domain/session.py tests/domain/test_session.py
git commit -m "feat: add session state machine with injected clock"
```

---

### Task 4: Time Reconstruction from RTP Timestamps

Implements Spec 6.2, including SSRC changes on reconnection and
overflow of the 32-bit counter.

**Files:**
- Create: `src/sturnus/domain/timeline.py`
- Test: `tests/domain/test_timeline.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `RTP_CLOCK_HZ: int = 48000`
  - `SpeakerClock()` with `absolute_time(ssrc: int, rtp_timestamp: int, wall_now: datetime) -> datetime`
    and `reset(ssrc: int) -> None`

- [ ] **Step 1: Write the test**

```python
# tests/domain/test_timeline.py
from datetime import datetime, timedelta, timezone

import pytest

from sturnus.domain.timeline import RTP_CLOCK_HZ, SpeakerClock

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
SSRC = 111


def test_first_packet_defines_the_reference() -> None:
    clock = SpeakerClock()
    assert clock.absolute_time(SSRC, 5_000_000, T0) == T0


def test_later_packet_uses_rtp_delta_not_wall_clock() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 5_000_000, T0)
    # one second in RTP ticks, but the wall clock claims 30 seconds
    later = clock.absolute_time(SSRC, 5_000_000 + RTP_CLOCK_HZ, T0 + timedelta(seconds=30))
    assert later == T0 + timedelta(seconds=1)


def test_silence_gap_is_reconstructed_from_timestamps() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 1_000, T0)
    # five minutes of silence: no packets arrived, the timestamp jumps
    resumed = clock.absolute_time(SSRC, 1_000 + RTP_CLOCK_HZ * 300, T0 + timedelta(minutes=99))
    assert resumed == T0 + timedelta(minutes=5)


def test_separate_ssrcs_keep_separate_references() -> None:
    clock = SpeakerClock()
    clock.absolute_time(111, 7_000, T0)
    other = clock.absolute_time(222, 9_999_999, T0 + timedelta(seconds=10))
    assert other == T0 + timedelta(seconds=10)


def test_reconnect_with_new_ssrc_starts_new_reference() -> None:
    clock = SpeakerClock()
    clock.absolute_time(111, 1_000, T0)
    reconnected = clock.absolute_time(333, 42, T0 + timedelta(minutes=2))
    assert reconnected == T0 + timedelta(minutes=2)


def test_timestamp_wraparound_is_handled() -> None:
    clock = SpeakerClock()
    start = 2**32 - RTP_CLOCK_HZ  # one second before the overflow
    clock.absolute_time(SSRC, start, T0)
    wrapped = clock.absolute_time(SSRC, RTP_CLOCK_HZ, T0 + timedelta(seconds=99))
    assert wrapped == T0 + timedelta(seconds=2)


def test_reset_drops_the_reference() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 1_000, T0)
    clock.reset(SSRC)
    again = clock.absolute_time(SSRC, 500_000, T0 + timedelta(minutes=1))
    assert again == T0 + timedelta(minutes=1)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SpeakerClock().absolute_time(SSRC, 1, datetime(2026, 8, 19))
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/domain/test_timeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sturnus.domain.timeline'`

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/domain/timeline.py
"""Converts RTP timestamps into absolute time.

Discord sends no packets during silence, so the position of a speech
segment can't be derived from arrival time. The RTP timestamp, however,
keeps running gap-free at 48 kHz.
"""

from __future__ import annotations

from datetime import datetime, timedelta

RTP_CLOCK_HZ = 48_000
_RTP_MODULO = 2**32


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value


class SpeakerClock:
    """Holds, per SSRC, the reference point from the first packet and wall-clock time."""

    def __init__(self) -> None:
        self._references: dict[int, tuple[datetime, int]] = {}

    def absolute_time(self, ssrc: int, rtp_timestamp: int, wall_now: datetime) -> datetime:
        _require_aware(wall_now)
        reference = self._references.get(ssrc)
        if reference is None:
            self._references[ssrc] = (wall_now, rtp_timestamp)
            return wall_now

        wall_first, rtp_first = reference
        # The counter is 32 bits wide and overflows after roughly 24.8 hours.
        # Modular arithmetic yields the correct difference even across the
        # overflow, as long as it's smaller than half the value range.
        delta_ticks = (rtp_timestamp - rtp_first) % _RTP_MODULO
        return wall_first + timedelta(seconds=delta_ticks / RTP_CLOCK_HZ)

    def reset(self, ssrc: int) -> None:
        """Discards the reference point, e.g. after a reconnection."""
        self._references.pop(ssrc, None)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/domain/test_timeline.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/sturnus/domain/timeline.py tests/domain/test_timeline.py
git commit -m "feat: reconstruct absolute time from rtp timestamps"
```

---

### Task 5: Transcript Model and Merging

Implements Spec 8.1: a target-neutral model with no markup at all, from
which each adapter later renders its own format.

**Files:**
- Create: `src/sturnus/domain/transcript.py`
- Test: `tests/domain/test_transcript.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `SpeakerIdentity(discord_user_id: int, discord_display_name: str, external_user_id: str | None, external_display_name: str | None)`
  - `Segment(speaker: SpeakerIdentity, start: datetime, end: datetime, text: str)`
  - `TranscriptBlock(speaker: SpeakerIdentity, start: datetime, text: str)`
  - `Transcript(session_started_at: datetime, session_ended_at: datetime, participants: tuple[SpeakerIdentity, ...], blocks: tuple[TranscriptBlock, ...])`
  - `build_transcript(segments, session_started_at, session_ended_at, merge_gap=timedelta(seconds=15)) -> Transcript`

- [ ] **Step 1: Write the test**

```python
# tests/domain/test_transcript.py
from datetime import datetime, timedelta, timezone

from sturnus.domain.transcript import Segment, SpeakerIdentity, build_transcript

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
ANNA = SpeakerIdentity(1, "anna", external_user_id="out-1", external_display_name="Anna Example")
BEN = SpeakerIdentity(2, "ben")


def seg(speaker: SpeakerIdentity, offset: int, length: int, text: str) -> Segment:
    return Segment(
        speaker=speaker,
        start=T0 + timedelta(seconds=offset),
        end=T0 + timedelta(seconds=offset + length),
        text=text,
    )


def build(*segments: Segment):
    return build_transcript(list(segments), T0, T0 + timedelta(hours=1))


def test_blocks_are_ordered_by_time_across_speakers() -> None:
    t = build(seg(BEN, 30, 3, "second"), seg(ANNA, 0, 2, "first"))
    assert [b.text for b in t.blocks] == ["first", "second"]


def test_consecutive_segments_of_same_speaker_merge() -> None:
    t = build(seg(ANNA, 0, 2, "first half"), seg(ANNA, 3, 2, "second half"))
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "first half second half"
    assert t.blocks[0].start == T0


def test_long_pause_splits_a_block() -> None:
    t = build(seg(ANNA, 0, 2, "before"), seg(ANNA, 300, 2, "after"))
    assert [b.text for b in t.blocks] == ["before", "after"]


def test_other_speaker_interrupts_a_block() -> None:
    t = build(seg(ANNA, 0, 2, "one"), seg(BEN, 3, 1, "interjection"), seg(ANNA, 5, 2, "three"))
    assert [b.text for b in t.blocks] == ["one", "interjection", "three"]


def test_participants_are_unique_and_ordered_by_first_appearance() -> None:
    t = build(seg(BEN, 0, 1, "b"), seg(ANNA, 5, 1, "a"), seg(BEN, 9, 1, "b again"))
    assert t.participants == (BEN, ANNA)


def test_empty_and_whitespace_segments_are_dropped() -> None:
    t = build(seg(ANNA, 0, 1, "   "), seg(ANNA, 60, 1, "real"))
    assert [b.text for b in t.blocks] == ["real"]


def test_no_segments_yields_empty_transcript() -> None:
    t = build()
    assert t.blocks == ()
    assert t.participants == ()


def test_transcript_carries_session_bounds() -> None:
    t = build(seg(ANNA, 0, 1, "x"))
    assert t.session_started_at == T0
    assert t.session_ended_at == T0 + timedelta(hours=1)


def test_model_carries_no_markup() -> None:
    t = build(seg(ANNA, 0, 1, "plain text"))
    assert t.blocks[0].text == "plain text"
    assert t.blocks[0].speaker.external_display_name == "Anna Example"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/domain/test_transcript.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sturnus.domain.transcript'`

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/domain/transcript.py
"""Target-neutral transcript model.

Deliberately contains no markup: which parts of a speaker identity show
up in the result, and in what form, is decided solely by the respective
adapter via its template.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_MERGE_GAP = timedelta(seconds=15)


@dataclass(frozen=True)
class SpeakerIdentity:
    discord_user_id: int
    discord_display_name: str
    external_user_id: str | None = None
    external_display_name: str | None = None


@dataclass(frozen=True)
class Segment:
    speaker: SpeakerIdentity
    start: datetime
    end: datetime
    text: str


@dataclass(frozen=True)
class TranscriptBlock:
    speaker: SpeakerIdentity
    start: datetime
    text: str


@dataclass(frozen=True)
class Transcript:
    session_started_at: datetime
    session_ended_at: datetime
    participants: tuple[SpeakerIdentity, ...]
    blocks: tuple[TranscriptBlock, ...]


def build_transcript(
    segments: Iterable[Segment],
    session_started_at: datetime,
    session_ended_at: datetime,
    merge_gap: timedelta = DEFAULT_MERGE_GAP,
) -> Transcript:
    """Orders segments from all speakers chronologically and merges them into blocks."""
    usable = sorted(
        (s for s in segments if s.text.strip()),
        key=lambda s: (s.start, s.speaker.discord_user_id),
    )

    blocks: list[TranscriptBlock] = []
    participants: list[SpeakerIdentity] = []
    open_speaker: SpeakerIdentity | None = None
    open_start: datetime | None = None
    open_end: datetime | None = None
    open_parts: list[str] = []

    def flush() -> None:
        nonlocal open_speaker, open_start, open_end, open_parts
        if open_speaker is not None and open_start is not None:
            blocks.append(TranscriptBlock(open_speaker, open_start, " ".join(open_parts)))
        open_speaker, open_start, open_end, open_parts = None, None, None, []

    for segment in usable:
        if segment.speaker not in participants:
            participants.append(segment.speaker)

        continues = (
            open_speaker == segment.speaker
            and open_end is not None
            and segment.start - open_end <= merge_gap
        )
        if not continues:
            flush()
            open_speaker = segment.speaker
            open_start = segment.start

        open_parts.append(segment.text.strip())
        open_end = segment.end

    flush()

    return Transcript(
        session_started_at=session_started_at,
        session_ended_at=session_ended_at,
        participants=tuple(participants),
        blocks=tuple(blocks),
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/domain/test_transcript.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/sturnus/domain/transcript.py tests/domain/test_transcript.py
git commit -m "feat: add target-neutral transcript model with block merging"
```

---

### Task 6: Consent Resolution

Implements Spec 3.1 and 3.3. Consent expires on revocation and on a
change to the privacy policy.

**Files:**
- Create: `src/sturnus/domain/consent.py`
- Test: `tests/domain/test_consent.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `ConsentRecord(granted_at: datetime | None, revoked_at: datetime | None, policy_version: str | None)`
  - `is_consent_active(record: ConsentRecord | None, current_policy_version: str) -> bool`
  - `may_record(record: ConsentRecord | None, current_policy_version: str, has_consent_role: bool) -> bool`

- [ ] **Step 1: Write the test**

```python
# tests/domain/test_consent.py
from datetime import datetime, timezone

from sturnus.domain.consent import ConsentRecord, is_consent_active, may_record

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
POLICY = "2026-08-01"


def granted(version: str = POLICY) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=None, policy_version=version)


def test_granted_consent_is_active() -> None:
    assert is_consent_active(granted(), POLICY) is True


def test_missing_record_is_not_active() -> None:
    assert is_consent_active(None, POLICY) is False


def test_revoked_consent_is_not_active() -> None:
    record = ConsentRecord(granted_at=T0, revoked_at=T0, policy_version=POLICY)
    assert is_consent_active(record, POLICY) is False


def test_outdated_policy_version_invalidates_consent() -> None:
    assert is_consent_active(granted("2026-01-01"), POLICY) is False


def test_recording_requires_both_role_and_consent() -> None:
    # The role check alone isn't enough: administrators bypass channel
    # permissions, which is why the record is also checked.
    assert may_record(granted(), POLICY, has_consent_role=True) is True
    assert may_record(granted(), POLICY, has_consent_role=False) is False
    assert may_record(None, POLICY, has_consent_role=True) is False


def test_revoked_user_with_stale_role_may_not_be_recorded() -> None:
    record = ConsentRecord(granted_at=T0, revoked_at=T0, policy_version=POLICY)
    assert may_record(record, POLICY, has_consent_role=True) is False
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/domain/test_consent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sturnus.domain.consent'`

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/domain/consent.py
"""Consent resolution.

The Discord role is the first line of defense, but not the only one:
users with administrator rights bypass channel permissions and could
speak without the role. That's why the stored record always decides too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConsentRecord:
    granted_at: datetime | None
    revoked_at: datetime | None
    policy_version: str | None


def is_consent_active(record: ConsentRecord | None, current_policy_version: str) -> bool:
    """Consent expires through revocation and through a changed policy."""
    if record is None or record.granted_at is None:
        return False
    if record.revoked_at is not None:
        return False
    return record.policy_version == current_policy_version


def may_record(
    record: ConsentRecord | None,
    current_policy_version: str,
    has_consent_role: bool,
) -> bool:
    return has_consent_role and is_consent_active(record, current_policy_version)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/domain/test_consent.py -v`
Expected: all PASS

- [ ] **Step 5: Full run and commit**

```bash
uv run ruff check . && uv run mypy && uv run pytest -v
git add src/sturnus/domain/consent.py tests/domain/test_consent.py
git commit -m "feat: add consent resolution with policy versioning"
```

---

### Task 7: Database Models and First Migration

Implements Spec 9. From here on SQLAlchemy is used — exclusively in
`infrastructure/db/`, never in `domain/`.

**Files:**
- Create: `src/sturnus/infrastructure/db/__init__.py`, `src/sturnus/infrastructure/db/models.py`
- Create: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial.py`
- Test: `tests/infrastructure/test_migrations.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing from previous tasks
- Produces:
  - `Base` (DeclarativeBase)
  - Models `GuildConfig`, `AccountLink`, `Consent`, `OAuthState`, `Session`, `SessionParticipant`, `TranscriptionJob`
  - pytest fixtures `postgres_url: str` (session-scoped) and `clean_database: str` (resets the schema per test)

- [ ] **Step 1: Write the Postgres test foundation**

```python
# tests/conftest.py
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """One container for the whole test run — starting it up costs seconds."""
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture
def clean_database(postgres_url: str) -> str:
    """Fully resets the schema before every test.

    Necessary because all tests share one container, and tables can come
    from two paths: via Alembic (with `alembic_version`) and via
    `create_all` (without). A plain `drop_all` would leave Alembic's
    bookkeeping in place, causing a later `upgrade head` to fail on tables
    that already exist. Dropping the schema handles both cases.
    """
    engine = create_engine(postgres_url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return postgres_url
```

Every test that touches the database depends on `clean_database` rather
than `postgres_url` — that makes test order irrelevant.

- [ ] **Step 2: Write the migration test**

```python
# tests/infrastructure/test_migrations.py
"""Migration tests deliberately run synchronously.

`alembic.command.*` is a synchronous API; called from an `async def`
test it breaks inside the running event loop.
"""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from sturnus.infrastructure.db.models import Base

EXPECTED_TABLES = {
    "guild_config",
    "account_link",
    "consent",
    "oauth_state",
    "session",
    "session_participant",
    "transcription_job",
}


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "+psycopg")


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url(url))
    return cfg


def _table_names(url: str) -> set[str]:
    engine = create_engine(_sync_url(url))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        return {row[0] for row in rows}


def test_migration_creates_every_table(clean_database: str) -> None:
    command.upgrade(_alembic_config(clean_database), "head")
    assert EXPECTED_TABLES <= _table_names(clean_database)


def test_downgrade_removes_the_tables(clean_database: str) -> None:
    cfg = _alembic_config(clean_database)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert not (EXPECTED_TABLES & _table_names(clean_database))


def test_models_and_migration_do_not_drift(clean_database: str) -> None:
    """After `upgrade head`, an autogenerate must find nothing left to do.

    Without this test, a model change with no matching migration goes
    unnoticed until it shows up in production.
    """
    command.upgrade(_alembic_config(clean_database), "head")

    engine = create_engine(_sync_url(clean_database))
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"models and migration have drifted: {diff}"
```

The drift test is the most valuable of the three: it fails as soon as
someone adds a field without running `alembic revision --autogenerate`.

- [ ] **Step 3: Add `psycopg` as a test dependency and run the test**

```bash
uv add --group test "psycopg[binary]>=3.2"
uv run pytest tests/infrastructure/test_migrations.py -v
```
Expected: FAIL — `alembic.ini` doesn't exist (`FileNotFoundError`, or Alembic reports the missing configuration)

- [ ] **Step 4: Write the models**

```python
# src/sturnus/infrastructure/db/models.py
"""SQLAlchemy models. The system's only data access path."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GuildConfig(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountLink(Base):
    __tablename__ = "account_link"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    __tablename__ = "consent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_consent_user_guild", "discord_user_id", "guild_id"),
    )


class OAuthState(Base):
    __tablename__ = "oauth_state"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    document_provider: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[str | None] = mapped_column(Text)
    document_url: Mapped[str | None] = mapped_column(Text)
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_session_status", "status"),
    )


class SessionParticipant(Base):
    __tablename__ = "session_participant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("session.id", ondelete="CASCADE"))
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_display_name: Mapped[str] = mapped_column(Text, nullable=False)
    detected_language: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("session_id", "discord_user_id", name="uq_participant_per_session"),
    )


class TranscriptionJob(Base):
    __tablename__ = "transcription_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("session.id", ondelete="CASCADE"))
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    audio_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("session_id", "discord_user_id", name="uq_job_per_speaker"),
        Index("ix_job_status", "status"),
        Index("ix_job_retention", "retention_until"),
    )
```

`wrapped_data_key` holds the session's data key encrypted with the
master key (Spec 12.1); `encryption_key_id` names the master key used,
so that rotating it remains possible without re-encryption.

- [ ] **Step 5: Set up Alembic — synchronous, not async**

```bash
uv run alembic init migrations
```

**Deliberately without `-t async`.** The application runs asynchronously
via `asyncpg`; the migrations run synchronously via `psycopg`. Reason:
with an asynchronous `env.py`, `alembic.command.upgrade()` internally
calls `asyncio.run()` — inside a running pytest-asyncio event loop that
breaks with `RuntimeError: asyncio.run() cannot be called from a running
event loop`. Keeping the migrations synchronous makes them callable
equally from tests and from an init container.

Clear the URL in `alembic.ini`, since it's set at runtime:

```ini
sqlalchemy.url =
```

`migrations/env.py` gets the metadata and the URL resolution. Replace
the line `target_metadata = None` generated by `alembic init` with:

```python
import os

from sturnus.infrastructure.db.models import Base

target_metadata = Base.metadata


def _resolve_url() -> str:
    """URL from -x url=..., else from DATABASE_URL. asyncpg becomes psycopg."""
    from alembic import context as _context

    supplied = _context.get_x_argument(as_dictionary=True).get("url")
    url = supplied or config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("no database url: pass -x url=... or set DATABASE_URL")
    return url.replace("+asyncpg", "+psycopg")


config.set_main_option("sqlalchemy.url", _resolve_url())
```

This line must come **before** `run_migrations_offline()`/
`run_migrations_online()`, so both paths see the resolved URL.

- [ ] **Step 6: Generate the first migration**

```bash
docker run -d --name sturnus-pg -e POSTGRES_USER=sturnus -e POSTGRES_PASSWORD=sturnus \
  -e POSTGRES_DB=sturnus -p 5432:5432 postgres:17-alpine
sleep 3
uv run alembic -x url="postgresql://sturnus:sturnus@localhost:5432/sturnus" \
  revision --autogenerate -m "initial schema"
```

Check the generated file under `migrations/versions/`: it must create
all seven tables and remove them again in `downgrade()`. If one is
missing, the model wasn't imported. Afterward run
`docker rm -f sturnus-pg`.

- [ ] **Step 7: Run the migration tests**

```bash
uv run pytest tests/infrastructure/test_migrations.py -v
```
Expected: all three PASS. If `test_models_and_migration_do_not_drift`
fails, something is missing from the generated migration — the output
names the discrepancy.

- [ ] **Step 8: Architecture test and full run**

Run: `uv run ruff check . && uv run mypy && uv run pytest -v`
Expected: everything green — in particular, `test_domain_has_no_outward_imports` does not trigger, because SQLAlchemy is only used under `infrastructure/`

- [ ] **Step 9: Commit**

```bash
git add src/sturnus/infrastructure alembic.ini migrations tests/conftest.py tests/infrastructure
git commit -m "feat: add sqlalchemy models and initial alembic migration"
```

---

### Task 8: Config Store with Precedence Resolution

Implements Spec 11: per-guild values, falling back to defaults from
the code when nothing is stored.

**Files:**
- Create: `src/sturnus/infrastructure/db/config_store.py`
- Create: `src/sturnus/domain/settings.py`
- Test: `tests/infrastructure/test_config_store.py`

**Interfaces:**
- Consumes: `Base`, `GuildConfig` from Task 7; `SessionTimeouts` from Task 3
- Produces:
  - `DEFAULTS: dict[str, str]` in `domain/settings.py`
  - `ConfigStore(session_factory)` with
    `get(guild_id: int, key: str) -> str | None`,
    `set(guild_id: int, key: str, value: str | None, now: datetime) -> None`,
    `timeouts(guild_id: int) -> SessionTimeouts`

- [ ] **Step 1: Write default values as pure domain data**

```python
# src/sturnus/domain/settings.py
"""Default values for runtime configuration (Spec 11)."""

from __future__ import annotations

VOICE_CHANNEL_ID = "voice_channel_id"
CONSENT_ROLE_ID = "consent_role_id"
EMPTY_GRACE_SECONDS = "empty_grace_seconds"
IDLE_TIMEOUT_MINUTES = "idle_timeout_minutes"
MAX_SESSION_HOURS = "max_session_hours"
PUBLISH_POLL_SECONDS = "publish_poll_seconds"
DOCUMENT_PROVIDER = "document_provider"
DOCUMENT_TARGET = "document_target"
AUDIO_RETENTION_DAYS = "audio_retention_days"
POLICY_VERSION = "policy_version"
POLICY_URL = "policy_url"

DEFAULTS: dict[str, str] = {
    EMPTY_GRACE_SECONDS: "60",
    IDLE_TIMEOUT_MINUTES: "15",
    MAX_SESSION_HOURS: "4",
    PUBLISH_POLL_SECONDS: "30",
    DOCUMENT_PROVIDER: "outline",
    AUDIO_RETENTION_DAYS: "30",
}

# No default value, so these must be set before going live.
REQUIRED_KEYS: frozenset[str] = frozenset(
    {VOICE_CHANNEL_ID, CONSENT_ROLE_ID, DOCUMENT_TARGET, POLICY_VERSION, POLICY_URL}
)
```

- [ ] **Step 2: Write the test**

```python
# tests/infrastructure/test_config_store.py
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
GUILD = 4711


@pytest.fixture
async def store(clean_database: str) -> ConfigStore:
    engine: AsyncEngine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return ConfigStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_unset_key_without_default_is_none(store: ConfigStore) -> None:
    assert await store.get(GUILD, settings.VOICE_CHANNEL_ID) is None


async def test_unset_key_falls_back_to_default(store: ConfigStore) -> None:
    assert await store.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "15"


async def test_stored_value_wins_over_default(store: ConfigStore) -> None:
    await store.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    assert await store.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "45"


async def test_set_is_idempotent_and_updates_in_place(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "8", T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "8"


async def test_clearing_a_value_restores_the_default(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    await store.set(GUILD, settings.MAX_SESSION_HOURS, None, T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "4"


async def test_guilds_are_isolated(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    assert await store.get(9999, settings.MAX_SESSION_HOURS) == "4"


async def test_timeouts_are_assembled_from_config(store: ConfigStore) -> None:
    await store.set(GUILD, settings.EMPTY_GRACE_SECONDS, "90", T0)
    timeouts = await store.timeouts(GUILD)
    assert timeouts.empty_grace_seconds == 90
    assert timeouts.idle_timeout_minutes == 15  # default value
    assert timeouts.max_session_hours == 4
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `uv run pytest tests/infrastructure/test_config_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sturnus.infrastructure.db.config_store'`

- [ ] **Step 4: Write the implementation**

```python
# src/sturnus/infrastructure/db/config_store.py
"""Per-guild runtime configuration with fallback to the defaults."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from sturnus.domain import settings
from sturnus.domain.session import SessionTimeouts
from sturnus.infrastructure.db.models import GuildConfig


class ConfigStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, guild_id: int, key: str) -> str | None:
        async with self._session_factory() as session:
            stored = await session.scalar(
                select(GuildConfig.value).where(
                    GuildConfig.guild_id == guild_id, GuildConfig.key == key
                )
            )
        if stored is not None:
            return stored
        return settings.DEFAULTS.get(key)

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None:
        """Sets a value; `None` removes it and restores the default."""
        async with self._session_factory() as session:
            if value is None:
                await session.execute(
                    delete(GuildConfig).where(
                        GuildConfig.guild_id == guild_id, GuildConfig.key == key
                    )
                )
            else:
                statement = insert(GuildConfig).values(
                    guild_id=guild_id, key=key, value=value, updated_at=now
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[GuildConfig.guild_id, GuildConfig.key],
                        set_={"value": value, "updated_at": now},
                    )
                )
            await session.commit()

    async def timeouts(self, guild_id: int) -> SessionTimeouts:
        return SessionTimeouts(
            empty_grace_seconds=await self._int(guild_id, settings.EMPTY_GRACE_SECONDS),
            idle_timeout_minutes=await self._int(guild_id, settings.IDLE_TIMEOUT_MINUTES),
            max_session_hours=await self._int(guild_id, settings.MAX_SESSION_HOURS),
        )

    async def _int(self, guild_id: int, key: str) -> int:
        value = await self.get(guild_id, key)
        if value is None:
            raise KeyError(f"no value and no default for {key!r}")
        return int(value)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/infrastructure/test_config_store.py -v`
Expected: all PASS

- [ ] **Step 6: Full run**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -v`
Expected: everything green

- [ ] **Step 7: Commit**

```bash
git add src/sturnus/domain/settings.py src/sturnus/infrastructure/db/config_store.py tests/infrastructure/test_config_store.py
git commit -m "feat: add per-guild config store with default fallback"
```

---

## Conclusion of Plan 1

After Task 8, we have:

- A repository with CI that runs linter, type checking, and tests on every PR.
- Release Please, ready for the first `feat:` commit.
- The complete domain logic: state machine, time reconstruction,
  transcript assembly, consent resolution — all testable without I/O
  and without a Discord dependency.
- An enforced architecture test that guards the layering from here on.
- The complete database schema with migration, and the config store.

**Not yet present, and the subject of Plan 2:** Discord integration,
voice recording, encryption, S3.
