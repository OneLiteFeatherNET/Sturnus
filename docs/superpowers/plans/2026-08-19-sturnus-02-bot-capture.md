# Sturnus Plan 2: The Bot and the Capture Path

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Discord bot that auto-joins a dedicated voice channel, records each consenting participant as a separate stream, and hands the encrypted audio to S3 with one transcription job per speaker.

**Architecture:** The domain layer from Plan 1 stays untouched except for one specified behaviour change. Everything new is either a pure, testable unit (encryption, audio writing, session orchestration) or a deliberately thin adapter over a library (Discord voice receive). The dependency rule from Plan 1 is enforced by a test and applies unchanged.

**Tech Stack:** Python 3.12, `discord.py` 2.x with `discord-ext-voice-recv`, `cryptography` (AES-256-GCM), `soxr` (resampling), `boto3` (S3), SQLAlchemy 2.0 async, `pydantic-settings`.

**Spec:** `docs/superpowers/specs/2026-08-19-sturnus-design.md`

**Predecessor:** `docs/superpowers/plans/2026-08-19-sturnus-01-foundation.md` — its output is the foundation this builds on: the session state machine, RTP time reconstruction, transcript model, consent resolution, seven database tables and the config store.

## Global Constraints

- **Python `>=3.12`**, dependency management exclusively through `uv`.
- **The dependency rule:** `sturnus.domain` imports only the standard library — enforced by `tests/test_architecture.py`, which uses an allowlist. `sturnus.application` may import `domain` and its own ports, never a concrete adapter. New third-party dependencies therefore cannot be used from `domain`.
- **One data access path:** SQLAlchemy 2.0 async ORM. No raw `asyncpg`.
- **Schema changes only through Alembic.** No `create_all()` outside test fixtures.
- **All code, comments, docstrings and assertion messages in English.**
- **Timestamps are timezone-aware `datetime`**, never naive.
- **Conventional Commits**; no Claude attribution in any commit.
- **`mypy` runs `strict = true`** over `src` and `tests`; **`ruff check`** and **`ruff format --check`** must stay clean.
- **Secrets never appear in logs.** Neither audio, nor transcripts, nor keys, nor tokens.

## What this plan does not build

Transcription, the Outline adapter, the OAuth link service, the Helm chart and the Flux manifests. Those are Plans 3 and 4. At the end of this plan the bot records and uploads; nothing yet reads what it produced.

`/audio delete` and `/audio purge` (Spec 12.3) are also out of scope here, deliberately: they exist to serve erasure requests against retained audio, and retention only begins to matter once recordings survive their transcription. They belong with the retention sweep in Plan 3. Until then, the only audio in the store is waiting to be transcribed, and deleting the session row removes it.

**One consequence worth stating plainly:** at the end of this plan the system records real people and stores their speech, but has no user-facing way to delete it. Do not run it against a production guild before Plan 3 lands — a test guild with informed participants is fine, a live server is not.

---

### Task 1: Behaviour change — restart the idle timer, and the audio epoch column

Two corrections the Plan 1 reviews surfaced, both now specified. Doing them first means every later task builds on the corrected behaviour.

**Files:**
- Modify: `src/sturnus/domain/session.py`
- Modify: `tests/domain/test_session.py`
- Modify: `src/sturnus/infrastructure/db/models.py`
- Create: `migrations/versions/0002_audio_started_at.py`
- Test: `tests/infrastructure/test_migrations.py` (extend)

**Interfaces:**
- Consumes: `SessionMachine`, `SessionParticipant` from Plan 1
- Produces: `SessionParticipant.audio_started_at: Mapped[datetime | None]`; `SessionMachine` restarting its idle timer on `GRACE` → `RECORDING`

- [ ] **Step 1: Write the failing test for the idle restart**

Add to `tests/domain/test_session.py`:

```python
def test_returning_participant_restarts_the_idle_timer() -> None:
    """Someone returning during grace gets the full idle window.

    Without this the timer keeps counting from before the channel emptied,
    and a returning participant who stays quiet briefly would see the bot
    leave seconds after they arrived.
    """
    m = machine()
    m.participants_changed(1, T0)
    m.audio_received(T0 + timedelta(minutes=1))
    m.participants_changed(0, T0 + timedelta(minutes=14))
    m.participants_changed(1, T0 + timedelta(minutes=14, seconds=30))
    # Without the restart the idle window would expire at T0+16min.
    assert m.tick(T0 + timedelta(minutes=20)) is None
    assert m.state is SessionState.RECORDING


def test_idle_still_closes_after_a_return_without_speech() -> None:
    """Restarting the timer delays the close; it does not disable it."""
    m = machine()
    m.participants_changed(1, T0)
    m.participants_changed(0, T0 + timedelta(minutes=1))
    m.participants_changed(1, T0 + timedelta(minutes=1, seconds=30))
    assert m.tick(T0 + timedelta(minutes=16, seconds=31)) is EndReason.IDLE_TIMEOUT
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/domain/test_session.py -k restarts -v`
Expected: FAIL — the session closes at 16 minutes because the timer was never restarted.

- [ ] **Step 3: Implement the restart**

In `participants_changed`, the branch that returns from `GRACE` to `RECORDING` must also reset `_last_audio_at` to `now`. The existing `IDLE` branch already sets it; extend the condition so a resumption does too, without touching `started_at` — the session continues, it does not restart.

- [ ] **Step 4: Run the full session test file**

Run: `uv run pytest tests/domain/test_session.py -v`
Expected: all pass, including the pre-existing transition tests. If `test_idle_timeout_closes_session` now fails, the reset was applied too broadly — it must fire only on the `GRACE` → `RECORDING` edge, not on every call.

- [ ] **Step 5: Add the `audio_started_at` column**

In `models.py`, `SessionParticipant` gains:

```python
    audio_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Nullable, because a participant may be present without ever having spoken.

- [ ] **Step 6: Generate and inspect the migration**

```bash
docker run -d --name sturnus-pg -e POSTGRES_USER=sturnus -e POSTGRES_PASSWORD=sturnus \
  -e POSTGRES_DB=sturnus -p 5432:5432 postgres:17-alpine
sleep 3
uv run alembic -x url="postgresql+psycopg://sturnus:sturnus@localhost:5432/sturnus" \
  upgrade head
uv run alembic -x url="postgresql+psycopg://sturnus:sturnus@localhost:5432/sturnus" \
  revision --autogenerate -m "audio started at"
docker rm -f sturnus-pg
```

Rename the generated file to `0002_audio_started_at.py` and set `revision = "0002"`, `down_revision = "0001"`. Confirm `upgrade()` adds exactly one column and `downgrade()` drops it.

- [ ] **Step 7: Verify no drift and commit**

```bash
uv run pytest tests/infrastructure/test_migrations.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: restart idle timer on return and record the audio epoch"
```

`test_models_and_migration_do_not_drift` must pass. If it does not, the migration and the model disagree — fix the migration, never the test.

---

### Task 2: Runtime settings and the application ports

**Files:**
- Create: `src/sturnus/config.py`
- Create: `src/sturnus/application/ports.py`
- Create: `src/sturnus/entrypoints/__init__.py`
- Test: `tests/test_config.py`, `tests/application/test_ports.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Settings` (pydantic-settings) with `discord_token: SecretStr`, `database_url: str`, `s3_endpoint: str`, `s3_bucket: str`, `s3_access_key: SecretStr`, `s3_secret_key: SecretStr`, `master_key: SecretStr`, `master_key_id: str`, `recording_dir: Path`, `health_port: int = 8080`
  - `get_settings() -> Settings` (cached)
  - Protocols `AudioStore`, `VoiceReceiver`, `Clock`

- [ ] **Step 1: Write the settings test**

```python
# tests/test_config.py
import pytest
from pydantic import ValidationError

from sturnus.config import Settings


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "STURNUS_DISCORD_TOKEN": "discord-secret-value",
        "STURNUS_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "STURNUS_S3_ENDPOINT": "https://s3.example",
        "STURNUS_S3_BUCKET": "sturnus-audio",
        "STURNUS_S3_ACCESS_KEY": "ak",
        "STURNUS_S3_SECRET_KEY": "s3-secret-value",
        "STURNUS_MASTER_KEY": "c3R1cm51cy10ZXN0LWtleS0zMi1ieXRlcy1sb25nISE=",
        "STURNUS_MASTER_KEY_ID": "k1",
        "STURNUS_RECORDING_DIR": "/tmp/rec",
    }
    base.update(overrides)
    return base


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.s3_bucket == "sturnus-audio"
    assert s.health_port == 8080


