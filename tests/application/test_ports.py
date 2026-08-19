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
