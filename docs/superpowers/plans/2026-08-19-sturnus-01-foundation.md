# Sturnus Plan 1: Fundament, Domäne und Persistenz

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein lauffähiges Python-Repository mit CI, der vollständig getesteten Domänenlogik (Session-Zustände, Zeitrekonstruktion, Transkript-Aufbau, Einwilligung) und der Datenbankschicht samt Migrationen.

**Architecture:** Schichtung mit nach innen gerichteter Abhängigkeitsregel. `domain/` enthält reine Logik ohne jede I/O und ohne Fremdbibliotheken; `infrastructure/db/` kapselt SQLAlchemy. Die Regel wird durch einen Test erzwungen, der ab Task 2 gilt — jede spätere Verletzung schlägt in der CI fehl.

**Tech Stack:** Python 3.12, uv, SQLAlchemy 2.0 (async, `asyncpg`), Alembic, pytest, pytest-asyncio, testcontainers, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-19-sturnus-design.md`

## Global Constraints

- **Python `>=3.12`**, Paketverwaltung ausschließlich über `uv`.
- **Abhängigkeitsregel:** `sturnus.domain` importiert weder `sturnus.application` noch `sturnus.infrastructure` noch eine Bibliothek mit I/O (`discord`, `sqlalchemy`, `boto3`, `jinja2`, `faster_whisper`). Durchgesetzt durch `tests/test_architecture.py`.
- **Ein Datenzugriffsweg:** ausschließlich SQLAlchemy 2.0 ORM im async-Modus. Rohes `asyncpg` neben dem ORM ist ausgeschlossen (Spec 9).
- **Schemaänderungen ausschließlich über Alembic.** Kein `create_all()` im Produktivpfad.
- **Conventional Commits** — Eingabe für Release Please. Commit-Präfixe: `feat:`, `fix:`, `chore:`, `test:`, `docs:`, `refactor:`.
- **Keine Claude-Attribution** in Commits (Organisationsvorgabe).
- **Zeitangaben sind `datetime` mit `timezone.utc`**, niemals naiv.
- Version steht **nur** in `pyproject.toml` mit dem Marker `# x-release-please-version`.

---

### Task 1: Repository-Grundgerüst, Werkzeuge und CI

**Files:**
- Create: `pyproject.toml`, `.python-version`, `README.md`, `CHANGELOG.md`
- Create: `release-please-config.json`, `.release-please-manifest.json`
- Create: `src/sturnus/__init__.py`, `src/sturnus/domain/__init__.py`, `src/sturnus/application/__init__.py`, `src/sturnus/infrastructure/__init__.py`
- Create: `.github/workflows/build.yml`, `.github/workflows/release-please.yml`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: nichts
- Produces: Paket `sturnus` importierbar; `sturnus.__version__` als `str`

- [ ] **Step 1: `pyproject.toml` anlegen**

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
requires = ["uv_build>=0.9.4,<0.10.0"]
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