def test_missing_required_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STURNUS_DISCORD_TOKEN")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings object must be safe to log or include in a traceback."""
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    rendered = repr(Settings())
    # Assert on the secret VALUES, never on words that also appear in field
    # names — `discord_token` contains "token", so such an assertion could
    # never hold regardless of whether masking works.
    assert "discord-secret-value" not in rendered
    assert "s3-secret-value" not in rendered
    assert "c3R1cm51cy" not in rendered
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `sturnus.config` does not exist.

- [ ] **Step 3: Add the dependencies**

```bash
uv add "pydantic-settings>=2.12.0" "cryptography>=44.0" "soxr>=0.5" "numpy>=2.1" \
  "boto3>=1.35" "discord.py>=2.6.4" "discord-ext-voice-recv>=0.5.2a179"
uv add --group test "moto[s3]>=5.0"
```

Also add the pydantic plugin to `[tool.mypy]`, or every `Settings()` reads as a
call missing each required argument:

```toml
plugins = ["pydantic.mypy"]
```

> The voice-receive extension resolves at `0.5.2a179`; earlier drafts of this
> plan guessed a version that does not exist. See
> `docs/verification/voice-receive-spike.md` for what the installed package
> actually exposes — `RTPPacket` carries `timestamp` and `ssrc`, which settles
> the assumption Spec 6.2 rests on.

- [ ] **Step 4: Write the settings module**

```python
# src/sturnus/config.py
"""Process configuration from the environment.

Runtime settings that administrators change live in the database (see
`ConfigStore`); this module holds only what must exist before the bot can
reach a database at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STURNUS_", frozen=True)

    discord_token: SecretStr
    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    # Base64-encoded 32 bytes. See the encryption task for how it is used.
    master_key: SecretStr
    # Names which master key encrypted a given recording, so the key can be
    # rotated without re-encrypting existing material.
    master_key_id: str
    recording_dir: Path
    health_port: int = 8080


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

`SecretStr` is what makes the third test pass: its `repr` renders as `**********`, so a settings object caught in a traceback or a log line carries no credentials.

- [ ] **Step 5: Write the ports test**

```python
# tests/application/test_ports.py
"""The ports exist so tests can substitute fakes for real systems.

This test does not exercise behaviour; it pins the shape a fake must have,
so a change to a protocol that would silently break every fake fails here
instead.
"""

from datetime import UTC, datetime
from pathlib import Path

from sturnus.application.ports import AudioStore, Clock


class FakeAudioStore:
    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}

    async def put(self, key: str, source: Path) -> None:
        self.uploaded[key] = source.read_bytes()

    async def delete(self, key: str) -> None:
        self.uploaded.pop(key, None)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def test_fake_audio_store_satisfies_the_port() -> None:
    store: AudioStore = FakeAudioStore()
    assert store is not None


def test_fixed_clock_satisfies_the_port() -> None:
    clock: Clock = FixedClock(datetime(2026, 8, 19, tzinfo=UTC))
    assert clock.now().tzinfo is UTC
```

- [ ] **Step 6: Write the ports**

```python
# src/sturnus/application/ports.py
"""Protocols for the systems the application talks to.

Only what genuinely varies is abstracted: things a test must replace with a
fake, or whose implementation may change. Repositories deliberately have no
protocol — they are tested against a real PostgreSQL through Testcontainers,
and an interface with exactly one implementation behind a real database test
would be ceremony.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, always timezone-aware."""
        ...


class AudioStore(Protocol):
    async def put(self, key: str, source: Path) -> None: ...

    async def delete(self, key: str) -> None: ...


class VoiceReceiver(Protocol):
    """Wraps the voice-receive extension.

    Kept deliberately narrow: the extension is a community project without
    official discord.py support, and this is the seam that keeps a future
    replacement from reaching into the rest of the system.
    """

    async def join(self, channel_id: int) -> None: ...

    async def leave(self) -> None: ...
```

- [ ] **Step 7: Create the entrypoints package and restore the console scripts**

```bash
mkdir -p src/sturnus/entrypoints
touch src/sturnus/entrypoints/__init__.py
```

In `pyproject.toml`, restore only the bot script — the other two arrive with their plans:

```toml
[project.scripts]
sturnus-bot = "sturnus.entrypoints.bot:main"
```

Leave the entry unused for now; Task 10 creates `bot.py`. Do not add the worker or link entries.

- [ ] **Step 8: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add process settings and application ports"
```

The architecture test must still pass — `application/ports.py` imports only the standard library and `domain`.

---

### Task 3: Envelope encryption

Implements Spec 12.1. Audio outlives its transcription by `audio_retention_days`, which makes it the most sensitive thing this system stores — raw speech from private conversations, held for weeks. Encrypting before upload means possessing the object store is not enough to hear anything.

**Files:**
- Create: `src/sturnus/infrastructure/crypto.py`
- Test: `tests/infrastructure/test_crypto.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `DataKey(plaintext: bytes, wrapped: bytes)`
  - `KeyWrapper(master_key: bytes, key_id: str)` with `new_data_key() -> DataKey` and `unwrap(wrapped: bytes) -> bytes`
  - `encrypt_file(source: Path, target: Path, data_key: bytes) -> None`
  - `decrypt_file(source: Path, target: Path, data_key: bytes) -> None`
  - `CHUNK_SIZE: int`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_crypto.py
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from sturnus.infrastructure.crypto import (
    CHUNK_SIZE,
    KeyWrapper,
    decrypt_file,
    encrypt_file,
)

MASTER = b"0" * 32


def wrapper() -> KeyWrapper:
    return KeyWrapper(master_key=MASTER, key_id="k1")


def test_data_key_round_trips_through_the_master_key() -> None:
    w = wrapper()
    key = w.new_data_key()
    assert len(key.plaintext) == 32
    assert w.unwrap(key.wrapped) == key.plaintext


def test_each_data_key_is_distinct() -> None:
    w = wrapper()
    assert w.new_data_key().plaintext != w.new_data_key().plaintext


def test_a_wrong_master_key_cannot_unwrap() -> None:
    wrapped = wrapper().new_data_key().wrapped
    other = KeyWrapper(master_key=b"1" * 32, key_id="k1")
    with pytest.raises(InvalidTag):
        other.unwrap(wrapped)


def test_file_round_trips(tmp_path: Path) -> None:
    plain = tmp_path / "audio.wav"
    plain.write_bytes(os.urandom(CHUNK_SIZE * 2 + 1234))
    key = wrapper().new_data_key().plaintext

    encrypt_file(plain, tmp_path / "audio.enc", key)
    decrypt_file(tmp_path / "audio.enc", tmp_path / "audio.out", key)

    assert (tmp_path / "audio.out").read_bytes() == plain.read_bytes()


def test_ciphertext_does_not_contain_the_plaintext(tmp_path: Path) -> None:
    marker = b"SPOKEN-WORDS-MARKER" * 100
    plain = tmp_path / "a.wav"
    plain.write_bytes(marker)
    key = wrapper().new_data_key().plaintext

    encrypt_file(plain, tmp_path / "a.enc", key)
    assert marker not in (tmp_path / "a.enc").read_bytes()


def test_empty_file_round_trips(tmp_path: Path) -> None:
    """A participant who never speaks produces a zero-length recording."""
    plain = tmp_path / "empty.wav"
    plain.write_bytes(b"")
    key = wrapper().new_data_key().plaintext

    encrypt_file(plain, tmp_path / "empty.enc", key)
    decrypt_file(tmp_path / "empty.enc", tmp_path / "empty.out", key)
    assert (tmp_path / "empty.out").read_bytes() == b""


def test_tampering_is_detected(tmp_path: Path) -> None:
    """AES-GCM authenticates; a modified ciphertext must not decrypt silently."""
    plain = tmp_path / "b.wav"
    plain.write_bytes(os.urandom(4096))
    key = wrapper().new_data_key().plaintext
    encrypted = tmp_path / "b.enc"
    encrypt_file(plain, encrypted, key)

    data = bytearray(encrypted.read_bytes())
    data[-1] ^= 0xFF
    encrypted.write_bytes(bytes(data))

    with pytest.raises(InvalidTag):
        decrypt_file(encrypted, tmp_path / "b.out", key)


def test_a_wrong_data_key_cannot_decrypt(tmp_path: Path) -> None:
    plain = tmp_path / "c.wav"
    plain.write_bytes(os.urandom(4096))
    w = wrapper()
    encrypt_file(plain, tmp_path / "c.enc", w.new_data_key().plaintext)
    with pytest.raises(InvalidTag):
        decrypt_file(tmp_path / "c.enc", tmp_path / "c.out", w.new_data_key().plaintext)


def test_two_encryptions_of_the_same_file_differ(tmp_path: Path) -> None:
    """A fresh nonce prefix per file, so identical audio yields different bytes."""
    plain = tmp_path / "d.wav"
    plain.write_bytes(b"x" * 8192)
    key = wrapper().new_data_key().plaintext
    encrypt_file(plain, tmp_path / "d1.enc", key)
    encrypt_file(plain, tmp_path / "d2.enc", key)
    assert (tmp_path / "d1.enc").read_bytes() != (tmp_path / "d2.enc").read_bytes()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/infrastructure/test_crypto.py -v`
