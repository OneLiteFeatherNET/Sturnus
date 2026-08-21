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
import logging
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
from sturnus.application.publishing import Announcer, render_silent_audio_warning
from sturnus.domain.session import EndReason, SessionMachine, SessionState, SessionTimeouts
from sturnus.domain.silence import SILENCE_EVIDENCE_SECONDS, SilentAudioWatch
from sturnus.domain.timeline import SpeakerClock
from sturnus.observability.events import Event, log_event, log_exception

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)


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

    async def record_silent_audio(
        self, session_id: int, discord_user_id: int, at: datetime
    ) -> None:
        """Records that this speaker's audio arrived carrying no audible level.

        Written once, at the moment the case is established, so that an
        operator reading the session afterwards can tell a broken capture
        path from a room in which nobody said anything -- the question two
        empty transcripts left unanswerable. The warning posted into the
        channel is gone by the next meeting; this is what survives it.
        """
        ...

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
    encryptor, the announcer -- is reached through a port or a narrow
    protocol, so this class never touches Discord, SQL, S3, the
    filesystem's audio format, or a crypto library directly.

    It speaks into the channel exactly once, and only about a fault it can
    see and the room cannot: audio arriving from a speaker with no audible
    level in it (`_report_silent_audio`). Everything else it has to say
    about a session it says by writing it down.
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
        announcer: Announcer,
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
        #: The only way this service says anything to the people it is
        #: recording. Reached through the same `Announcer` port the link
        #: publisher already posts through, rather than a second route out
        #: to Discord, so this layer still has no idea Discord exists.
        self._announcer = announcer
        self._retention_days = retention_days

        #: Watches each speaker's amplitude for audio that arrives and
        #: decodes but carries nothing (`sturnus.domain.silence`). Rebuilt
        #: per session in `reset()`, like `_clock`, so "once per speaker"
        #: means once per session.
        self._silence = SilentAudioWatch()
        self._session_id: int | None = None
        self._data_key: SessionKey | None = None
        self._writers: dict[int, AudioWriter] = {}
        self._closed = False
        #: Packet and byte counters for `session.closed`'s verdict. They
        #: live here, in a class with thorough unit tests, rather than in
        #: `sturnus.infrastructure.discord.voice` -- whose sink callback
        #: runs on the extension's packet-router thread and has no unit
        #: tests at all, by explicit design. Putting the count in the
        #: untested file would make the replacement signal less trustworthy
        #: than the flood it replaces.
        self._packets = 0
        self._bytes = 0
        self._seen_participants = False

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
    def guild_id(self) -> int:
        """The guild this service records for.

        Read-only, and public so telemetry in `infrastructure` can label a
        session span without reaching into a private attribute.
        """
        return self._guild_id

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
        if consented_count > 0:
            self._seen_participants = True
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
            # The anchor line of the whole story: every later event, in this
            # process and in the worker, joins to it on `session_id`.
            # `key_id` says which master key must still exist for this
            # session ever to be decrypted -- the one fact that makes a
            # rotation mistake recoverable rather than merely visible.
            log_event(
                log,
                logging.INFO,
                Event.SESSION_OPENED,
                "Opened a recording session",
                session_id=self._session_id,
                guild_id=self._guild_id,
                channel_id=self._channel_id,
                consented_present=consented_count,
                key_id=self._encryptor.key_id,
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
            # One line per speaker, never per packet. This is what separates
            # "nobody consented" from "consented but silent" from "capture
            # is broken" -- three very different incidents that look
            # identical from outside without it.
            #
            # `display_name` is deliberately absent: it is directly
            # identifying and tells an operator nothing the user id does
            # not. It is in `fields.DENIED_NAMES` so the build fails if
            # anyone adds it here later.
            log_event(
                log,
                logging.INFO,
                Event.SESSION_SPEAKER_FIRST_PACKET,
                "First audio packet from a speaker",
                session_id=self._session_id,
                discord_user_id=discord_user_id,
                ssrc=ssrc,
            )

        writer.write(at, pcm)
        self._packets += 1
        self._bytes += len(pcm)
        self._machine.audio_received(now)

        # Last, and only after the audio itself is safely written: this is
        # a report about the recording, never a step in making it. The
        # watch reads amplitude and nothing else -- no sample is kept,
        # logged or passed on -- and answers `True` exactly once per
        # speaker per session, on the packet that completes the case.
        if self._silence.observe(discord_user_id, pcm):
            await self._report_silent_audio(discord_user_id, at)

    async def _report_silent_audio(self, discord_user_id: int, at: datetime) -> None:
        """Says, three ways, that this speaker's audio is arriving empty.

        Three, because each survives something the others do not. The log
        line reaches the operator watching the pod and is the one thing
        that cannot itself fail. The channel message reaches the meeting
        while it can still act -- at the end it would be worthless, since
        the recording is already lost. The participant row outlives both
        and is what turns "the transcript is empty" into an answerable
        question weeks later.

        Each of the two that can fail is guarded on its own rather than
        together: a Discord rate limit must not swallow the durable
        record, and a database hiccup must not swallow the message that
        could still get somebody's microphone fixed. Neither may reach the
        caller at all -- `voice_packet` is the capture path, and a warning
        that took the recording down with it would be worse than no
        warning.
        """
        assert self._session_id is not None
        # `display_name` is deliberately not here, and this is the one line
        # in the three where leaving it out costs something: it is what the
        # room would recognise. It is directly identifying, it is in
        # `fields.DENIED_NAMES`, and the id answers the operator's question
        # -- "whose microphone" is a question for the channel message, which
        # renders the mention and is read by people who are in the meeting.
        log_event(
            log,
            logging.WARNING,
            Event.SPEAKER_AUDIO_SILENT,
            "Audio from this speaker has been arriving with no audible level: packets are "
            "being received and decoded, and every sample in them is at the noise floor. "
            "This is what a microphone muted at system level produces, and it transcribes "
            "to nothing. Recording continues.",
            session_id=self._session_id,
            discord_user_id=discord_user_id,
            duration_seconds=SILENCE_EVIDENCE_SECONDS,
        )
        try:
            await self._announcer.post(
                self._channel_id, render_silent_audio_warning(discord_user_id)
            )
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.SPEAKER_SILENT_WARNING_FAILED,
                "Could not post the silent-audio warning into the channel; the durable "
                "record below is what is left of it",
                exc,
                session_id=self._session_id,
                discord_user_id=discord_user_id,
                channel_id=self._channel_id,
            )
        try:
            await self._sessions.record_silent_audio(self._session_id, discord_user_id, at)
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.SPEAKER_SILENT_RECORD_FAILED,
                "Could not record this speaker's silent audio on the session row; the "
                "finding survives only as this line",
                exc,
                session_id=self._session_id,
                discord_user_id=discord_user_id,
            )

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
        log_event(
            log,
            logging.INFO,
            Event.SESSION_CLOSING,
            "Closing the session: encrypting, uploading and enqueuing",
            session_id=session_id,
            reason=reason.value,
            speakers=len(self._writers),
        )

        jobs_enqueued = 0
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
            jobs_enqueued += 1
            enc_paths.append(enc_path)
            # The object key is not logged: it is
            # `sessions/{session_id}/speakers/{discord_user_id}.enc`, so
            # both halves are on this line already and the key itself would
            # be duplication with a wider blast radius. `audio_key()`
            # reconstructs it.
            log_event(
                log,
                logging.INFO,
                Event.SESSION_SPEAKER_FINALIZED,
                "Encrypted, uploaded and enqueued one speaker's recording",
                session_id=session_id,
                discord_user_id=discord_user_id,
                bytes=enc_path.stat().st_size if enc_path.exists() else 0,
            )

        await self._sessions.close_session(session_id, now, reason.value)

        # The verdict, and the reason this line exists at all: a session
        # that had consenting participants and enqueued nothing recorded
        # nothing. Today that outcome is expressed as complete silence --
        # no document, no announcement, and not one log line saying so.
        # Here it is a single ERROR an alert can fire on.
        recorded_nothing = jobs_enqueued == 0 and self._seen_participants
        log_event(
            log,
            logging.ERROR if recorded_nothing else logging.INFO,
            Event.SESSION_CLOSED,
            "Session closed having recorded nothing despite participants being present"
            if recorded_nothing
            else "Session closed",
            session_id=session_id,
            reason=reason.value,
            speakers=len(self._writers),
            jobs_enqueued=jobs_enqueued,
            packets=self._packets,
            bytes=self._bytes,
        )

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

        Also zeroes the packet/byte counters and the "we saw participants"
        flag, so the next session's `session.closed` verdict is about that
        session rather than a running total across the process's lifetime.

        Without this, `_closed` stays `True` and `_machine` stays stuck in
        `SessionState.CLOSING` forever: `is_recording` never becomes
        `True` again, `voice_packet` keeps returning early, and
        `participants_changed` can never open a new session row. One
        session per process lifetime is the bug this method exists to fix.
        """
        assert self._closed, "reset() must only follow close()"
        self._machine.reset()
        self._clock = SpeakerClock()
        self._silence = SilentAudioWatch()
        self._session_id = None
        self._data_key = None
        self._writers = {}
        self._closed = False
        self._packets = 0
        self._bytes = 0
        self._seen_participants = False