# Diese drei bringen keine Typinformationen mit. Die Strenge bleibt für
# eigenen Code unangetastet; ohne diesen Block scheitert der Testlauf an
# import-untyped, ohne dass ein Fehler im Code vorliegt.
[[tool.mypy.overrides]]
module = ["testcontainers.*", "alembic.*", "psycopg.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Die Einsprungpunkte unter `[project.scripts]` zeigen auf Module, die erst in
späteren Plänen entstehen. Das ist beabsichtigt — `uv sync` prüft sie nicht,
erst ein Aufruf würde fehlschlagen.

- [ ] **Step 2: Paketstruktur und Version anlegen**

```bash
mkdir -p src/sturnus/{domain,application,infrastructure} tests
echo "3.12" > .python-version
printf '__version__ = "0.1.0"\n' > src/sturnus/__init__.py
touch src/sturnus/domain/__init__.py src/sturnus/application/__init__.py src/sturnus/infrastructure/__init__.py
printf '# Changelog\n' > CHANGELOG.md
printf '# Sturnus\n\nDiscord voice transcription with Outline document output.\n' > README.md
```

- [ ] **Step 3: Rauchtest schreiben**

```python
# tests/test_smoke.py
import sturnus


def test_package_exposes_version() -> None:
    assert isinstance(sturnus.__version__, str)
    assert sturnus.__version__.count(".") == 2
```

- [ ] **Step 4: Abhängigkeiten installieren und Test laufen lassen**

Run: `uv sync --all-groups && uv run pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Linter und Typprüfung laufen lassen**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: beide ohne Befund. Bei Formatierungsbefund `uv run ruff format .` ausführen und erneut prüfen.

- [ ] **Step 6: Release-Please-Dateien anlegen**

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

Der Eintrag für `charts/sturnus/Chart.yaml` in `extra-files` kommt in Plan 4
dazu, sobald das Chart existiert — ein Verweis auf eine fehlende Datei ließe
Release Please fehlschlagen.

- [ ] **Step 7: PR-Ablauf anlegen**

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

Dieser Ablauf ist bewusst repositoryeigen: der zentrale Katalog hat kein
Python-Gegenstück zu `gradle-build-pr.yml` (Spec 13.3).

- [ ] **Step 8: Release-Please-Ablauf anlegen**

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

Der `publish`-Auftrag, der `docker-publish.yml@v2.4.0` einhängt, kommt in
Plan 4 dazu — ohne Dockerfile hätte er nichts zu bauen. Ein per Tag
ausgelöster Ablauf wird nicht angelegt (Spec 13.1).

- [ ] **Step 9: `.gitignore` ergänzen und committen**

```bash
printf '.venv/\n__pycache__/\n*.pyc\n.env\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n' > .gitignore
git add -A
git commit -m "chore: scaffold python project with ci and release-please"
```

---

### Task 2: Architektur-Test für die Abhängigkeitsregel

Dieser Test kommt vor der Domänenlogik, damit die Regel ab der ersten
Zeile Domänencode gilt statt nachträglich durchgesetzt zu werden.

**Files:**
- Test: `tests/test_architecture.py`

**Interfaces:**
- Consumes: Paketstruktur aus Task 1
- Produces: nichts (reiner Test)

- [ ] **Step 1: Test schreiben**

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
            # level > 0 ist ein relativer Import und bleibt damit im Paket
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def test_domain_has_no_outward_imports() -> None:
    violations: list[str] = []
    for path in DOMAIN.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")
    assert not violations, "domain darf nicht nach außen importieren:\n" + "\n".join(violations)


def test_application_does_not_import_infrastructure() -> None:
    app = DOMAIN.parent / "application"
    violations: list[str] = []
    for path in app.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith("sturnus.infrastructure"):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")
    assert not violations, "application darf infrastructure nicht importieren:\n" + "\n".join(violations)
```

- [ ] **Step 2: Test laufen lassen**

Run: `uv run pytest tests/test_architecture.py -v`
Expected: PASS (beide Pakete sind noch leer, es gibt nichts zu verletzen)

- [ ] **Step 3: Test gegen eine echte Verletzung prüfen**

```bash
printf 'import sqlalchemy\n' > src/sturnus/domain/_probe.py
uv run pytest tests/test_architecture.py -v
```
Expected: FAIL mit `domain darf nicht nach außen importieren: domain/_probe.py: sqlalchemy`

Ein Test, der nie fehlschlägt, prüft nichts — dieser Schritt belegt, dass er greift.

- [ ] **Step 4: Sonde entfernen und erneut prüfen**

```bash
rm src/sturnus/domain/_probe.py
uv run pytest tests/test_architecture.py -v
```
Expected: PASS

- [ ] **Step 5: Committen**

```bash
git add tests/test_architecture.py
git commit -m "test: enforce inward dependency rule for domain layer"
```

---

### Task 3: Session-Zustandsautomat

Setzt Spec 5.1 um. Der Automat kennt weder Discord noch Datenbank und
erhält die Zeit von außen.

**Files:**
- Create: `src/sturnus/domain/session.py`
- Test: `tests/domain/test_session.py`

**Interfaces:**
- Consumes: nichts
- Produces:
  - `SessionState` (StrEnum): `IDLE`, `RECORDING`, `GRACE`, `CLOSING`
  - `EndReason` (StrEnum): `EMPTY`, `IDLE_TIMEOUT`, `MAX_DURATION`
  - `SessionTimeouts(empty_grace_seconds: int, idle_timeout_minutes: int, max_session_hours: int)`
  - `SessionMachine(timeouts: SessionTimeouts)` mit
    `state: SessionState`, `started_at: datetime | None`, `end_reason: EndReason | None`,
    `participants_changed(consented_count: int, now: datetime) -> None`,
    `audio_received(now: datetime) -> None`,
    `tick(now: datetime) -> EndReason | None`

- [ ] **Step 1: Test schreiben**

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
    assert m.started_at == T0  # dieselbe Session, kein Neustart


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
    assert m.tick(T0 + timedelta(seconds=62)) is None  # meldet nicht doppelt
    assert m.end_reason is EndReason.EMPTY


def test_tick_before_start_does_nothing() -> None:
    assert machine().tick(T0 + timedelta(hours=10)) is None


def test_naive_datetime_is_rejected() -> None:
    m = machine()
    with pytest.raises(ValueError, match="timezone-aware"):
        m.participants_changed(1, datetime(2026, 8, 19, 20, 0, 0))
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag prüfen**

Run: `uv run pytest tests/domain/test_session.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'sturnus.domain.session'`

- [ ] **Step 3: Implementierung schreiben**

```python
# src/sturnus/domain/session.py
"""Zustandsautomat einer Aufnahmesitzung.

Kennt weder Discord noch Datenbank; die Zeit wird bei jedem Aufruf
übergeben, damit sämtliche Übergänge deterministisch prüfbar sind.
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
        """Meldet, wie viele einwilligende Teilnehmer im Kanal sind."""
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
        """Prüft die Zeitbedingungen. Gibt den Grund zurück, sobald geschlossen wird.

        Meldet jeden Abschluss genau einmal; weitere Aufrufe geben None zurück.
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/domain/test_session.py -v`
Expected: alle PASS

- [ ] **Step 5: Linter, Typprüfung und Architekturtest**

Run: `uv run ruff check . && uv run mypy && uv run pytest -v`
Expected: alles ohne Befund

- [ ] **Step 6: Committen**

```bash
git add src/sturnus/domain/session.py tests/domain/test_session.py
git commit -m "feat: add session state machine with injected clock"
```

---

### Task 4: Zeitrekonstruktion aus RTP-Zeitstempeln

Setzt Spec 6.2 um, einschließlich Wechsel der SSRC bei Wiederverbindung
und Überlauf des 32-Bit-Zählers.

**Files:**
- Create: `src/sturnus/domain/timeline.py`
- Test: `tests/domain/test_timeline.py`

**Interfaces:**
- Consumes: nichts
- Produces:
  - `RTP_CLOCK_HZ: int = 48000`
  - `SpeakerClock()` mit `absolute_time(ssrc: int, rtp_timestamp: int, wall_now: datetime) -> datetime`
    und `reset(ssrc: int) -> None`

- [ ] **Step 1: Test schreiben**

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
    # eine Sekunde in RTP-Ticks, aber die Wanduhr behauptet 30 Sekunden
    later = clock.absolute_time(SSRC, 5_000_000 + RTP_CLOCK_HZ, T0 + timedelta(seconds=30))
    assert later == T0 + timedelta(seconds=1)


def test_silence_gap_is_reconstructed_from_timestamps() -> None:
    clock = SpeakerClock()
    clock.absolute_time(SSRC, 1_000, T0)
    # fünf Minuten Stille: es kamen keine Pakete, der Zeitstempel springt
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
    start = 2**32 - RTP_CLOCK_HZ  # eine Sekunde vor dem Überlauf
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

- [ ] **Step 2: Test laufen lassen, Fehlschlag prüfen**

Run: `uv run pytest tests/domain/test_timeline.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'sturnus.domain.timeline'`

- [ ] **Step 3: Implementierung schreiben**

```python
# src/sturnus/domain/timeline.py
"""Umrechnung von RTP-Zeitstempeln in absolute Zeit.

Discord sendet während Stille keine Pakete, weshalb sich die Position
eines Sprechabschnitts nicht aus der Ankunftszeit ergibt. Der RTP-Zeitstempel
läuft dagegen lückenlos mit 48 kHz weiter.
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
    """Hält je SSRC den Referenzpunkt aus erstem Paket und Wanduhrzeit."""

    def __init__(self) -> None:
        self._references: dict[int, tuple[datetime, int]] = {}

    def absolute_time(self, ssrc: int, rtp_timestamp: int, wall_now: datetime) -> datetime:
        _require_aware(wall_now)
        reference = self._references.get(ssrc)
        if reference is None:
            self._references[ssrc] = (wall_now, rtp_timestamp)
            return wall_now

        wall_first, rtp_first = reference
        # Der Zähler ist 32 Bit breit und läuft nach rund 24,8 Stunden über.
        # Die Restklassenrechnung liefert auch über den Überlauf hinweg die
        # richtige Differenz, solange sie kleiner als der halbe Wertebereich ist.
        delta_ticks = (rtp_timestamp - rtp_first) % _RTP_MODULO
        return wall_first + timedelta(seconds=delta_ticks / RTP_CLOCK_HZ)

    def reset(self, ssrc: int) -> None:
        """Verwirft den Referenzpunkt, etwa nach einer Wiederverbindung."""
        self._references.pop(ssrc, None)
```

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/domain/test_timeline.py -v`
Expected: alle PASS

- [ ] **Step 5: Committen**

```bash
git add src/sturnus/domain/timeline.py tests/domain/test_timeline.py
git commit -m "feat: reconstruct absolute time from rtp timestamps"
```

---

### Task 5: Transkript-Modell und Zusammenführung

Setzt Spec 8.1 um: ein zielneutrales Modell ohne jedes Markup, aus dem
später jeder Adapter sein eigenes Format rendert.

**Files:**
- Create: `src/sturnus/domain/transcript.py`
- Test: `tests/domain/test_transcript.py`

**Interfaces:**
- Consumes: nichts
- Produces:
  - `SpeakerIdentity(discord_user_id: int, discord_display_name: str, external_user_id: str | None, external_display_name: str | None)`
  - `Segment(speaker: SpeakerIdentity, start: datetime, end: datetime, text: str)`
  - `TranscriptBlock(speaker: SpeakerIdentity, start: datetime, text: str)`
  - `Transcript(session_started_at: datetime, session_ended_at: datetime, participants: tuple[SpeakerIdentity, ...], blocks: tuple[TranscriptBlock, ...])`
  - `build_transcript(segments, session_started_at, session_ended_at, merge_gap=timedelta(seconds=15)) -> Transcript`

- [ ] **Step 1: Test schreiben**

```python
# tests/domain/test_transcript.py
from datetime import datetime, timedelta, timezone

from sturnus.domain.transcript import Segment, SpeakerIdentity, build_transcript

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=timezone.utc)
ANNA = SpeakerIdentity(1, "anna", external_user_id="out-1", external_display_name="Anna Beispiel")
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
    t = build(seg(BEN, 30, 3, "zweitens"), seg(ANNA, 0, 2, "erstens"))
    assert [b.text for b in t.blocks] == ["erstens", "zweitens"]


