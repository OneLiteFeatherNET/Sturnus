"""Recording session orchestration (Spec 11, Spec 12.1).

This is the heart of the bot: it owns the lifecycle of one recording
session -- from the first consenting participant to the encrypted,
uploaded, queued-for-transcription result -- without knowing that Discord,
a database, or an object store exist. Every call is explicit and every
decision it returns is explicit, so the whole lifecycle can be exercised
with fakes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from sturnus.application.ports import (
    AudioStore,
    AudioWriter,
    AudioWriterFactory,
    Encryptor,
    SessionKey,
)
from sturnus.domain.session import EndReason, SessionMachine, SessionState, SessionTimeouts
from sturnus.domain.timeline import SpeakerClock


def audio_key(session_id: int, discord_user_id: int) -> str:
    """Object key for one speaker's recording within a session.

    Lives here rather than beside the object store because it is a pure
    naming rule with no I/O: both the orchestrator, which records it on the
    transcription job, and the store adapter, which uploads under it, must
    agree on the same string. Two copies would drift, and the failure would
    be a job pointing at an object nobody ever wrote.
    """
    return f"sessions/{session_id}/speakers/{discord_user_id}.enc"


class SessionRecorder(Protocol):
    """What `RecordingService` needs to persist about a session's rows."""

    async def open_session(self, guild_id: int, channel_id: int, now: datetime) -> int: ...

    async def add_participant(
        self, session_id: int, discord_user_id: int, display_name: str, now: datetime
    ) -> None: ...

    async def set_audio_epoch(
        self, session_id: int, discord_user_id: int, at: datetime
    ) -> None: ...

    async def close_session(self, session_id: int, ended_at: datetime, reason: str) -> None: ...

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None:
        """Persists the session's data key onto its row, once, when it opens.

        The session row is the source of truth for which key encrypted a
        session's recordings -- crash recovery reads it back from here
        rather than ever generating a fresh key that could not decrypt
        files the original key already produced.
        """
        ...

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:
        """Returns the `(encryption_key_id, wrapped_data_key)` recorded when the
        session opened, or `None` if nothing was ever stored.

        `None` covers two cases alike: a session that predates this column,
        and one that crashed before `record_session_key` ever ran. Either
        way, the caller has no key to recover with.
        """
        ...


class JobQueue(Protocol):
    """Where a finished speaker recording is handed off for transcription."""

    async def enqueue(
        self,
        *,
        session_id: int,
        discord_user_id: int,
        s3_key: str,
        encryption_key_id: str,
        wrapped_data_key: bytes,
        retention_until: datetime,
    ) -> int: ...


class RecordingService:
    """Drives one recording session end to end.

    Holds a `SessionMachine` for the lifecycle, a `SpeakerClock` to place
    packets on an absolute timeline, and one `AudioWriter` per speaker who
    has actually spoken. Every collaborator that reaches outward -- the
    session and job repositories, the object store, the audio writers, the
    encryptor -- is reached through a port or a narrow protocol, so this
    class never touches Discord, SQL, S3, the filesystem's audio format, or
    a crypto library directly.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        timeouts: SessionTimeouts,
        sessions: SessionRecorder,
        jobs: JobQueue,
        store: AudioStore,
        writers: AudioWriterFactory,
        encryptor: Encryptor,
        retention_days: int,
    ) -> None:
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._machine = SessionMachine(timeouts)
        self._clock = SpeakerClock()
        self._sessions = sessions
        self._jobs = jobs
        self._store = store
        self._writer_factory = writers
        self._encryptor = encryptor
        self._retention_days = retention_days

        self._session_id: int | None = None
        self._data_key: SessionKey | None = None
        self._writers: dict[int, AudioWriter] = {}
        self._closed = False

    @property
    def is_recording(self) -> bool:
        return self._session_id is not None and not self._closed

    @property
    def session_id(self) -> int | None:
        return self._session_id

    async def participants_changed(self, consented_count: int, now: datetime) -> None:
        """Forwards to the machine; opens a session row on the IDLE -> RECORDING edge."""
        was_idle = self._machine.state is SessionState.IDLE
        self._machine.participants_changed(consented_count, now)
        if was_idle and self._machine.state is SessionState.RECORDING:
            self._session_id = await self._sessions.open_session(
                self._guild_id, self._channel_id, now
            )
            self._data_key = self._encryptor.new_session_key()
            # The session row is the source of truth for which key encrypted
            # this session's recordings. Persist it now, at the moment it is
            # generated, instead of waiting for the first job to enqueue --
            # a crash before any speaker has finished must not leave the
            # key stranded only in this process's memory.
            await self._sessions.record_session_key(
                self._session_id, self._encryptor.key_id, self._data_key.wrapped
            )

    async def voice_packet(
        self,
        discord_user_id: int,
        display_name: str,
        ssrc: int,
        rtp_timestamp: int,
        pcm: bytes,
        now: datetime,
    ) -> None:
        """Places a packet on the timeline and appends it to the speaker's file."""
        if not self.is_recording:
            return
        assert self._session_id is not None

        at = self._clock.absolute_time(ssrc, rtp_timestamp, now)

        writer = self._writers.get(discord_user_id)
        if writer is None:
            writer = self._writer_factory.open(self._session_id, discord_user_id, at)
            self._writers[discord_user_id] = writer
            await self._sessions.add_participant(
                self._session_id, discord_user_id, display_name, now
            )
            await self._sessions.set_audio_epoch(self._session_id, discord_user_id, at)

        writer.write(at, pcm)
        self._machine.audio_received(now)

    async def tick(self, now: datetime) -> EndReason | None:
        """Forwards to the machine and closes the session once it reports a reason."""
        reason = self._machine.tick(now)
        if reason is not None:
            await self.close(reason, now)
        return reason

    async def close(self, reason: EndReason, now: datetime) -> None:
        """Finalizes the session: encrypt, upload, enqueue, close -- idempotently.

        For each speaker who actually has a file: close the writer, encrypt
        the WAV beside it, remove the plaintext, upload, then enqueue a job.
        The job is enqueued only after the upload succeeds, so a job never
        points at an object that was never written. The session row closes
        once, after every speaker has been handled.
        """
        if self._closed:
            return
        self._closed = True
        assert self._session_id is not None
        assert self._data_key is not None
        session_id = self._session_id
        retention_until = now + timedelta(days=self._retention_days)

        for discord_user_id, writer in self._writers.items():
            writer.close()
            wav_path = writer.path
            enc_path = wav_path.with_suffix(".enc")
            self._encryptor.encrypt(wav_path, enc_path, self._data_key.plaintext)
            wav_path.unlink()

            key = audio_key(session_id, discord_user_id)
            await self._store.put(key, enc_path)
            await self._jobs.enqueue(
                session_id=session_id,
                discord_user_id=discord_user_id,
                s3_key=key,
                encryption_key_id=self._encryptor.key_id,
                wrapped_data_key=self._data_key.wrapped,
                retention_until=retention_until,
            )

        await self._sessions.close_session(session_id, now, reason.value)
