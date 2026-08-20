"""Recovery of recordings a crash left behind.

A session is unsplittable (Spec 6.4): the bot records to a PVC for the whole
session and uploads at the end. A hard kill therefore leaves a complete
recording on disk that nothing has uploaded. Losing hours of audio because
the process restarted would be the worst failure this system has, so the
files are picked up on the next start.
"""

from datetime import UTC, datetime
from pathlib import Path

from sturnus.application.ports import SessionKey
from sturnus.application.recovery import find_orphans, recover_orphans

T0 = datetime(2026, 8, 19, tzinfo=UTC)


class FakeSessions:
    """Stands in for `SessionRepository`, keyed by session id.

    `keys` seeds what a real session row would already carry when it was
    opened -- `record_session_key` having run, or not, before the crash
    recovery is now cleaning up after.
    """

    def __init__(
        self,
        keys: dict[int, tuple[str, bytes]] | None = None,
        status: dict[int, str] | None = None,
    ) -> None:
        self.closed: list[tuple[int, str]] = []
        self._keys: dict[int, tuple[str, bytes]] = dict(keys or {})
        #: Seeds what a real session row's `status` would already be --
        #: `"open"` (the default, i.e. a genuine crash), `"closed"`, or
        #: `"documented"` -- for the sessions this test wants to pre-exist.
        self._status: dict[int, str] = dict(status or {})

    async def open_session(
        self, _guild_id: int, _channel_id: int, _channel_name: str | None, _now: datetime
    ) -> int:
        raise AssertionError("recovery must never open a new session")

    async def add_participant(
        self, _session_id: int, _user_id: int, _display_name: str, _now: datetime
    ) -> None:
        raise AssertionError("recovery must never add a participant")

    async def set_audio_epoch(self, _session_id: int, _user_id: int, _at: datetime) -> None:
        raise AssertionError("recovery must never set an audio epoch")

    async def record_silent_audio(self, _session_id: int, _user_id: int, _at: datetime) -> None:
        # Only `voice_packet` ever detects silent audio, and recovery never
        # reaches it -- there are no packets left to observe, only files.
        raise AssertionError("recovery must never record silent audio")

    async def close_session(self, session_id: int, _ended_at: datetime, reason: str) -> None:
        self.closed.append((session_id, reason))
        self._status[session_id] = "closed"

    async def session_status(self, session_id: int) -> str | None:
        return self._status.get(session_id, "open")

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None:
        self._keys[session_id] = (encryption_key_id, wrapped_data_key)

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:
        return self._keys.get(session_id)


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


class FakeEncryptor:
    """A fresh key distinguishable from any key a test seeds as "stored".

    If recovery ever reaches for this instead of the stored key, the
    mismatch is what a test asserting on the enqueued job catches.
    """

    def __init__(self, key_id: str = "freshly-generated-key-id") -> None:
        self.key_id = key_id
        self.encrypted: list[Path] = []

    def new_session_key(self) -> SessionKey:
        return SessionKey(plaintext=b"0" * 32, wrapped=b"freshly-generated-wrapped-key")

    def encrypt(self, source: Path, target: Path, _key: bytes) -> None:
        self.encrypted.append(target)
        target.write_bytes(b"FAKE-ENCRYPTED:" + source.read_bytes())


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


async def test_recovering_an_enc_file_uses_the_stored_key(tmp_path: Path) -> None:
    """A `.enc` orphan must be enqueued with the key that actually encrypted it.

    Before the fix, recovery generated a brand new session key and used
    *that* for every `.enc` job -- a key that never touched the file's
    bytes and therefore can never decrypt them. This asserts on the exact
    key the job is enqueued with, so it fails against that old behaviour:
    the freshly generated `FakeEncryptor` key and the stored key are
    deliberately distinct byte strings.
    """
    d = tmp_path / "session-7"
    d.mkdir()
    (d / "100.enc").write_bytes(b"STRN-ENCRYPTED-DATA")

    sessions = FakeSessions(keys={7: ("original-key-id", b"original-wrapped-key")})
    jobs = FakeJobs()
    store = FakeStore()
    encryptor = FakeEncryptor()

    recovered = await recover_orphans(
        tmp_path, sessions, jobs, store, encryptor, retention_days=30, now=T0
    )

    assert len(recovered) == 1
    assert len(jobs.enqueued) == 1
    job = jobs.enqueued[0]
    assert job["encryption_key_id"] == "original-key-id"
    assert job["wrapped_data_key"] == b"original-wrapped-key"
    assert store.put_keys == ["sessions/7/speakers/100.enc"]
    assert not (d / "100.enc").exists()


