"""Thin adapter over `discord-ext-voice-recv` (Spec 6, Spec 3.1).

Everything decidable already lives elsewhere: the session state machine,
the speaker clock and consent policy in `RecordingService`, the decode
failure policy in `.decoding`, the frame pipeline in `.sink`. This
module's job is the one boundary nobody else can own -- between the
extension's threads and the event loop -- plus turning the two ways
capture can die into a session end reason someone can read afterwards.

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
check may need a database read, so it stays on the event loop, in
`_record`, before anything reaches `RecordingService`.

**Capture failure is an end reason, not a timeout.** If the library's
`after=` hook fires, or the initial `join()` fails, no frame and no
speaking event will ever arrive again; the session machine's own timers
know nothing about that, so the session would sit open until it closed as
an ordinary `idle_timeout`. That row -- a normal-looking session with
nothing in it -- is exactly what the production incident left behind, and
the whole reason nobody noticed for hours. `EndReason.CAPTURE_FAILURE` and
`EndReason.DECODE_FAILURE` exist so whoever reads that row next can tell
"nobody spoke" from "we could not hear".

Unlike the callback this replaces, the sink is now unit tested without a
gateway connection: `tests/infrastructure/discord/test_sink.py` and
`tests/infrastructure/discord/test_decoding.py`. What is still not
exercised here is the thread hop itself and `discord.py`'s own
`connect()`; see `docs/verification/voice-receive-spike.md` for what the
installed library actually hands back on each packet -- this adapter is
written against those findings, not against the extension's documentation.
That document also records the limitations this adapter knowingly carries.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import discord
from discord.ext import voice_recv

from sturnus.application.ports import Clock
from sturnus.application.recording import RecordingService
from sturnus.domain import settings
from sturnus.domain.session import EndReason
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.consent_cache import ConsentCache
from sturnus.infrastructure.discord.decoding import (
    DecoderFactory,
    ResilientOpusDecoder,
    new_opus_decoder,
)
from sturnus.infrastructure.discord.sink import (
    CapturedFrame,
    CaptureMessage,
    CaptureStopped,
    DecodeFailure,
    RecordingSink,
    SpeakerStreamEnded,
)
from sturnus.infrastructure.telemetry import VOICE_PACKET_ERRORS, VOICE_PACKETS, record
from sturnus.observability.events import Event, RateLimiter, log_event, log_exception

log = logging.getLogger(__name__)

#: One line per thousand occurrences, plus the first. The event below is
#: per-frame in origin -- ~50/s per speaker -- and an unrate-limited
#: `log.exception` on each is its own outage during exactly the systematic
#: failure that makes it worth reading. The aggregate lives in
#: `sturnus.voice.packet_errors`, which answers "is the adapter throwing"
#: without forty thousand identical tracebacks standing in the way.
_MESSAGE_ERROR_LOG_EVERY = 1000


def voice_close_code(error: BaseException | None) -> int | None:
    """The websocket close code of a `discord.ConnectionClosed`, else `None`.

    **Why a field and not an entry in `redaction.SAFE_MESSAGE_TYPES`.**
    `ConnectionClosed` is not on that tuple, so `safe_exception_message`
    reduces it to `<message withheld: discord.errors.ConnectionClosed>` --
    in exactly the situation the detail is wanted, because this is the
    exception the bot gets when it cannot join voice or is dropped from it.

    Its message was read rather than guessed. `discord/errors.py` builds it
    as `f'Shard ID {self.shard_id} WebSocket closed with {self.code}'` --
    two integers and no third-party text at all, `reason` being set to `''`
    unconditionally a line above with the comment "aiohttp doesn't seem to
    consistently provide close reason". So the message *is* safe, and
    admitting it would be defensible on its own terms. It is still not
    admitted, for a structural reason that outweighs it:
    `sturnus.observability` is standard-library-only by construction --
    `tests/observability/test_package_boundaries.py` enforces it, and it is
    what lets `sturnus.application` import the field registry at all -- so
    `SAFE_MESSAGE_TYPES` cannot name a type from `discord` without
    dragging the gateway library into the package that decides what leaves
    the pod. One list would become two.

    So the diagnosis is lifted out as a registered field instead, which is
    strictly better than the sentence it came from: `close_code=4014`
    ("disconnected by Discord"), `4006` ("session no longer valid"), `4009`
    ("session timeout"), `4015` ("voice server crashed") and `4021`
    ("rate limited") are each a different answer to "why can this bot not
    hear the channel", and as a field they are queryable in Loki and
    groupable rather than being characters in a message.

    Returns `None` for every other exception type, which is the honest
    answer: `OSError`, `asyncio.TimeoutError` and `OpusNotLoaded` all reach
    the same call sites and none of them has a close code.
    """
    return error.code if isinstance(error, discord.ConnectionClosed) else None


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
        decoder_factory: DecoderFactory = new_opus_decoder,
    ) -> None:
        self._client = client
        self._service = service
        self._config_store = config_store
        self._clock = clock
        self._consent_cache = ConsentCache(consent_repo, config_store, clock)
        self._decoder_factory = decoder_factory

        self._voice_client: voice_recv.VoiceRecvClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[CaptureMessage] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._guild_id: int | None = None
        self._stopping = False
        self._message_errors = RateLimiter(_MESSAGE_ERROR_LOG_EVERY)

    async def join(self, channel_id: int) -> None:
        """Connects to the voice channel and starts listening on a sink.

        Raises rather than returning quietly on any failure: the caller
        must be able to tell that capture never started, because a session
        left open with nothing arriving is the failure this branch exists
        to remove. `SturnusClient.on_voice_state_update` turns that into
        `EndReason.CAPTURE_FAILURE`.
        """
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

        # Nothing that has to be torn down is built before the connection
        # exists. Creating the hand-off and its drain task first left an
        # orphaned task behind whenever `connect` failed -- one that would
        # outlive the failed join and keep running against a channel
        # nobody was ever going to speak into.
        self._voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)

        self._guild_id = channel.guild.id
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        # Per session, so `count` on the line below reads as "errors in this
        # recording" rather than "errors since the process started".
        self._message_errors = RateLimiter(_MESSAGE_ERROR_LOG_EVERY)
        self._drain_task = asyncio.create_task(self._drain(self._queue))
        try:
            self._start_listening(int(stored_role_id))
        except Exception:
            # Connected but not listening is worse than not connected:
            # the bot would sit in the channel recording nothing.
            await self.leave()
            raise
        log_event(
            log,
            logging.INFO,
            Event.VOICE_JOINED,
            "Joined the voice channel and started listening",
            guild_id=channel.guild.id,
            channel_id=channel_id,
            session_id=self._service.session_id,
            listening=True,
        )

    async def leave(self) -> None:
        """Stops listening and disconnects."""
        guild_id = self._guild_id
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
        # The counterpart of `voice.joined`, and the reason
        # `_on_listen_stopped` does not log a clean stop: a stop we asked
        # for is reported by the side that asked. Everything else that
        # reaches that hook is a failure and says so.
        log_event(
            log,
            logging.INFO,
            Event.VOICE_LEFT,
            "Stopped listening and left the voice channel",
            guild_id=guild_id,
            session_id=self._service.session_id,
            listening=False,
        )

    # -- capture side: everything below runs on the extension's threads --

    def _start_listening(self, consent_role_id: int) -> None:
        """Builds a fresh decoder and sink and attaches them to the voice client.

        A fresh pair on purpose: `AudioReader._stop` calls `cleanup()` on
        the sink, which is one-shot, so re-attaching a used sink would
        leave its decoders unreleased.
        """
        assert self._voice_client is not None
        decoder = ResilientOpusDecoder(
            factory=self._decoder_factory,
            on_decode_failure=self._on_decode_failure,
        )
        sink = RecordingSink(
            consent_role_id=consent_role_id,
            decoder=decoder,
            clock=self._clock,
            emit=self._emit,
            guild_id=self._guild_id,
        )
        self._voice_client.listen(sink, after=self._on_listen_stopped)

    def _emit(self, message: CaptureMessage) -> None:
        """Hands one message to the event loop. Called from the extension's threads.

        Never raises: it is reached from `RecordingSink.write`, and an
        exception there would propagate into `PacketRouter.run`, which
        stops capture for every speaker.

        The queue is unbounded. See `docs/verification/voice-receive-spike.md`
        for the backlog limitation that carries with it.
        """
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, message)
        except RuntimeError:
            # The loop is closed. There is nothing left to deliver to, and
            # certainly nothing to raise about back into `write()`.
            #
            # Counted only for frames: `sturnus.voice.packets` is "voice
            # packets by what happened to them", and a `CaptureStopped`
            # that never landed is not a packet. It is also the one drop
            # path with no other trace at all -- the frame is gone before
            # anything on the loop side could have noticed it.
            if isinstance(message, CapturedFrame):
                record(VOICE_PACKETS, 1, outcome="loop_gone", guild_id=self._guild_id)
            log.debug("Dropped a %s: the event loop is gone", type(message).__name__)

    def _on_decode_failure(self) -> None:
        """Fired once when no live stream decodes anything. Router thread; only emits."""
        self._emit(DecodeFailure())

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
            except Exception as exc:
                # **ERROR, and rate limited -- two separate decisions.** An
                # exception escaping `_handle` is a defect in this adapter,
                # not a condition it expects to recover from: the frame it
                # was carrying is discarded, nothing retries it, and the
                # audio it held is gone from the recording for good. That is
                # `events`' definition of ERROR ("a human must act"), and it
                # is what main logged here with `log.exception`.
                #
                # The flood this line can become is answered by
                # `_MESSAGE_ERROR_LOG_EVERY`, not by the level. Lowering the
                # severity to buy quiet would keep the noise exactly where
                # it was and remove the only part of the line an alert can
                # key on -- and `sturnus.voice.packet_errors` below is the
                # rate an operator watches, while this line is the one that
                # says a human should look at all.
                record(VOICE_PACKET_ERRORS, 1, error_type=type(exc).__qualname__)
                if self._message_errors.should_log():
                    log_exception(
                        log,
                        logging.ERROR,
                        Event.VOICE_PACKET_HANDLER_FAILED,
                        "A capture message handler raised; capture continues",
                        exc,
                        guild_id=self._guild_id,
                        session_id=self._service.session_id,
                        count=self._message_errors.count,
                    )

    async def _handle(self, message: CaptureMessage) -> None:
        match message:
            case CapturedFrame():
                await self._record(message)
            case SpeakerStreamEnded():
                self._service.speaker_stream_ended(message.ssrc)
            case DecodeFailure():
                self._report_decode_failure()
            case CaptureStopped():
                self._report_capture_stopped(message)

    async def _record(self, frame: CapturedFrame) -> None:
        """Consults the cached consent record, then forwards the frame if allowed.

        `has_consent_role=True` is not an assumption: the sink rejected the
        frame otherwise, on this frame, synchronously.
        """
        assert self._guild_id is not None
        allowed = await self._consent_cache.may_record(self._guild_id, frame.discord_user_id, True)
        if not allowed:
            record(VOICE_PACKETS, 1, outcome="no_consent", guild_id=self._guild_id)
            return
        if not self._service.is_recording:
            # `voice_packet` already returns early in this case, so this is
            # a label rather than a decision: it separates "the session was
            # closing while frames were still in the queue" from "we never
            # got the frame", which look identical without it. The check
            # stays a read of the service's own state, not a second copy of
            # the rule.
            record(VOICE_PACKETS, 1, outcome="not_recording", guild_id=self._guild_id)
            return
        record(VOICE_PACKETS, 1, outcome="recorded", guild_id=self._guild_id)
        await self._service.voice_packet(
            frame.discord_user_id,
            frame.display_name,
            frame.ssrc,
            frame.rtp_timestamp,
            frame.pcm,
            frame.captured_at,
        )

    def _report_decode_failure(self) -> None:
        """The only *decode* failure that ends a session.

        Per-speaker degradation never does: it costs one person some audio
        and everyone else keeps recording. But if *nothing* decodes on any
        stream, the bot is writing empty files while telling everyone in
        the channel they are recorded -- the original incident in a new
        costume. `.decoding` has already logged why at ERROR; closing is
        not a reconnect and does not retry anything. It stops pretending,
        and the reason lands on the session row.
        """
        log_event(
            log,
            logging.ERROR,
            Event.VOICE_DECODE_FAILED,
            "No voice stream is decoding any longer; ending the session rather than "
            "recording silence.",
            guild_id=self._guild_id,
            session_id=self._service.session_id,
            end_reason=EndReason.DECODE_FAILURE.value,
        )
        self._service.request_close(EndReason.DECODE_FAILURE)

    def _report_capture_stopped(self, message: CaptureStopped) -> None:
        """Capture ended without us asking, so the session ends saying so.

        The policy lives here rather than in the `after=` hook, and so does
        the log line: `_on_listen_stopped` runs on the library's
        `audioreader-stopper` thread and does nothing but hand the signal
        over, while this runs on the event loop where the session can
        actually be ended.
        """
        # Two spellings of one event rather than one call with a variable
        # message: `log_event`'s message has to be a literal written here
        # (`tests/test_logging_discipline.py` rule R1), because it is the
        # one field `scrub_event` forwards to Sentry. The two cases differ
        # in substance anyway -- an `after=` that fired with no error at
        # all is a different thing to explain than one carrying an
        # `OpusError` -- and inventing an `error_type` for the first would
        # be a lie the field cannot carry.
        if message.error is None:
            log_event(
                log,
                logging.ERROR,
                Event.VOICE_READER_STOPPED,
                "Voice capture stopped unexpectedly with no error reported; ending the "
                "session rather than leaving it open with nothing arriving. This is the "
                "failure mode that silently ended a recording in production.",
                guild_id=self._guild_id,
                session_id=self._service.session_id,
                end_reason=EndReason.CAPTURE_FAILURE.value,
                listening=False,
            )
        else:
            log_exception(
                log,
                logging.ERROR,
                Event.VOICE_READER_STOPPED,
                "Voice capture stopped unexpectedly; ending the session rather than "
                "leaving it open with nothing arriving. This is the failure mode that "
                "silently ended a recording in production.",
                message.error,
                guild_id=self._guild_id,
                session_id=self._service.session_id,
                end_reason=EndReason.CAPTURE_FAILURE.value,
                listening=False,
                # `error_type` alone says `ConnectionClosed`, which is the
                # least informative true statement available about a bot
                # that was dropped from voice. See `voice_close_code`.
                close_code=voice_close_code(message.error),
            )
        self._service.request_close(EndReason.CAPTURE_FAILURE)