def test_consecutive_segments_of_same_speaker_merge() -> None:
    t = build(seg(ANNA, 0, 2, "erster Teil"), seg(ANNA, 3, 2, "zweiter Teil"))
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "erster Teil zweiter Teil"
    assert t.blocks[0].start == T0


def test_long_pause_splits_a_block() -> None:
    t = build(seg(ANNA, 0, 2, "vorher"), seg(ANNA, 300, 2, "nachher"))
    assert [b.text for b in t.blocks] == ["vorher", "nachher"]


def test_other_speaker_interrupts_a_block() -> None:
    t = build(seg(ANNA, 0, 2, "eins"), seg(BEN, 3, 1, "dazwischen"), seg(ANNA, 5, 2, "drei"))
    assert [b.text for b in t.blocks] == ["eins", "dazwischen", "drei"]


def test_participants_are_unique_and_ordered_by_first_appearance() -> None:
    t = build(seg(BEN, 0, 1, "b"), seg(ANNA, 5, 1, "a"), seg(BEN, 9, 1, "b again"))
    assert t.participants == (BEN, ANNA)


def test_empty_and_whitespace_segments_are_dropped() -> None:
    t = build(seg(ANNA, 0, 1, "   "), seg(ANNA, 60, 1, "echt"))
    assert [b.text for b in t.blocks] == ["echt"]


