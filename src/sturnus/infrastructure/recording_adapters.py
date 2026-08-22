"""Adapters wiring `RecordingService`'s ports to the concrete audio-writing
and encryption code.

Kept thin deliberately: `SpeakerWriter`, `to_mono_16k`, `KeyWrapper` and
`encrypt_file` already carry the tested logic (see `tests/infrastructure`);
these classes only adapt their shape to the `AudioWriter`,
`AudioWriterFactory` and `Encryptor` protocols the application layer depends
on (`sturnus.application.ports`), so `sturnus.application.recording` never
has to import this package.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sturnus.application.ports import SessionKey
from sturnus.infrastructure.audio import SpeakerWriter, to_mono_16k
from sturnus.infrastructure.crypto import KeyWrapper, encrypt_file


class FileAudioWriter:
    """Adapts `SpeakerWriter` to the `AudioWriter` port.

    Converts each packet to 16 kHz mono on arrival, so the application
    layer only ever hands over raw Discord PCM and never needs to know the
    target sample rate or channel layout -- that conversion is entirely an
    adapter concern.
    """

    def __init__(self, path: Path, epoch: datetime) -> None:
        self.path = path
        self._writer = SpeakerWriter(path, epoch)
        # The previous frame, kept so the resampling filter has signal on
        # both sides of what it is producing. One writer exists per
        # speaker, which is exactly the scope this history belongs to:
        # two speakers share no filter state, and neither should they.
        self._history = b""

    def write(self, at: datetime, pcm: bytes) -> None:
        self._writer.write(at, to_mono_16k(pcm, self._history))
        # Kept whatever the frame was: a silent frame is still the signal
        # the next frame's filter has to start from, and dropping it would
        # put a boundary artefact after every pause.
        self._history = pcm

    def close(self) -> None:
        self._writer.close()


class FileAudioWriterFactory:
    """Adapts a base directory to the `AudioWriterFactory` port.

    Each speaker's file lives at
    `<recording_dir>/session-<session_id>/<discord_user_id>.wav`.
    """

    def __init__(self, recording_dir: Path) -> None:
        self._recording_dir = recording_dir

    def open(self, session_id: int, discord_user_id: int, epoch: datetime) -> FileAudioWriter:
        path = self._recording_dir / f"session-{session_id}" / f"{discord_user_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileAudioWriter(path, epoch)


class CryptoEncryptor:
    """Adapts `KeyWrapper` and `encrypt_file` to the `Encryptor` port."""

    def __init__(self, master_key: bytes, master_key_id: str) -> None:
        self._key_wrapper = KeyWrapper(master_key, master_key_id)

    @property
    def key_id(self) -> str:
        return self._key_wrapper.key_id

    def new_session_key(self) -> SessionKey:
        data_key = self._key_wrapper.new_data_key()
        return SessionKey(plaintext=data_key.plaintext, wrapped=data_key.wrapped)

    def encrypt(self, source: Path, target: Path, key: bytes) -> None:
        encrypt_file(source, target, key)
