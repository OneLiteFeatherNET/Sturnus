"""Asking Discord for video, which nothing below this file ever does.

The first probe (`.video_probe`) measured a live screen share and found
that **not even the announcement arrived**: no `VIDEO` gateway op, no
packets, nothing. The listener was registered, the session closed
cleanly. Discord simply never mentioned the stream.

Three things are missing, and all three are things a real client sends.
None of them is in Discord's official documentation; the protocol below
is community reverse engineering (Userdoccers, `topics/voice-connections`),
which is why this module measures rather than assumes.

**1. `IDENTIFY` never claims video support.** The payload carries an
optional field -- ``video?  boolean  Whether this connection supports
video (default false)`` -- and `DiscordVoiceWebSocket.identify` sends
`server_id`, `user_id`, `session_id`, `token` and
`max_dave_protocol_version`, no `video`. Every bot built on `discord.py`
therefore identifies as video-incapable, and a voice server has no reason
to describe video to a connection that says it cannot handle it.

**2. Op 12 (`VIDEO`) is never sent.** It is bidirectional: the server uses
it to say "this user is sharing, on this SSRC", and the client uses it to
declare its own SSRCs. The documentation is unusually blunt about the
consequence of skipping it -- *"You must send at least one Video payload
before sending or receiving video data, or you will be disconnected with
an Invalid SSRC error."* `discord.py` defines exactly this payload as
`DiscordVoiceWebSocket.client_connect`, and **calls it from nowhere**;
`grep -rn client_connect` over the installed package returns the
definition and nothing else. `discord-ext-voice-recv` only listens for the
inbound direction.

**3. Op 15 (`MEDIA_SINK_WANTS`) is never sent.** It is how a receiver
tells the SFU which remote SSRCs it wants and at what layer: keys are
SSRCs, values are 0 (disable) to 100 (highest layer), and the key `any`
covers streams not named. `voice_recv/gateway.py` labels this opcode
`(useless)` -- true for a connection that only ever receives audio, which
is the only kind it supports.

**The screen-share caveat, which may matter more than any of the above.**
"Share Your Screen" in a guild voice channel is Go Live, and Go Live is
a *separate* RTC connection: a client watches one by sending main-gateway
op 20 `WATCH_STREAM` with a `stream_key` and then opening a second voice
websocket from the resulting `STREAM_SERVER_UPDATE`. Op 20 is an
undocumented user-client opcode with no bot-API equivalent, and no report
was found of a bot being allowed to use it. So this module may well prove
that camera video reaches a bot while a shared screen still does not --
which is itself the answer somebody needs before building anything.
Nothing here attempts op 20.

**And the direction that is documented as closed:** `Discord-video-stream`
states plainly that "Discord blocks video from bots", which is why it
requires a user token. That is about *sending*. Nothing in the protocol
documentation conditions op 12 or op 15 on account type, and `self_video`
is defined as "whether the client is streaming video to the channel" --
outbound only, and not a receive gate. Whether a receive gate exists
anyway is exactly what is unknown.

**Off unless `STURNUS_CAPTURE_DIAGNOSTICS` is on.** Declaring video
support changes the live voice handshake, and a handshake Discord rejects
is a bot that cannot join a channel at all. That risk is worth one
deliberate measurement; it is not worth carrying in production before the
measurement says anything.

**It still records nothing.** Requesting a stream is not decoding one.
Video packets arriving here land where they already landed -- in
`RecordingSink.write`, counted by `.video_probe` and dropped. Whether
they may ever be *kept* is a consent question with a role of its own, and
that decision is not made in this file.
"""

from __future__ import annotations

import logging
from typing import Any

from discord.ext import voice_recv
from discord.gateway import DiscordVoiceWebSocket
from discord.utils import MISSING
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

log = logging.getLogger(__name__)

#: Voice gateway opcodes. Op 12 is `client_connect` in `discord.py`'s
#: naming and `VIDEO` in `voice_recv`'s -- one opcode, two names, because
#: it is the same message in both directions. Op 15 appears in neither
#: library as anything but a constant, so both are spelled out here.
VIDEO = 12
MEDIA_SINK_WANTS = 15

#: Quality on the 0-100 scale Media Sink Wants uses: 0 disables a stream,
#: 100 requests the highest layer offered. A measurement wants the highest
#: -- the question is whether *anything* arrives, and a low layer that
#: happened to be dropped would answer it wrongly.
HIGHEST_QUALITY = 100

#: The key that applies to every stream not named explicitly. Sent once at
#: connect so a share already running when the bot joins is covered too:
#: a subscription keyed to an SSRC cannot be sent before that SSRC is
#: known, and the op 12 that would name it may already have gone past.
ANY_STREAM = "any"

#: Offsets for the SSRCs this connection declares as its own. A client
#: derives its video and RTX SSRCs from the audio SSRC the server assigned
#: it, and the server rejects overlapping or unannounced SSRCs with
#: "Invalid SSRC". Sturnus sends no video on either, and says so with
#: `active: false` below -- they exist to make the declaration well formed.
VIDEO_SSRC_OFFSET = 1
RTX_SSRC_OFFSET = 2