def test_no_segments_yields_empty_transcript() -> None:
    t = build()
    assert t.blocks == ()
    assert t.participants == ()


def test_transcript_carries_session_bounds() -> None:
    t = build(seg(ANNA, 0, 1, "x"))
    assert t.session_started_at == T0
    assert t.session_ended_at == T0 + timedelta(hours=1)


def test_model_carries_no_markup() -> None:
    t = build(seg(ANNA, 0, 1, "reiner Text"))
    assert t.blocks[0].text == "reiner Text"
    assert t.blocks[0].speaker.external_display_name == "Anna Beispiel"
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag prüfen**

Run: `uv run pytest tests/domain/test_transcript.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'sturnus.domain.transcript'`

- [ ] **Step 3: Implementierung schreiben**

```python
# src/sturnus/domain/transcript.py
"""Zielneutrales Transkript-Modell.

Enthält bewusst kein Markup: welche Bestandteile einer Sprecheridentität
im Ergebnis auftauchen und in welcher Form, entscheidet allein der
jeweilige Adapter über sein Template.
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
    """Ordnet Segmente aller Sprecher chronologisch und fasst Blöcke zusammen."""
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/domain/test_transcript.py -v`
Expected: alle PASS

- [ ] **Step 5: Committen**

```bash
git add src/sturnus/domain/transcript.py tests/domain/test_transcript.py
git commit -m "feat: add target-neutral transcript model with block merging"
```

