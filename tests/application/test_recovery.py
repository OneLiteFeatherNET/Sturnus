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
