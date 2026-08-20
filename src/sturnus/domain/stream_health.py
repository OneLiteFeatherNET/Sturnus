"""What an accumulating run of Opus decode failures *means* (Spec 6.1).

A single frame that will not decode is a non-event: it costs 20 ms of one
speaker's audio and the recording continues. A thousand of them in a row
mean something else entirely, and telling those two apart is the decision
that the production incident turned on -- one `OpusError: corrupted stream`
killed the packet-router thread, capture stopped for every speaker, and the
session ended with no audio and no transcription job while everyone in the
channel believed they were being recorded.

The rule that separates "ignore it" from "shout about it" is decidable
logic over counters: no I/O, no libopus, no Discord. So it lives here,
beside `SessionMachine` and `SpeakerClock`, rather than next to the ctypes
call in `sturnus.infrastructure.discord.decoding` -- which is also what
lets it be exercised exhaustively without a voice connection or even a
loaded `libopus` (`tests/domain/test_stream_health.py`).

One `StreamHealth` belongs to one SSRC. Opus decoding is stateful per
stream, so health is too: a speaker whose connection is falling apart says
nothing about the speaker next to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Discord sends one Opus frame every 20 ms, i.e. 50 frames per second per
#: speaking participant. Every threshold below is expressed in frames and
#: commented in wall-clock terms against this rate.
FRAMES_PER_SECOND = 50


class StreamState(StrEnum):
    """How much of one speaker's stream we are currently getting through."""

    #: Decoding normally, or failing only in isolated frames.
    HEALTHY = "healthy"
    #: A sustained run of failures. Reported once; recording continues.
    DEGRADED = "degraded"
    #: A long sustained run. Worth one attempt at a fresh decoder.
    UNUSABLE = "unusable"
    #: Nothing from this stream has *ever* decoded. A different diagnosis
    #: from `DEGRADED`: it points at the input shape (wrong bytes handed
    #: to the decoder, or a payload change on Discord's side), not at one
    #: stream going bad partway through.
    NEVER_DECODED = "never_decoded"


@dataclass(frozen=True)
class DecodePolicy:
    """Where the thresholds live, so no number is buried in a call site.

    Validated on construction in the same style as `SessionTimeouts`: a
    policy that cannot escalate (or escalates in the wrong order) would
    fail exactly the way the incident did -- quietly.
    """

    #: 20 frames = 400 ms of one speaker. Long enough that isolated
    #: corruption on a lossy connection does not trip it.
    degraded_after_consecutive: int = 20
    #: 250 frames = 5 s. At this point the stream is not "lossy", it is
    #: not arriving in a form we can read at all.
    unusable_after_consecutive: int = 250
    #: 50 failed decode attempts with zero successes ever = 1 s of a
    #: stream that has never once worked.
    never_decoded_after: int = 50
    #: 5 frames = 100 ms. libopus packet-loss concealment is informative
    #: for the first frame or two and low-level noise after that; past
    #: this, real silence is the more honest reconstruction.
    conceal_max_consecutive: int = 5
    #: How many times, over the whole life of one stream, a wedged
    #: decoder may be rebuilt. Bounded on purpose: rebuilding forever
    #: would turn a permanent fault into a permanent, invisible retry.
    recycle_attempts: int = 1

    def __post_init__(self) -> None:
        if self.degraded_after_consecutive <= 0:
            raise ValueError("degraded_after_consecutive must be positive")
        if self.unusable_after_consecutive <= 0:
            raise ValueError("unusable_after_consecutive must be positive")
        if self.unusable_after_consecutive <= self.degraded_after_consecutive:
            raise ValueError(
                "unusable_after_consecutive must exceed degraded_after_consecutive; "
                "otherwise a stream reaches UNUSABLE without ever reporting DEGRADED"
            )
        if self.never_decoded_after <= 0:
            raise ValueError("never_decoded_after must be positive")
        if self.conceal_max_consecutive <= 0:
            raise ValueError("conceal_max_consecutive must be positive")
        if self.recycle_attempts < 0:
            raise ValueError("recycle_attempts must not be negative")


@dataclass(frozen=True)
class StreamStats:
    """An immutable snapshot of one stream's counters.

    Deliberately a plain value object: the same numbers render into a log
    line, a Prometheus counter, and -- the point of the whole exercise --
    a caveat on the published protocol telling a participant that some of
    their audio could not be decoded. Somebody who was told they were
    being recorded is entitled to know what was lost.
    """

    frames_seen: int
    frames_decoded: int
    frames_discarded: int
    frames_lost: int
    frames_concealed: int
    consecutive_failures: int
    consecutive_lost: int
    state: StreamState
    last_error_code: int | None
    decoder_recycles: int

    @property
    def lost_seconds(self) -> float:
        """Wall-clock audio that never made it into this speaker's file."""
        return (self.frames_discarded + self.frames_lost - self.frames_concealed) / (
            FRAMES_PER_SECOND
        )


