"""Shared fixtures and doubles for the console's API tests.

The `aiohttp_client` fixture is the same one `tests/infrastructure/
test_linkserver.py` builds for the same reason: `pytest-aiohttp` is not a
dependency of this project, and aiohttp's own bundled plugin runs an event
loop that races pytest-asyncio's. This reproduces the shape those tests
need on pytest-asyncio's loop instead.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sturnus.console.ports import Track
from sturnus.infrastructure.crypto import CHUNK_SIZE, encrypt_file
from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity, LinkExchangeError

AiohttpClientFactory = Callable[
    [web.Application], Awaitable["TestClient[web.Request, web.Application]"]
]

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
SECRET = "s" * 32
ANNA, BEN = 100, 200
ANNA_OUTLINE = "c9a1b2e3-4f5a-4b3c-8d2e-1a2b3c4d5e6f"


@pytest.fixture
async def aiohttp_client() -> AsyncIterator[AiohttpClientFactory]:
    clients: list[TestClient[web.Request, web.Application]] = []

    async def make(app: web.Application) -> TestClient[web.Request, web.Application]:
        test_client = TestClient(TestServer(app))
        await test_client.start_server()
        clients.append(test_client)
        return test_client

    yield make
    for test_client in clients:
        await test_client.close()


class FakeOAuth:
    """Stands in for `OutlineOAuth` without a live Outline."""

    def __init__(self, identity: ExternalIdentity | None = None, fail: bool = False) -> None:
        self.identity = identity or ExternalIdentity(ANNA_OUTLINE, "Anna Example")
        self.fail = fail
        self.authorize_calls: list[str] = []

    def authorize_url(self, state: str) -> str:
        self.authorize_calls.append(state)
        return f"https://outline.example/oauth/authorize?state={state}"

    async def identity_from_code(self, code: str) -> ExternalIdentity:
        if self.fail:
            raise LinkExchangeError("refused", status_code=400)
        del code
        return self.identity


class FakeStates:
    """The single-use OAuth state store, in memory."""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self._valid: set[str] = set()

    async def issue(self, state: str, now: datetime) -> None:
        del now
        self.issued.append(state)
        self._valid.add(state)

    async def consume(self, state: str, now: datetime) -> bool:
        del now
        if state not in self._valid:
            return False
        self._valid.discard(state)
        return True


class FakeLinks:
    """`account_link`, reversed: Outline identity to Discord user."""

    def __init__(self, mapping: dict[str, int] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {ANNA_OUTLINE: ANNA}

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None:
        del provider
        return self.mapping.get(external_user_id)


class FakeAdmins:
    def __init__(self, admins: set[int] | None = None) -> None:
        self.admins = admins or set()

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        return discord_user_id in self.admins


def now_at(moment: datetime = T0) -> Callable[[], datetime]:
    return lambda: moment


# ---------------------------------------------------------------------------
# Audio delivery
# ---------------------------------------------------------------------------

SESSION = 4711
S3_KEY = "sessions/4711/speakers/100.enc"
KEY_ID = "test-key-1"
DATA_KEY = bytes(range(32))

#: Small enough that a whole test stays fast, large enough that a stream of
#: it crosses several chunk boundaries and several read boundaries at once.
TRACK_BYTES = CHUNK_SIZE * 2 + 5_000


def sealed(plaintext: bytes, tmp_path: Path, data_key: bytes = DATA_KEY) -> bytes:
    """One recording in the real on-disk format, as bytes.

    Goes through `encrypt_file` rather than assembling the framing by hand:
    a fixture that reimplements the format would agree with itself and with
    nothing else, and the whole point of the reader under test is that it
    understands what the writer actually wrote.
    """
    source = tmp_path / "plain.pcm"
    source.write_bytes(plaintext)
    target = tmp_path / "sealed.enc"
    encrypt_file(source, target, data_key)
    return target.read_bytes()


class FakeAudioSource:
    """The object store, in memory, with every read it served recorded.

    The recording is what the range tests assert on: "a listener who wants
    minute 30 must not download minutes 0 to 29" is a statement about which
    bytes were fetched, not about which bytes came back.
    """

    #: Deliberately not a round number and deliberately smaller than a
    #: chunk, so every frame the reader assembles spans several pieces and
    #: no boundary lines up with anything.
    PIECE = 7_919

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = objects if objects is not None else {}
        self.reads: list[tuple[int, int]] = []
        self.streamed_from: list[int] = []
        self.streamed_bytes = 0

    async def size(self, key: str) -> int:
        if key not in self.objects:
            raise KeyError(key)
        return len(self.objects[key])

    async def read(self, key: str, start: int, length: int) -> bytes:
        if key not in self.objects:
            raise KeyError(key)
        self.reads.append((start, length))
        return self.objects[key][start : start + length]

    async def stream(self, key: str, start: int) -> AsyncGenerator[bytes, None]:
        if key not in self.objects:
            raise KeyError(key)
        self.streamed_from.append(start)
        body = self.objects[key][start:]
        for offset in range(0, len(body), self.PIECE):
            piece = body[offset : offset + self.PIECE]
            self.streamed_bytes += len(piece)
            yield piece


class FakeKeys:
    """The master key, without one. Records every unwrap it was asked for."""

    def __init__(self, key_id: str = KEY_ID, data_key: bytes = DATA_KEY) -> None:
        self.key_id = key_id
        self._data_key = data_key
        self.unwrapped: list[bytes] = []

    def unwrap(self, wrapped: bytes) -> bytes:
        self.unwrapped.append(wrapped)
        return self._data_key


class FakeTracks:
    """`transcription_job` joined to `session_participant`, in memory.

    Scoped by the asking user in `track_for` rather than by a filter the
    caller applies afterwards -- the same shape the real adapter has, so a
    handler that forgot to pass `requested_by` would fail here too.
    """

    def __init__(
        self,
        tracks: dict[tuple[int, int], Track] | None = None,
        participants: dict[int, set[int]] | None = None,
    ) -> None:
        self.tracks = tracks if tracks is not None else {}
        self.participants = participants if participants is not None else {}
        self.asked: list[tuple[int, int, int]] = []

    async def track_for(
        self, session_id: int, speaker_id: int, *, requested_by: int
    ) -> Track | None:
        self.asked.append((session_id, speaker_id, requested_by))
        if requested_by not in self.participants.get(session_id, set()):
            return None
        return self.tracks.get((session_id, speaker_id))


async def collect(pieces: AsyncIterator[bytes]) -> bytes:
    return b"".join([piece async for piece in pieces])