---

### Task 6: Einwilligungsauflösung

Setzt Spec 3.1 und 3.3 um. Eine Einwilligung erlischt bei Widerruf und
bei einer Änderung der Datenschutzerklärung.

**Files:**
- Create: `src/sturnus/domain/consent.py`
- Test: `tests/domain/test_consent.py`

**Interfaces:**
- Consumes: nichts
- Produces:
  - `ConsentRecord(granted_at: datetime | None, revoked_at: datetime | None, policy_version: str | None)`
  - `is_consent_active(record: ConsentRecord | None, current_policy_version: str) -> bool`
  - `may_record(record: ConsentRecord | None, current_policy_version: str, has_consent_role: bool) -> bool`

- [ ] **Step 1: Test schreiben**

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
    # Der Rollencheck allein genügt nicht: Administratoren umgehen die
    # Kanalberechtigung, weshalb der Datensatz zusätzlich geprüft wird.
    assert may_record(granted(), POLICY, has_consent_role=True) is True
    assert may_record(granted(), POLICY, has_consent_role=False) is False
    assert may_record(None, POLICY, has_consent_role=True) is False


def test_revoked_user_with_stale_role_may_not_be_recorded() -> None:
    record = ConsentRecord(granted_at=T0, revoked_at=T0, policy_version=POLICY)
    assert may_record(record, POLICY, has_consent_role=True) is False
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag prüfen**

Run: `uv run pytest tests/domain/test_consent.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'sturnus.domain.consent'`

- [ ] **Step 3: Implementierung schreiben**

```python
# src/sturnus/domain/consent.py
"""Auflösung der Einwilligung.

Die Discord-Rolle ist der erste Schutz, aber nicht der einzige: Nutzer mit
Administratorrecht umgehen Kanalberechtigungen und könnten ohne Rolle
sprechen. Deshalb entscheidet stets auch der gespeicherte Datensatz.
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
    """Eine Einwilligung erlischt durch Widerruf und durch eine geänderte Erklärung."""
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

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/domain/test_consent.py -v`
Expected: alle PASS

- [ ] **Step 5: Gesamtlauf und Committen**

```bash
uv run ruff check . && uv run mypy && uv run pytest -v
git add src/sturnus/domain/consent.py tests/domain/test_consent.py
git commit -m "feat: add consent resolution with policy versioning"
```

---

### Task 7: Datenbankmodelle und erste Migration

Setzt Spec 9 um. Ab hier wird SQLAlchemy verwendet — ausschließlich in
`infrastructure/db/`, niemals in `domain/`.

**Files:**
- Create: `src/sturnus/infrastructure/db/__init__.py`, `src/sturnus/infrastructure/db/models.py`
- Create: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial.py`
- Test: `tests/infrastructure/test_migrations.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nichts aus vorigen Tasks
- Produces:
  - `Base` (DeclarativeBase)
  - Modelle `GuildConfig`, `AccountLink`, `Consent`, `OAuthState`, `Session`, `SessionParticipant`, `TranscriptionJob`
  - pytest-Fixtures `postgres_url: str` (sitzungsweit) und `clean_database: str` (setzt das Schema je Test zurück)

- [ ] **Step 1: Testfundament für Postgres schreiben**

```python
# tests/conftest.py
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Ein Container für den gesamten Testlauf — das Hochfahren kostet Sekunden."""
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture
def clean_database(postgres_url: str) -> str:
    """Setzt das Schema vor jedem Test vollständig zurück.

    Notwendig, weil sich alle Tests einen Container teilen und auf zwei Wegen
    Tabellen entstehen: über Alembic (mit `alembic_version`) und über
    `create_all` (ohne). Ein blosses `drop_all` liesse die Alembic-Buchführung
    stehen, worauf ein späteres `upgrade head` an bereits vorhandenen Tabellen
    scheitert. Das Schema zu verwerfen trifft beide Fälle.
    """
    engine = create_engine(postgres_url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return postgres_url
```

