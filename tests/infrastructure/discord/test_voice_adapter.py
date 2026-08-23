"""The boundary the adapter owns, without a gateway connection.

The thread hop and the failure path, driven directly: `_emit` is what the
extension's threads call and `_drain` is the task on the other side.
Neither needs a voice connection to be wrong, so neither needs one to be
tested.

The tests that matter here are about one disease rather than several
defects. Capture stopped in production and nothing noticed. A capture
failure that ends as an ordinary timeout, and a join that fails leaving a
session open with nothing behind it, are both new ways of arriving at that
same afternoon -- so these assert on outcomes, on what the session row
ends up saying, rather than on a code path having been entered. "A branch
was taken" is exactly the kind of evidence that was available last time
and told nobody anything.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import voice_recv
from discord.opus import OpusNotLoaded

from sturnus.application.ports import SessionKey
from sturnus.application.recording import RecordingService
from sturnus.domain.session import EndReason, SessionTimeouts
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.decoding import FrameDecoder
from sturnus.infrastructure.discord.sink import (
    CapturedFrame,
    CaptureStopped,
    DecodeFailure,
    SpeakerStreamEnded,
)
from sturnus.infrastructure.discord.video_subscription import VideoCapableVoiceClient
from sturnus.infrastructure.discord.voice import VoiceReceiveAdapter
from sturnus.observability.redaction import safe_exception_message

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD_ID, CHANNEL_ID, ROLE_ID = 1, 2, 3
ANNA_ID, ANNA_SSRC = 100, 111

VOICE_LOGGER = "sturnus.infrastructure.discord.voice"


def voice_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """The first record this adapter logged, ignoring anybody else's.

    `caplog.at_level(level, logger=...)` sets a level; it does not filter.
    Every record that passes any logger lands in `caplog.records`, so
    `records[0]` is "the first thing anything logged" -- which is this
    adapter only as long as nothing else in the process happens to log at
    the same moment. A task left running by an earlier test is enough to
    make that false occasionally, and it did: one parametrisation of
    `test_capture_dropped_from_voice_reports_the_close_code` failed in a
    full run and passed on its own.

    A flaky assertion is worse than a missing one, because it teaches
    people to re-run the suite instead of reading it.
    """
    for record in caplog.records:
        if record.name == VOICE_LOGGER:
            return record
    raise AssertionError(
        f"nothing was logged by {VOICE_LOGGER}; got {[record.name for record in caplog.records]}"
    )


class FakeClock:
    def __init__(self) -> None:
        self.value = T0

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class FakeSessions:
    """Just enough of `SessionRecorder` to see what a session row ends up saying."""

    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def open_session(
        self, _guild_id: int, _channel_id: int, _channel_name: str | None, _now: datetime
    ) -> int:
        return 1

    async def record_session_key(self, _sid: int, _key_id: str, _wrapped: bytes) -> None:
        return None

    async def session_key(self, _session_id: int) -> tuple[str, bytes] | None:
        return None

    async def add_participant(
        self, _sid: int, _user: int, _display_name: str, _now: datetime
    ) -> None:
        return None

    async def set_audio_epoch(self, _sid: int, _user: int, _at: datetime) -> None:
        return None

    async def record_silent_audio(self, _sid: int, _user: int, _at: datetime) -> None:
        return None

    async def close_session(self, session_id: int, _ended_at: datetime, reason: str) -> None:
        self.closed.append((session_id, reason))

    async def session_status(self, _session_id: int) -> str | None:
        return None


class FakeEncryptor:
    key_id = "k1"

    def new_session_key(self) -> SessionKey:
        return SessionKey(plaintext=b"0" * 32, wrapped=b"wrapped")

    def encrypt(self, _source: object, _target: object, _key: bytes) -> None:
        return None


def recording_service(sessions: FakeSessions) -> RecordingService:
    """A real `RecordingService` on fakes, so the end reason is the real one."""
    return RecordingService(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        timeouts=SessionTimeouts(
            empty_grace_seconds=60, idle_timeout_minutes=15, max_session_hours=4
        ),
        sessions=sessions,
        jobs=AsyncMock(),
        store=AsyncMock(),
        writers=MagicMock(),
        encryptor=FakeEncryptor(),
        announcer=AsyncMock(),
        retention_days=30,
    )


def _gateway_client() -> MagicMock:
    """A stand-in `discord.Client` that answers `shard_count`.

    `shard_count` is an *instance* attribute discord.py assigns in
    `Client.__init__`, not a class attribute, so `MagicMock(spec=...)` does
    not know about it and raises on access. The adapter reads it to decide
    whether a `shard_id` is worth putting on `voice.joined`/`voice.left`
    (see `VoiceReceiveAdapter._shard`), so it has to be present. `None`
    is what a client whose shards have not launched reports, and it is the
    value that keeps these tests asserting the single-shard behaviour.
    """
    return MagicMock(spec=discord.Client, shard_count=None)


def adapter(
    *,
    service: RecordingService | None = None,
    clock: FakeClock | None = None,
) -> VoiceReceiveAdapter:
    voice = VoiceReceiveAdapter(
        _gateway_client(),
        service or MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore),
        clock or FakeClock(),
        MagicMock(spec=ConsentRepository),
    )
    voice._guild_id = GUILD_ID
    return voice


def connected(voice: VoiceReceiveAdapter) -> None:
    """Puts the adapter in the state `join()` leaves it in, minus the gateway."""
    voice._loop = asyncio.get_running_loop()
    voice._queue = asyncio.Queue()
    voice._drain_task = asyncio.create_task(voice._drain(voice._queue))


async def settle() -> None:
    """Lets the drain run to a standstill."""
    for _ in range(20):
        await asyncio.sleep(0)


def frame() -> CapturedFrame:
    return CapturedFrame(
        discord_user_id=ANNA_ID,
        display_name="anna",
        ssrc=ANNA_SSRC,
        rtp_timestamp=960,
        pcm=b"pcm",
        captured_at=T0,
    )


def consenting() -> MagicMock:
    return MagicMock(may_record=AsyncMock(return_value=True))


# --- capture dying must not look like a quiet meeting ---


async def test_capture_death_ends_the_session_as_a_capture_failure() -> None:
    """The incident's exact signature, read from the session row.

    Nothing armed a close when capture died: the session stayed open with
    nothing arriving and eventually closed as `idle_timeout`, indistinguish-
    able in the database from a meeting where nobody happened to speak.
    Whoever reads that row next has to be able to tell "nobody spoke" from
    "we could not hear".
    """
    sessions = FakeSessions()
    service = recording_service(sessions)
    await service.participants_changed(1, T0)
    voice = adapter(service=service)

    await voice._handle(CaptureStopped(RuntimeError("router died")))
    reason = await service.tick(T0 + timedelta(seconds=1))

    assert reason is EndReason.CAPTURE_FAILURE
    assert sessions.closed == [(1, "capture_failure")]


async def test_capture_stopping_on_its_own_is_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure that was invisible in production, made loud."""
    voice = adapter()

    with caplog.at_level(logging.ERROR, logger=VOICE_LOGGER):
        await voice._handle(CaptureStopped(RuntimeError("router died")))

    assert len(caplog.records) == 1
    # The exception's type is now a structured field rather than part of the
    # message. `tests/test_logging_discipline.py` rule R6 forbids
    # interpolating an exception into a log message at all -- `str(exc)` is
    # unbounded third-party text and `logentry.message` is what
    # `infrastructure.observability.scrub_event` forwards to Sentry -- so
    # `log_exception` puts the class name in `error_type` instead. The
    # assertion is unchanged in substance: an operator must still be able to
    # see *what* stopped capture, and `error_type` is now where they see it.
    # `getattr`, because `sturnus_fields` is an `extra=` key rather than a
    # declared `LogRecord` attribute -- which is exactly what makes it a
    # field the registry governs rather than part of the message.
    fields = getattr(voice_record(caplog), "sturnus_fields", {})
    assert fields["error_type"] == "RuntimeError"
    assert voice_record(caplog).exc_info is not None, "the cause is carried, not summarised away"


