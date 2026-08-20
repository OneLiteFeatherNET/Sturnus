# Voice receive: what the library actually exposes

Date: 2026-08-19 · `discord-ext-voice-recv==0.5.2a179` · `discord.py` 2.6.x

This settles the largest technical unknown in the project. Spec 6.2 reconstructs
the position of every speech segment from RTP timestamps, because Discord sends
no packets during silence and arrival time therefore cannot locate anything. If
those timestamps were not reachable, that design would not hold.

**They are reachable.**

## Findings from the installed package

`RTPPacket` exposes both fields the design needs:

```
adjust_rtpsize, cc, csrcs, data, decrypted_data, extended, extension,
extension_data, header, is_silence, marker, nonce, padding, payload,
sequence, ssrc, timestamp, update_ext_headers, version
```

- `timestamp` — the 48 kHz RTP clock value `SpeakerClock` consumes.
- `ssrc` — the per-stream identifier the clock keys its reference points on.
- `is_silence` — a marker worth investigating during implementation; it may
  allow skipping packets that carry no speech. **Answered below: decode them,
  do not skip them.**

`AudioSink.write` is the callback, receiving a user and a `VoiceData`. That
object carries `opus`, `pcm`, `packet` and `source` — so decoded audio, the raw
RTP packet, and the associated user arrive together.

`VoiceRecvClient` maintains an SSRC mapping (`_get_id_from_ssrc`,
`_get_ssrc_from_id`, `_add_ssrc`, `_remove_ssrc`). The names are private, which
is worth noting: the adapter should prefer `VoiceData.source` where possible and
treat the mapping as an implementation detail that may change between alpha
releases.

## What production answered — 2026-08-20

The three "still open" questions were closed the expensive way. One Opus frame
that failed to decode ended a whole live recording:

```
ERROR:discord.ext.voice_recv.router:Error in <PacketRouter(...)> loop
discord.opus.OpusError: corrupted stream
```

`PacketRouter.run()` catches it, logs it, sets `reader.error`, calls
`stop_listening()` in its `finally`, and the thread exits. Capture stopped for
every speaker at once. The session stayed open, the bot stayed in the channel,
and it closed with no audio and no transcription job — a `sessions` row with
zero participants was the first anyone knew. Everyone in that channel had been
told they were being recorded.

### 1. Is the PCM 48 kHz 16-bit stereo? — **Yes, and Sturnus now produces it itself.**

Measured against the installed `discord.py`: a normal 20 ms frame decodes to
3840 bytes, which is 960 samples × 2 channels × 2 bytes at 48 kHz. `to_mono_16k`
is correct, and stays exactly where it was, in `FileAudioWriter.write`.

**The frame size is not constant, and nothing may hardcode 3840.** `b"\x00"`
decodes to 1920 bytes (10 ms). `SpeakerWriter` derives its sample count from
`len(pcm)` and places audio by RTP timestamp, so this is already handled;
`tests/infrastructure/discord/test_decoding.py` pins it.

Sturnus now owns decoding (see the last section), so this is no longer a
question about what the library hands back — it is a property of our own
decoder, and it is asserted in a test.

### 2. Is `source` populated on the very first packet? — **No.**

For a speaker who was **already talking when the bot connected**, it is not.
Discord maps an SSRC to a user only in its `SPEAKING` event (op 5), which it
sends when someone *starts* speaking. A participant mid-sentence has already
sent theirs, so their frames arrive with `VoiceData.source is None` and no
member attached.

The design answer, implemented in `sturnus.infrastructure.discord.sink`:

- **Unattributed audio is never decoded, never buffered, never written.** No
  consent record can be verified for an identity we do not know, and buffering
  unattributed speech in RAM is the wrong trade both legally and operationally.
- **It is never silent, either.** It is counted
  (`sturnus_voice_frames_unattributed_total`), logged at WARNING with the SSRC
  and frame count, and reported once into the channel.
- **The report is also the fix.** `VoiceReceiveAdapter.join` posts a one-time
  notice asking anyone who was already speaking to pause and speak again —
  stopping and restarting speech is precisely what makes Discord emit op 5, and
  the bot has no way to request the mapping directly.

Identity *inference* (attributing an unmapped SSRC by elimination when exactly
one channel member and exactly one SSRC are unaccounted for) was considered and
deliberately not built. It is sound as a deduction, but it puts a guess in front
of a legal gate to recover at most a second or two of audio. Losing the audio
and saying so is the better failure.

### 3. What happens on reconnect? — **Still needs a live channel.**

