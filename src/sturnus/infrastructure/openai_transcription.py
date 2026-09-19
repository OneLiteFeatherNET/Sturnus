"""Transcription against an OpenAI-compatible `/audio/transcriptions` endpoint.

The worker holds no model. A speaker's track is read, gated, divided into
windows small enough for one request, and each window is posted to whatever
host the operator configured -- OpenAI's own, a self-hosted vLLM, anything
that speaks the same shape. `TranscriptionEngine` does not change by one
character, so `sturnus.application.worker` cannot tell the difference.

**Why the cuts land in silence, which is the load-bearing decision here.**
The obvious shape is to drop the gaps and concatenate the speech, which is
what the engine this replaces did. It cannot be done over HTTP. That engine
attributed each decoded segment to a clip by `Segment.seek` -- the encoder
window it came out of -- precisely because a decoded `end` routinely
overruns the window it belongs to, and resolving the clip from that `end`
adds the *next* clip's offset and moves the text across an entire removed
silence. Its own measurement: on a 100-minute recording with 187 clips, 57
of 462 segments overran, 5 of them by more than half their own length,
landing 20 to 31 seconds from where they were spoken. The error is always
exactly one gap wide, so on a session with three utterances forty minutes
apart it is forty minutes.

**An HTTP response carries no `seek`.** The only safe attribution is
therefore to make attribution unnecessary: if nothing inside a window was
removed, every segment in it maps onto the recording by one constant
addition, and an overrunning `end` can do no worse than claim a few tenths
of a second of the silence beside it -- which `_on_the_recording` then
takes back.

The speech gate stays, and not out of habit. The bit-exact zero padding
`sturnus.infrastructure.audio` writes between packets is the single most
reliable way to make a speech model invent text, and an HTTP endpoint
exposes no decoding parameter that would defend against it.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import numpy as np
from openai import AsyncOpenAI, omit
from openai.types.audio import TranscriptionSegment, TranscriptionVerbose

from sturnus.application.transcription import TranscribedSegment, TranscriptionResult
from sturnus.domain import transcription_models
from sturnus.domain.measurements import JobMeasurements
from sturnus.domain.transcription_models import UnknownTranscriptionModel
from sturnus.infrastructure.speech_gate import speech_clips
from sturnus.infrastructure.telemetry import TRANSCRIPTION_PROGRESS, set_current_span_fields
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: `sturnus.infrastructure.audio.SpeakerWriter` writes 16-bit PCM, and the
#: sample rate is read from each file rather than assumed: every duration
#: below is in seconds, so a file written at another rate still divides
#: into windows at the right instants -- it is their *size* that changes,
#: which is why the cap is configuration rather than a constant.
_SAMPLE_WIDTH_BYTES: Final = 2
#: int16 full scale. `faster_whisper.audio.decode_audio` divided by this to
#: reach mono float in [-1.0, 1.0]; the gate is written against that range
#: and the division has to happen somewhere.
_FULL_SCALE: Final = 32768.0

#: A segment whose no-speech probability is above this is invented, and is
#: dropped whatever else it reports.
#:
#: **`avg_logprob` must not be added as a second condition here, and this
#: is the one place in this module where getting it wrong reintroduces a
#: defect this project has already paid for.** faster-whisper's silence
#: skip read
#:
#:     should_skip = result.no_speech_prob > no_speech_threshold
#:     if log_prob_threshold is not None and avg_logprob > log_prob_threshold:
#:         should_skip = False
#:
#: An invented subtitle credit is a *fluent, high-probability* token
#: sequence -- that is why a model reaches for one when there is nothing to
#: transcribe -- so its `avg_logprob` of about -0.88 sits above the
#: library's -1.0 default and switches off the guard aimed at it. That is
#: how `" Untertitelung des ZDF, 2020"` reached a production protocol with
#: `no_speech_threshold` already set.
#:
#: **0.6 is not a margin and this comment will not pretend otherwise.** On
#: the measured set -- `tiny`, German, 111 fragments -- real 0.4 s fragments
#: reached 0.804 while invented lines went as low as 0.639: the two classes
#: overlap and no threshold separates them. 0.6 rejects every invented line
#: at the cost of four 0.4 s fragments of a real word. The same measurement
#: found that lowering it to 0.4 changed nothing while the veto was live, so
#: that is not a lever either. Whoever moves this should move it against a
#: second speaker and a second fixture, and should expect to trade rather
#: than to find a gap.
_NO_SPEECH_PROB_MAX: Final = 0.6

#: A segment more compressible than this is repetition -- the model stuck
#: in a loop -- and is dropped independently of the rule above. Carried
#: over unchanged from the decoder this replaces, and kept for what it does
#: catch: the same measurement is explicit that it does *not* catch the
#: invented credit (0.69, against 0.43 for real speech, so the invented
#: line is *less* repetitive than the thing worth keeping).
_COMPRESSION_RATIO_MAX: Final = 2.4

#: Sent when the operator configured no key. The SDK refuses to construct a
#: client without one -- `""` and `None` both raise `OpenAIError` -- while a
#: local vLLM neither needs nor reads it. A literal placeholder is therefore
#: the only way to express "this endpoint has no authentication", and it is
#: better here, named and explained, than as an operator's private
#: superstition about what to type into `STURNUS_TRANSCRIPTION_API_KEY`.
_NO_CREDENTIAL: Final = "no-credential-configured"


def endpoint_host(base_url: str) -> str:
    """The host a deployment sends audio to, for the three places that say so.

    **The host and nothing else.** A base URL can carry a path, a port, a
    query and -- for some hosted gateways -- a key in either of the last
    two. `/about` and the consent prompt exist to tell a person where
    their voice goes, which the host answers; anything after it answers a
    question nobody asked and is the shape a credential would arrive in.

    Falls back to the whole string only when it parses to no host at all,
    which cannot happen for a URL the SDK accepted -- and an operator
    looking at a malformed value in the consent prompt is better served by
    seeing it than by seeing nothing.
    """
    parsed = urlparse(base_url)
    return parsed.hostname or base_url


@dataclass(frozen=True)
class _Window:
    """One request's worth of audio, on the recording's own timeline.

    `cut_through_speech` records that this window's *end* fell inside a
    clip rather than in a silence, which happens only when the cap is
    reached with no gap to use. It travels on the window rather than being
    recomputed at the log site because the planner is the only thing that
    knows it.
    """

    start: float
    end: float
    cut_through_speech: bool

    @property
    def seconds(self) -> float:
        return self.end - self.start


def _read_mono_float(path: Path) -> tuple[np.ndarray[Any, Any], int]:
    """A WAV as mono float in [-1.0, 1.0], and the rate it was written at.

    The standard library rather than a decoder: these files are written by
    `sturnus.infrastructure.audio.SpeakerWriter` through `wave`, and
    `sturnus.application.worker` already reads them back the same way. The
    only thing added here is the division that `decode_audio` used to
    perform on the way to the gate's range.

    The rate is returned rather than assumed so that every duration below
    is in seconds of the actual recording. A file at another rate divides
    into windows of the same *duration* and therefore of a different size,
    which is why `max_chunk_seconds` is configuration and not a constant.
    """
    with wave.open(str(path), "rb") as track:
        rate = track.getframerate()
        channels = track.getnchannels()
        width = track.getsampwidth()
        if width != _SAMPLE_WIDTH_BYTES:
            raise ValueError(f"expected 16-bit PCM, got {width * 8}-bit")
        raw = track.readframes(track.getnframes())

    interleaved = np.frombuffer(raw, dtype=np.int16)
    mono: np.ndarray[Any, Any] = interleaved
    if channels > 1:
        # Never produced by `SpeakerWriter`, which writes one mono track per
        # speaker. Averaged rather than refused because a file that arrived
        # some other way is still worth transcribing, and the gate needs one
        # channel.
        mono = interleaved.reshape(-1, channels).mean(axis=1)
    return np.asarray(mono, dtype=np.float32) / _FULL_SCALE, rate


def _plan_windows(clips: tuple[tuple[float, float], ...], cap_seconds: float) -> list[_Window]:
    """Divide a track into windows of at most `cap_seconds`, cutting in silence.

    Every boundary falls inside a gap the gate reported, except where a
    single clip is itself longer than the cap -- there the cut happens at
    the cap, through whatever is being said. **The order of those two
    constraints is deliberate:** a cut word costs one word's accuracy at
    one boundary, while a window over the endpoint's size limit is a
    rejected request and a failed job.

    Windows holding no clip at all are dropped rather than sent. They arise
    on their own: a gap longer than the cap between two clips leaves a
    stretch of pure padding that has to be cut somewhere, and sending it
    would be sending a decoder nothing but silence -- slow, and the very
    thing the gate exists to prevent.
    """
    if not clips:
        return []

    # First pass: cut in the gaps, as late as the cap allows.
    coarse: list[tuple[float, float]] = []
    start, last_end = clips[0]
    for clip_start, clip_end in clips[1:]:
        if clip_end - start <= cap_seconds:
            last_end = clip_end
            continue
        # Halfway through the gap, but never past the cap. Both bounds lie
        # between `last_end` and `clip_start`, so the cut is inside silence
        # either way -- the `min` only decides how much of a very long gap
        # travels with the window that precedes it.
        cut = min((last_end + clip_start) / 2.0, start + cap_seconds)
        coarse.append((start, cut))
        start, last_end = cut, clip_end
    coarse.append((start, last_end))

    # Second pass: anything still over the cap is one clip longer than a
    # window, or a gap that outran the first pass. Cut it at the cap.
    windows: list[_Window] = []
    for coarse_start, coarse_end in coarse:
        window_start = coarse_start
        while coarse_end - window_start > cap_seconds:
            cut = window_start + cap_seconds
            windows.append(
                _Window(window_start, cut, cut_through_speech=_inside_a_clip(cut, clips))
            )
            window_start = cut
        windows.append(_Window(window_start, coarse_end, cut_through_speech=False))

    return [window for window in windows if _holds_speech(window, clips)]


def _inside_a_clip(instant: float, clips: tuple[tuple[float, float], ...]) -> bool:
    return any(start < instant < end for start, end in clips)


def _holds_speech(window: _Window, clips: tuple[tuple[float, float], ...]) -> bool:
    return any(start < window.end and end > window.start for start, end in clips)


def _as_wav(samples: np.ndarray[Any, Any], rate: int) -> bytes:
    """One window as a WAV file, in memory.

    In memory rather than through a temporary file because the worker's
    `work_dir` already holds the track being transcribed and a second copy
    of every window on disk buys nothing: the bytes are handed straight to
    an HTTP request and released.
    """
    clipped = np.clip(samples, -1.0, 1.0)
    frames = (clipped * _FULL_SCALE).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(_SAMPLE_WIDTH_BYTES)
        out.setframerate(rate)
        out.writeframes(frames.tobytes())
    return buffer.getvalue()


def _optional(segment: TranscriptionSegment, field: str) -> float | None:
    """One confidence figure, or `None` where the endpoint reported none.

    The SDK's model annotates these as required `float`s and constructs
    responses leniently, so a field an implementation omits arrives as
    `None` against an annotation that says it cannot be. vLLM's
    `verbose_json` omits `no_speech_prob`, and other implementations will
    omit other fields, so this is the ordinary case rather than a
    malformed response. `model_fields_set` separates "absent" from
    "present and null" for anybody who later needs the distinction; the
    rules below do not, because both mean the same thing to them.
    """
    if field not in segment.model_fields_set:
        return None
    value = getattr(segment, field, None)
    return float(value) if isinstance(value, int | float) else None


def _is_invented(segment: TranscriptionSegment) -> str | None:
    """Which rule refuses this segment, or `None` to keep it.

    **A missing figure means the rule does not apply**, never that the
    segment is dropped and never that the job fails. A deployment against
    an endpoint that reports neither keeps the speech gate and
    `temperature=0` and nothing else -- which is the configuration the
    measurement behind `_NO_SPEECH_PROB_MAX` was taken *without*, so
    `docs/operations.md` says plainly that an endpoint reporting
    `no_speech_prob` is the safer one to point at.
    """
    no_speech = _optional(segment, "no_speech_prob")
    if no_speech is not None and no_speech > _NO_SPEECH_PROB_MAX:
        return "no_speech_prob"
    compression = _optional(segment, "compression_ratio")
    if compression is not None and compression > _COMPRESSION_RATIO_MAX:
        return "compression_ratio"
    return None


def _on_the_recording(segment: TranscriptionSegment, window: _Window) -> TranscribedSegment:
    """One returned segment, moved onto the recording's timeline.

    One addition, because nothing inside the window was removed. The clamp
    is what keeps an overrunning `end` -- routine, see this module's
    docstring -- from claiming time that belongs to the next window.
    """
    start = min(max(window.start + segment.start, window.start), window.end)
    end = min(max(window.start + segment.end, start), window.end)
    return TranscribedSegment(start=start, end=end, text=segment.text)


class OpenAITranscriptionEngine:
    """`TranscriptionEngine` against an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        default_language: str,
        timeout_seconds: float,
        max_retries: int,
        max_chunk_seconds: float,
    ) -> None:
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or _NO_CREDENTIAL,
            timeout=timeout_seconds,
            # The SDK's own retries, deliberately below `max_job_attempts`:
            # without them a rate-limited request burns one of the job's
            # attempts and re-uploads a whole window from the start. They
            # honour `Retry-After`, which nothing written here would.
            max_retries=max_retries,
        )
        self._model = model
        self._default_language = default_language
        self._max_chunk_seconds = max_chunk_seconds

    async def transcribe(
        self,
        path: Path,
        language: str | None,
        initial_prompt: str | None,
        model: str | None = None,
    ) -> TranscriptionResult:
        model_name = self._model_for(model)
        audio, rate = _read_mono_float(path)
        audio_seconds = audio.shape[0] / rate
        clips = speech_clips(audio, sample_rate=rate)
        # The gate's own verdict, in seconds. `speech_gate` stays a pure
        # numpy function with no logger of its own, so reporting it is the
        # caller's job.
        speech_seconds = sum(end - start for start, end in clips)

        if not clips:
            # Announced rather than returned silently, and **not** counted
            # towards `decoded_seconds`. An empty transcript has exactly two
            # causes -- the gate found nothing, or the endpoint was called
            # and produced nothing -- and only the second is a defect.
            log_event(
                log,
                logging.INFO,
                Event.TRANSCRIPTION_SKIPPED,
                "The speech gate found nothing above the silence floor; the endpoint was "
                "not called and this speaker produced no segments.",
                model=model_name,
                audio_seconds=round(audio_seconds, 3),
                speech_seconds=0.0,
                clips=0,
            )
            return TranscriptionResult(segments=(), language=self._default_language)

        windows = _plan_windows(clips, self._max_chunk_seconds)
        for window in windows:
            if window.cut_through_speech:
                # A transcript with an unexplained broken sentence should be
                # explicable from the worker's output. Twelve minutes of
                # continuous speech from one participant is the rare case
                # this is, and it is allowed to be the worse one.
                log.warning(
                    "A transcription window reached the %.0f s cap with no silence to cut "
                    "at; the cut at %.3f s falls inside speech and may break a word",
                    self._max_chunk_seconds,
                    window.end,
                )

        started = time.monotonic()
        # Before the first request, not after it: a job that wedges before
        # anything comes back has to be distinguishable from one that has
        # merely just started. See `TranscriptionProgress.begin`.
        TRANSCRIPTION_PROGRESS.begin(model_name)
        try:
            collected, detected = await self._transcribe_windows(
                windows, audio, rate, model_name, language, initial_prompt
            )
        finally:
            # Runs on the failure path too, or a request that raised would
            # leave the job "in flight" forever and `seconds_since_progress`
            # would climb past every threshold while the worker moved on.
            TRANSCRIPTION_PROGRESS.end()

        wall_seconds = time.monotonic() - started
        sent_seconds = sum(window.seconds for window in windows)
        log_event(
            log,
            logging.INFO,
            Event.TRANSCRIPTION_DECODED,
            "A speaker's recording was transcribed against the configured endpoint.",
            model=model_name,
            language=detected,
            audio_seconds=round(audio_seconds, 3),
            speech_seconds=round(speech_seconds, 3),
            clips=len(clips),
            windows=len(windows),
            segments=len(collected),
            wall_seconds=round(wall_seconds, 3),
            realtime_factor=round(wall_seconds / sent_seconds, 4) if sent_seconds else None,
        )
        # The wrapper around this call sees a `TranscriptionResult` and the
        # gate's numbers are not in it, so it cannot set these two.
        set_current_span_fields(speech_seconds=speech_seconds, clips=len(clips))
        return TranscriptionResult(
            segments=tuple(collected),
            language=detected,
            measurements=JobMeasurements(
                model=model_name,
                audio_seconds=audio_seconds,
                speech_seconds=speech_seconds,
                segment_count=len(collected),
            ),
        )

    def _model_for(self, name: str | None) -> str:
        """The model a job asked for, refusing a name this deployment has not.

        **Checked before a single sample is read**, because an unknown name
        must fail the job before it costs a hundred minutes of audio
        processing and an upload.

        **The check is here as well as at the boundary, and it is not the
        same check.** `transcription_models.resolve` refuses a name where a
        re-queue is *planned*, which is where a caller can still be told to
        fix their request. This one catches a name that got past it -- a row
        written by hand, a caller nobody has added the validation to yet --
        and fails with a message an operator can read out of the job's
        `error` column rather than with whatever the endpoint says about a
        model it does not serve.

        **The worker's own configured model is always allowed.** An operator
        who pointed this deployment at an endpoint serving a privately named
        model has said so deliberately, and a worker refusing the model it
        was started with would be refusing its own configuration.
        """
        wanted = name or self._model
        if wanted != self._model and not transcription_models.is_known(wanted):
            raise UnknownTranscriptionModel(wanted)
        return wanted

    async def _transcribe_windows(
        self,
        windows: list[_Window],
        audio: np.ndarray[Any, Any],
        rate: int,
        model_name: str,
        language: str | None,
        initial_prompt: str | None,
    ) -> tuple[list[TranscribedSegment], str]:
        """Every window in order, and the language they were all read in."""
        # The denominator is the audio that will actually be sent, which is
        # the windows rather than the recording: the padding between them
        # never reaches the endpoint and counting it would report a job as
        # slower than it is.
        TRANSCRIPTION_PROGRESS.set_total(sum(window.seconds for window in windows))

        collected: list[TranscribedSegment] = []
        # `None` until the first window answers. From then on it is passed
        # *in* to every later window: one speaker's track must not change
        # language halfway through because a detector guessed differently on
        # a quieter stretch. The same pinning `set_detected_language`
        # performs across a session's jobs, one level down.
        pinned = language
        detected = self._default_language
        sent_seconds = 0.0

        for window in windows:
            response = await self._one_window(
                audio, rate, window, model_name, pinned, initial_prompt
            )
            if pinned is None:
                detected = _language_code(response.language, self._default_language)
                pinned = detected
            for segment in response.segments or ():
                refused_by = _is_invented(segment)
                if refused_by is not None:
                    # Ours to log, with the segment's own numbers, at a
                    # severity we choose. The decoder this replaces dropped
                    # such a window inside the library, leaving nothing but a
                    # DEBUG line on a logger nothing here configures.
                    log.info(
                        "Dropping an invented segment: %s above its threshold "
                        "(no_speech_prob=%s, compression_ratio=%s)",
                        refused_by,
                        _optional(segment, "no_speech_prob"),
                        _optional(segment, "compression_ratio"),
                    )
                    continue
                collected.append(_on_the_recording(segment, window))
            sent_seconds += window.seconds
            # Once per window, which is as often as there is anything to
            # report: one window is one request with no intermediate signal.
            TRANSCRIPTION_PROGRESS.advance(sent_seconds)

        if language is not None:
            detected = language
        return collected, detected

    async def _one_window(
        self,
        audio: np.ndarray[Any, Any],
        rate: int,
        window: _Window,
        model_name: str,
        language: str | None,
        initial_prompt: str | None,
    ) -> TranscriptionVerbose:
        """One request. Anything it raises fails the job, deliberately.

        A timeout or a 5xx that reached here has already exhausted the SDK's
        own retries, and `worker.process_one` is the thing that owns what
        happens next: it records an attempt, and at `max_job_attempts` the
        job is dead with an error an operator can read. Swallowing it here
        would produce a partial transcript that looks like a complete one.
        """
        first = round(window.start * rate)
        last = round(window.end * rate)
        payload = _as_wav(audio[first:last], rate)
        return await self._client.audio.transcriptions.create(
            file=(f"window-{first}.wav", payload, "audio/wav"),
            model=model_name,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            # Omitted rather than sent as `""`: an empty string is a value
            # the endpoint would have to interpret, while "detect it" and
            # "no vocabulary to offer" are expressed by the parameter being
            # absent from the request at all.
            language=language if language else omit,
            prompt=initial_prompt if initial_prompt else omit,
            # `condition_on_previous_text=False` is not expressible over
            # HTTP and does not need to be: each window is a separate
            # request carrying the guild's configured prompt and nothing
            # else, and no returned text is ever fed back, so the cascade it
            # guards against is structurally impossible rather than switched
            # off.
            temperature=0,
        )


