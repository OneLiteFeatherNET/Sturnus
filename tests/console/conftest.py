"""Shared fixtures and doubles for the console's API tests.

The `aiohttp_client` fixture is the same one `tests/infrastructure/
test_linkserver.py` builds for the same reason: `pytest-aiohttp` is not a
dependency of this project, and aiohttp's own bundled plugin runs an event
loop that races pytest-asyncio's. This reproduces the shape those tests
need on pytest-asyncio's loop instead.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sturnus.console.app import build_api
from sturnus.console.audio import AudioDelivery
from sturnus.console.filters import SessionFilter
from sturnus.console.ports import (
    AdminDirectory,
    LinkDirectory,
    OAuthClient,
    QueueControl,
    QueueSnapshot,
    RequeueOutcome,
    SessionReads,
    SettingsStore,
    StateStore,
    TagWriter,
    Track,
)
from sturnus.console.session import SessionCookie
from sturnus.console.statistics import AttendedSession, SessionPage, TagUse
from sturnus.infrastructure.crypto import CHUNK_SIZE, encrypt_file
from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity, LinkExchangeError

AiohttpClientFactory = Callable[
    [web.Application], Awaitable["TestClient[web.Request, web.Application]"]
]

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
SECRET = "s" * 32
ANNA, BEN = 100, 200

#: The guild a test means when it does not name one. Real-shaped rather
#: than a small integer, so a test that confuses a guild id with a user id
#: fails rather than coincidentally passing.
GUILD = 4711
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
    """The mirrored administrator membership, in memory.

    One source of truth -- the per-guild mapping -- because that is what
    `admin_member` is. `admins` is shorthand for "administers the one
    guild the test did not bother to name", and every question is derived
    from the mapping rather than tracked beside it: a double that can
    answer "yes" to `is_admin_anywhere` and "no" to every `is_admin` would
    prove the opposite of what a test using it claims.

    The endpoints themselves are tested against the real `AdminMemberStore`
    on the real database (`tests/console/test_settings_routes.py`), since
    a per-guild authorisation rule is not worth proving against a
    dictionary.
    """

    def __init__(
        self,
        admins: set[int] | None = None,
        by_guild: dict[int, set[int]] | None = None,
    ) -> None:
        self.by_guild: dict[int, set[int]] = {
            guild_id: set(members) for guild_id, members in (by_guild or {}).items()
        }
        if admins:
            self.by_guild.setdefault(GUILD, set()).update(admins)

    async def is_admin_anywhere(self, discord_user_id: int) -> bool:
        return any(discord_user_id in members for members in self.by_guild.values())

    async def administered_guilds(self, discord_user_id: int) -> Sequence[int]:
        return tuple(
            sorted(
                guild_id
                for guild_id, members in self.by_guild.items()
                if discord_user_id in members
            )
        )

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool:
        return discord_user_id in self.by_guild.get(guild_id, set())


def now_at(moment: datetime = T0) -> Callable[[], datetime]:
    return lambda: moment


class FakeReads:
    """The console's reads, in memory.

    Note what this does *not* do: it does not scope. Scoping is a property
    of the SQL and is tested against the real database in
    `tests/console/test_queries.py` -- a double that filtered in Python
    would only ever prove that the double filters. What the route tests
    use it for is the other half: that each handler asks for the
    signed-in user and nobody else, and what it does with the answer.
    """

    def __init__(
        self,
        sessions: Sequence[AttendedSession] = (),
        transcripts: Sequence[str] = (),
        tags: Sequence[TagUse] = (),
    ) -> None:
        self.sessions = tuple(sessions)
        self.transcripts = tuple(transcripts)
        self.tags = tuple(tags)
        #: Every Discord id this was asked about, in order. The route
        #: tests assert on it, because "the handler passed the session's
        #: own user id through" is the thing that cannot be checked from
        #: the response body.
        self.asked_for: list[int] = []
        self.years: list[int] = []
        self.days: list[date] = []
        #: Every window this was asked for, as `(limit, offset)`. The
        #: route tests assert on it, because "the handler served the
        #: window the query string named" cannot be seen in a body that
        #: happens to be short.
        self.windows: list[tuple[int, int]] = []
        #: Every filter this was asked to apply, in order.
        self.filters: list[SessionFilter] = []

    async def sessions_for(self, discord_user_id: int) -> Sequence[AttendedSession]:
        self.asked_for.append(discord_user_id)
        return self.sessions

    async def sessions_page(
        self,
        discord_user_id: int,
        *,
        limit: int,
        offset: int,
        matching: SessionFilter,
    ) -> SessionPage:
        # Windowed in Python, which a double may do and the real query may
        # not: what the route tests need from this is that the handler
        # passed the window it parsed, and `self.windows` is where they
        # read that off.
        self.asked_for.append(discord_user_id)
        self.windows.append((limit, offset))
        # Recorded and never applied. Narrowing is a property of the SQL
        # and is tested against the real database in
        # `tests/console/test_queries.py`; a double that filtered in
        # Python would only ever prove that the double filters. What the
        # route tests need from this is that the handler read the query
        # string into the filter it passed on.
        self.filters.append(matching)
        return SessionPage(
            sessions=self.sessions[offset : offset + limit],
            total=len(self.sessions),
            limit=limit,
            offset=offset,
        )

    async def session_for(self, discord_user_id: int, session_id: int) -> AttendedSession | None:
        self.asked_for.append(discord_user_id)
        return next((s for s in self.sessions if s.id == session_id), None)

    async def sessions_in_year(self, discord_user_id: int, year: int) -> Sequence[AttendedSession]:
        self.asked_for.append(discord_user_id)
        self.years.append(year)
        return self.sessions

    async def sessions_on_day(self, discord_user_id: int, day: date) -> Sequence[AttendedSession]:
        self.asked_for.append(discord_user_id)
        self.days.append(day)
        return self.sessions

    async def transcripts_of(self, discord_user_id: int) -> Sequence[str]:
        self.asked_for.append(discord_user_id)
        return self.transcripts

    async def tags_of(self, discord_user_id: int) -> Sequence[TagUse]:
        self.asked_for.append(discord_user_id)
        return self.tags


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


class FakeConfig:
    """A settings store for tests that are not about settings.

    The settings endpoints themselves are tested against the real
    `ConfigStore` on the real database (`test_settings_routes.py`), because
    the value validation they enforce is the store's and a fake would have
    had to reimplement it -- which is the drift the endpoints exist to
    avoid.

    This exists only so `build_test_api` can construct an application for
    the other test modules. A test that reaches for it to assert something
    about settings is asking the wrong object.
    """

    def __init__(self, values: dict[int, dict[str, str]] | None = None) -> None:
        self.values: dict[int, dict[str, str]] = values or {}

    async def snapshot(self, guild_id: int) -> dict[str, str]:
        return dict(self.values.get(guild_id, {}))

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None:
        del now
        guild = self.values.setdefault(guild_id, {})
        if value is None:
            guild.pop(key, None)
        else:
            guild[key] = value


# ---------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------


class FakeQueue:
    """A transcription queue nobody administers, until a test says otherwise.

    Defaults to answering `None` everywhere, which is what the real
    control answers for "no such session or not yours" -- so a test that
    has no interest in re-queueing gets 404s rather than a fake that
    quietly authorises everything.
    """

    def __init__(
        self,
        snapshot: QueueSnapshot | None = None,
        outcome: RequeueOutcome | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.outcome = outcome
        self.requeued: list[tuple[int, int]] = []

    async def status_for(self, session_id: int, *, requested_by: int) -> QueueSnapshot | None:
        del session_id, requested_by
        return self.snapshot

    async def requeue(self, session_id: int, *, requested_by: int) -> RequeueOutcome | None:
        # Recorded rather than merely counted: "an administrator's own id
        # reached the write, not one from the URL" is the property the
        # authorisation tests assert on.
        self.requeued.append((session_id, requested_by))
        return self.outcome


class FakeTags:
    """The tag write path, in memory, scoped by the asking user.

    Answers `None` for a session nobody says this person was in, which is
    what the real writer answers for both "no such session" and "not
    yours" -- so a route test that forgot to pass the signed-in user gets
    a 404 here too rather than a fake that writes for anybody.

    Whether the *statement* scopes is a property of SQL and is tested
    against the real database in `tests/console/test_adapters.py`.
    """

    def __init__(
        self,
        participants: dict[int, set[int]] | None = None,
        stored: dict[tuple[int, int], tuple[str, ...]] | None = None,
    ) -> None:
        self.participants = participants if participants is not None else {}
        self.stored = stored if stored is not None else {}
        #: Every write this was asked to make, in order. The route tests
        #: assert on it, because "the handler wrote for the signed-in
        #: person and not for anybody the request could name" cannot be
        #: seen in a response body.
        self.written: list[tuple[int, int, tuple[str, ...]]] = []

    async def replace(
        self, session_id: int, *, owner: int, tags: Sequence[str], now: datetime
    ) -> tuple[str, ...] | None:
        del now
        self.written.append((session_id, owner, tuple(tags)))
        if owner not in self.participants.get(session_id, set()):
            return None
        self.stored[(session_id, owner)] = tuple(sorted(tags))
        return self.stored[(session_id, owner)]


def build_test_api(
    *,
    oauth: OAuthClient | None = None,
    states: StateStore | None = None,
    links: LinkDirectory | None = None,
    admins: AdminDirectory | None = None,
    reads: SessionReads | None = None,
    config: SettingsStore | None = None,
    audio: AudioDelivery | None = None,
    queue: QueueControl | None = None,
    tags: TagWriter | None = None,
    sessions: SessionCookie | None = None,
    now: Callable[[], datetime] | None = None,
    schema_ready: bool = True,
) -> web.Application:
    """Builds the console API with every collaborator defaulted.

    Typed to the ports rather than to the doubles, so a test may hand it
    a real store where a real one is what it means to exercise --
    `test_settings_routes` passes the actual `ConfigStore`, because the
    validation under test is the store's and a fake would have had to
    reimplement it.

    One factory rather than one per test module. Three modules each
    constructing the application themselves meant that adding a
    collaborator broke the two files that had no interest in it -- which is
    precisely what happened when the audio and read changes met. Here a new
    collaborator is one default in one place.

    Every argument is an override, so a test names only what it is about.
    """
    return build_api(
        oauth=oauth or FakeOAuth(),
        states=states or FakeStates(),
        links=links or FakeLinks(),
        admins=admins or FakeAdmins(),
        reads=reads or FakeReads(),
        config=config or FakeConfig(),
        audio=audio
        or AudioDelivery(
            tracks=FakeTracks(),
            keys=FakeKeys(),
            source=FakeAudioSource(),
        ),
        queue=queue or FakeQueue(),
        tags=tags or FakeTags(),
        sessions=sessions or SessionCookie(SECRET, timedelta(hours=12)),
        now=now or now_at(),
        schema_ready=lambda: schema_ready,
        console_origin="https://sturnus.example",
    )
