"""Telling a quiet meeting apart from a microphone that produces nothing.

Two live sessions produced transcripts of one hallucinated "Thank you." and
of nothing at all, from WAV files that were exactly as long as the meeting.
Bytes had flowed in the right volume the whole time; the amplitude in them
was at the noise floor. Nobody could tell whether that was a broken decode
path or somebody who simply never spoke, and by the time anyone looked the
meeting was over and the recording was worthless.

**Silence on its own is never the signal.** People are quiet for most of a
meeting, and Discord sends no packets while they are. The narrower
condition this module recognises is the one that means something:

    packets arrived for this speaker, decoded successfully, and every
    sample in them stayed at or near zero, for a meaningful amount of
    *received* audio.

Three states exist and only the third is a fault. No packets at all is a
participant not speaking (nothing to report). Packets that will not decode
is already `EndReason.DECODE_FAILURE`, handled in the capture layer. This
module covers the third: packets that decode into nothing audible.

Only amplitude is ever read. No sample is buffered, logged or handed on --
a peak is a number about loudness, not about content, which is what keeps
this feature outside the consent model entirely: it is exactly as true of
somebody who never consented as of somebody who did.

Pure arithmetic over bytes and per-speaker counters, so it lives in the
domain beside `SessionMachine` and `SpeakerClock` and is driven from
`sturnus.application.recording.RecordingService.voice_packet`.
"""

from __future__ import annotations

import sys
from array import array

#: Discord's voice wire format, which is fixed by the platform rather than
#: chosen by us: 48 kHz, 16-bit, two channels. Restated here rather than
#: imported from `sturnus.infrastructure.audio.SOURCE_RATE`, which names
#: the same rate for the resampler, because the domain must not import
#: outward (tests/test_architecture.py). The two cannot drift in practice:
#: neither is a setting, and if Discord ever changed the wire format both
#: would have to change together anyway.
SOURCE_SAMPLE_RATE_HZ = 48_000
BYTES_PER_SAMPLE_FRAME = 4

#: The largest peak sample still counted as silence, on the 16-bit scale
#: whose full deflection is 32767 -- about -60 dBFS.
#:
#: Not `0`, because Opus is lossy: a muted microphone feeds the encoder
#: digital silence and the decoder does not always hand back exact zeros,
#: so an equality test would find nothing on the very recordings this was
#: written for. Not much higher either, because everything above this is
#: sound somebody could have meant: -60 dBFS is far below any spoken word,
#: below a whisper, and below what Whisper can transcribe. The cost of the
#: threshold sitting a little too high is one factual message to somebody
#: whose recording continues regardless; the cost of it sitting too low is
#: the silence this whole feature exists to break.
SILENCE_PEAK_AMPLITUDE = 32

#: How much *received* audio must stay at the noise floor before it counts
#: as evidence. Thirty seconds: long enough that nothing a working
#: microphone does -- a long pause, a held breath, a codec artefact --
#: reaches it, and short enough that the warning still arrives while the
#: meeting can act on it. At the end of the session the information would
#: be worthless, because the recording is already lost.
#:
#: Measured in bytes of PCM rather than wall-clock seconds, and that is the
#: whole point: a speaker who transmits nothing for half an hour has
#: produced no evidence about their microphone at all, and warning them
#: would be the false positive that makes this unusable.
SILENCE_EVIDENCE_SECONDS = 30
SILENCE_EVIDENCE_BYTES = SILENCE_EVIDENCE_SECONDS * SOURCE_SAMPLE_RATE_HZ * BYTES_PER_SAMPLE_FRAME


def peak_amplitude(pcm: bytes) -> int:
    """The loudest sample in one packet, as a distance from zero.

    Distance, not the signed value: a waveform is symmetric around zero, so
    reading the maximum alone would report a loud negative half-cycle as
    quieter than digital silence and warn somebody in mid-sentence.

    `array` reads the samples in one C-level pass rather than a Python loop
    over ~1920 of them fifty times a second per speaker; `max`/`min` over
    it are C-level too. `numpy` would be the obvious tool and is already a
    dependency of the audio adapter, but the domain may not import it
    (tests/test_architecture.py), and at this size the standard library is
    not the bottleneck.

    Total by construction. A packet length that is not a whole number of
    samples costs the trailing half-sample rather than raising: this runs
    on every frame of every speaker, and the length comes from outside this
    process.
    """
    usable = len(pcm) - len(pcm) % 2
    if usable == 0:
        return 0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        # `array("h")` is native-endian while the wire format is not.
        # No supported deployment target is big-endian, so this branch is
        # never taken in practice -- but a silently byte-swapped peak would
        # be the kind of wrong that still looks plausible.
        samples.byteswap()
    # `-min(...)` can reach 32768, one past the positive range. It is only
    # ever compared against a threshold, never stored as a sample.
    return max(max(samples), -min(samples))


class SilentAudioWatch:
    """Accumulates, per speaker, how much received audio has stayed inaudible.

    One instance belongs to one recording session -- `RecordingService`
    replaces it in `reset()`, the same way it replaces its `SpeakerClock`
    -- so "once per speaker" means once per session, and the next meeting
    starts with a clean slate for everybody.
    """

    def __init__(
        self,
        evidence_bytes: int = SILENCE_EVIDENCE_BYTES,
        peak_threshold: int = SILENCE_PEAK_AMPLITUDE,
    ) -> None:
        self._evidence_bytes = evidence_bytes
        self._peak_threshold = peak_threshold
        #: Bytes of *consecutive* inaudible audio received per speaker.
        self._silent_bytes: dict[int, int] = {}
        #: Speakers already reported. Never cleared while the session runs.
        self._reported: set[int] = set()

    def observe(self, discord_user_id: int, pcm: bytes) -> bool:
        """Records one decoded packet; `True` on the packet that completes the case.

        Returns `True` exactly once per speaker per session, on the packet
        that pushes the accumulated evidence over the threshold, so the
        caller can act on the return value alone and needs no state of its
        own. Every later packet from that speaker returns `False`,
        inaudible or not -- repeating the message would only put the same
        person on the spot again.

        Any audible packet clears that speaker's evidence: the case has to
        be continuous, because somebody who spoke twenty seconds ago has a
        microphone that demonstrably works.
        """
        if peak_amplitude(pcm) > self._peak_threshold:
            self._silent_bytes[discord_user_id] = 0
            return False
        if discord_user_id in self._reported:
            return False
        collected = self._silent_bytes.get(discord_user_id, 0) + len(pcm)
        self._silent_bytes[discord_user_id] = collected
        if collected < self._evidence_bytes:
            return False
        self._reported.add(discord_user_id)
        return True
