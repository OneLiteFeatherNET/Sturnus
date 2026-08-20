"""Capture end to end: real Opus frames in, a real WAV out.

Everything from the sink down is the production code path -- the real
`RecordingSink`, the real `ResilientOpusDecoder` over real libopus, the
real `RecordingService`, `SpeakerClock` and `SpeakerWriter`. Only the
extension's threads and the repositories are replaced, because the
question these tests answer is not "does a mock get called" but "what is
actually in the file afterwards".

That matters because the production failure was invisible: the database
showed a closed session with zero participants, and nobody found out until
long after. The assertions here are on sample counts and sample values in
the finished WAV, which is the artefact a participant was told existed.
"""

from __future__ import annotations

import math
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

import discord
import pytest
from discord.ext import voice_recv
from discord.opus import Encoder, OpusNotLoaded

from sturnus.application.ports import SessionKey
from sturnus.application.recording import RecordingService
from sturnus.domain.session import SessionTimeouts
from sturnus.infrastructure.discord.decoding import ResilientOpusDecoder, new_opus_decoder
from sturnus.infrastructure.discord.sink import CapturedFrame, CaptureMessage, RecordingSink
from sturnus.infrastructure.recording_adapters import FileAudioWriterFactory

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ROLE_ID = 42
ANNA_ID, BEN_ID = 1001, 2002
ANNA_SSRC, BEN_SSRC = 111, 222

#: One 20 ms Opus frame at 48 kHz: the RTP clock advances by this much per
#: frame, and 20 ms of 16 kHz mono is 320 samples in the finished file.
SAMPLES_PER_FRAME = 960
SAMPLES_PER_FRAME_16K = 320


@pytest.fixture(autouse=True)
def libopus_loaded() -> None:
    try:
        new_opus_decoder()
    except OpusNotLoaded:  # pragma: no cover - depends on the host
        pytest.skip("libopus is not available on this host")


class FakeClock:
    def now(self) -> datetime:
        return T0


class FakeSessions:
    """Just enough of `SessionRecorder` to let a session open and close."""

    def __init__(self) -> None:
        self.participants: list[int] = []

    async def open_session(
        self,
        guild_id: int,  # noqa: ARG002
        channel_id: int,  # noqa: ARG002
        now: datetime,  # noqa: ARG002
    ) -> int:
        return 1

    async def add_participant(
        self,
        session_id: int,  # noqa: ARG002
        discord_user_id: int,
        display_name: str,  # noqa: ARG002
        now: datetime,  # noqa: ARG002
    ) -> None:
        self.participants.append(discord_user_id)

    async def set_audio_epoch(
        self, session_id: int, discord_user_id: int, at: datetime
    ) -> None: ...

    async def close_session(self, session_id: int, ended_at: datetime, reason: str) -> None: ...

    async def record_session_key(
        self, session_id: int, encryption_key_id: str, wrapped_data_key: bytes
    ) -> None: ...

    async def session_key(self, session_id: int) -> tuple[str, bytes] | None:  # noqa: ARG002
        return None

    async def session_status(self, session_id: int) -> str | None:  # noqa: ARG002
        return "open"


class FakeJobs:
    async def enqueue(self, **kwargs: object) -> int:  # noqa: ARG002
        return 1


class FakeStore:
    async def put(self, key: str, source: Path) -> None: ...

    async def delete(self, key: str) -> None: ...  # noqa: ARG002 - port shape


class FakeEncryptor:
    key_id = "test-key"

    def new_session_key(self) -> SessionKey:
        return SessionKey(plaintext=b"0" * 32, wrapped=b"wrapped")

    def encrypt(self, source: Path, target: Path, key: bytes) -> None:  # noqa: ARG002
        target.write_bytes(source.read_bytes())


def tone(hz: float, samples: int = SAMPLES_PER_FRAME) -> bytes:
    """48 kHz 16-bit stereo, the shape Discord's encoder expects."""
    return b"".join(
        struct.pack("<hh", value, value)
        for value in (
            int(12000 * math.sin(2 * math.pi * hz * index / 48_000)) for index in range(samples)
        )
    )


def member(user_id: int) -> discord.Member:
    from unittest.mock import MagicMock

    stand_in = MagicMock(spec=discord.Member)
    stand_in.id = user_id
    stand_in.display_name = f"user-{user_id}"
    role = MagicMock(spec=discord.Role)
    role.id = ROLE_ID
    stand_in.roles = [role]
    return stand_in


def member_without_role(user_id: int) -> discord.Member:
    from unittest.mock import MagicMock

    stand_in = MagicMock(spec=discord.Member)
    stand_in.id = user_id
    stand_in.display_name = f"user-{user_id}"
    stand_in.roles = []
    return stand_in


def voice_data(payload: bytes, ssrc: int, rtp_timestamp: int) -> voice_recv.VoiceData:
    from unittest.mock import MagicMock

    packet = MagicMock()
    packet.ssrc = ssrc
    packet.timestamp = rtp_timestamp
    packet.decrypted_data = payload
    return voice_recv.VoiceData(packet, None)


def build_service(tmp_path: Path) -> RecordingService:
    return RecordingService(
        guild_id=1,
        channel_id=2,
        timeouts=SessionTimeouts(),
        sessions=FakeSessions(),
        jobs=FakeJobs(),
        store=FakeStore(),
        writers=FileAudioWriterFactory(tmp_path),
        encryptor=FakeEncryptor(),
        retention_days=30,
    )