#: Whisper reports a language as an English name; every language in this
#: codebase is an ISO-639-1 code (`transcription_language`,
#: `session_participant.detected_language`, `DETECT_LANGUAGE`). The table is
#: the Whisper language set, and it was checked entry by entry against
#: `faster_whisper.tokenizer._LANGUAGE_CODES` while that library was still
#: installed: 100 names, 100 distinct codes, every one of them a code that
#: library accepted, no gaps in either direction.
_LANGUAGE_CODES: Final[dict[str, str]] = {
    "english": "en", "chinese": "zh", "german": "de", "spanish": "es", "russian": "ru",
    "korean": "ko", "french": "fr", "japanese": "ja", "portuguese": "pt", "turkish": "tr",
    "polish": "pl", "catalan": "ca", "dutch": "nl", "arabic": "ar", "swedish": "sv",
    "italian": "it", "indonesian": "id", "hindi": "hi", "finnish": "fi", "vietnamese": "vi",
    "hebrew": "he", "ukrainian": "uk", "greek": "el", "malay": "ms", "czech": "cs",
    "romanian": "ro", "danish": "da", "hungarian": "hu", "tamil": "ta", "norwegian": "no",
    "thai": "th", "urdu": "ur", "croatian": "hr", "bulgarian": "bg", "lithuanian": "lt",
    "latin": "la", "maori": "mi", "malayalam": "ml", "welsh": "cy", "slovak": "sk",
    "telugu": "te", "persian": "fa", "latvian": "lv", "bengali": "bn", "serbian": "sr",
    "azerbaijani": "az", "slovenian": "sl", "kannada": "kn", "estonian": "et",
    "macedonian": "mk", "breton": "br", "basque": "eu", "icelandic": "is", "armenian": "hy",
    "nepali": "ne", "mongolian": "mn", "bosnian": "bs", "kazakh": "kk", "albanian": "sq",
    "swahili": "sw", "galician": "gl", "marathi": "mr", "punjabi": "pa", "sinhala": "si",
    "khmer": "km", "shona": "sn", "yoruba": "yo", "somali": "so", "afrikaans": "af",
    "occitan": "oc", "georgian": "ka", "belarusian": "be", "tajik": "tg", "sindhi": "sd",
    "gujarati": "gu", "amharic": "am", "yiddish": "yi", "lao": "lo", "uzbek": "uz",
    "faroese": "fo", "haitian creole": "ht", "pashto": "ps", "turkmen": "tk", "nynorsk": "nn",
    "maltese": "mt", "sanskrit": "sa", "luxembourgish": "lb", "myanmar": "my",
    "tibetan": "bo", "tagalog": "tl", "malagasy": "mg", "assamese": "as", "tatar": "tt",
    "hawaiian": "haw", "lingala": "ln", "hausa": "ha", "bashkir": "ba", "javanese": "jw",
    "sundanese": "su", "cantonese": "yue",
}  # fmt: skip

_KNOWN_CODES: Final[frozenset[str]] = frozenset(_LANGUAGE_CODES.values())


def _language_code(reported: str | None, default: str) -> str:
    """What the endpoint said, as a code this codebase can store.

    Codes pass through untouched, names are translated, and **anything else
    becomes the configured default rather than failing the job**. A
    transcript in hand with a language nobody could name is worth more than
    no transcript, and the guild's configured default is the better guess
    than an empty column: every guild this serves meets in one language.
    """
    if not reported:
        return default
    spelling = reported.strip().lower()
    if spelling in _KNOWN_CODES:
        return spelling
    return _LANGUAGE_CODES.get(spelling, default)
