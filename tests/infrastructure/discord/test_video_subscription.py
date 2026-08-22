"""What this bot has to say before Discord will send it video.

Every assertion here is about a payload leaving the process, because that
is the entire content of the module: three messages nothing in the stack
sends, and the reason the first probe measured a live screen share and saw
nothing at all. None of it proves Discord answers -- only a live capture
can -- but a payload that is silently wrong would make that measurement
lie, and a measurement that lies is what this whole line of work exists to
avoid.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from discord.ext import voice_recv

from sturnus.infrastructure.discord import video_subscription
from sturnus.infrastructure.discord.video_subscription import (
    HIGHEST_QUALITY,
    MEDIA_SINK_WANTS,
    VIDEO,
    VideoCapableConnectionState,
    VideoCapableVoiceClient,
    VideoCapableVoiceWebSocket,
    announce_video_capability,
    request_all_video,
    request_video_streams,
)

OUR_SSRC = 4242


class FakeSocket:
    """The aiohttp websocket, reduced to the one method that is used."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FakeWebSocket:
    """A voice websocket that records payloads instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_as_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _client(ws: object | None = None, ssrc: int | None = OUR_SSRC) -> Any:
    """A voice client stub with the two attributes the module reaches for."""
    return SimpleNamespace(_connection=SimpleNamespace(ws=ws, ssrc=ssrc))


# -- the IDENTIFY flag, which cannot be added after the handshake --


@pytest.mark.asyncio
async def test_identify_declares_video_support() -> None:
    """The field the whole question turns on.

    `video` defaults to false, and a connection that says it cannot handle
    video is never told about any. Nothing else in this module can
    compensate for it, because `IDENTIFY` is sent once.
    """
    socket = FakeSocket()
    ws = VideoCapableVoiceWebSocket(socket, loop=None)  # type: ignore[arg-type]

    await ws.send_as_json({"op": ws.IDENTIFY, "d": {"server_id": "1", "token": "t"}})

    assert socket.sent == [{"op": 0, "d": {"server_id": "1", "token": "t", "video": True}}]


@pytest.mark.asyncio
async def test_identify_keeps_every_field_the_library_sends() -> None:
    """Why the payload is amended in flight rather than rebuilt.

    `max_dave_protocol_version` is the field that decides whether
    end-to-end decryption works at all. Overriding `identify()` would mean
    copying the library's field list into this repo, where losing that one
    line would make every recording noise again -- the exact defect this
    project already spent days on.
    """
    socket = FakeSocket()
    ws = VideoCapableVoiceWebSocket(socket, loop=None)  # type: ignore[arg-type]

    await ws.send_as_json({"op": ws.IDENTIFY, "d": {"max_dave_protocol_version": 1}})

    assert socket.sent[0]["d"]["max_dave_protocol_version"] == 1


@pytest.mark.asyncio
async def test_resume_is_left_alone() -> None:
    """A resumed session keeps what it declared; re-declaring is at best
    redundant, and this is an undocumented protocol to be conservative in."""
    socket = FakeSocket()
    ws = VideoCapableVoiceWebSocket(socket, loop=None)  # type: ignore[arg-type]

    await ws.send_as_json({"op": ws.RESUME, "d": {"token": "t"}})

    assert socket.sent == [{"op": 7, "d": {"token": "t"}}]


@pytest.mark.asyncio
async def test_a_payload_that_is_not_a_dict_passes_through_untouched() -> None:
    """This wraps every outbound frame, including heartbeats, and must not
    be the reason a connection dies."""
    socket = FakeSocket()
    ws = VideoCapableVoiceWebSocket(socket, loop=None)  # type: ignore[arg-type]

    await ws.send_as_json({"op": 3, "d": 12345})

    assert socket.sent == [{"op": 3, "d": 12345}]


# -- the connection state, whose only job is to build that websocket --


@pytest.mark.asyncio
async def test_the_connection_state_builds_a_video_capable_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, Any] = {}

    async def fake_from_connection_state(_state: Any, **kwargs: Any) -> str:
        built.update(kwargs)
        return "ws"

    monkeypatch.setattr(
        VideoCapableVoiceWebSocket,
        "from_connection_state",
        fake_from_connection_state,
    )
    state = SimpleNamespace(ws=SimpleNamespace(seq_ack=7), hook="hook", state=None)

    result = await VideoCapableConnectionState._connect_websocket(state, resume=True)  # type: ignore[arg-type]

    assert result == "ws"  # type: ignore[comparison-overlap]
    assert built == {"resume": True, "hook": "hook", "seq_ack": 7}


def test_the_hook_that_dispatches_video_events_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subclass exists to change one field, and must change nothing else.

    `voice_recv` installs its own gateway hook, and that hook is the only
    thing that turns op 12 into `on_voice_member_video`. Dropping it while
    reaching for video would remove the event this work is trying to
    receive -- a failure that would look exactly like Discord staying
    silent.
    """
    hook = object()
    captured: dict[str, Any] = {}

    class FakeState:
        def __init__(self, _voice_client: Any, *, hook: Any) -> None:
            captured["hook"] = hook

    monkeypatch.setattr(
        voice_recv.VoiceRecvClient,
        "create_connection_state",
        lambda _self: SimpleNamespace(hook=hook),
    )
    monkeypatch.setattr(video_subscription, "VideoCapableConnectionState", FakeState)

    client = object.__new__(VideoCapableVoiceClient)
    client.create_connection_state()

    assert captured["hook"] is hook


