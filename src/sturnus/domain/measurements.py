"""What a finished transcription job measured about its recording.

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
    """

    audio_seconds: float
    speech_seconds: float
    segment_count: int

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