async def test_an_enc_file_with_no_stored_key_is_skipped(tmp_path: Path) -> None:
    """A session with no stored key must not get a job that is certain to fail.

    Covers both an old session that predates the key column and one that
    crashed before the key was ever written -- either way there is nothing
    to decrypt with, so the honest outcome is a loud skip, not a doomed job.
    """
    d = tmp_path / "session-9"
    d.mkdir()
    enc_path = d / "100.enc"
    enc_path.write_bytes(b"STRN-ENCRYPTED-DATA")

    sessions = FakeSessions()  # no key stored for session 9
    jobs = FakeJobs()
    store = FakeStore()
    encryptor = FakeEncryptor()

    recovered = await recover_orphans(
        tmp_path, sessions, jobs, store, encryptor, retention_days=30, now=T0
    )

    assert len(recovered) == 1  # still reported as found on disk
    assert jobs.enqueued == []
    assert store.put_keys == []
    assert enc_path.exists()  # left in place, not silently discarded
    assert sessions.closed == [(9, "crashed")]


async def test_recover_orphans_skips_a_session_that_is_already_closed(tmp_path: Path) -> None:
    """A leftover `.enc` for an already-closed session must not be reprocessed.

    Before this guard, `close()` never deleted its own `.enc` files, so
    every normal, successful session left one behind; the next start's
    `recover_orphans` treated it as a fresh crash, rewrote the
    already-closed row with `end_reason="crashed"`, re-uploaded audio a
    user may have had erased via `/audio delete`, and then hit
    `uq_job_per_speaker`'s `IntegrityError` re-enqueuing a job that
    already existed -- taking the whole next bot start down with it. The
    row's `status` is the belt-and-braces guard against that even after
    `close()` is fixed to clean up after itself (a crash could still land
    between the upload and that cleanup): a `closed` (or `documented`)
    row means every speaker in it was already uploaded and enqueued, so
    recovery must only remove the stale local copy, never touch the
    database or the store again.
    """
    d = tmp_path / "session-7"
    d.mkdir()
    enc_path = d / "100.enc"
    enc_path.write_bytes(b"STRN-ENCRYPTED-DATA")

    sessions = FakeSessions(
        keys={7: ("original-key-id", b"original-wrapped-key")}, status={7: "closed"}
    )
    jobs = FakeJobs()
    store = FakeStore()
    encryptor = FakeEncryptor()

    recovered = await recover_orphans(
        tmp_path, sessions, jobs, store, encryptor, retention_days=30, now=T0
    )

    assert len(recovered) == 1  # still reported as found on disk
    assert jobs.enqueued == []  # never re-enqueued -- would violate uq_job_per_speaker
    assert store.put_keys == []  # never re-uploaded -- could resurrect erased audio
    assert sessions.closed == []  # the already-closed row is never touched again
    assert not enc_path.exists()  # the stale local copy is still cleaned up


async def test_recover_orphans_skips_a_session_that_is_already_documented(tmp_path: Path) -> None:
    """`documented` is further along than `closed` and must be skipped the same way."""
    d = tmp_path / "session-11"
    d.mkdir()
    enc_path = d / "100.enc"
    enc_path.write_bytes(b"STRN-ENCRYPTED-DATA")

    sessions = FakeSessions(status={11: "documented"})
    jobs = FakeJobs()
    store = FakeStore()
    encryptor = FakeEncryptor()

    recovered = await recover_orphans(
        tmp_path, sessions, jobs, store, encryptor, retention_days=30, now=T0
    )

    assert len(recovered) == 1
    assert jobs.enqueued == []
    assert store.put_keys == []
    assert not enc_path.exists()
