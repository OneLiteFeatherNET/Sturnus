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

log = logging.getLogger(__name__)


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
        self._drain_task = asyncio.create_task(self._drain(self._queue))
        try:
            self._start_listening(int(stored_role_id))
        except Exception:
            # Connected but not listening is worse than not connected:
            # the bot would sit in the channel recording nothing.
            await self.leave()
            raise

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
            except Exception:
                log.exception("Error handling a %s message", type(message).__name__)

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
            return
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
        log.error(
            "Ending the session with %s: no voice stream is decoding any longer.",
            EndReason.DECODE_FAILURE.value,
        )
        self._service.request_close(EndReason.DECODE_FAILURE)

    def _report_capture_stopped(self, message: CaptureStopped) -> None:
        """Capture ended without us asking, so the session ends saying so."""
        log.error(
            "Voice capture stopped unexpectedly (%s); ending the session with %s rather "
            "than leaving it open with nothing arriving. This is the failure mode that "
            "silently ended a recording in production.",
            type(message.error).__name__ if message.error is not None else "no error reported",
            EndReason.CAPTURE_FAILURE.value,
            exc_info=message.error,
        )
        self._service.request_close(EndReason.CAPTURE_FAILURE)