def build_sink(emitted: list[CaptureMessage]) -> RecordingSink:
    return RecordingSink(
        consent_role_id=ROLE_ID,
        decoder=ResilientOpusDecoder(),
        clock=FakeClock(),
        emit=emitted.append,
        counters=None,
    )


async def pump(service: RecordingService, emitted: list[CaptureMessage]) -> None:
    """Stands in for the adapter's drain task: the loop side of the hand-off."""
    for message in emitted:
        assert isinstance(message, CapturedFrame)
        await service.voice_packet(
            message.discord_user_id,
            message.display_name,
            message.ssrc,
            message.rtp_timestamp,
            message.pcm,
            message.captured_at,
        )
    emitted.clear()


def read_wav(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16_000
        raw = handle.readframes(handle.getnframes())
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


async def test_a_corrupt_frame_leaves_a_gap_instead_of_shifting_later_audio(
    tmp_path: Path,
) -> None:
    """The defect, asserted on the file rather than on a caught exception.

    Four consecutive 20 ms frames, the second of them unreadable. The
    frame is skipped, capture continues, and -- because `SpeakerWriter`
    places audio by RTP-derived absolute time rather than by byte count --
    the third and fourth frames land at 40 ms and 60 ms, exactly where
    they were spoken. A naive "just append what decoded" would produce a
    60 ms file with everything after the gap 20 ms early, and every
    transcription offset wrong from there on.
    """
    encoder = Encoder()
    frames = [encoder.encode(tone(440 + step * 30), SAMPLES_PER_FRAME) for step in range(4)]
    frames[1] = b"garbage-not-opus"

    service = build_service(tmp_path)
    await service.participants_changed(1, T0)
    emitted: list[CaptureMessage] = []
    sink = build_sink(emitted)

    for index, payload in enumerate(frames):
        sink.write(member(ANNA_ID), voice_data(payload, ANNA_SSRC, index * SAMPLES_PER_FRAME))

    # The frame that would not decode never reached the loop at all...
    assert len(emitted) == 3
    await pump(service, emitted)

    writer = service._writers[ANNA_ID]  # the real artefact
    path = writer.path
    writer.close()
    samples = read_wav(path)

    # ...and the file is four frames long, not three.
    assert len(samples) == 4 * SAMPLES_PER_FRAME_16K
    first, gap, third, fourth = (
        samples[0:320],
        samples[320:640],
        samples[640:960],
        samples[960:1280],
    )
    assert any(first), "the first frame carries real audio"
    assert not any(gap), "the unreadable frame became 20 ms of true silence"
    assert any(third), "the frame after the gap is present..."
    assert any(fourth)
    # ...and it is at 40 ms, which is where it was spoken.
    assert samples[640:1280] != samples[320:960]


async def test_one_speakers_bad_frames_never_touch_another_speakers_file(
    tmp_path: Path,
) -> None:
    """Opus is stateful; sharing a decoder across speakers corrupts both streams."""
    anna_encoder, ben_encoder = Encoder(), Encoder()
    service = build_service(tmp_path)
    await service.participants_changed(2, T0)
    emitted: list[CaptureMessage] = []
    sink = build_sink(emitted)

    for index in range(6):
        rtp = index * SAMPLES_PER_FRAME
        anna_payload = b"garbage" if index % 2 else anna_encoder.encode(tone(440), 960)
        sink.write(member(ANNA_ID), voice_data(anna_payload, ANNA_SSRC, rtp))
        sink.write(
            member(BEN_ID),
            voice_data(ben_encoder.encode(tone(880), 960), BEN_SSRC, rtp),
        )

    await pump(service, emitted)

    for writer in service._writers.values():
        writer.close()
    anna = read_wav(service._writers[ANNA_ID].path)
    ben = read_wav(service._writers[BEN_ID].path)

    def slots(samples: list[int]) -> list[bool]:
        """Which 20 ms slots of a file carry real audio."""
        return [
            any(samples[start : start + SAMPLES_PER_FRAME_16K])
            for start in range(0, len(samples), SAMPLES_PER_FRAME_16K)
        ]

    # Anna spoke six frames and three of them were unreadable. Each one
    # became silence exactly where it belonged, so her surviving audio is
    # still at 0 ms, 40 ms and 80 ms. Her file stops after the last frame
    # that decoded: a *trailing* gap has no later packet to be padded
    # against, and inventing one would be inventing a duration.
    assert slots(anna) == [True, False, True, False, True]

    # Ben's stream is untouched. Every one of his frames is present, at
    # full length -- which is the isolation the old design did not have,
    # where one bad frame from Anna ended capture for everyone.
    assert slots(ben) == [True] * 6
    assert len(ben) == 6 * SAMPLES_PER_FRAME_16K


async def test_a_member_without_the_consent_role_never_reaches_a_writer(
    tmp_path: Path,
) -> None:
    """Spec 3.1's first layer, asserted where it counts: nothing on disk."""
    encoder = Encoder()
    service = build_service(tmp_path)
    await service.participants_changed(1, T0)
    emitted: list[CaptureMessage] = []
    sink = build_sink(emitted)

    for index in range(4):
        sink.write(
            member_without_role(ANNA_ID),
            voice_data(encoder.encode(tone(440), 960), ANNA_SSRC, index * SAMPLES_PER_FRAME),
        )

    await pump(service, emitted)

    assert service._writers == {}
    assert list(tmp_path.rglob("*.wav")) == []