Expected: FAIL — `sturnus.infrastructure.crypto` does not exist.

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/infrastructure/crypto.py
"""Envelope encryption for recorded audio (Spec 12.1).

A fresh data key is generated per session and encrypted with the master key
from the environment; only the wrapped form is stored, alongside the id of
the master key that wrapped it. Rotating the master key therefore does not
require re-encrypting existing recordings.

Files are encrypted in fixed-size chunks rather than in one piece: a
recording can run to hundreds of megabytes, and AES-GCM in a single call
would require holding all of it in memory at once.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK_SIZE = 4 * 1024 * 1024
_KEY_BYTES = 32
_WRAP_NONCE_BYTES = 12
_FILE_PREFIX_BYTES = 8
_COUNTER_BYTES = 4
_LENGTH_BYTES = 4
_MAGIC = b"STRN\x01"


@dataclass(frozen=True)
class DataKey:
    plaintext: bytes
    wrapped: bytes


class KeyWrapper:
    """Wraps and unwraps per-session data keys with the master key."""

    def __init__(self, master_key: bytes, key_id: str) -> None:
        if len(master_key) != _KEY_BYTES:
            raise ValueError(f"master key must be {_KEY_BYTES} bytes")
        self._aead = AESGCM(master_key)
        self.key_id = key_id

    def new_data_key(self) -> DataKey:
        plaintext = os.urandom(_KEY_BYTES)
        nonce = os.urandom(_WRAP_NONCE_BYTES)
        return DataKey(plaintext, nonce + self._aead.encrypt(nonce, plaintext, None))

    def unwrap(self, wrapped: bytes) -> bytes:
        nonce, ciphertext = wrapped[:_WRAP_NONCE_BYTES], wrapped[_WRAP_NONCE_BYTES:]
        return self._aead.decrypt(nonce, ciphertext, None)


def _nonce(prefix: bytes, counter: int) -> bytes:
    # 8 random bytes per file plus a 4-byte counter fills AES-GCM's 12-byte
    # nonce. The prefix makes two encryptions of the same file differ; the
    # counter keeps chunks within one file distinct. Since every file also
    # gets a fresh data key per session, nonce reuse under one key is
    # impossible.
    return prefix + struct.pack(">I", counter)


def encrypt_file(source: Path, target: Path, data_key: bytes) -> None:
    aead = AESGCM(data_key)
    prefix = os.urandom(_FILE_PREFIX_BYTES)
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(_MAGIC)
        dst.write(prefix)
        counter = 0
        while chunk := src.read(CHUNK_SIZE):
            sealed = aead.encrypt(_nonce(prefix, counter), chunk, None)
            dst.write(struct.pack(">I", len(sealed)))
            dst.write(sealed)
            counter += 1


def decrypt_file(source: Path, target: Path, data_key: bytes) -> None:
    aead = AESGCM(data_key)
    with source.open("rb") as src, target.open("wb") as dst:
        if src.read(len(_MAGIC)) != _MAGIC:
            raise ValueError("not a sturnus encrypted file")
        prefix = src.read(_FILE_PREFIX_BYTES)
        if len(prefix) != _FILE_PREFIX_BYTES:
            raise ValueError("truncated header")
        counter = 0
        while header := src.read(_LENGTH_BYTES):
            if len(header) != _LENGTH_BYTES:
                raise ValueError("truncated chunk header")
            (size,) = struct.unpack(">I", header)
            sealed = src.read(size)
            if len(sealed) != size:
                raise ValueError("truncated chunk")
            dst.write(aead.decrypt(_nonce(prefix, counter), sealed, None))
            counter += 1
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/infrastructure/test_crypto.py -v`
Expected: all pass.

- [ ] **Step 5: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add envelope encryption for recorded audio"
```

---

### Task 4: The audio writer

Implements Spec 6.1 and 6.3. Discord sends nothing during silence, so the writer must insert it — otherwise a speaker's file is a concatenation of their utterances with all the pauses removed, and every offset the transcription returns is wrong.

**Files:**
- Create: `src/sturnus/infrastructure/audio.py`
- Test: `tests/infrastructure/test_audio.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `TARGET_RATE: int = 16_000`, `SOURCE_RATE: int = 48_000`
  - `to_mono_16k(pcm: bytes) -> bytes`
  - `SpeakerWriter(path: Path, epoch: datetime)` with
    `write(at: datetime, pcm_16k_mono: bytes) -> None`,
    `close() -> None`,
    property `epoch: datetime`, property `samples_written: int`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_audio.py
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sturnus.infrastructure.audio import TARGET_RATE, SpeakerWriter, to_mono_16k

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def samples(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == TARGET_RATE
        assert w.getsampwidth() == 2
        return w.getnframes()


def tone(frames: int) -> bytes:
    """`frames` samples of non-silence at 16 kHz mono, 16-bit."""
    return b"\x10\x27" * frames


def test_resampling_reduces_stereo_48k_to_mono_16k() -> None:
    # 4800 stereo frames at 48 kHz = 100 ms; expect ~1600 mono frames at 16 kHz.
    src = b"\x00\x10" * 2 * 4800
    out = to_mono_16k(src)
    frames = len(out) // 2
    assert abs(frames - 1600) <= 8  # resampler edge effects


def test_first_write_defines_no_leading_silence(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(1600))
    w.close()
    assert samples(path) == 1600


def test_a_gap_is_filled_with_silence(tmp_path: Path) -> None:
    path = tmp_path / "b.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(1600))                                  # 0.0 - 0.1 s
    w.write(T0 + timedelta(seconds=5), tone(1600))           # 5.0 - 5.1 s
    w.close()
    # 5.1 seconds of timeline at 16 kHz.
    assert samples(path) == pytest.approx(int(5.1 * TARGET_RATE), abs=8)


def test_silence_is_actually_silent(tmp_path: Path) -> None:
    path = tmp_path / "c.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(160))
    w.write(T0 + timedelta(seconds=1), tone(160))
    w.close()
    with wave.open(str(path), "rb") as f:
        f.readframes(160)
        assert set(f.readframes(int(0.5 * TARGET_RATE))) == {0}


def test_a_late_packet_does_not_rewind(tmp_path: Path) -> None:
    """An out-of-order packet must not corrupt the file.

    The signed-delta fix in the speaker clock means a timestamp can precede
    one already written. Seeking backwards in a sequential file is not
    possible, so such a packet is appended at the current position rather
    than dropped — losing its exact placement but never its content.
    """
    path = tmp_path / "d.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0 + timedelta(seconds=1), tone(1600))
    before = w.samples_written
    w.write(T0 + timedelta(seconds=0.5), tone(1600))
    w.close()
    assert w.samples_written == before + 1600
    assert samples(path) == before + 1600


def test_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "e.wav"
    w = SpeakerWriter(path, epoch=T0)
    w.write(T0, tone(160))
    w.close()
    w.close()
    assert samples(path) == 160


def test_a_writer_that_never_receives_audio_yields_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "f.wav"
    SpeakerWriter(path, epoch=T0).close()
    assert samples(path) == 0


def test_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    w = SpeakerWriter(tmp_path / "g.wav", epoch=T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        w.write(datetime(2026, 8, 19, 20, 0, 0), tone(160))
    w.close()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/infrastructure/test_audio.py -v`
Expected: FAIL — `sturnus.infrastructure.audio` does not exist.

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/infrastructure/audio.py
"""Writing one speaker's stream as a continuous 16 kHz mono WAV file.

