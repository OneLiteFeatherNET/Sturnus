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
  allow skipping packets that carry no speech.

`AudioSink.write` is the callback, receiving a user and a `VoiceData`. That
object carries `opus`, `pcm`, `packet` and `source` — so decoded audio, the raw
RTP packet, and the associated user arrive together.

`VoiceRecvClient` maintains an SSRC mapping (`_get_id_from_ssrc`,
`_get_ssrc_from_id`, `_add_ssrc`, `_remove_ssrc`). The names are private, which
is worth noting: the adapter should prefer `VoiceData.source` where possible and
treat the mapping as an implementation detail that may change between alpha
releases.

## Still open — needs a live voice channel

1. **Is the PCM 48 kHz 16-bit stereo?** `to_mono_16k` assumes it. The docstring
   does not say, so confirm against real packets before trusting the resampler.
2. **Is `source` populated on the very first packet of a speaker**, or does the
   mapping arrive slightly later? Spec 6.3 anchors a speaker's recording at the
   first packet, so a late mapping shifts their whole timeline. This is the one
   remaining question that can change a design decision.
3. **What happens on reconnect** — a new SSRC for the same user, as Spec 6.2
   assumes, and how quickly the mapping follows.

## Consequence for Task 10 of Plan 2

The library question is answered; the adapter can be written against the API
above. The live spike still has to happen, but its scope is now three specific
questions rather than an open-ended investigation, and only question 2 can
change the design.
