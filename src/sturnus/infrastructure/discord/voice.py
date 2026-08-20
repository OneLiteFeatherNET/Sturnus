"""Thin adapter over `discord-ext-voice-recv` (Spec 6, Spec 3.1).

Everything decidable already lives elsewhere: the session state machine,
the speaker clock and consent policy in `RecordingService`, the decode
failure policy in `sturnus.domain.stream_health`, the frame pipeline in
`.sink`. This module's job is the two boundaries nobody else can own --
between the extension's threads and the event loop, and between a stream
that has stopped working and a human who needs to hear about it.

Sturnus decodes its own Opus. `RecordingSink.wants_opus()` returns `True`,
so the library builds no `discord.opus.Decoder` and never reaches
`_decode_packet`, the line whose uncaught `OpusError: corrupted stream`
ended a whole production recording by killing the packet-router thread.
See `.sink` and `.decoding` for why that is a structural fix rather than a
guard.

The consent gate is unchanged and still has two layers, neither redundant
(Spec 3.1). The role check is synchronous and in-memory, runs on the
extension's own thread on every single frame, and now happens *before* the
frame is even decoded: guild administrators bypass channel permissions and
can speak without holding the role, and a role removed mid-session takes
effect on the very next frame with no cache in the way. The consent-record
check cannot run there because it may need a database read, so it stays on
the event loop, in `_drain`, before anything reaches `RecordingService`.

Frames cross from the router thread to the loop as immutable messages
through one bounded queue drained by a single task, rather than as one
`run_coroutine_threadsafe` future per packet. That preserves per-speaker
ordering end to end, and replaces unbounded future accumulation under a
stalled loop with a counted drop.

Unlike the callback this replaces, the sink is now unit tested without a
gateway connection: `tests/infrastructure/discord/test_sink.py` and
`tests/infrastructure/discord/test_decoding.py`. What is still not
exercised here is the thread hop itself and `discord.py`'s own
`connect()`; see `docs/verification/voice-receive-spike.md` for what the
installed library actually hands back on each packet -- this adapter is
written against those findings, not against the extension's documentation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import voice_recv

from sturnus.application.ports import Clock
from sturnus.application.recording import RecordingService
from sturnus.domain import settings
from sturnus.domain.session import EndReason
from sturnus.domain.stream_health import DecodePolicy, StreamState, StreamStats
from sturnus.infrastructure import metrics
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.consent_cache import ConsentCache
from sturnus.infrastructure.discord.decoding import (
    DecoderFactory,
    ResilientOpusDecoder,
    log_state_change,
    new_opus_decoder,
)
from sturnus.infrastructure.discord.sink import (
    CapturedFrame,
    CaptureMessage,
    CaptureStopped,
    DecodeTotalFailure,
    RecordingSink,
    SpeakerStreamEnded,
    StreamStateChanged,
    UnattributedAudio,
)

log = logging.getLogger(__name__)

#: Frames the hand-off queue may hold before the router thread starts
#: dropping. About two seconds of ten speakers talking at once. Reaching it
#: means the event loop has stalled hard; dropping the newest frame keeps
#: what is already queued continuous, and `SpeakerWriter` pads the gap.
QUEUE_MAXSIZE = 1000

#: How long between two channel messages about the same subject, and how
#: many of them one session may ever produce. Told once is information;
#: told every five seconds is something people mute.
NOTICE_INTERVAL = timedelta(seconds=60)
NOTICE_LIMIT = 3

ATTRIBUTION_HINT = (
    "Recording has started. If you were already speaking when I joined, please pause "
    "for a moment and speak again -- until you do, Discord does not tell me which "
    "audio stream is yours, and audio I cannot attribute is not recorded."
)


class VoiceReceiveAdapter:
    """Satisfies the `VoiceReceiver` port over `discord-ext-voice-recv`."""

    def __init__(
        self,
        client: discord.Client,
        service: RecordingService,
        config_store: ConfigStore,
        clock: Clock,
        consent_repo: ConsentRepository,
        *,
        counters: metrics.Counters | None = None,
        decoder_factory: DecoderFactory = new_opus_decoder,
        decode_policy: DecodePolicy | None = None,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self._client = client
        self._service = service
        self._config_store = config_store
        self._clock = clock
        self._consent_cache = ConsentCache(consent_repo, config_store, clock)
        self._counters = counters or metrics.COUNTERS
        self._decoder_factory = decoder_factory
        self._decode_policy = decode_policy or DecodePolicy()
        self._queue_maxsize = queue_maxsize

        self._voice_client: voice_recv.VoiceRecvClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[CaptureMessage] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._channel: discord.VoiceChannel | None = None
        self._decoder: ResilientOpusDecoder | None = None
        self._guild_id: int | None = None
        self._consent_role_id: int | None = None
        self._stopping = False
        self._relisten_used = False
        self._notices: dict[str, tuple[int, datetime | None]] = {}

    async def join(self, channel_id: int) -> None:
        """Connects to the voice channel and starts listening on a sink."""
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError(f"channel {channel_id} is not a voice channel")

        stored_role_id = await self._config_store.get(channel.guild.id, settings.CONSENT_ROLE_ID)
        if stored_role_id is None:
            raise ValueError(f"guild {channel.guild.id} has no consent role configured")

        # The libopus probe, before `connect`. `Decoder.__init__` raises
        # `OpusNotLoaded` when the shared library is missing, and that must
        # stay a startup failure: caught per frame instead, every frame
        # would fail, the session would run to completion, and the result
        # would be hours of silent WAVs -- exactly the failure this work
        # exists to eliminate, made worse. The bot refuses to enter the
        # channel rather than sit in it recording nothing.
        self._decoder_factory()

        self._loop = asyncio.get_running_loop()
        self._guild_id = channel.guild.id
        self._consent_role_id = int(stored_role_id)
        self._channel = channel
        self._stopping = False
        self._relisten_used = False
        self._notices = {}
        self._queue = asyncio.Queue()
        self._drain_task = asyncio.create_task(self._drain(self._queue))

        self._voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        self._start_listening()
        await self._maybe_post_attribution_hint(channel)

    async def leave(self) -> None:
        """Stops listening and disconnects."""
        self._stopping = True
        voice_client, self._voice_client = self._voice_client, None
        if voice_client is not None:
            voice_client.stop_listening()
            await voice_client.disconnect()

        # No attempt to flush the queue first: the client closes the
        # session (encrypt, upload, enqueue) *before* it calls `leave`, so
        # anything still in flight could no longer be written to a file
        # anyway -- `RecordingService.voice_packet` returns early once the
        # session is closed. Draining here would look thorough and do
        # nothing.
        drain_task, self._drain_task = self._drain_task, None
        if drain_task is not None:
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task

        self._queue = None
        self._loop = None
        self._channel = None
        self._decoder = None

    # -- capture side: everything below runs on the extension's threads --

    def _start_listening(self) -> None:
        """Builds a fresh decoder and sink and attaches them to the voice client.

        A fresh pair each time on purpose: `AudioReader._stop` calls
        `cleanup()` on the sink, which is one-shot, so re-attaching a used
        sink would leave its decoders unreleased.
        """
        assert self._voice_client is not None
        assert self._consent_role_id is not None

        self._decoder = ResilientOpusDecoder(
            factory=self._decoder_factory,
            policy=self._decode_policy,
            on_state_change=self._on_state_change,
            on_total_failure=self._on_total_failure,
        )
        sink = RecordingSink(
            consent_role_id=self._consent_role_id,
            decoder=self._decoder,
            clock=self._clock,
            emit=self._emit,
            counters=self._counters,
        )
        self._voice_client.listen(sink, after=self._on_listen_stopped)

    def _emit(self, message: CaptureMessage) -> None:
        """Hands one message to the event loop. Called from the extension's threads.

        Never raises: it is reached from `RecordingSink.write`, and an
        exception there would propagate into `PacketRouter.run`, which
        stops capture for every speaker.

        The queue's bound is enforced *here*, on the producer side, rather
        than by `asyncio.Queue(maxsize=...)`: `call_soon_threadsafe` would
        otherwise keep every dropped frame alive in the loop's callback
        queue until the loop got round to rejecting it, which is the
        accumulation the bound exists to prevent. `qsize()` read across
        threads is a plain `len()` and is racy by however many callbacks
        are already in flight -- a deliberate trade for a backpressure
        heuristic that costs nothing on the hot path.
        """
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None:
            return
        if queue.qsize() >= self._queue_maxsize:
            self._counters.inc(metrics.QUEUE_DROPPED)
            return
        # A closed loop raises RuntimeError. Nothing left to deliver to,
        # and certainly nothing to raise about back into `write()`.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, message)

    def _on_state_change(self, ssrc: int, state: StreamState, stats: StreamStats) -> None:
        """`StreamStateListener`; runs on the packet-router thread, so it only emits.

        Logging and metrics deliberately happen on the loop side instead:
        one slow handler here stalls packet routing for every speaker,
        because the extension dispatches sink work under the router's own
        lock.
        """
        self._emit(StreamStateChanged(ssrc, state, stats))

    def _on_total_failure(self) -> None:
        """Fired once when no stream decodes anything. Router thread; only emits."""
        self._emit(DecodeTotalFailure())

    def _on_listen_stopped(self, error: BaseException | None) -> None:
        """The library's `after=` hook, from the `audioreader-stopper` thread.

        This is the direct answer to "capture died and nobody noticed":
        `AudioReader._stop` calls it with whatever `reader.error` holds,
        including the case that killed the production session. A stop we
        asked for is not news, so `leave()` marks itself first.
        """
        if self._stopping:
            return
        self._emit(CaptureStopped(error))

    # -- loop side: everything below runs on the event loop --

    async def _drain(self, queue: asyncio.Queue[CaptureMessage]) -> None:
        """The single consumer, so per-speaker frame order is preserved end to end."""
        while True:
            message = await queue.get()
            try:
                await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error handling a %s message", type(message).__name__)

    async def _handle(self, message: CaptureMessage) -> None:
        match message:
            case CapturedFrame():
                await self._record(message)
            case SpeakerStreamEnded():
                self._service.speaker_stream_ended(message.ssrc)
            case StreamStateChanged():
                await self._report_state_change(message)
            case UnattributedAudio():
                await self._report_unattributed(message)
            case DecodeTotalFailure():
                await self._report_total_failure()
            case CaptureStopped():
                await self._report_capture_stopped(message)

    async def _record(self, frame: CapturedFrame) -> None:
        """Consults the cached consent record, then forwards the frame if allowed.

        Runs on the event loop (unlike the role check in the sink) since
        the cache may need a database read. `has_role=True` is not an
        assumption: the sink rejected the frame otherwise, on this frame,
        synchronously.
        """
        assert self._guild_id is not None
        allowed = await self._consent_cache.may_record(
            self._guild_id, frame.discord_user_id, has_consent_role=True
        )
        if not allowed:
            return
        await self._service.voice_packet(
            frame.discord_user_id,
            frame.display_name,
            frame.ssrc,
            frame.rtp_timestamp,
            frame.pcm,
            frame.captured_at,
        )
        self._counters.inc(metrics.FRAMES_DECODED)

    async def _report_state_change(self, message: StreamStateChanged) -> None:
        log_state_change(message.ssrc, message.state, message.stats)
        self._counters.inc(metrics.STREAM_STATE_CHANGES, state=message.state.value)

    async def _report_unattributed(self, message: UnattributedAudio) -> None:
        log.warning(
            "Audio from ssrc=%s cannot be attributed to a member after %d frames; "
            "it is not being decoded or recorded. Discord supplies the mapping only "
            "with its speaking event.",
            message.ssrc,
            message.frames,
        )
        # Same debounce key as the join-time hint: the message is the
        # same instruction, and a participant should not receive it twice
        # because two code paths noticed the same problem.
        await self._notify("attribution-hint", ATTRIBUTION_HINT)

    async def _report_total_failure(self) -> None:
        """The only decode failure that ends a session.

        Per-speaker degradation never does: it costs one person some audio
        and everyone else keeps recording. But if *nothing* decodes on any
        stream, the bot is writing empty files while telling everyone in
        the channel they are recorded -- the original incident in a new
        costume. Closing is not a reconnect and does not retry anything;
        it stops pretending, and the reason is recorded on the session row.
        """
        log.error(
            "No voice stream is decoding any longer; closing the session rather than "
            "recording silence. See the per-stream errors above for the cause."
        )
        self._counters.inc(metrics.DECODE_TOTAL_FAILURES)
        self._service.request_close(EndReason.DECODE_FAILURE)
        await self._notify(
            "total-failure",
            "I can no longer decode any audio in this channel, so I am ending the "
            "recording instead of producing an empty one. Nothing further is being "
            "recorded from this point.",
            limit=1,
        )

    async def _report_capture_stopped(self, message: CaptureStopped) -> None:
        """Capture ended without us asking. One re-listen, then reporting only."""
        log.error(
            "Voice capture stopped unexpectedly (%s). This is the failure mode that "
            "silently ended a recording in production.",
            type(message.error).__name__ if message.error is not None else "no error reported",
        )
        self._counters.inc(metrics.CAPTURE_STOPPED)

        # The last-resort backstop, explicitly not the mechanism: the fix
        # for the known cause is that the library no longer decodes at all
        # (see `.sink`). One attempt, guarded by a flag so it can never
        # loop, and only while we are still connected.
        voice_client = self._voice_client
        may_resume = (
            not self._relisten_used
            and not self._stopping
            and voice_client is not None
            and voice_client.is_connected()
        )
        resumed = False
        if may_resume:
            self._relisten_used = True
            try:
                self._start_listening()
                resumed = True
            except Exception:
                log.exception("Could not resume voice capture after an unexpected stop")

        await self._notify(
            "capture-stopped",
            "Audio capture stopped unexpectedly and I have resumed it. If this message "
            "repeats, assume nothing further is being recorded."
            if resumed
            else "Audio capture stopped unexpectedly and could not be resumed. Nothing "
            "further is being recorded from this point.",
            limit=NOTICE_LIMIT,
        )

    async def _maybe_post_attribution_hint(self, channel: discord.VoiceChannel) -> None:
        """Provokes the speaking event for anyone who was already talking.

        Discord maps an SSRC to a user only when it sends op 5, which it
        does when someone starts speaking. A participant already mid-
        sentence when the bot connects therefore has no mapping, their
        frames arrive unattributed, and unattributed audio is never
        recorded. Asking them to pause and speak again *is* the mechanism
        that supplies the missing mapping -- there is no way to request op
        5 directly.
        """
        others = [member for member in channel.members if not member.bot]
        if not others:
            return
        await self._notify("attribution-hint", ATTRIBUTION_HINT, limit=1)

    async def _notify(self, key: str, text: str, *, limit: int = NOTICE_LIMIT) -> None:
        """Posts one debounced message into the recorded channel.

        Failing to post must never take down the drain -- the channel may
        deny the bot messages entirely -- so a failure is logged and
        swallowed here rather than at the caller.
        """
        channel = self._channel
        if channel is None:
            return
        sent, last = self._notices.get(key, (0, None))
        now = self._clock.now()
        if sent >= limit:
            return
        if last is not None and now - last < NOTICE_INTERVAL:
            return
        self._notices[key] = (sent + 1, now)
        try:
            await channel.send(text)
        except Exception:
            log.warning("Could not post the %r notice into channel %s", key, channel.id)
