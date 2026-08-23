"""What a finished transcription job measured about its recording.

Two values, kept apart deliberately. `JobMeasurements` is what *decoding*
observed and is only meaningful as a whole; `RecordedAudio` is what the
file is, and would be the same had nobody decoded it. See each type for
why they are not one.

Three numbers the worker has always computed and never kept. They went
into a log line and a metric, and both of those are retained for weeks
while the job's own row lives as long as the guild does -- so "how much
has this person actually said, across every meeting" was a question the
database could not answer at all.

They are one value rather than three arguments because they are only
meaningful together. `speech_seconds` on its own says nothing: eighty
seconds of speech is a quiet participant in a ten-minute call and a broken
microphone in a two-hour one. The invariant below exists for the same
reason -- it is the check that would have caught the defect where one
figure was the concatenated speech and the other the padded file, and a
track of a hundred minutes appeared to finish in forty-three seconds.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobMeasurements:
    """Durations and a count, as observed once a job has been decoded.

    `audio_seconds` is the length of the recording as written -- silence
    and all. `speech_seconds` is what the gate handed the decoder after
    removing that silence. `segment_count` is how many segments came back,
    which is what separates "said nothing" from "was never transcribed":
    both leave an empty transcript, and only this number tells them apart.

    `model` is what produced all three. It is optional only because rows
    written before it existed cannot be backfilled -- there is nothing to
    read it from, and guessing the current default would turn an absence
    into a claim.
    """

    audio_seconds: float
    speech_seconds: float
    segment_count: int
    #: The model that produced them, or `None` for a job finished before
    #: this was recorded.
    #:
    #: Not a measurement itself, and kept here anyway, because none of the
    #: three above means anything without it. `sturnus.infrastructure.
    #: whisper` already says so about the metric -- "a real-time factor
    #: that mixes `large-v3` with `tiny` says nothing" -- and the same is
    #: true of a segment count. Two runs over one recording are only
    #: comparable if each says what produced it.
    model: str | None = None

    def __post_init__(self) -> None:
        if self.audio_seconds < 0:
            raise ValueError(f"audio_seconds cannot be negative: {self.audio_seconds}")
        if self.speech_seconds < 0:
            raise ValueError(f"speech_seconds cannot be negative: {self.speech_seconds}")
        if self.segment_count < 0:
            raise ValueError(f"segment_count cannot be negative: {self.segment_count}")
        # The gate removes silence; it cannot invent speech. A violation
        # means the two figures were measured against different things --
        # the exact confusion this type exists to make unrepresentable.
        if self.speech_seconds > self.audio_seconds:
            raise ValueError(
                f"speech_seconds ({self.speech_seconds}) exceeds "
                f"audio_seconds ({self.audio_seconds}); the two were measured "
                "against different audio"
            )

    @property
    def speech_share(self) -> float:
        """The fraction of the recording that was speech.

        Zero for a zero-length track rather than a division error: every
        caller would otherwise guard the same division, and a track with
        no audio has no speech in it either -- 0.0 is the honest answer,
        not a placeholder for one.
        """
        if self.audio_seconds == 0:
            return 0.0
        return self.speech_seconds / self.audio_seconds


@dataclass(frozen=True)
class RecordedAudio:
    """What a recording *is* as a file, as opposed to what it said.

    Three facts the system has always had and never kept. `sample_rate`
    and `channels` are re-read out of the object store on every request
    that wants them (`sturnus.console.spectrogram.parse_track_format`
    walks the RIFF header live, which costs a ranged GET and a chunk
    decrypt to answer "how many channels"), and `stored_bytes` is a
    second round trip asking S3 how big the object is. The worker holds
    both files on disk at the moment it transcribes and can simply write
    them down.

    Separate from `JobMeasurements` rather than three more fields on it,
    because the two are answers to different questions and are not
    meaningful together: `JobMeasurements` is what decoding observed and
    carries an invariant between its numbers, while these three are
    properties of the file that would be the same if nobody ever decoded
    it. Folding them in would put a size next to a speech duration and
    invite a fourth reader to compare them.

    Every field is a positive quantity and none of them is optional here.
    A track whose header cannot be read produces no `RecordedAudio` at
    all, so the columns stay null -- null is "nobody ever looked", zero
    would be a claim about the recording (see `sturnus.console.
    statistics`).
    """

    sample_rate: int
    channels: int
    #: The size of the *stored* object, which is the encrypted one -- the
    #: number `S3AudioStore.size` is asked for today. Bigger than the
    #: plaintext WAV by the envelope framing, and that is the honest
    #: figure for "what does this recording cost us to keep".
    stored_bytes: int

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive: {self.sample_rate}")
        if self.channels <= 0:
            raise ValueError(f"channels must be positive: {self.channels}")
        # Zero is allowed: an object can genuinely be empty, and that is a
        # fact about the recording rather than a failure to measure one.
        if self.stored_bytes < 0:
            raise ValueError(f"stored_bytes cannot be negative: {self.stored_bytes}")