class VideoCapableVoiceWebSocket(DiscordVoiceWebSocket):
    """A voice websocket that identifies as able to receive video."""

    async def send_as_json(self, data: Any) -> None:
        """Adds `video: true` to `IDENTIFY` on its way out.

        Done here rather than by overriding `identify()` on purpose: that
        method builds the whole payload inline, so overriding it would
        mean copying `discord.py`'s current field list into this repo,
        where it would silently rot the next time the library adds one --
        `max_dave_protocol_version` is itself a recent addition, and
        losing it would break end-to-end decryption for every recording.
        Amending the payload in flight inherits every field the library
        sends, now and later.

        `RESUME` (op 7) is deliberately left alone. It re-establishes the
        session the server already holds, including whatever that session
        declared, so re-declaring would be redundant at best.
        """
        if isinstance(data, dict) and data.get("op") == self.IDENTIFY:
            payload = data.get("d")
            if isinstance(payload, dict):
                data = {**data, "d": {**payload, "video": True}}
        await super().send_as_json(data)


class VideoCapableConnectionState(VoiceConnectionState):
    """Connection state that builds the websocket above."""

    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        # Mirrors `VoiceConnectionState._connect_websocket`. The only
        # difference is the class the websocket is built from, and
        # `from_connection_state` is a classmethod, so that is the only
        # line that can differ.
        seq_ack = -1
        if self.ws is not MISSING:
            seq_ack = self.ws.seq_ack
        ws = await VideoCapableVoiceWebSocket.from_connection_state(
            self, resume=resume, hook=self.hook, seq_ack=seq_ack
        )
        self.state = ConnectionFlowState.websocket_connected
        return ws


class VideoCapableVoiceClient(voice_recv.VoiceRecvClient):
    """`VoiceRecvClient` that declares video support during the handshake.

    Substituted for the plain client only under diagnostics, and only in
    `voice.py`'s `join`. Everything else about the connection -- the sink,
    the reader, DAVE, the hook `voice_recv` installs -- is untouched: this
    subclass exists to change one field in one payload.
    """

    def create_connection_state(self) -> VoiceConnectionState:
        # The parent builds a `VoiceConnectionState` around its own
        # `hook`, and that hook is what turns op 12 into
        # `on_voice_member_video`. It has to survive, so the parent's
        # construction is reused for it rather than rewritten here.
        state = super().create_connection_state()
        return VideoCapableConnectionState(self, hook=state.hook)


async def announce_video_capability(client: voice_recv.VoiceRecvClient) -> bool:
    """Sends the op 12 without which video is refused outright.

    "You must send at least one Video payload before sending or receiving
    video data, or you will be disconnected with an Invalid SSRC error."
    So this is not an optimisation; it is the precondition, and the reason
    `discord.py` never receiving video is unsurprising in hindsight.

    The declared streams are marked `active: false` and given no SSRC:
    Sturnus sends no video and must not appear to. What the declaration is
    for is the SSRC ownership the server needs before it will route video
    on this connection at all.
    """
    ssrc = getattr(getattr(client, "_connection", None), "ssrc", None)
    if not isinstance(ssrc, int):
        log.debug("No audio SSRC yet; cannot announce video capability")
        return False
    return await _send(
        client,
        {
            "op": VIDEO,
            "d": {
                "audio_ssrc": ssrc,
                "video_ssrc": ssrc + VIDEO_SSRC_OFFSET,
                "rtx_ssrc": ssrc + RTX_SSRC_OFFSET,
                "streams": [
                    {
                        "type": "video",
                        "rid": "100",
                        "ssrc": 0,
                        "active": False,
                        "quality": HIGHEST_QUALITY,
                        "rtx_ssrc": 0,
                        "max_bitrate": 0,
                        "max_framerate": 0,
                        "max_resolution": {"type": "fixed", "width": 0, "height": 0},
                    }
                ],
            },
        },
        "video capability",
    )


async def request_all_video(client: voice_recv.VoiceRecvClient) -> bool:
    """Asks for every stream the server has, at the highest layer.

    Sent once after connecting, before any individual SSRC is known.
    """
    return await _send(
        client,
        {"op": MEDIA_SINK_WANTS, "d": {ANY_STREAM: HIGHEST_QUALITY}},
        "media sink wants (any)",
    )


async def request_video_streams(client: voice_recv.VoiceRecvClient, ssrcs: list[int]) -> bool:
    """Asks for the named video SSRCs, at the highest layer.

    Sent on `on_voice_member_video`, the first moment an SSRC exists to
    name. Redundant with `request_all_video` if `any` behaves as
    documented, and the cheap insurance if it does not.
    """
    if not ssrcs:
        return False
    return await _send(
        client,
        {"op": MEDIA_SINK_WANTS, "d": {str(ssrc): HIGHEST_QUALITY for ssrc in ssrcs}},
        "media sink wants",
    )


async def _send(client: voice_recv.VoiceRecvClient, payload: dict[str, Any], what: str) -> bool:
    """Sends one payload, and never raises.

    Reached from a gateway event handler during a live recording. An
    exception escaping a diagnostic would end a capture that was working
    -- strictly worse than not learning the answer.

    Returns whether it went out, which is what lets the probe report "we
    asked and nothing came" separately from "we never asked". Those two
    look identical in a log that only counts packets, and they mean
    opposite things.
    """
    ws = getattr(getattr(client, "_connection", None), "ws", None)
    if ws is None or ws is MISSING:
        log.debug("No voice websocket to send %s on", what)
        return False
    try:
        await ws.send_as_json(payload)
    except Exception:
        log.debug("Could not send %s", what, exc_info=True)
        return False
    return True