async def test_a_stop_we_asked_for_is_not_reported_as_a_failure() -> None:
    """`leave()` calls `stop_listening()`, which fires the same `after=` hook."""
    voice = adapter()
    connected(voice)
    voice._stopping = True

    voice._on_listen_stopped(None)
    await asyncio.sleep(0)

    assert voice._queue is not None
    assert voice._queue.empty()


async def test_decode_failure_ends_the_session_with_its_own_reason() -> None:
    """The only decode failure that ends a session, and why.

    If nothing decodes on any stream the bot is writing empty files while
    telling everyone in the channel they are recorded -- the original
    incident in a new costume. Closing is not a reconnect and retries
    nothing; the reason lands on the session row.
    """
    sessions = FakeSessions()
    service = recording_service(sessions)
    await service.participants_changed(1, T0)
    voice = adapter(service=service)

    await voice._handle(DecodeFailure())
    reason = await service.tick(T0 + timedelta(seconds=1))

    assert reason is EndReason.DECODE_FAILURE
    assert sessions.closed == [(1, "decode_failure")]


# --- the consent gate's second layer, on the loop ---


async def test_a_frame_the_consent_record_rejects_never_reaches_the_service() -> None:
    """Spec 3.1's second layer, still on the loop and still per frame.

    The role check in the sink is not the whole gate: a hand-granted role,
    or one granted under a privacy policy that has since changed, leaves
    the role in place with no active consent record behind it.
    """
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = MagicMock(may_record=AsyncMock(return_value=False))

    await voice._handle(frame())

    service.voice_packet.assert_not_awaited()


