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
- **It is never silent, either.** `RecordingSink._note_unattributed` logs one
  WARNING per SSRC — once, however long the stream goes on, and capped at 256
  distinct SSRCs, so a stuck stream at 50 frames a second cannot become a log
  flood. The line names what a human has to do about it: pause and speak again.
- **Nothing tells the affected participant, though.** See *Known limitations*
  below: only the operator reading logs finds out.

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

- `sturnus.infrastructure.discord.decoding` — one `discord.opus.Decoder` per
  SSRC, mirroring `PacketRouter.decoders`; the only module importing
  `discord.opus`. It also holds the whole failure policy, which is one counter
  and one threshold: consecutive unreadable frames per stream, one ERROR when a
  stream crosses it, and — only when *every* live stream is over it —
  `EndReason.DECODE_FAILURE` on the session row.
- `sturnus.infrastructure.discord.sink` — `wants_opus() -> True`, a `write()`
  that cannot raise, and the consent gate ahead of the decoder.
- `sturnus.infrastructure.discord.voice` — the thread hop, and turning capture
  death into `EndReason.CAPTURE_FAILURE` instead of an idle timeout.

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

**Named trigger:** if participants report audible dropouts on real sessions,
implement FEC by holding one frame back per SSRC — latency is free here, since
Sturnus transcribes offline after the session closes. There is no counter to
decide that from yet; see *Known limitations*.

## Known limitations

Each of these was addressed by machinery that was written on
`fix/voice-decode-resilience` and then taken back out, because the branch had
grown well past the one defect it exists to fix and the additions were producing
their own silent failures faster than review could close them. The concerns are
real; the implementations were not yet worth their surface. They are written
down here so they survive as work to do rather than as something nobody noticed.

### A database outage stalls the frame drain

`_record` awaits `ConsentCache.may_record`, and `ConsentCache` reads through to
the database whenever an entry is missing or older than its five-second TTL.
The drain is a single consumer, and everything queued behind it is somebody's
audio, so one slow query stalls capture for **every** speaker in the channel for
as long as it takes, not just the packet in hand.

*What was tried:* a non-blocking `verdict()` that answered from cache and
refreshed beside the drain. It made the failure worse in a way that matters
more: with the database down nothing was ever cached, every frame got a `None`
verdict, nothing was recorded, no ERROR was logged, no notice reached the
channel, and the session closed as `idle_timeout` — a database outage turned
into a silent stop, which is strictly worse than a stall you can see. A
non-blocking cache is still probably right; it needs a loud, distinguishable
failure of its own, and its own review.

### An audio backlog is unbounded

The hand-off from the extension's threads to the event loop is a plain
`asyncio.Queue` with no maximum. If the loop falls far enough behind, frames
accumulate in memory with nothing to stop them.

*What was tried:* a two-lane `CaptureChannel` — a bounded audio lane that
dropped the newest frame under load, and a separate unbounded control lane so an
alarm could not be discarded along with the audio it was reporting on. The
separate lane brought its own failure mode: a control message can overtake the
audio ahead of it, which reorders `SpeakerStreamEnded` ahead of the frames it
should follow and corrupts that SSRC's RTP reference point. Bounding the audio
is worth doing. Reordering it against control messages is not, and the two need
to be separated before either lands.

### Nobody in the channel is told

Every failure here is visible to an operator — WARNING or ERROR in the log, and
an end reason on the session row that says "we could not hear" rather than
"nobody spoke". None of it reaches the people in the voice channel: a
participant whose audio is unattributed, or who is in a session that just
stopped capturing, learns nothing.

*What was tried:* debounced `channel.send` notices posted from side tasks, plus
a join-time hint asking anyone who was already speaking to pause and speak again
(which is what makes Discord emit op 5 and supply the missing SSRC mapping).
That hint is genuinely the only way to recover an already-speaking participant,
so it is the piece most worth bringing back. It needs the task lifecycle,
debounce and rate-limit handling to be reviewed on their own terms — a
rate-limited `channel.send` awaited on the drain would stall every speaker's
audio behind a courtesy message.

### There are no voice metrics

`/metrics` serves an empty Prometheus exposition. Frames decoded, discarded
(by libopus error code), lost and unattributed are all counted nowhere, so
questions like "is FEC worth implementing" and "how often does this actually
happen" can only be answered from logs.

*What was tried:* a small dependency-free counter registry wired into the sink,
the decoder and `/metrics`. Removed with the rest of the growth, not because
counting is wrong but because it arrived as a passenger.
