"""Protocols for the systems the application talks to.

Only what genuinely varies is abstracted: things a test must replace with a
fake, or whose implementation may change. Repositories deliberately have no
protocol — they are tested against a real PostgreSQL through Testcontainers,
and an interface with exactly one implementation behind a real database test
would be ceremony.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class AudioWriter(Protocol):
    """Appends one speaker's audio to a continuous file, padding silence.

    `path` is where the finished recording lives once `close()` returns --
    still plaintext. The orchestrator needs it to hand the file to the
    `Encryptor` and to remove the plaintext once the encrypted copy exists.
    Packet-level detail (sample rate, channel layout, gap-filling) is the
    adapter's business; the orchestrator only ever hands over raw audio and
    a timestamp.
    """

    path: Path

    def write(self, at: datetime, pcm: bytes) -> None: ...

    def close(self) -> None: ...


class AudioWriterFactory(Protocol):
    """Opens one `AudioWriter` per speaker who has actually spoken.

    `session_id` and `discord_user_id` identify whose file this is;
    `epoch` is the absolute time of that speaker's first packet, which the
    writer uses to place every later packet on the timeline.
    """

    def open(self, session_id: int, discord_user_id: int, epoch: datetime) -> AudioWriter: ...


@dataclass(frozen=True)
class SessionKey:
    """A per-session data key: usable in memory, and wrapped for storage.

    `plaintext` is handed to `Encryptor.encrypt` and then discarded;
    `wrapped` is what gets persisted alongside the recording so the key can
    be recovered later.
    """

    plaintext: bytes
    wrapped: bytes


class Encryptor(Protocol):
    """Envelope-encrypts recordings with a fresh key per session.

    `key_id` identifies which master key wrapped the session's data key, so
    a later decrypt knows which master key to unwrap it with -- it is
    recorded alongside `wrapped_data_key` on each transcription job.
    """

    @property
    def key_id(self) -> str: ...

    def new_session_key(self) -> SessionKey: ...

    def encrypt(self, source: Path, target: Path, key: bytes) -> None: ...


class VoiceReceiver(Protocol):
    """Wraps the voice-receive extension.

    Kept deliberately narrow: the extension is a community project without
    official discord.py support, and this is the seam that keeps a future
    replacement from reaching into the rest of the system.
    """

    async def join(self, channel_id: int) -> None: ...

    async def leave(self) -> None: ...