async def test_an_allowed_frame_is_recorded_at_its_arrival_time() -> None:
    """`captured_at` comes from the router thread, not from after the hop.

    For a speaker's first frame that value is their audio epoch (Spec
    6.3), so keeping the queue latency out of it is a free accuracy win.
    """
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    clock = FakeClock()
    voice = adapter(service=service, clock=clock)
    voice._consent_cache = consenting()
    clock.advance(timedelta(seconds=5))  # the loop is behind

    await voice._handle(frame())

    service.voice_packet.assert_awaited_once_with(ANNA_ID, "anna", ANNA_SSRC, 960, b"pcm", T0)


async def test_a_departing_speaker_retires_their_rtp_reference() -> None:
    service = MagicMock(spec=RecordingService)
    voice = adapter(service=service)

    await voice._handle(SpeakerStreamEnded(ANNA_SSRC))

    service.speaker_stream_ended.assert_called_once_with(ANNA_SSRC)


# --- the hand-off from the extension's threads to the loop ---


async def test_a_frame_emitted_from_outside_the_loop_reaches_the_service() -> None:
    """`_emit` is what `RecordingSink.write` calls; `_drain` is the other side."""
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = consenting()
    connected(voice)

    await asyncio.to_thread(voice._emit, frame())
    await settle()

    service.voice_packet.assert_awaited_once()


