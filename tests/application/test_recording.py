"""Orchestration tests.

Every collaborator is a fake, so these exercise the real decision logic
without a voice channel, a database or an object store.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sturnus.application.ports import SessionKey
from sturnus.application.recording import RecordingService
from sturnus.domain.session import EndReason, SessionTimeouts

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD, CHANNEL, ANNA, BEN = 1, 2, 100, 200
RTP = 48_000


def pcm(frames: int) -> bytes:
    """`frames` of 48 kHz stereo 16-bit input, as Discord delivers it."""
    return b"\x10\x27" * 2 * frames


class FakeSessions:
    def __init__(self) -> None:
        self.opened: list[int] = []
        self.channel_names: list[str | None] = []
        self.participants: dict[int, str] = {}
        self.epochs: dict[int, datetime] = {}
        self.closed: list[tuple[int, str]] = []
        self.keys: dict[int, tuple[str, bytes]] = {}
        self.status: dict[int, str] = {}
        self._next = 1

    async def open_session(
        self, _guild_id: int, _channel_id: int, channel_name: str | None, _now: datetime
    ) -> int:
        sid = self._next
        self._next += 1
        self.opened.append(sid)
        self.channel_names.append(channel_name)
        self.status[sid] = "open"
        return sid

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None:
        self.keys[session_id] = (encryption_key_id, wrapped_data_key)

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:
        return self.keys.get(session_id)

    async def add_participant(
        self, _session_id: int, user_id: int, display_name: str, _now: datetime
    ) -> None:
        self.participants.setdefault(user_id, display_name)

    async def set_audio_epoch(self, _session_id: int, user_id: int, at: datetime) -> None:
        self.epochs.setdefault(user_id, at)

    async def close_session(self, session_id: int, _ended_at: datetime, reason: str) -> None:
        self.closed.append((session_id, reason))
        self.status[session_id] = "closed"

    async def session_status(self, session_id: int) -> str | None:
        return self.status.get(session_id)

    async def find_open_session(self, _guild_id: int) -> int | None:
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
        #: The uploaded bytes, captured at `put()` time -- `close()` removes
        #: the local `.enc` right after a successful upload (Spec 12.4), so
        #: a test that wants to inspect what was actually uploaded cannot
        #: read the file back off disk afterwards.
        self.put_contents: dict[str, bytes] = {}

    async def put(self, key: str, source: Path) -> None:
        assert source.exists(), "uploading a file that is not there"
        self.put_keys.append(key)
        self.put_contents[key] = source.read_bytes()

    async def delete(self, key: str) -> None:
        pass


class FakeAudioWriter:
    """A minimal stand-in for the infrastructure `SpeakerWriter` adapter.

    Neither pads silence nor resamples -- the orchestrator does not care
    how the bytes reach disk, only that they do -- but it writes a real
    file, so filesystem-observable properties (a plaintext file exists
    until it is encrypted and removed) can still be asserted the same way
    they would be against the real adapter.
    """

    def __init__(self, path: Path, epoch: datetime) -> None:
        self.path = path
        self.epoch = epoch
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")
        self._closed = False
        #: Where each packet was placed on the timeline. The real adapter
        #: turns this into padding; here it is kept so tests can assert on
        #: the placement itself.
        self.placed_at: list[datetime] = []

    def write(self, _at: datetime, pcm: bytes) -> None:
        if self._closed:
            raise RuntimeError("writer is closed")
        self._file.write(pcm)
        self.placed_at.append(_at)

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True


class FakeAudioWriterFactory:
    def __init__(self, recording_dir: Path) -> None:
        self._recording_dir = recording_dir
        self.opened: list[tuple[int, int]] = []

    def open(self, session_id: int, discord_user_id: int, epoch: datetime) -> FakeAudioWriter:
        self.opened.append((session_id, discord_user_id))
        path = self._recording_dir / f"session-{session_id}" / f"{discord_user_id}.wav"
        return FakeAudioWriter(path, epoch)


class FakeEncryptor:
    """A minimal stand-in for the infrastructure `CryptoEncryptor` adapter.

    "Encrypts" by prefixing the plaintext with a marker instead of running
    real AES-GCM. What the orchestrator must guarantee is that encryption
    happens before upload and that the same session key is used throughout
    -- which byte sequence a real encrypted file starts with is the
    adapter's business, not something this layer's tests should pin.
    """

    MARKER = b"FAKE-ENCRYPTED:"

    def __init__(self, key_id: str = "k1") -> None:
        self.key_id = key_id
        self.encrypted: list[Path] = []

    def new_session_key(self) -> SessionKey:
        return SessionKey(plaintext=b"0" * 32, wrapped=b"wrapped-key")

    def encrypt(self, source: Path, target: Path, _key: bytes) -> None:
        self.encrypted.append(target)
        target.write_bytes(self.MARKER + source.read_bytes())


def service(
    tmp_path: Path,
    sessions: FakeSessions | None = None,
    jobs: FakeJobs | None = None,
    store: FakeStore | None = None,
    writers: FakeAudioWriterFactory | None = None,
    encryptor: FakeEncryptor | None = None,
    channel_name: str | None = None,
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
        writers=writers or FakeAudioWriterFactory(tmp_path),
        encryptor=encryptor or FakeEncryptor(),
        retention_days=30,
        channel_name=channel_name,
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


async def test_the_session_row_carries_the_key_once_opened(tmp_path: Path) -> None:
    """The wrapped key must reach the session row when the session opens.

    A crash right after this point -- before any recording finishes and
    the first job enqueues -- must still leave crash recovery something to
    read the key from.
    """
    sessions = FakeSessions()
    encryptor = FakeEncryptor(key_id="k1")
    svc = service(tmp_path, sessions=sessions, encryptor=encryptor)
    await svc.participants_changed(1, T0)
    assert sessions.keys == {1: ("k1", b"wrapped-key")}


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
    """Each speaker gets their own upload, and the recording directory ends up empty.

    Spec 12.4 requires the local `.enc` to go once the upload succeeds --
    left behind, it would fill the PVC and resurface as a false orphan on
    the next restart (see `test_recovery.py`). This asserts the directory
    is empty rather than counting `.enc` files, so it fails if a leftover
    file of *any* kind survives `close()`, not just the two expected ones.
    """
    jobs = FakeJobs()
    svc = service(tmp_path, jobs=jobs)
    await svc.participants_changed(2, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.voice_packet(BEN, "ben", 2, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=5))
    assert len(jobs.enqueued) == 2  # both speakers were still uploaded and enqueued
    assert list(tmp_path.rglob("*")) == []


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
    """The orchestrator must encrypt before uploading.

    Which byte sequence a real encrypted file starts with is the crypto
    adapter's business (see `tests/infrastructure/test_crypto.py`); what
    this layer must guarantee is that every uploaded file passed through
    the `Encryptor` port first -- checked here against the bytes the
    `FakeStore` actually received, since by the time `close()` returns the
    `.enc` file itself must already be gone from disk (Spec 12.4).
    """
    store = FakeStore()
    encryptor = FakeEncryptor()
    svc = service(tmp_path, store=store, encryptor=encryptor)
    await svc.participants_changed(1, T0)
    marker = b"\x11\x22" * 2 * 4800
    await svc.voice_packet(ANNA, "anna", 1, RTP, marker, T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))

    assert encryptor.encrypted  # the writer's plaintext went through the encryptor
    assert store.put_keys == ["sessions/1/speakers/100.enc"]
    assert store.put_contents["sessions/1/speakers/100.enc"].startswith(FakeEncryptor.MARKER)
    assert list(tmp_path.rglob("*")) == []  # nothing left behind after the upload


async def test_plaintext_audio_is_removed_after_upload(tmp_path: Path) -> None:
    """Only the encrypted form may survive the upload."""
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))
    assert list(tmp_path.glob("**/*.wav")) == []


async def test_close_leaves_no_files_behind(tmp_path: Path) -> None:
    """Spec 12.4: nothing may survive on the volume once close() returns.

    Before the fix, `close()` encrypted, uploaded and enqueued but never
    unlinked the `.enc` -- it stayed on disk forever, filling the PVC and
    getting rediscovered by `recover_orphans` on every future restart
    (`tests/application/test_recovery.py`). This covers several speakers
    and the now-empty session directory too, not just one file.
    """
    svc = service(tmp_path)
    await svc.participants_changed(2, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    await svc.voice_packet(BEN, "ben", 2, RTP, pcm(960), T0)
    await svc.close(EndReason.EMPTY, T0 + timedelta(minutes=1))

    assert list(tmp_path.rglob("*")) == []
    assert not (tmp_path / "session-1").exists()


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


async def test_reset_after_close_lets_a_new_session_open(tmp_path: Path) -> None:
    """One `RecordingService` must be able to record a second, independent session.

    Before `reset()` existed, `_closed` stayed `True` and the machine
    stayed in `CLOSING` forever once a session closed: `is_recording`
    never became `True` again, so a second participant could never open a
    new session on this instance -- the bot recorded exactly one session
    per process lifetime. This is the same guarantee
    `tests/infrastructure/discord/test_client.py`'s two-session test
    checks at the client boundary; this one pins it directly against
    `RecordingService`.
    """
    sessions = FakeSessions()
    jobs = FakeJobs()
    svc = service(tmp_path, sessions=sessions, jobs=jobs)

    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    assert await svc.tick(T0 + timedelta(minutes=20)) is EndReason.IDLE_TIMEOUT
    svc.reset()
    assert svc.is_recording is False
    assert svc.session_id is None

    second_start = T0 + timedelta(hours=1)
    await svc.participants_changed(1, second_start)
    await svc.voice_packet(BEN, "ben", 2, RTP, pcm(960), second_start)
    await svc.close(EndReason.EMPTY, second_start + timedelta(minutes=1))

    # The second session got its own row, own writer, and its own job --
    # nothing carried over from the first.
    assert sessions.opened == [1, 2]
    assert sessions.keys.keys() == {1, 2}
    assert len(jobs.enqueued) == 2
    assert {job["session_id"] for job in jobs.enqueued} == {1, 2}
    assert {job["discord_user_id"] for job in jobs.enqueued} == {ANNA, BEN}


async def test_returning_participant_keeps_the_same_session(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    await svc.participants_changed(0, T0 + timedelta(seconds=10))
    await svc.participants_changed(1, T0 + timedelta(seconds=30))
    assert sessions.opened == [1]
    assert svc.session_id == 1


async def test_the_channel_name_is_recorded_when_the_session_opens(tmp_path: Path) -> None:
    """The worker writes the protocol and has no Discord connection, so the
    name has to be captured here or not at all. Capturing it at open time
    also means a channel renamed later does not rewrite the protocols of
    meetings held under the old name.
    """
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions, channel_name="Meeting-Raum")

    await svc.participants_changed(1, T0)

    assert sessions.channel_names == ["Meeting-Raum"]


async def test_apply_tunables_mid_session_changes_the_retention_that_close_stamps(
    tmp_path: Path,
) -> None:
    """A retention change reaches the session in progress -- intentionally.

    `_retention_days` is read at exactly one point, `close()`, when it
    stamps `retention_until` on the jobs it enqueues. The value in force
    when a recording is *filed* is the one that governs it, so a change
    made while it is still recording applies to it.
    """
    jobs = FakeJobs()
    svc = service(tmp_path, jobs=jobs)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)

    svc.apply_tunables(
        SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4),
        retention_days=7,
    )

    assert svc.is_recording is True, "a tunable change must not close the session"
    assert svc.session_id == 1, "nor open a new one"
    closed_at = T0 + timedelta(minutes=5)
    await svc.close(EndReason.EMPTY, closed_at)
    assert jobs.enqueued[0]["retention_until"] == closed_at + timedelta(days=7)


async def test_apply_tunables_mid_session_changes_the_next_timeout_decision(
    tmp_path: Path,
) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)
    assert await svc.tick(T0 + timedelta(minutes=10)) is None

    svc.apply_tunables(
        SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=5, max_session_hours=4),
        retention_days=30,
    )

    assert await svc.tick(T0 + timedelta(minutes=10)) is EndReason.IDLE_TIMEOUT
    # It closed through the ordinary path: the row is closed and the audio
    # was uploaded and enqueued, not discarded.
    assert sessions.closed == [(1, "idle_timeout")]


async def test_retarget_between_sessions_moves_the_next_session_row(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    assert svc.channel_id == CHANNEL

    svc.retarget(999, "Anderer-Raum")

    assert svc.channel_id == 999
    await svc.participants_changed(1, T0)
    assert sessions.opened == [1]


async def test_retarget_carries_the_channel_name_to_the_next_session(tmp_path: Path) -> None:
    """The name has to follow the channel, or the header names the wrong room.

    Two features that landed separately meet here: the protocol header
    names the channel, and reconfiguration moves an idle service to a new
    channel in place. Without the name travelling along, the next protocol
    would be headed with the room the recording did not come from -- a
    header that is confidently wrong, which is worse than the `None`
    fallback to a bare link.
    """
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions, channel_name="Alter-Raum")

    svc.retarget(999, "Neuer-Raum")
    await svc.participants_changed(1, T0)

    assert svc.channel_id == 999
    assert sessions.channel_names == ["Neuer-Raum"]


async def test_retarget_refuses_while_recording(tmp_path: Path) -> None:
    """The guard that keeps a session row from disagreeing with its own audio.

    `_channel_id` is written onto the `sessions` row by `open_session`.
    Moving it mid-session would produce a protocol whose header names one
    channel while the audio came from another.
    """
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    with pytest.raises(AssertionError, match="mid-session"):
        svc.retarget(999, "Anderer-Raum")


async def test_due_reason_does_not_close_the_session(tmp_path: Path) -> None:
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    svc.apply_tunables(
        SessionTimeouts(empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=1),
        retention_days=30,
    )

    assert svc.due_reason(T0 + timedelta(hours=2)) is EndReason.MAX_DURATION
    assert svc.is_recording is True
    assert sessions.closed == []


async def test_end_now_uploads_everything_and_leaves_the_service_reusable(
    tmp_path: Path,
) -> None:
    """`/config apply force:true` ends a recording early; it never discards one.

    Both halves matter. The recording must take the ordinary route out --
    encrypt, upload, enqueue, close the row -- and the service must be left
    exactly as ready for the next session as a timed-out one is. `close()`
    alone gives only the first: it leaves the machine in RECORDING, so the
    `reset()` that follows used to raise and the guild recorded nothing at
    all afterwards.
    """
    sessions, jobs = FakeSessions(), FakeJobs()
    svc = service(tmp_path, sessions=sessions, jobs=jobs)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)

    await svc.end_now(EndReason.SHUTDOWN, T0 + timedelta(minutes=5))

    assert sessions.closed == [(1, "shutdown")]
    assert [job["discord_user_id"] for job in jobs.enqueued] == [ANNA]
    assert svc.is_recording is False

    svc.reset()
    await svc.participants_changed(1, T0 + timedelta(minutes=6))
    assert sessions.opened == [1, 2], "the service must be able to record again"
    assert svc.is_recording is True


async def test_end_now_without_a_session_does_nothing(tmp_path: Path) -> None:
    """Safe on any path that merely might have a session open, e.g. shutdown."""
    sessions, jobs = FakeSessions(), FakeJobs()
    svc = service(tmp_path, sessions=sessions, jobs=jobs)

    await svc.end_now(EndReason.SHUTDOWN, T0)

    assert sessions.opened == []
    assert sessions.closed == []
    assert jobs.enqueued == []


async def test_request_close_ends_the_session_through_the_next_tick(tmp_path: Path) -> None:
    """What the capture path does when nothing decodes any more.

    It does not close the session behind the orchestrator's back; it asks,
    and the reason comes back out of `tick()` so the caller still gets its
    one chance to leave the channel and reset.
    """
    sessions = FakeSessions()
    svc = service(tmp_path, sessions=sessions)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)

    svc.request_close(EndReason.DECODE_FAILURE)
    reason = await svc.tick(T0 + timedelta(seconds=1))

    assert reason is EndReason.DECODE_FAILURE
    assert sessions.closed == [(1, EndReason.DECODE_FAILURE.value)]
    assert svc.is_recording is False


async def test_speaker_stream_ended_retires_that_ssrcs_reference_point(tmp_path: Path) -> None:
    """An SSRC is per-connection, not per-user.

    A participant who reconnects comes back under a new one, and Discord
    may reissue an abandoned one; a stale reference would place the next
    stream's packets against the wrong origin. Anna's second stream
    therefore starts a fresh reference at its own arrival time rather than
    being placed hours before it, from an RTP value that means nothing to
    it.
    """
    svc = service(tmp_path)
    await svc.participants_changed(1, T0)
    await svc.voice_packet(ANNA, "anna", 1, RTP, pcm(960), T0)

    svc.speaker_stream_ended(1)

    later = T0 + timedelta(minutes=5)
    await svc.voice_packet(ANNA, "anna", 1, 0, pcm(960), later)

    writer = svc._writers[ANNA]
    assert isinstance(writer, FakeAudioWriter)
    # Without the reset, an RTP timestamp of 0 against a reference of
    # 48_000 would place this packet a second *before* the epoch.
    assert writer.placed_at[-1] == later