Discord sends no packets while a participant is silent, so silence has to be
inserted deliberately. Without it the file would be that speaker's utterances
spliced together with every pause removed, and each offset the transcription
returns would point at the wrong moment.

Audio is converted to Whisper's own format on arrival — 16 kHz mono — so no
resampling is needed later.
"""

from __future__ import annotations

import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import soxr

SOURCE_RATE = 48_000
TARGET_RATE = 16_000
_SAMPLE_WIDTH = 2


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value


def to_mono_16k(pcm: bytes) -> bytes:
    """Convert Discord's 48 kHz 16-bit stereo PCM to 16 kHz mono.

    `soxr` is used rather than the standard library's `audioop`, which is
    removed in Python 3.13, and rather than naive decimation, which would
    alias without a low-pass filter.
    """
    if not pcm:
        return b""
    stereo = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
    mono = stereo.mean(axis=1).astype(np.int16)
    return soxr.resample(mono, SOURCE_RATE, TARGET_RATE).astype("<i2").tobytes()


class SpeakerWriter:
    """Appends one speaker's audio, padding the gaps between packets."""

    def __init__(self, path: Path, epoch: datetime) -> None:
        self.epoch = _require_aware(epoch)
        self.samples_written = 0
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(_SAMPLE_WIDTH)
        self._wave.setframerate(TARGET_RATE)
        self._closed = False

    def write(self, at: datetime, pcm_16k_mono: bytes) -> None:
        _require_aware(at)
        if self._closed:
            raise RuntimeError("writer is closed")

        expected = int((at - self.epoch).total_seconds() * TARGET_RATE)
        gap = expected - self.samples_written
        if gap > 0:
            self._wave.writeframes(b"\x00" * (gap * _SAMPLE_WIDTH))
            self.samples_written += gap
        # A negative gap means a packet arrived out of order. The file is
        # written sequentially, so its exact placement cannot be recovered;
        # appending keeps the words and loses only sub-second accuracy,
        # which is preferable to discarding speech.

        self._wave.writeframes(pcm_16k_mono)
        self.samples_written += len(pcm_16k_mono) // _SAMPLE_WIDTH

    def close(self) -> None:
        if not self._closed:
            self._wave.close()
            self._closed = True
```

- [ ] **Step 4: Add numpy and run the tests**

```bash
uv add "numpy>=2.1"
uv run pytest tests/infrastructure/test_audio.py -v
```
Expected: all pass. If the resampling test fails on frame count, check the tolerance rather than changing the resampler — `soxr` has small edge effects at buffer boundaries by design.

- [ ] **Step 5: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: write per-speaker audio with silence padding"
```

---

### Task 5: The S3 adapter

**Files:**
- Create: `src/sturnus/infrastructure/objectstore.py`
- Test: `tests/infrastructure/test_objectstore.py`

**Interfaces:**
- Consumes: `AudioStore` protocol from Task 2
- Produces: `S3AudioStore(endpoint, bucket, access_key, secret_key)` satisfying `AudioStore`, plus `audio_key(session_id: int, discord_user_id: int) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_objectstore.py
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from sturnus.infrastructure.objectstore import S3AudioStore, audio_key

BUCKET = "sturnus-audio"


@pytest.fixture
def store() -> Iterator[S3AudioStore]:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3AudioStore(endpoint=None, bucket=BUCKET, access_key="ak", secret_key="sk")


def test_key_is_stable_and_scoped_to_session_and_speaker() -> None:
    assert audio_key(42, 1234) == "sessions/42/speakers/1234.enc"


async def test_put_then_delete(store: S3AudioStore, tmp_path: Path) -> None:
    source = tmp_path / "a.enc"
    source.write_bytes(b"encrypted-bytes")

    await store.put("sessions/1/speakers/2.enc", source)
    assert await store.exists("sessions/1/speakers/2.enc") is True

    await store.delete("sessions/1/speakers/2.enc")
    assert await store.exists("sessions/1/speakers/2.enc") is False


async def test_deleting_a_missing_object_is_not_an_error(store: S3AudioStore) -> None:
    await store.delete("sessions/9/speakers/9.enc")


async def test_put_transfers_the_bytes_unchanged(store: S3AudioStore, tmp_path: Path) -> None:
    payload = bytes(range(256)) * 40
    source = tmp_path / "b.enc"
    source.write_bytes(payload)
    await store.put("k", source)

    stored = boto3.client("s3", region_name="us-east-1").get_object(Bucket=BUCKET, Key="k")
    assert stored["Body"].read() == payload
```

> **Note:** `moto`'s `mock_aws` patches botocore at the client level, so it
> keeps working when the calls are dispatched to a worker thread by
> `asyncio.to_thread`. If it turns out not to, do not switch the adapter to
> synchronous calls to make the test pass — the event loop must not block on
> uploads while voice packets are arriving. Report instead.

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/infrastructure/test_objectstore.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the implementation**

```python
# src/sturnus/infrastructure/objectstore.py
"""S3 storage for encrypted recordings.

`boto3` is synchronous, so every call runs in a worker thread — the bot's
event loop must never block on a network transfer while it is receiving
voice packets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def audio_key(session_id: int, discord_user_id: int) -> str:
    """Object key for one speaker's recording within a session."""
    return f"sessions/{session_id}/speakers/{discord_user_id}.enc"


class S3AudioStore:
    def __init__(
        self,
        endpoint: str | None,
        bucket: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    async def put(self, key: str, source: Path) -> None:
        await asyncio.to_thread(
            self._client.upload_file, str(source), self._bucket, key
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self._bucket, Key=key
        )

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"404", "NoSuchKey"}:
                    return False
                raise
            return True

        return await asyncio.to_thread(_head)
```

- [ ] **Step 4: Run the tests, then verify and commit**

```bash
uv run pytest tests/infrastructure/test_objectstore.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add s3 store for encrypted recordings"
```

---

### Task 6: Repositories

Everything the bot needs to read and write, in one place. Tested against real PostgreSQL — no repository interfaces, per Spec 4.5.

**Files:**
- Create: `src/sturnus/infrastructure/db/repositories.py`
- Test: `tests/infrastructure/test_repositories.py`

**Interfaces:**
- Consumes: models from Plan 1; `ConsentRecord` from `domain.consent`
- Produces:
  - `ConsentRepository` — `record_grant`, `record_revocation`, `current(discord_user_id, guild_id) -> ConsentRecord | None`
  - `SessionRepository` — `open_session`, `add_participant`, `set_audio_epoch`, `close_session`, `find_open_session`
  - `JobRepository` — `enqueue(session_id, discord_user_id, s3_key, key_id, wrapped, retention_until)`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_repositories.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.repositories import (
    ConsentRepository,
    JobRepository,
    SessionRepository,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200
POLICY = "2026-08-01"


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_consent_grant_is_readable(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0)
    record = await repo.current(ANNA, GUILD)
    assert record is not None
    assert record.granted_at == T0
    assert record.revoked_at is None
    assert record.policy_version == POLICY


async def test_no_record_returns_none(factory: async_sessionmaker[AsyncSession]) -> None:
    assert await ConsentRepository(factory).current(BEN, GUILD) is None


async def test_revocation_is_visible_in_the_current_record(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0)
    await repo.record_revocation(ANNA, GUILD, T0 + timedelta(hours=1))
    record = await repo.current(ANNA, GUILD)
    assert record is not None
    assert record.revoked_at == T0 + timedelta(hours=1)


async def test_current_returns_the_newest_grant(factory: async_sessionmaker[AsyncSession]) -> None:
    """Consent history is kept permanently (Spec 12.4), so several rows exist.

    A user who revokes and later consents again must read as consenting; the
    repository, not the caller, owns that selection rule.
    """
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, "2026-01-01", "button", T0)
    await repo.record_revocation(ANNA, GUILD, T0 + timedelta(days=1))
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0 + timedelta(days=2))

    record = await repo.current(ANNA, GUILD)
    assert record is not None
    assert record.revoked_at is None
    assert record.policy_version == POLICY


async def test_guilds_do_not_share_consent(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = ConsentRepository(factory)
    await repo.record_grant(ANNA, GUILD, POLICY, "button", T0)
    assert await repo.current(ANNA, 999) is None


async def test_session_lifecycle(factory: async_sessionmaker[AsyncSession]) -> None:
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    assert await repo.find_open_session(GUILD) == session_id

    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=3))
    await repo.close_session(session_id, T0 + timedelta(hours=1), "empty")

    assert await repo.find_open_session(GUILD) is None