async def test_the_drain_survives_a_handler_that_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One bad message must not stop every later frame from being recorded."""
    service = MagicMock(spec=RecordingService)
    service.voice_packet = AsyncMock()
    voice = adapter(service=service)
    voice._consent_cache = MagicMock(
        may_record=AsyncMock(side_effect=[RuntimeError("database gone"), True])
    )
    connected(voice)

    # Captured at WARNING so that a *downgrade* is visible here rather than
    # silently reducing the record count to zero: the level is asserted
    # below, against a literal, instead of being implied by the capture
    # threshold.
    with caplog.at_level(logging.WARNING, logger=VOICE_LOGGER):
        voice._emit(frame())
        voice._emit(frame())
        await settle()

    service.voice_packet.assert_awaited_once()
    assert len(caplog.records) == 1, "the swallowed failure is still reported"
    # ERROR, and pinned. An exception escaping the message handler is a
    # defect in this adapter, not a condition it expects to self-heal from:
    # the frame it was carrying is gone for good, and nothing retries it.
    # Rate limiting is what keeps a systematic failure from flooding Loki
    # (`_MESSAGE_ERROR_LOG_EVERY`); rate and severity are separate
    # decisions, and lowering the severity to buy quiet costs the one
    # signal that says a human should look.
    assert voice_record(caplog).levelno == logging.ERROR


async def test_emit_before_join_is_a_no_op_rather_than_a_crash() -> None:
    """`_emit` is reached from `write()`; raising there kills the router thread."""
    voice = adapter()

    voice._emit(frame())  # the boundary under test


# --- join and leave ---


async def test_join_refuses_to_enter_the_channel_when_libopus_is_missing() -> None:
    """`OpusNotLoaded` must stay a startup failure.

    Caught per frame instead, every frame would fail, the session would
    run to completion, and the result would be hours of silent WAVs --
    the exact failure this work exists to eliminate, made worse. It has to
    leave `join` as an exception, because that is what
    `SturnusClient._start_capture` turns into `CAPTURE_FAILURE`.
    """
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild = MagicMock(id=GUILD_ID)
    channel.connect = AsyncMock()
    client = _gateway_client()
    client.get_channel = MagicMock(return_value=channel)

    def broken_factory() -> FrameDecoder:
        raise OpusNotLoaded

    voice = VoiceReceiveAdapter(
        client,
        MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value=str(ROLE_ID))),
        FakeClock(),
        MagicMock(spec=ConsentRepository),
        decoder_factory=broken_factory,
    )

    with pytest.raises(OpusNotLoaded):
        await voice.join(CHANNEL_ID)

    channel.connect.assert_not_awaited(), "the bot never sat in a channel recording nothing"


async def test_a_failed_connect_leaves_no_task_running_behind_it() -> None:
    """Nothing that needs tearing down is built before the connection exists.

    The hand-off and its drain task used to be created first, so a
    `connect` that raised left the task running against a channel nobody
    was ever going to speak into -- for the rest of the process's life.
    """
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild = MagicMock(id=GUILD_ID)
    channel.connect = AsyncMock(side_effect=discord.ClientException("no voice"))
    client = _gateway_client()
    client.get_channel = MagicMock(return_value=channel)

    voice = VoiceReceiveAdapter(
        client,
        MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value=str(ROLE_ID))),
        FakeClock(),
        MagicMock(spec=ConsentRepository),
        decoder_factory=lambda: MagicMock(),
    )
    before = asyncio.all_tasks()

    with pytest.raises(discord.ClientException):
        await voice.join(CHANNEL_ID)
    await asyncio.sleep(0)

    assert voice._drain_task is None
    assert voice._queue is None
    assert asyncio.all_tasks() == before


def _joinable() -> tuple[MagicMock, MagicMock]:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.guild = MagicMock(id=GUILD_ID)
    # A plain `MagicMock` for the client itself: an `AsyncMock`'s children
    # are `AsyncMock`s too, so `listen()` would hand back an un-awaited
    # coroutine and the resulting warnings would drown the assertion.
    channel.connect = AsyncMock(return_value=MagicMock(disconnect=AsyncMock()))
    client = _gateway_client()
    client.get_channel = MagicMock(return_value=channel)
    return client, channel


def _adapter_for(client: MagicMock, *, capture_diagnostics: bool) -> VoiceReceiveAdapter:
    return VoiceReceiveAdapter(
        client,
        MagicMock(spec=RecordingService),
        MagicMock(spec=ConfigStore, get=AsyncMock(return_value=str(ROLE_ID))),
        FakeClock(),
        MagicMock(spec=ConsentRepository),
        decoder_factory=lambda: MagicMock(),
        capture_diagnostics=capture_diagnostics,
    )


async def test_a_normal_recording_does_not_change_the_voice_handshake() -> None:
    """Declaring video support is a change to the live handshake, and a
    handshake Discord rejects is a bot that cannot join a channel at all.

    That risk belongs to one deliberate measurement, not to every
    recording -- so without the diagnostics switch the connection is
    exactly the one that has been working.
    """
    client, channel = _joinable()

    await _adapter_for(client, capture_diagnostics=False).join(CHANNEL_ID)

    assert channel.connect.await_args.kwargs["cls"] is voice_recv.VoiceRecvClient


async def test_the_diagnostics_switch_declares_video_during_the_handshake() -> None:
    """`IDENTIFY` is sent once, so this is the one part of asking Discord
    for video that cannot be added after connecting."""
    client, channel = _joinable()

    await _adapter_for(client, capture_diagnostics=True).join(CHANNEL_ID)

    assert channel.connect.await_args.kwargs["cls"] is VideoCapableVoiceClient


async def test_asking_for_video_cannot_take_the_recording_down_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`join` must still succeed when the requests fail.

    Everything after `_start_listening` is diagnostic. A capture that
    records audio correctly must never be lost because a question could
    not be asked -- which is exactly what a raising send would do, on the
    join path, before a single frame arrives.
    """
    client, channel = _joinable()
    channel.connect.return_value._connection.ws.send_as_json = AsyncMock(
        side_effect=RuntimeError("closed")
    )
    voice = _adapter_for(client, capture_diagnostics=True)

    await voice.join(CHANNEL_ID)

    probe = voice._video_probe
    assert probe is not None
    with caplog.at_level(logging.WARNING):
        probe.report()
    await voice.leave()

    # And the failure is on the record rather than silently absent: a send
    # that never went out must not read as Discord staying silent.
    assert "op12-video=FAILED" in "\n".join(r.getMessage() for r in caplog.records)