Jeder Test, der die Datenbank berührt, hängt an `clean_database` statt an
`postgres_url` — das macht die Testreihenfolge gleichgültig.

- [ ] **Step 2: Migrationstest schreiben**

```python
# tests/infrastructure/test_migrations.py
"""Migrationstests laufen bewusst synchron.

`alembic.command.*` ist eine synchrone API; aus einem `async def`-Test heraus
aufgerufen bricht sie in der laufenden Ereignisschleife ab.
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
    """Nach `upgrade head` darf ein Autogenerate nichts mehr zu tun finden.

    Ohne diesen Test bleibt eine Modelländerung ohne zugehörige Migration
    unbemerkt, bis sie in der Produktion auffällt.
    """
    command.upgrade(_alembic_config(clean_database), "head")

    engine = create_engine(_sync_url(clean_database))
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"Modelle und Migration weichen ab: {diff}"
```

Der Drift-Test ist der wertvollste der drei: er schlägt fehl, sobald jemand ein
Feld ergänzt, ohne `alembic revision --autogenerate` laufen zu lassen.

- [ ] **Step 3: `psycopg` als Testabhängigkeit ergänzen und Test laufen lassen**

```bash
uv add --group test "psycopg[binary]>=3.2"
uv run pytest tests/infrastructure/test_migrations.py -v
```
Expected: FAIL — `alembic.ini` existiert nicht (`FileNotFoundError` bzw. Alembic meldet die fehlende Konfiguration)

- [ ] **Step 4: Modelle schreiben**

```python
# src/sturnus/infrastructure/db/models.py
"""SQLAlchemy-Modelle. Einziger Datenzugriffsweg des Systems."""

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

`wrapped_data_key` hält den mit dem Hauptschlüssel verschlüsselten
Datenschlüssel der Sitzung (Spec 12.1); `encryption_key_id` benennt den
verwendeten Hauptschlüssel, damit ein Wechsel ohne Neuverschlüsselung möglich
bleibt.

- [ ] **Step 5: Alembic einrichten — synchron, nicht async**

```bash
uv run alembic init migrations
```

**Bewusst ohne `-t async`.** Die Anwendung läuft asynchron über `asyncpg`, die
Migrationen laufen synchron über `psycopg`. Grund: `alembic.command.upgrade()`
ruft bei einer asynchronen `env.py` intern `asyncio.run()` auf — innerhalb eines
laufenden pytest-asyncio-Ereignisschleife bricht das mit
`RuntimeError: asyncio.run() cannot be called from a running event loop` ab. Die
Migrationen synchron zu halten macht sie aus Tests und aus einem
Init-Container gleichermaßen aufrufbar.

In `alembic.ini` die URL leeren, da sie zur Laufzeit gesetzt wird:

```ini
sqlalchemy.url =
```

`migrations/env.py` bekommt die Metadaten und die URL-Auflösung. Die von
`alembic init` erzeugte Zeile `target_metadata = None` ersetzen durch:

```python
import os

from sturnus.infrastructure.db.models import Base

target_metadata = Base.metadata


def _resolve_url() -> str:
    """URL aus -x url=..., sonst aus DATABASE_URL. asyncpg wird zu psycopg."""
    from alembic import context as _context

    supplied = _context.get_x_argument(as_dictionary=True).get("url")
    url = supplied or config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("no database url: pass -x url=... or set DATABASE_URL")
    return url.replace("+asyncpg", "+psycopg")


config.set_main_option("sqlalchemy.url", _resolve_url())
```

Diese Zeile muss **vor** `run_migrations_offline()`/`run_migrations_online()`
stehen, damit beide Pfade die aufgelöste URL sehen.

- [ ] **Step 6: Erste Migration erzeugen**

```bash
docker run -d --name sturnus-pg -e POSTGRES_USER=sturnus -e POSTGRES_PASSWORD=sturnus \
  -e POSTGRES_DB=sturnus -p 5432:5432 postgres:17-alpine
sleep 3
uv run alembic -x url="postgresql://sturnus:sturnus@localhost:5432/sturnus" \
  revision --autogenerate -m "initial schema"