class StreamHealth:
    """Counters plus the escalation rule for one SSRC.

    `record_discarded` returns the new state **only on a transition**.
    That single decision is what keeps escalation edge-triggered: a stream
    failing at 50 frames per second produces one WARNING, not three
    thousand log lines a minute. Everything else about this class follows
    from wanting that property to be testable in isolation.
    """

    def __init__(self, policy: DecodePolicy | None = None) -> None:
        self._policy = policy or DecodePolicy()
        self._frames_seen = 0
        self._frames_decoded = 0
        self._frames_discarded = 0
        self._frames_lost = 0
        self._frames_concealed = 0
        self._consecutive_failures = 0
        self._consecutive_lost = 0
        self._state = StreamState.HEALTHY
        self._last_error_code: int | None = None
        self._decoder_recycles = 0

    @property
    def policy(self) -> DecodePolicy:
        return self._policy

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def may_recycle(self) -> bool:
        """Whether a fresh decoder is worth one more try for this stream.

        Only from `UNUSABLE`. `NEVER_DECODED` is deliberately excluded: a
        stream that never decoded a single frame is not a decoder that
        got wedged, it is the wrong bytes arriving, and a new decoder
        would fail on them in exactly the same way.
        """
        return self._state is StreamState.UNUSABLE and (
            self._decoder_recycles < self._policy.recycle_attempts
        )

    def record_decoded(self) -> None:
        """One frame decoded. Clears both consecutive runs, keeps the totals."""
        self._frames_seen += 1
        self._frames_decoded += 1
        self._consecutive_failures = 0
        # Re-arms concealment: the next lost frame is the first of a new
        # run, so packet-loss concealment starts over from a decoder that
        # has just seen real audio.
        self._consecutive_lost = 0
        self._state = StreamState.HEALTHY

    def record_discarded(self, error_code: int | None) -> StreamState | None:
        """One frame that would not decode. Returns the new state on a transition.

        Returns `None` on every failing frame that does not change the
        verdict -- which is almost all of them.
        """
        self._frames_seen += 1
        self._frames_discarded += 1
        self._consecutive_failures += 1
        self._last_error_code = error_code

        verdict = self._verdict()
        if verdict is self._state:
            return None
        self._state = verdict
        return verdict

    def record_lost(self) -> bool:
        """One frame the network lost. Returns whether to conceal it.

        Concealment is capped by policy and requires that this stream has
        decoded at least once: libopus reconstructs a lost frame from the
        decoder's memory of the previous one, and a decoder with no
        memory has nothing to reconstruct from.
        """
        self._frames_seen += 1
        self._frames_lost += 1
        self._consecutive_lost += 1
        if self._frames_decoded == 0:
            return False
        return self._consecutive_lost <= self._policy.conceal_max_consecutive

    def record_concealed(self) -> None:
        """One lost frame filled in by packet-loss concealment."""
        self._frames_concealed += 1

    def record_recycled(self) -> None:
        """A fresh decoder replaced the wedged one.

        Clears the failure run and the verdict with it, so the fresh
        decoder is judged on its own record. If it crosses `UNUSABLE`
        again the listener fires again -- and `may_recycle` is already
        false by then, because the budget is spent over the stream's
        lifetime rather than per episode.
        """
        self._decoder_recycles += 1
        self._consecutive_failures = 0
        self._state = StreamState.HEALTHY

    def stats(self) -> StreamStats:
        return StreamStats(
            frames_seen=self._frames_seen,
            frames_decoded=self._frames_decoded,
            frames_discarded=self._frames_discarded,
            frames_lost=self._frames_lost,
            frames_concealed=self._frames_concealed,
            consecutive_failures=self._consecutive_failures,
            consecutive_lost=self._consecutive_lost,
            state=self._state,
            last_error_code=self._last_error_code,
            decoder_recycles=self._decoder_recycles,
        )

    def _verdict(self) -> StreamState:
        """The state the counters currently justify.

        `NEVER_DECODED` is checked first and suppresses `DEGRADED`
        entirely while nothing has ever decoded, so a stream whose input
        shape is wrong produces one ERROR naming that, instead of a
        WARNING about degradation followed by an ERROR about something
        else. It is counted against *failed decode attempts*, not frames
        seen -- a stream that has only ever lost packets to the network
        has not told us anything about whether it can be decoded.
        """
        if self._frames_decoded == 0:
            if self._frames_discarded >= self._policy.never_decoded_after:
                return StreamState.NEVER_DECODED
            return StreamState.HEALTHY
        if self._consecutive_failures >= self._policy.unusable_after_consecutive:
            return StreamState.UNUSABLE
        if self._consecutive_failures >= self._policy.degraded_after_consecutive:
            return StreamState.DEGRADED
        return StreamState.HEALTHY