async def test_adding_a_participant_twice_is_harmless(factory: async_sessionmaker[AsyncSession]) -> None:
    """Someone may leave and rejoin within one session."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.add_participant(session_id, ANNA, "anna-renamed", T0 + timedelta(minutes=1))
    # The first display name wins: it is the one in force when recording began.
    names = await repo.participant_names(session_id)
    assert names == {ANNA: "anna"}


async def test_audio_epoch_is_written_once(factory: async_sessionmaker[AsyncSession]) -> None:
    """The epoch marks the first packet; a later packet must not move it."""
    repo = SessionRepository(factory)
    session_id = await repo.open_session(GUILD, CHANNEL, T0)
    await repo.add_participant(session_id, ANNA, "anna", T0)
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=3))
    await repo.set_audio_epoch(session_id, ANNA, T0 + timedelta(seconds=9))
    assert await repo.audio_epoch(session_id, ANNA) == T0 + timedelta(seconds=3)


async def test_job_enqueue(factory: async_sessionmaker[AsyncSession]) -> None:
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(GUILD, CHANNEL, T0)
    await sessions.add_participant(session_id, ANNA, "anna", T0)

    job_id = await jobs.enqueue(
        session_id=session_id,
        discord_user_id=ANNA,
        s3_key="sessions/1/speakers/100.enc",
        encryption_key_id="k1",
        wrapped_data_key=b"wrapped",
        retention_until=T0 + timedelta(days=30),
    )
    assert job_id > 0
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/infrastructure/test_repositories.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the repositories**

Write `src/sturnus/infrastructure/db/repositories.py` with the three classes and exactly the methods the tests call, each taking an `async_sessionmaker[AsyncSession]` in its constructor. Requirements the tests encode:

- `ConsentRepository.current` returns the **newest** row for `(discord_user_id, guild_id)` ordered by `granted_at` descending, mapped into the domain's `ConsentRecord`. This is the selection rule the Plan 1 review found missing; it lives here so no caller has to invent it.
- `record_revocation` sets `revoked_at` on that newest row rather than inserting a new one — the history keeps grants, and a revocation modifies the grant it revokes.
- `SessionRepository.add_participant` is idempotent per `(session_id, discord_user_id)` and keeps the first display name.
- `set_audio_epoch` writes only when `audio_started_at` is still null.
- `find_open_session` returns the id of the guild's session whose `status` is not `closed`, or `None`.
- `close_session` sets `ended_at`, `end_reason` and `status = "closed"`.
- `participant_names(session_id) -> dict[int, str]` and `audio_epoch(session_id, discord_user_id) -> datetime | None` exist because the tests use them.

Use the ORM throughout. No raw SQL beyond what SQLAlchemy constructs.

- [ ] **Step 4: Run the tests, verify and commit**

```bash
uv run pytest tests/infrastructure/test_repositories.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add consent, session and job repositories"
```

---

### Task 7: Recording orchestration

The heart of this plan, and the part that must stay testable. It owns the session lifecycle without knowing Discord exists: it is driven by explicit calls and returns explicit decisions, so every path can be exercised with fakes.

**Files:**
- Create: `src/sturnus/application/recording.py`
- Test: `tests/application/test_recording.py`

**Interfaces:**
- Consumes: `SessionMachine`, `EndReason`, `SessionTimeouts`, `SpeakerClock`, `may_record` from `domain`; `AudioStore`, `Clock` from ports; `SpeakerWriter`, `to_mono_16k`, `KeyWrapper`, `encrypt_file`, `audio_key`
- Produces: `RecordingService` with
  - `participants_changed(consented_count: int, now) -> None`
  - `voice_packet(discord_user_id: int, display_name: str, ssrc: int, rtp_timestamp: int, pcm: bytes, now) -> None`
  - `tick(now) -> EndReason | None`
  - `close(reason, now) -> None`
  - properties `is_recording: bool`, `session_id: int | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/application/test_recording.py
"""Orchestration tests.

Every collaborator is a fake, so these exercise the real decision logic
without a voice channel, a database or an object store.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sturnus.domain.session import EndReason, SessionTimeouts
from sturnus.application.recording import RecordingService

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200
RTP = 48_000


def pcm(frames: int) -> bytes:
    """`frames` of 48 kHz stereo 16-bit input, as Discord delivers it."""
    return b"\x10\x27" * 2 * frames


class FakeSessions:
    def __init__(self) -> None:
        self.opened: list[int] = []
        self.participants: dict[int, str] = {}
        self.epochs: dict[int, datetime] = {}
        self.closed: list[tuple[int, str]] = []
        self._next = 1

    async def open_session(self, guild_id: int, channel_id: int, now: datetime) -> int:
        sid = self._next
        self._next += 1
        self.opened.append(sid)
        return sid

    async def add_participant(
        self, session_id: int, user_id: int, display_name: str, now: datetime
    ) -> None:
        self.participants.setdefault(user_id, display_name)

    async def set_audio_epoch(self, session_id: int, user_id: int, at: datetime) -> None:
        self.epochs.setdefault(user_id, at)

    async def close_session(self, session_id: int, ended_at: datetime, reason: str) -> None:
        self.closed.append((session_id, reason))

    async def find_open_session(self, guild_id: int) -> int | None:
        return None


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    async def enqueue(self, **kwargs: object) -> int:
        self.enqueued.append(kwargs)
        return len(self.enqueued)


class FakeStore:
    def __init__(self) -> None:
        self.put_keys: list[str] = []

    async def put(self, key: str, source: Path) -> None:
        assert source.exists(), "uploading a file that is not there"
        self.put_keys.append(key)

    async def delete(self, key: str) -> None:
        pass


def service(
    tmp_path: Path,
    sessions: FakeSessions | None = None,
    jobs: FakeJobs | None = None,
    store: FakeStore | None = None,
) -> RecordingService:
    return RecordingService(
        guild_id=GUILD,
        channel_id=CHANNEL,
        timeouts=SessionTimeouts(
            empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
        ),
        sessions=sessions or FakeSessions(),
        jobs=jobs or FakeJobs(),
        store=store or FakeStore(),
        recording_dir=tmp_path,
        master_key=b"0" * 32,
        master_key_id="k1",
        retention_days=30,
    )


async def test_no_session_until_someone_consenting_joins(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.participants_changed(0, T0)
    assert svc.is_recording is False
    assert svc.session_id is None


async def test_a_consenting_participant_opens_a_session(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    assert svc.is_recording is True
    assert sessions.opened == [1]


async def test_the_first_packet_defines_the_audio_epoch(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0 + timedelta(seconds=3))
    assert sessions.epochs[ANNA] == T0 + timedelta(seconds=3)


async def test_the_epoch_is_not_moved_by_later_packets(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0 + timedelta(seconds=3))
    await svc.voice_packet(ANNA, "anna", 1, RTP * 2, pcm(960), T0 + timedelta(seconds=9))
    assert sessions.epochs[ANNA] == T0 + timedelta(seconds=3)


async def test_each_speaker_gets_their_own_file(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.participants_changed(2, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.voice_packet(BEN, "ben", 2, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=5))
    assert len(list(tmp_path.glob("**/*.enc"))) == 2


async def test_closing_uploads_and_enqueues_one_job_per_speaker(tmp_path: Path) -> None:
    jobs, store = FakeJobs(), FakeStore()
    svc = service(tmp_path, jobs=jobs, store=store)
    await svc.participants_changed(2, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.voice_packet(BEN, "ben", 2, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=5))

    assert len(jobs.enqueued) == 2
    assert sorted(store.put_keys) == [
        "sessions/1/speakers/100.enc",
        "sessions/1/speakers/200.enc",
    ]
    for job in jobs.enqueued:
        assert job["encryption_key_id"] == "k1"
        assert job["wrapped_data_key"]
        assert job["retention_until"] == T0 + timedelta(minutes=5) + timedelta(days=30)


async def test_a_silent_participant_gets_no_job(tmp_path: Path) -> None:
    """Someone present but never speaking produces nothing to transcribe."""
    jobs = FakeJobs()
    svc = service(tmp_path, jobs=jobs)
    await svc.participants_changed(1, T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))
    assert jobs.enqueued == []


async def test_the_uploaded_file_is_encrypted(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    marker = b"\x11\x22" * 2 * 4800
    await svc.voice_packet(ANNA, "anna", 1, RTP, marker, T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))

    for path in tmp_path.glob("**/*.enc"):
        assert path.read_bytes().startswith(b"STRN")


async def test_plaintext_audio_is_removed_after_upload(tmp_path: Path) -> None:
    """Only the encrypted form may survive the upload."""
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))
    assert list(tmp_path.glob("**/*.wav")) == []