```

Die erzeugte Datei nach `migrations/versions/` prüfen: sie muss alle sieben
Tabellen anlegen und in `downgrade()` wieder entfernen. Fehlt eine, ist das
Modell nicht importiert worden. Anschließend
`docker rm -f sturnus-pg`.

- [ ] **Step 7: Migrationstests laufen lassen**

```bash
uv run pytest tests/infrastructure/test_migrations.py -v
```
Expected: alle drei PASS. Schlägt `test_models_and_migration_do_not_drift` fehl,
fehlt in der erzeugten Migration etwas — die Ausgabe benennt die Abweichung.

- [ ] **Step 8: Architekturtest und Gesamtlauf**

Run: `uv run ruff check . && uv run mypy && uv run pytest -v`
Expected: alles grün — insbesondere schlägt `test_domain_has_no_outward_imports` nicht an, weil SQLAlchemy nur unter `infrastructure/` verwendet wird

- [ ] **Step 9: Committen**

```bash
git add src/sturnus/infrastructure alembic.ini migrations tests/conftest.py tests/infrastructure
git commit -m "feat: add sqlalchemy models and initial alembic migration"
```

---

### Task 8: Konfigurationsspeicher mit Vorrangauflösung

Setzt Spec 11 um: Werte je Guild, mit Standardwerten aus dem Code, wenn
nichts hinterlegt ist.

**Files:**
- Create: `src/sturnus/infrastructure/db/config_store.py`
- Create: `src/sturnus/domain/settings.py`
- Test: `tests/infrastructure/test_config_store.py`

**Interfaces:**
- Consumes: `Base`, `GuildConfig` aus Task 7; `SessionTimeouts` aus Task 3
- Produces:
  - `DEFAULTS: dict[str, str]` in `domain/settings.py`
  - `ConfigStore(session_factory)` mit
    `get(guild_id: int, key: str) -> str | None`,
    `set(guild_id: int, key: str, value: str | None, now: datetime) -> None`,
    `timeouts(guild_id: int) -> SessionTimeouts`

- [ ] **Step 1: Standardwerte als reine Domänendaten schreiben**

```python
# src/sturnus/domain/settings.py
"""Standardwerte der Laufzeitkonfiguration (Spec 11)."""

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

# Ohne Standardwert und damit vor dem Betrieb zwingend zu setzen.
REQUIRED_KEYS: frozenset[str] = frozenset(
    {VOICE_CHANNEL_ID, CONSENT_ROLE_ID, DOCUMENT_TARGET, POLICY_VERSION, POLICY_URL}
)
```

- [ ] **Step 2: Test schreiben**

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
    assert timeouts.idle_timeout_minutes == 15  # Standardwert
    assert timeouts.max_session_hours == 4
```

- [ ] **Step 3: Test laufen lassen, Fehlschlag prüfen**

Run: `uv run pytest tests/infrastructure/test_config_store.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'sturnus.infrastructure.db.config_store'`

- [ ] **Step 4: Implementierung schreiben**

```python
# src/sturnus/infrastructure/db/config_store.py
"""Laufzeitkonfiguration je Guild mit Rückfall auf die Standardwerte."""

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
        """Setzt einen Wert; `None` entfernt ihn und stellt den Standard wieder her."""
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

- [ ] **Step 5: Tests laufen lassen**

Run: `uv run pytest tests/infrastructure/test_config_store.py -v`
Expected: alle PASS

- [ ] **Step 6: Gesamtlauf**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -v`
Expected: alles grün

- [ ] **Step 7: Committen**

```bash
git add src/sturnus/domain/settings.py src/sturnus/infrastructure/db/config_store.py tests/infrastructure/test_config_store.py
git commit -m "feat: add per-guild config store with default fallback"
```

---

## Abschluss von Plan 1

Nach Task 8 steht:

- Ein Repository mit CI, das Linter, Typprüfung und Tests auf jedem PR ausführt.
- Release Please, bereit für den ersten `feat:`-Commit.
- Die vollständige Domänenlogik: Zustandsautomat, Zeitrekonstruktion,
  Transkript-Aufbau, Einwilligungsauflösung — alle ohne I/O und ohne
  Discord-Abhängigkeit prüfbar.
- Ein durchgesetzter Architekturtest, der die Schichtung ab hier bewacht.
- Das vollständige Datenbankschema mit Migration und den Konfigurationsspeicher.

**Noch nicht vorhanden und Gegenstand von Plan 2:** Discord-Anbindung,
Sprachaufzeichnung, Verschlüsselung, S3.