A new SSRC for the same user is what Spec 6.2 assumes and what the library's
own bookkeeping implies, but it has not been observed directly. The code is
written so that it does not matter much either way:
`on_voice_member_disconnect` evicts that SSRC's decoder (mirroring the
library, `voice_recv/gateway.py`) and calls `SpeakerClock.reset(ssrc)`, so a
returning participant gets a fresh reference point rather than being placed
against a stale one.

### `is_silence` — decode it, do not skip it

`SilencePacket.decrypted_data` is the real three-byte `OPUS_SILENCE`
(`b"\xf8\xff\xfe"`), and it decodes normally to a full 3840-byte frame of
zeros. Skipping it would save nothing measurable — three bytes through libopus
is free — and would desynchronise the decoder's `_get_last_packet_duration`,
which packet-loss concealment reads to size the frame it synthesises. So it is
decoded like any other frame.

## Measured against the installed packages

`discord-ext-voice-recv 0.5.2a` · `discord.py` 2.6.x · run directly, not recalled.

| Fact | Result |
| --- | --- |
| `OpusError(-4)` message | exactly `"corrupted stream"` — the production error |
| Error codes via `_err_lt` | `-1` invalid argument, `-2` buffer too small, `-3` internal error, `-4` corrupted stream, `-5` request not implemented, `-6` invalid state, `-7` memory allocation failed |
| A decoder after an `OpusError` | **survives**: `[good, garbage, good, garbage, good, good]` through one instance yields `ok, -4, ok, -4, ok, ok`, no reset |
| `decode(b"")` | raises `OpusError(-1)` — an empty payload must take the loss path, never `decode()` |
| `FakePacket.decrypted_data` | `b""`, and `bool(packet)` is `False` |
| `SilencePacket.decrypted_data` | `OPUS_SILENCE`, decodes to 3840 zero bytes |
| PLC (`decode(None, fec=False)`) | peaks on the first concealed frame and decays to low-level noise within one or two more |
| Constructing `OpusError` | needs libopus **loaded**: `__init__` calls `_lib.opus_strerror`, and `_lib` is only populated by `_OpusStruct.get_opus_version()`. Building a `Decoder` first is what makes the exception constructible at all — which is one more reason the startup probe runs before any frame does. |

## The fix, and where it lives

`wants_opus() -> True`. `PacketDecoder.__init__` builds a `discord.opus.Decoder`
only when the sink returns `False`, and `_process_packet` calls
`_decode_packet` — the crash site — only on the same condition. A sink that
wants Opus makes that line unreachable, through the library's documented public
API, with no monkey-patching and nothing that a version bump can break quietly.

- `sturnus.domain.stream_health` — what an accumulating run of failures *means*.
- `sturnus.infrastructure.discord.decoding` — one `discord.opus.Decoder` per
  SSRC, mirroring `PacketRouter.decoders`; the only module importing
  `discord.opus`.
- `sturnus.infrastructure.discord.sink` — `wants_opus() -> True`, a `write()`
  that cannot raise, and the consent gate ahead of the decoder.

`VoiceData.opus` is `packet.decrypted_data`, already stripped of RTP extension
headers by `PacketDecryptor` before the router sees it, so it needs no further
handling. `_get_id_from_ssrc` and friends remain off-limits, as this document
originally warned: the sink uses only `wants_opus`, `write`, `cleanup`,
`AudioSink.listener`, `VoiceData.opus` and `VoiceData.packet`.

### What we gave up: FEC

The library conceals loss two ways in `_decode_packet`. When a fake packet's
*successor* is already in the jitter buffer it calls `decode(next, fec=True)`,
recovering the lost frame from the LBRR copy the encoder embedded in the next
packet; otherwise it falls back to plain PLC, `decode(None, fec=False)`.

A `wants_opus` sink still receives a `VoiceData` for every fake packet, so we
know exactly when a frame was lost — but `_buffer.peek_next()` is not reachable
from a sink. **We keep PLC in full and lose FEC.** FEC only ever applied to the
subset of losses where the successor had already arrived *and* the sending
client emitted LBRR at all; on every other loss the library was already doing
the PLC we kept. Against that, the alternative was the whole session ending for
every speaker.

This is measured rather than argued: `sturnus_voice_frames_lost_total` and
`sturnus_voice_frames_decoded_total` are exported on `/metrics`. **Named
trigger:** if `frames_lost / frames_seen` exceeds roughly 2 % on a real session,
implement FEC by holding one frame back per SSRC — latency is free here, since
Sturnus transcribes offline after the session closes.