async def test_closing_twice_does_not_duplicate_jobs(tmp_path: Path) -> None:
    jobs = FakeJobs()
    svc = service(tmp_path, jobs=jobs)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=2))
    assert len(jobs.enqueued) == 1


async def test_packets_after_close_are_ignored(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0 + timedelta(minutes=2))
    assert svc.is_recording is False


async def test_tick_reports_the_close_reason(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    await svc.participants_changed(0, T0 + timedelta(minutes=1))
    assert await svc.tick(T0 + timedelta(minutes=2, seconds=1)) is EndReason.EMPTY


async def test_returning_participant_keeps_the_same_session(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    await svc.participants_changed(0, T0 + timedelta(seconds=10))
    await svc.participants_changed(1, T0 + timedelta(seconds=30))
    assert sessions.opened == [1]
    assert svc.session_id == 1
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/application/test_recording.py -v`
Expected: FAIL — `sturnus.application.recording` does not exist.

- [ ] **Step 3: Write the service**

`RecordingService` composes what the earlier tasks built. Its shape:

- Holds a `SessionMachine`, a `SpeakerClock`, and one `SpeakerWriter` per speaker.
- `participants_changed` forwards to the machine; on the `IDLE` → `RECORDING` edge it opens a session row and remembers the id.
- `voice_packet`:
  1. ignores the packet when not recording;
  2. asks the `SpeakerClock` for the packet's absolute time from its SSRC and RTP timestamp;
  3. on a speaker's first packet, creates a `SpeakerWriter` with that time as its epoch, records the participant, and persists the epoch;
  4. converts the PCM with `to_mono_16k` and writes it;
  5. tells the machine audio arrived, so the idle timer resets.
- `tick` forwards to the machine and, when it returns a reason, closes.
- `close` is idempotent and, for each speaker who actually has a file:
  1. closes the writer;
  2. encrypts the WAV to a `.enc` beside it with the session's data key;
  3. removes the plaintext WAV;
  4. uploads under `audio_key(session_id, user_id)`;
  5. enqueues a job carrying the key id, the wrapped data key, and `now + retention_days`;
  6. closes the session row.

One data key is generated per session, when the session opens, and its wrapped form goes on every job of that session.

The order in `close` matters and the tests pin it: the plaintext must be gone before the method returns, and a failure partway must not leave a job pointing at an object that was never uploaded — enqueue only after `put` succeeds.

- [ ] **Step 4: Run the tests, verify and commit**

```bash
uv run pytest tests/application/test_recording.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add recording orchestration"
```

---

### Task 8: Consent commands

Implements Spec 3.3 and 10. Every reply is ephemeral — an interaction about someone's consent is nobody else's business.

**Files:**
- Create: `src/sturnus/infrastructure/discord/__init__.py`, `src/sturnus/infrastructure/discord/consent_cog.py`
- Create: `src/sturnus/infrastructure/discord/views.py`
- Test: `tests/infrastructure/discord/test_consent_flow.py`

**Interfaces:**
- Consumes: `ConsentRepository`, `ConfigStore`, `is_consent_active`
- Produces: `ConsentCog`, `ConsentView`, and the pure function `consent_outcome(...)` described below

- [ ] **Step 1: Extract the decision logic so it can be tested**

Discord interactions are awkward to test, so the decisions do not live in the callbacks. Write `src/sturnus/application/consent_flow.py`:

```python
"""Decisions behind the consent commands, separated from Discord.

The cog turns interactions into these calls and renders their results; all
the branching that matters is here, where it can be tested without a gateway
connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sturnus.domain.consent import ConsentRecord, is_consent_active


@dataclass(frozen=True)
class ConsentStatus:
    has_role: bool
    consent_active: bool
    policy_version: str | None
    linked: bool


def grant_needed(record: ConsentRecord | None, current_policy: str, has_role: bool) -> bool:
    """True when granting would actually change something."""
    return not (is_consent_active(record, current_policy) and has_role)


def revoke_needed(record: ConsentRecord | None, current_policy: str, has_role: bool) -> bool:
    """True when there is any consent or role left to withdraw."""
    return is_consent_active(record, current_policy) or has_role
```

- [ ] **Step 2: Write the failing test**

```python
# tests/infrastructure/discord/test_consent_flow.py
from datetime import UTC, datetime

from sturnus.application.consent_flow import grant_needed, revoke_needed
from sturnus.domain.consent import ConsentRecord

T0 = datetime(2026, 8, 19, tzinfo=UTC)
POLICY = "2026-08-01"


def granted(version: str = POLICY) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=None, policy_version=version)


def test_granting_is_needed_without_a_record() -> None:
    assert grant_needed(None, POLICY, has_role=False) is True


def test_granting_is_needed_when_the_role_is_missing() -> None:
    """The record alone does not let anyone speak; the role is what Discord checks."""
    assert grant_needed(granted(), POLICY, has_role=False) is True


def test_granting_is_needed_after_a_policy_change() -> None:
    assert grant_needed(granted("2026-01-01"), POLICY, has_role=True) is True


def test_granting_is_not_needed_when_fully_consented() -> None:
    assert grant_needed(granted(), POLICY, has_role=True) is False


def test_revoking_is_needed_while_the_role_remains() -> None:
    """A stale role must be removable even once the record has lapsed."""
    assert revoke_needed(granted("2026-01-01"), POLICY, has_role=True) is True


def test_revoking_is_not_needed_when_nothing_is_held() -> None:
    assert revoke_needed(None, POLICY, has_role=False) is False
```

- [ ] **Step 3: Run it, confirm it fails, then implement**

Run: `uv run pytest tests/infrastructure/discord/test_consent_flow.py -v`

- [ ] **Step 4: Write the cog**

`ConsentCog` with an `app_commands.Group(name="consent")` and three commands:

| Command | Behaviour |
|---|---|
| `/consent` | Ephemeral embed stating what is recorded, for how long, and linking `policy_url`; buttons **Agree** and **Decline**. Agreeing assigns the configured role and writes a record with the current `policy_version` and `source="button"`. |
| `/consent revoke` | Removes the role, sets `revoked_at`. Replies with what was withdrawn. |
| `/consent status` | Reports role, consent validity, and the policy version consented to. |

The embed text comes from a Jinja2 template shipped in the image (Spec 8.2) — but templating arrives with Plan 3's document adapter. For now, put the strings in one module-level constant with a comment naming Spec 8.2 as where they will move.

Requirements the code must meet, none of which are testable through Discord and all of which a reviewer will check by reading:

- **Every response is ephemeral**, including button callbacks.
- **The button view times out** and disables itself, so a stale message cannot grant consent days later.
- **The view checks that the person pressing is the person who invoked** the command; Discord components are clickable by anyone who can see the message.
- Role assignment failing (missing permission, role above the bot) is reported to the user rather than swallowed — a consent record without the role would let someone believe they can speak when they cannot.

- [ ] **Step 5: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add consent commands with an ephemeral agreement flow"
```

---

### Task 9: Config commands

**Files:**
- Create: `src/sturnus/infrastructure/discord/config_cog.py`
- Create: `src/sturnus/infrastructure/discord/permissions.py`
- Test: `tests/infrastructure/discord/test_config_validation.py`

**Interfaces:**
- Consumes: `ConfigStore`, `settings.DEFAULTS`, `settings.REQUIRED_KEYS`
- Produces: `ConfigCog`, `require_admin()`, and `missing_required(store, guild_id) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/discord/test_config_validation.py
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.discord.config_cog import missing_required

T0 = datetime(2026, 8, 19, tzinfo=UTC)
GUILD = 4711


@pytest.fixture
async def store(clean_database: str) -> ConfigStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return ConfigStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_a_fresh_guild_is_missing_every_required_key(store: ConfigStore) -> None:
    assert set(await missing_required(store, GUILD)) == set(settings.REQUIRED_KEYS)


async def test_setting_a_key_removes_it_from_the_missing_list(store: ConfigStore) -> None:
    await store.set(GUILD, settings.VOICE_CHANNEL_ID, "12345", T0)
    assert settings.VOICE_CHANNEL_ID not in await missing_required(store, GUILD)


async def test_a_fully_configured_guild_is_missing_nothing(store: ConfigStore) -> None:
    for key in settings.REQUIRED_KEYS:
        await store.set(GUILD, key, "1", T0)
    assert await missing_required(store, GUILD) == []


async def test_an_unknown_key_is_rejected(store: ConfigStore) -> None:
    """A typo must not silently store a setting nobody reads."""
    with pytest.raises(ValueError, match="unknown"):
        await store.set(GUILD, "voice_chanel_id", "1", T0)
```

The last test requires a change to `ConfigStore.set` from Plan 1: it currently accepts any key. Add a check against the union of `DEFAULTS` and `REQUIRED_KEYS`, and keep the existing integer validation. `REQUIRED_KEYS` was flagged as unused in the Plan 1 review — this is what puts it to work.

- [ ] **Step 2: Run it, confirm it fails, then implement**

- [ ] **Step 3: Write the permission check and the cog**

`permissions.py` provides `require_admin()`, following the pattern already used in the organisation's RAG bot: guild administrators always pass; otherwise membership in a configured admin role is required.

`ConfigCog` provides an `app_commands.Group(name="config")`, admin-only, with:

| Command | Behaviour |
|---|---|
| `/config get <key>` | Shows the effective value and whether it is stored or a default |
| `/config set <key> <value>` | Validates and stores; rejects unknown keys and bad integers with a readable message |
| `/config clear <key>` | Deletes the row, restoring the default |
| `/config show` | Lists every key, its effective value, its source, and **which required keys are still missing** |

All replies ephemeral. `/config show` must not print secrets — none of these keys hold any today, and none may be added later without revisiting this.

- [ ] **Step 4: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add admin config commands with key validation"
```

---

### Task 10: The voice receive adapter

The thinnest possible layer over the extension, deliberately. It is the one piece of this plan that cannot be unit-tested, so everything decidable was moved into `RecordingService` in Task 7.

**Files:**
- Create: `src/sturnus/infrastructure/discord/voice.py`
- Create: `docs/verification/voice-receive-spike.md`

**Interfaces:**
- Consumes: `VoiceReceiver` protocol; `RecordingService`
- Produces: `VoiceReceiveAdapter` satisfying `VoiceReceiver`

- [ ] **Step 1: Run a spike before writing the adapter**

**Do not skip this and do not write the adapter from documentation alone.** The library's exact API surface — how a sink receives packets, whether RTP timestamps and SSRC are exposed, and how a user id is associated with an SSRC — is the single largest technical unknown in the project. Every design decision from Spec 6.2 onwards assumes those timestamps are reachable.

Write a throwaway script that connects a bot to a real voice channel, joins, receives from two speakers, and prints for each packet: the SSRC, the RTP timestamp, the associated user id, and the size of the decoded PCM. Record the findings in `docs/verification/voice-receive-spike.md`:

- What object does the sink callback receive, with its exact attribute names?
- Is the RTP timestamp exposed, or only decoded audio?
- Is the PCM 48 kHz 16-bit stereo, as `to_mono_16k` assumes?
- How does the SSRC map to a Discord user, and when does that mapping become available? (Spec 6.2 requires a fresh reference point when it changes.)
- Does the mapping ever arrive *after* the first packets for an SSRC?

**If the RTP timestamp turns out not to be reachable, stop and report.** Spec 6.2's whole approach rests on it, and the fallback — arrival timestamps — would silently misplace every segment following a pause. That is a decision for the spec, not for an implementer.

- [ ] **Step 2: Write the adapter to the findings**

The adapter's entire job:

1. `join(channel_id)` — connect and start recording into a sink.
2. For each packet, call `service.voice_packet(user_id, display_name, ssrc, rtp_timestamp, pcm, now)`.
3. Drop packets from users who lack the consent role, **before** they reach the service. This is the second layer from Spec 3.1, and it is not redundant: administrators bypass channel permissions and can speak without the role.
4. `leave()` — stop and disconnect.

No decisions here. If you find yourself adding a condition beyond the consent check, it belongs in `RecordingService`.

- [ ] **Step 3: Commit, including the spike findings**

```bash
git add -A
git commit -m "feat: add voice receive adapter over discord-ext-voice-recv"
```

The spike document is part of the deliverable — it is what makes the next reader able to judge whether the adapter is right.

---

### Task 11: Client, lifecycle and recovery

Brings it together into a process: a Discord client, the event wiring, health endpoints, orphan recovery and a graceful shutdown.

**Files:**
- Create: `src/sturnus/entrypoints/bot.py`
- Create: `src/sturnus/infrastructure/discord/client.py`
- Create: `src/sturnus/infrastructure/health.py`
- Create: `src/sturnus/application/recovery.py`
- Test: `tests/application/test_recovery.py`, `tests/infrastructure/test_health.py`

**Interfaces:**
- Consumes: everything above
- Produces: `main()`, `SturnusClient`, `health_app`, `recover_orphans(...)`

- [ ] **Step 1: Write the failing recovery test**

```python
# tests/application/test_recovery.py
"""Recovery of recordings a crash left behind.

A session is unsplittable (Spec 6.4): the bot records to a PVC for the whole
session and uploads at the end. A hard kill therefore leaves a complete
recording on disk that nothing has uploaded. Losing hours of audio because
the process restarted would be the worst failure this system has, so the
files are picked up on the next start.
"""

from datetime import UTC, datetime
from pathlib import Path

from sturnus.application.recovery import find_orphans

T0 = datetime(2026, 8, 19, tzinfo=UTC)


def test_no_recordings_means_nothing_to_recover(tmp_path: Path) -> None:
    assert find_orphans(tmp_path) == []


def test_a_leftover_wav_is_an_orphan(tmp_path: Path) -> None:
    d = tmp_path / "session-7"
    d.mkdir()
    (d / "100.wav").write_bytes(b"RIFF")
    orphans = find_orphans(tmp_path)
    assert len(orphans) == 1
    assert orphans[0].session_id == 7
    assert orphans[0].discord_user_id == 100


def test_an_encrypted_file_without_its_wav_is_not_an_orphan(tmp_path: Path) -> None:
    """Encryption finished; the upload is what may still be pending."""
    d = tmp_path / "session-7"
    d.mkdir()
    (d / "100.enc").write_bytes(b"STRN")
    orphans = find_orphans(tmp_path)
    assert len(orphans) == 1
    assert orphans[0].encrypted is True


def test_several_speakers_in_one_session(tmp_path: Path) -> None:
    d = tmp_path / "session-3"
    d.mkdir()
    (d / "1.wav").write_bytes(b"RIFF")
    (d / "2.wav").write_bytes(b"RIFF")
    assert len(find_orphans(tmp_path)) == 2


def test_unrecognised_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "stray.txt").write_text("not a recording")
    d = tmp_path / "not-a-session"
    d.mkdir()
    (d / "1.wav").write_bytes(b"RIFF")
    assert find_orphans(tmp_path) == []
```

- [ ] **Step 2: Run it, confirm it fails, then implement `find_orphans` and the recovery routine**

`find_orphans(root)` returns descriptors parsed from the layout `<root>/session-<id>/<discord_user_id>.wav|.enc`. The recovery routine, run at startup before the client connects, encrypts any plain `.wav` with a fresh data key, uploads, enqueues a job, and removes the local files — reusing the same code path as `close`, not a parallel copy of it.

An orphan whose session row is already `closed` still needs its job: the crash may have happened between closing the row and uploading.

- [ ] **Step 3: Write the health endpoints**

`health.py` serves `/healthz`, `/readyz`, `/metrics` and `/version` on `health_port` using `aiohttp` (already a discord.py dependency, so nothing new is added). `/readyz` reports not-ready until the Discord client is connected and the database answers.

Test what is testable: that `/healthz` returns 200 and `/readyz` reflects an injected readiness flag.

- [ ] **Step 4: Write the client and the entrypoint**

`SturnusClient` wires the pieces:

- On start: run Alembic migrations? **No** — the worker owns migrations (Spec 13.1). The bot waits for the schema and fails loudly if a required table is missing.
- Loads the cogs, syncs the command tree.
- Reads `voice_channel_id` and `consent_role_id` per guild from `ConfigStore`; a guild missing them is skipped with a log line naming `/config show`.
- `on_voice_state_update` recomputes how many consenting members are in the channel and calls `participants_changed`. **Count members carrying the consent role**, not everyone present — the count drives the state machine.
- A background task calls `tick(now)` roughly every 10 seconds and closes when it returns a reason.
- `SIGTERM` triggers a graceful close: stop receiving, close writers, encrypt, upload, enqueue, then disconnect. Without this a routine deploy discards the entire session (Spec 6.4).

`main()` loads settings, builds the dependency graph, starts the health server and runs the client.

- [ ] **Step 5: Manual verification against a real server**

Automated tests cannot cover this. Perform and record in the report:

1. Join the channel with two accounts, both consenting; speak alternately; leave; confirm two objects appear in S3 and two jobs in `transcription_job`.
2. Confirm the recording's `audio_started_at` for each speaker is close to when they first spoke, not when they joined.
3. Restart the bot mid-session with `SIGTERM`; confirm the session is closed cleanly and the audio uploaded.
4. Kill the bot with `SIGKILL` mid-session; restart; confirm recovery picks up the leftover recording.
5. Join with an account lacking the consent role and speak (as an administrator, who can bypass the channel permission); confirm **nothing** of that account reaches the recording.

Point 5 is the legal gate from Spec 3.1. If it fails, stop: the bot must not run against a real server until it passes.

- [ ] **Step 6: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add discord client, health endpoints and orphan recovery"
```

---

## What exists after this plan

The bot joins the configured channel when a consenting participant enters, records every consenting speaker separately, pads their silence so offsets stay meaningful, encrypts each recording before it leaves the pod, uploads it, and leaves one transcription job per speaker in the queue. It survives a deploy without losing a session and picks up what a crash left behind.

Nothing reads those jobs yet. Plan 3 adds the worker: transcription, the transcript assembly, the Outline adapter and the link posted back to the channel.

## Risks carried into Plan 3

- **The spike in Task 10 may invalidate Spec 6.2.** If the extension does not expose RTP timestamps, silence reconstruction has no basis and the design needs revisiting before the worker is built on top of it.
- **CPU under real load is unmeasured.** Resampling several concurrent streams on one core is plausible but untested; the first real session is also the first measurement.
- **`audio_started_at` accuracy depends on the SSRC-to-user mapping being available at the first packet.** If it arrives later, the first packets of a speaker cannot be attributed, and the epoch shifts by however long that takes. The spike must answer this.

---

### Task 12: Guided setup and two constants moved into configuration

Implements Spec 10.1 and the two configuration keys added alongside it.

**Files:**
- Create: `src/sturnus/infrastructure/discord/setup_cog.py`
- Create: `src/sturnus/application/setup_plan.py`
- Modify: `src/sturnus/domain/settings.py`, `src/sturnus/infrastructure/discord/permissions.py`, `src/sturnus/application/documents.py`
- Test: `tests/application/test_setup_plan.py`

**Interfaces:**
- Produces:
  - `settings.ADMIN_ROLE_ID`, `settings.MERGE_GAP_SECONDS` and their defaults
  - `PermissionChange(target: str, allow_speak: bool | None)`, `SetupPlan(writes: dict[str, str], permission_changes: list[PermissionChange], role_to_create: str | None, missing: list[str])`
  - `plan_setup(current: dict[str, str | None], channel_id: int, role_id: int | None, policy_url: str, policy_version: str, everyone_may_speak: bool, role_may_speak: bool) -> SetupPlan`
  - `SetupCog`

- [ ] **Step 1: Write the failing test for the planning function**

The command's decisions live in a pure function so they can be tested without a guild. Discord objects never reach it — only the ids and permission facts read off them.

```python
# tests/application/test_setup_plan.py
from sturnus.application.setup_plan import PermissionChange, plan_setup
from sturnus.domain import settings

CHANNEL, ROLE = 111, 222
POLICY_URL = "https://example.org/privacy"
POLICY_VERSION = "2026-08-01"


def plan(current: dict[str, str | None] | None = None, **kw: object) -> object:
    defaults: dict[str, object] = {
        "current": current or {},
        "channel_id": CHANNEL,
        "role_id": ROLE,
        "policy_url": POLICY_URL,
        "policy_version": POLICY_VERSION,
        "everyone_may_speak": True,
        "role_may_speak": False,
    }
    defaults.update(kw)
    return plan_setup(**defaults)  # type: ignore[arg-type]


def test_a_fresh_guild_writes_every_required_key() -> None:
    result = plan(None)
    assert result.writes[settings.VOICE_CHANNEL_ID] == str(CHANNEL)
    assert result.writes[settings.CONSENT_ROLE_ID] == str(ROLE)
    assert result.writes[settings.POLICY_URL] == POLICY_URL


def test_nothing_required_remains_missing_after_a_full_setup() -> None:
    assert plan(None).missing == []


def test_a_missing_document_target_is_reported_not_invented() -> None:
    """The Outline collection cannot be guessed from Discord."""
    result = plan({settings.DOCUMENT_TARGET: None})
    assert settings.DOCUMENT_TARGET in result.missing


def test_everyone_speaking_is_denied() -> None:
    """The primary layer of the consent protection (Spec 3.1)."""
    result = plan(everyone_may_speak=True)
    assert PermissionChange("everyone", allow_speak=False) in result.permission_changes


def test_the_consent_role_is_allowed_to_speak() -> None:
    result = plan(role_may_speak=False)
    assert PermissionChange("consent_role", allow_speak=True) in result.permission_changes


def test_correct_permissions_produce_no_changes() -> None:
    """Re-running against a configured guild must be a no-op, not a rewrite."""
    result = plan(everyone_may_speak=False, role_may_speak=True)
    assert result.permission_changes == []


def test_a_missing_role_is_requested_for_creation() -> None:
    result = plan(role_id=None)
    assert result.role_to_create is not None
    assert settings.CONSENT_ROLE_ID not in result.writes


def test_an_unchanged_value_is_not_rewritten() -> None:
    result = plan({settings.VOICE_CHANNEL_ID: str(CHANNEL)})
    assert settings.VOICE_CHANNEL_ID not in result.writes


def test_a_changed_channel_is_rewritten() -> None:
    result = plan({settings.VOICE_CHANNEL_ID: "999"})
    assert result.writes[settings.VOICE_CHANNEL_ID] == str(CHANNEL)
```

- [ ] **Step 2: Run it, confirm it fails, then implement `plan_setup`**

It compares the desired state against what is already configured and returns only the differences. Re-running against a correctly configured guild yields an empty plan — that is what makes the command safe to run twice.

`document_target` cannot be derived from Discord, so it is reported as missing rather than guessed.

- [ ] **Step 3: Add the two configuration keys**

In `domain/settings.py`: `ADMIN_ROLE_ID = "admin_role_id"` (required, no default) and `MERGE_GAP_SECONDS = "merge_gap_seconds"` with default `"15"`. Add the latter to the integer keys that `ConfigStore.set` validates, and `ADMIN_ROLE_ID` to `REQUIRED_KEYS`.

Then remove the two constants they replace:
- `permissions.py`'s hardcoded role name — `require_admin()` reads `admin_role_id` from the config store instead. Discord's own administrator permission still passes unconditionally, so a guild that has not configured the key yet is not locked out of `/config` and `/setup`.
- `documents.py`'s merge gap default — `render_transcript` already takes the gap from its caller; make the worker read it from configuration in plan 3.

- [ ] **Step 4: Write the cog**

`/setup` with typed parameters so Discord renders native pickers:

```python
@app_commands.command()
@require_admin()
async def setup(
    self,
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    policy_url: str,
    policy_version: str,
    consent_role: discord.Role | None = None,
) -> None: ...
```

The cog reads the current configuration and the channel's existing overwrites, calls `plan_setup`, applies the result, and reports what it changed, what it left alone, and what is still missing. Ephemeral, like every other reply.

Requirements a reviewer checks by reading:

- **Permission failures are reported, never swallowed.** If the bot may not edit the channel or create the role, it says exactly what a human must do instead. A half-applied setup that claims success is worse than one that admits it stopped.
- **It applies the configuration writes even when the permission changes fail**, and says so — the two are independent, and refusing to store anything because a permission edit failed would leave the guild worse off than before.
- **It never prints a token or a secret**, and its summary names keys, not values, for anything that could be sensitive later.

- [ ] **Step 5: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A
git commit -m "feat: add guided setup and move two constants into configuration"
```
