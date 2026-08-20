"""Recording session orchestration (Spec 11, Spec 12.1).

This is the heart of the bot: it owns the lifecycle of one recording
session -- from the first consenting participant to the encrypted,
uploaded, queued-for-transcription result -- without knowing that Discord,
a database, or an object store exist. Every call is explicit and every
decision it returns is explicit, so the whole lifecycle can be exercised
with fakes.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from pathlib import Path
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

    async def open_session(
        self, guild_id: int, channel_id: int, channel_name: str | None, now: datetime
    ) -> int: ...

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

    async def session_status(self, session_id: int) -> str | None:
        """Returns the session row's `status` ("open"/"closed"/"documented"), or
        `None` if the row does not exist.

        Used by `recover_orphans` to tell a session that has genuinely
        crashed (status still `open`) from one that already finished --
        `close()` ran to completion and only its local cleanup was
        interrupted by the crash. Reprocessing the latter would enqueue
        the same speaker's job a second time and violate
        `uq_job_per_speaker`.
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
        channel_name: str | None = None,
    ) -> None:
        self._guild_id = guild_id
        self._channel_id = channel_id
        #: Recorded onto the session row so the protocol can name the room.
        #: Optional because a caller that cannot resolve it should still be
        #: able to record -- a protocol without the channel name beats none.
        self._channel_name = channel_name
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
    def needs_reset(self) -> bool:
        """True while only `reset()` can make this service recordable again.

        Exactly `reset()`'s own precondition, asked rather than asserted, so
        a caller recovering from a failed `close()` can tell "the session was
        moved to CLOSING and never came back" from "nothing was closing at
        all". The distinction matters: `close()` reaches the object store and
        the job queue and can fail there, and it flips `_closed` before it
        does, so a failure leaves precisely this state -- a machine parked in
        CLOSING that no longer records anything until someone resets it.
        """
        return self._closed and self._machine.state is SessionState.CLOSING

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def channel_id(self) -> int:
        """The voice channel this service opens its session rows against."""
        return self._channel_id

    def apply_tunables(self, timeouts: SessionTimeouts, retention_days: int) -> None:
        """Adopts new timeouts and retention immediately, session in progress or not.

        Both values are read at exactly one point each and nowhere else:
        `_retention_days` only inside `close()`, when it stamps
        `retention_until` on the jobs it enqueues, and the timeouts only
        inside the machine's `_due_reason`, on the next `tick()` (see
        `SessionMachine.retime`). Neither is captured anywhere at
        construction, so swapping them touches nothing in flight -- no
        writer, no file, no open session row.

        That a mid-session retention change applies to the session in
        progress is the intended semantics, not an accident: the value in
        force when a recording is *filed* is the one that governs it.
        """
        self._retention_days = retention_days
        self._machine.retime(timeouts)

    def retarget(self, channel_id: int, channel_name: str | None) -> None:
        """Points future sessions at a different voice channel. Idle only.

        `_channel_id` is read once per session, by `open_session` on the
        IDLE -> RECORDING edge, so changing it between sessions is
        invisible to everything else. Changing it *during* one would leave
        the already-written `sessions` row naming one channel while the
        audio kept arriving from another -- a protocol whose header lies
        about where it came from. The caller must therefore wait for the
        session to end (see `SturnusClient._apply_pending`), which is what
        this assertion enforces rather than silently allowing.

        `channel_name` is required rather than defaulted: a caller that
        moves the channel and forgets the name would leave the next
        protocol headed with the room the recording did not come from,
        which is worse than the `None` fallback to a bare link.
        """
        assert not self.is_recording, "retarget() must not run mid-session"
        self._channel_id = channel_id
        self._channel_name = channel_name

    def due_reason(self, now: datetime) -> EndReason | None:
        """What `tick()` would decide right now, without deciding it.

        Lets a caller that has just shortened a timeout report honestly
        that the session in progress already exceeds it, without being
        the thing that closes it.
        """
        return self._machine.due_reason(now)

    async def participants_changed(self, consented_count: int, now: datetime) -> None:
        """Forwards to the machine; opens a session row on the IDLE -> RECORDING edge."""
        was_idle = self._machine.state is SessionState.IDLE
        self._machine.participants_changed(consented_count, now)
        if was_idle and self._machine.state is SessionState.RECORDING:
            self._session_id = await self._sessions.open_session(
                self._guild_id, self._channel_id, self._channel_name, now
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

    def request_close(self, reason: EndReason) -> None:
        """Arms an out-of-band close that the next `tick()` acts on.

        The capture side calls this when every speaker's audio has stopped
        decoding: the session would otherwise run to its idle timeout
        writing empty files while everyone in the channel believes they
        are being recorded. Routed through the machine so the close still
        comes back out of `tick()` and the caller's existing close/leave/
        reset sequence handles it unchanged.
        """
        self._machine.request_close(reason)

    def speaker_stream_ended(self, ssrc: int) -> None:
        """Retires one SSRC's RTP reference point after that stream ends.

        SSRCs are per-connection, not per-user: a participant who
        reconnects comes back under a new one, and Discord may reissue an
        abandoned one. Keeping a stale reference would place the next
        stream's packets against the wrong origin. Called from the voice
        adapter on the library's `voice_member_disconnect` event, which is
        also where the decoder for that SSRC is evicted.
        """
        self._clock.reset(ssrc)

    async def tick(self, now: datetime) -> EndReason | None:
        """Forwards to the machine and closes the session once it reports a reason."""
        reason = self._machine.tick(now)
        if reason is not None:
            await self.close(reason, now)
        return reason

    async def end_now(self, reason: EndReason, now: datetime) -> None:
        """Ends the session in progress on an outside decision, losing nothing.

        The counterpart of `tick()` for the two reasons the clock never
        reports: an orderly shutdown, and an administrator ending the
        recording so a deferred channel or role change can apply
        immediately (`/config apply force:true`). It takes exactly the same
        route out -- encrypt, upload, enqueue, close the row -- so an early
        end is still a complete recording.

        `close()` on its own is *not* that route: it finalizes the files
        but leaves the `SessionMachine` in RECORDING, because only `tick()`
        and `end_now()` move it to CLOSING. A caller that closed and then
        `reset()` -- which it must, if this instance is ever to record
        again -- would trip `SessionMachine.reset`'s guard, leave the
        service closed-but-never-reset, and with it a guild that captures
        nothing until some later timeout happens to fire. Moving the
        machine first, here, in one place, is what stops every caller
        having to know that.

        A no-op when nothing is recording, so it is safe on any path that
        merely might have a session open.
        """
        if not self.is_recording:
            return
        self._machine.end_now(reason)
        await self.close(reason, now)

    async def close(self, reason: EndReason, now: datetime) -> None:
        """Finalizes the session: encrypt, upload, enqueue, close -- idempotently.

        For each speaker who actually has a file: close the writer, encrypt
        the WAV beside it, remove the plaintext, upload, then enqueue a job.
        The job is enqueued only after the upload succeeds, so a job never
        points at an object that was never written. The session row closes
        once, after every speaker has been handled.

        Local cleanup happens last, after the session row is closed: each
        speaker's `.enc` is removed once its upload and job are both
        confirmed, and the now-empty session directory follows once every
        speaker is done (Spec 12.4). Leaving the encrypted file behind
        would keep filling the PVC forever, break `/audio delete`'s
        erasure guarantee (the S3 copy is gone but this one survives), and
        make `recover_orphans` rediscover it on every future restart --
        which is also why cleanup runs only after `close_session` commits:
        if a crash lands between upload and this unlink, the row is
        already `closed` and recovery's own check for that skips it
        instead of enqueuing the same speaker's job a second time.
        """
        if self._closed:
            return
        self._closed = True
        assert self._session_id is not None
        assert self._data_key is not None
        session_id = self._session_id
        retention_until = now + timedelta(days=self._retention_days)

        enc_paths: list[Path] = []
        session_dir: Path | None = None
        for discord_user_id, writer in self._writers.items():
            writer.close()
            wav_path = writer.path
            enc_path = wav_path.with_suffix(".enc")
            session_dir = wav_path.parent
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
            enc_paths.append(enc_path)

        await self._sessions.close_session(session_id, now, reason.value)

        for enc_path in enc_paths:
            enc_path.unlink(missing_ok=True)
        if session_dir is not None:
            # Only succeeds once it is actually empty -- if anything
            # unexpected is still in there, leave it rather than losing
            # data or raising out of an otherwise-successful close().
            with contextlib.suppress(OSError):
                session_dir.rmdir()

    def reset(self) -> None:
        """Forgets this session so the next consenting participant starts a fresh one.

        Must only be called after `close()` has finished -- everything
        that needed to outlive this process was already written by then:
        the session row, the uploaded recordings, the enqueued jobs. This
        only clears this instance's own bookkeeping, so the same
        `RecordingService` (and whatever holds a reference to it, such as
        the voice adapter that dispatches packets into it) can be reused
        for a second, third, ... session without ever being reconstructed.

        Without this, `_closed` stays `True` and `_machine` stays stuck in
        `SessionState.CLOSING` forever: `is_recording` never becomes
        `True` again, `voice_packet` keeps returning early, and
        `participants_changed` can never open a new session row. One
        session per process lifetime is the bug this method exists to fix.
        """
        assert self._closed, "reset() must only follow close()"
        self._machine.reset()
        self._clock = SpeakerClock()
        self._session_id = None
        self._data_key = None
        self._writers = {}
        self._closed = False