async def test_leave_stops_the_drain() -> None:
    voice = adapter()
    connected(voice)
    drain_task = voice._drain_task

    await voice.leave()

    assert drain_task is not None and drain_task.cancelled()
    assert voice._queue is None


# ---------------------------------------------------------------------------
# `discord.ConnectionClosed`: the exception whose message is withheld
# ---------------------------------------------------------------------------


def _connection_closed(code: int) -> discord.ConnectionClosed:
    """The exception discord.py raises, built the way discord.py builds it.

    `ConnectionClosed.__init__(socket, *, shard_id, code)` reads
    `socket.close_code` only when `code` is falsy, so a stand-in socket is
    never touched here -- the code under test is `exc.code`, and that comes
    from the keyword argument.
    """
    return discord.ConnectionClosed(MagicMock(), shard_id=None, code=code)


def test_the_message_of_a_connection_closed_really_is_withheld() -> None:
    """The premise of the field, asserted rather than assumed.

    If `SAFE_MESSAGE_TYPES` ever grew to admit this type, `close_code`
    would be duplicating what the message already says and this test is
    what would notice. Until then the class name is *all* an operator gets
    from the exception, and "ConnectionClosed" is the least informative
    true statement available about a bot that cannot hear a channel.
    """
    withheld = safe_exception_message(_connection_closed(4014))
    assert "4014" not in withheld
    assert withheld == "<message withheld: discord.errors.ConnectionClosed>"
    # The control: the code really is on the exception, so a test asserting
    # it reaches the log line is asserting something that could be there.
    assert _connection_closed(4014).code == 4014


@pytest.mark.parametrize(
    ("code", "what_it_means"),
    [
        (4006, "the voice session is no longer valid"),
        (4009, "the voice session timed out"),
        (4014, "Discord disconnected us -- moved, or the channel was deleted"),
        (4015, "the voice server crashed"),
    ],
)
async def test_capture_dropped_from_voice_reports_the_close_code(
    code: int, what_it_means: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Four different faults that `error_type` cannot tell apart.

    Parametrised over the codes rather than asserted once, because the
    value has to *travel* -- a call site that hard-coded one, or that
    passed the exception's own type instead, would pass a single-case
    test.
    """
    voice = adapter()

    with caplog.at_level(logging.ERROR, logger=VOICE_LOGGER):
        await voice._handle(CaptureStopped(_connection_closed(code)))

    fields = getattr(voice_record(caplog), "sturnus_fields", {})
    assert fields["close_code"] == code, what_it_means
    assert fields["error_type"] == "ConnectionClosed"


async def test_a_stop_with_no_close_code_reports_none_rather_than_inventing_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`OpusNotLoaded`, `OSError` and `TimeoutError` reach the same line.

    `None` is the honest answer for them. A default of `-1` or `0` would
    be indistinguishable in Loki from a close code that really was
    reported.
    """
    voice = adapter()

    with caplog.at_level(logging.ERROR, logger=VOICE_LOGGER):
        await voice._handle(CaptureStopped(OpusNotLoaded()))

    assert getattr(voice_record(caplog), "sturnus_fields", {})["close_code"] is None


# --- whose video this connection is willing to ask Discord for ---
#
# Nothing here records video and this section does not make it
# recordable. What it pins is narrower and comes first: the bot must not
# *ask Discord for* a stream from somebody whose consent does not name
# video. Asking and then discarding is not the same act as not asking --
# a person's client can show them that a stream is being consumed, and
# nothing about the discard reaches them.


def announcement(ssrcs: list[int], *, member: object) -> object:
    """What `voice_recv` dispatches on op 12, reduced to what is read."""
    return SimpleNamespace(
        member=member,
        streams=[SimpleNamespace(ssrc=ssrc) for ssrc in ssrcs],
    )


def sharer(*, role_id: int | None = ROLE_ID, user_id: int = ANNA_ID) -> object:
    return SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=role_id)] if role_id is not None else [],
    )


def watching(voice: VoiceReceiveAdapter, *, video_allowed: bool) -> tuple[MagicMock, AsyncMock]:
    """Puts the adapter where `_ask_for_video` leaves it, minus the gateway.

    Returns the probe and the consent lookup, because both are what the
    assertions are about: what was recorded, and what was asked.
    """
    probe = MagicMock()
    voice._video_probe = probe
    voice._voice_client = MagicMock()
    voice._consent_role_id = ROLE_ID
    asked = AsyncMock(return_value=video_allowed)
    voice._consent_cache = MagicMock(may_record_video=asked)
    return probe, asked