# -- op 12, the precondition --


@pytest.mark.asyncio
async def test_op_12_declares_this_connection_s_ssrcs() -> None:
    """ "You must send at least one Video payload before sending or
    receiving video data, or you will be disconnected with an Invalid SSRC
    error." `discord.py` builds this payload and calls it from nowhere."""
    ws = FakeWebSocket()

    assert await announce_video_capability(_client(ws)) is True

    payload = ws.sent[0]
    assert payload["op"] == VIDEO
    assert payload["d"]["audio_ssrc"] == OUR_SSRC
    assert payload["d"]["video_ssrc"] == OUR_SSRC + 1
    assert payload["d"]["rtx_ssrc"] == OUR_SSRC + 2


@pytest.mark.asyncio
async def test_op_12_never_claims_to_be_sending_video() -> None:
    """Sturnus receives; it must not appear in anybody's client as a
    participant with a camera on. The declaration exists for SSRC
    ownership, not to offer a stream."""
    ws = FakeWebSocket()

    await announce_video_capability(_client(ws))

    assert all(not stream["active"] for stream in ws.sent[0]["d"]["streams"])


@pytest.mark.asyncio
async def test_op_12_is_not_sent_before_the_server_assigns_an_ssrc() -> None:
    """Sending an SSRC the server did not assign is what "Invalid SSRC"
    is for, and the failure it causes is a dropped voice connection."""
    ws = FakeWebSocket()

    assert await announce_video_capability(_client(ws, ssrc=None)) is False
    assert ws.sent == []


# -- op 15, the subscription --


@pytest.mark.asyncio
async def test_op_15_asks_for_every_stream_at_the_highest_layer() -> None:
    """Sent before any SSRC is known, so a share already running when the
    bot joins is covered. The highest layer on purpose: the question is
    whether anything arrives, and a low layer that happened to be dropped
    would answer it wrongly."""
    ws = FakeWebSocket()

    assert await request_all_video(_client(ws)) is True

    assert ws.sent == [{"op": MEDIA_SINK_WANTS, "d": {"any": HIGHEST_QUALITY}}]


@pytest.mark.asyncio
async def test_op_15_names_the_ssrcs_the_server_announced() -> None:
    """The keys are SSRCs as strings, because that is what the wire format
    is -- a JSON object cannot be keyed by an integer."""
    ws = FakeWebSocket()

    assert await request_video_streams(_client(ws), [5001, 5002]) is True

    assert ws.sent == [{"op": MEDIA_SINK_WANTS, "d": {"5001": 100, "5002": 100}}]


@pytest.mark.asyncio
async def test_no_ssrcs_means_no_payload() -> None:
    """An empty Media Sink Wants would read as "I want nothing"."""
    ws = FakeWebSocket()

    assert await request_video_streams(_client(ws), []) is False
    assert ws.sent == []


# -- the failure paths, which run inside a live recording --


@pytest.mark.asyncio
async def test_a_missing_websocket_is_reported_not_raised() -> None:
    """Reached from a gateway event handler mid-capture. An exception here
    would end a recording that was working, to answer a question nobody
    asked for."""
    assert await request_all_video(_client(None)) is False


@pytest.mark.asyncio
async def test_a_websocket_that_refuses_the_payload_is_reported_not_raised() -> None:
    """And the return value is what lets the probe say "we asked and it
    failed" rather than blaming Discord for the silence."""

    class Refusing:
        async def send_as_json(self, _payload: dict[str, Any]) -> None:
            raise RuntimeError("closed")

    assert await request_all_video(_client(Refusing())) is False
