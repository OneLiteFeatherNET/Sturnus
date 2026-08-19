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