async def test_video_is_not_asked_for_from_somebody_who_consented_to_audio_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[list[int]] = []

    async def never(_client: object, ssrcs: list[int]) -> bool:
        asked.append(ssrcs)
        return True

    monkeypatch.setattr("sturnus.infrastructure.discord.voice.request_video_streams", never)
    voice = adapter()
    probe, _ = watching(voice, video_allowed=False)

    await voice._on_video_announced(sharer(), announcement([5001], member=sharer()))

    assert asked == []
    probe.note_subscription.assert_called_once_with([5001], subscribed=False)


async def test_a_refusal_is_recorded_rather_than_silently_returned() -> None:
    """ "Announced and never asked for" and "asked for and never delivered"
    are the same zero in every packet count, and they mean opposite things
    about whether Discord sends a bot video at all. The probe is where
    that difference survives; the sink reports packets on a refused stream
    under its own outcome for the same reason."""
    voice = adapter()
    probe, _ = watching(voice, video_allowed=False)

    await voice._on_video_announced(sharer(), announcement([5001, 5002], member=sharer()))

    probe.note_subscription.assert_called_once_with([5001, 5002], subscribed=False)


async def test_video_is_asked_for_when_the_record_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscriptions: list[list[int]] = []

    async def note(_client: object, ssrcs: list[int]) -> bool:
        subscriptions.append(ssrcs)
        return True

    monkeypatch.setattr("sturnus.infrastructure.discord.voice.request_video_streams", note)
    voice = adapter()
    probe, _ = watching(voice, video_allowed=True)

    await voice._on_video_announced(sharer(), announcement([5001], member=sharer()))

    assert subscriptions == [[5001]]
    probe.note_subscription.assert_called_once_with([5001], subscribed=True)


async def test_the_consent_role_is_read_off_the_member_rather_than_assumed() -> None:
    """Spec 3.1's first layer applies here too, and there is no packet to
    carry it: this runs on the event loop before anything has arrived, so
    the role has to be read directly off the `Member` the gateway named."""
    voice = adapter()
    _, asked = watching(voice, video_allowed=True)

    await voice._on_video_announced(
        sharer(role_id=None), announcement([5001], member=sharer(role_id=None))
    )

    asked.assert_awaited_once_with(GUILD_ID, ANNA_ID, False)


async def test_an_announcement_naming_nobody_is_refused_rather_than_guessed_at() -> None:
    """The consent of a person who cannot be identified cannot be read, and
    the answer to "we do not know" is no."""
    voice = adapter()
    probe, asked = watching(voice, video_allowed=True)

    await voice._on_video_announced(object(), SimpleNamespace(streams=[SimpleNamespace(ssrc=1)]))

    probe.note_subscription.assert_called_once_with([1], subscribed=False)
    asked.assert_not_awaited()


async def test_a_consent_lookup_that_fails_does_not_end_the_recording() -> None:
    """A database that cannot be reached is not a yes -- and it is also not
    a reason to lose a capture that is recording audio correctly."""
    voice = adapter()
    probe, _ = watching(voice, video_allowed=True)
    voice._consent_cache = MagicMock(
        may_record_video=AsyncMock(side_effect=RuntimeError("database gone"))
    )

    await voice._on_video_announced(sharer(), announcement([5001], member=sharer()))

    probe.note_subscription.assert_called_once_with([5001], subscribed=False)


async def test_nothing_is_asked_for_at_connect_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_ask_for_video` used to send `{"any": 100}` -- everybody's camera,
    before a word had been said about anybody's consent. It now sends the
    refusal, and every stream is named individually afterwards."""
    sent: list[str] = []

    async def announce(_client: object) -> bool:
        sent.append("op12")
        return True

    async def refuse(_client: object) -> bool:
        sent.append("op15-any-off")
        return True

    monkeypatch.setattr("sturnus.infrastructure.discord.voice.announce_video_capability", announce)
    monkeypatch.setattr("sturnus.infrastructure.discord.voice.refuse_unnamed_video", refuse)
    voice = adapter()
    watching(voice, video_allowed=True)

    await voice._ask_for_video()

    assert sent == ["op12", "op15-any-off"]
