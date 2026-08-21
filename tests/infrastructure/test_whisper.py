import logging
import wave
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from faster_whisper import WhisperModel  # type: ignore[import-untyped]
from faster_whisper.feature_extractor import (  # type: ignore[import-untyped]
    FeatureExtractor,
)
from faster_whisper.transcribe import (  # type: ignore[import-untyped]
    TranscriptionOptions,
)

from sturnus.infrastructure.whisper import WhisperEngine, _on_the_frame_grid

FIXTURE = Path(__file__).parent.parent / "fixtures" / "hello.wav"

#: faster-whisper counts seek positions in log-mel frames, 16 kHz over a
#: 160-sample hop. The fakes below report seeks on that grid because a real
#: `Segment` does; `test_a_window_never_spans_two_clips` checks this number
#: against the library's own `frames_per_second` rather than leaving two
#: hundreds to agree by luck.
_FRAMES_PER_SECOND = 100


@pytest.fixture(scope="module")
def engine() -> WhisperEngine:
    # `tiny` keeps the test fast; production uses large-v3-turbo (Spec 7).
    return WhisperEngine(
        model_size="tiny", device="cpu", compute_type="int8", default_language="de"
    )


@pytest.mark.slow
async def test_transcribes_real_speech(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language="de", initial_prompt=None)
    assert result.segments
    assert any(segment.text.strip() for segment in result.segments)


@pytest.mark.slow
async def test_offsets_are_within_the_recording(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language="de", initial_prompt=None)
    for segment in result.segments:
        assert 0.0 <= segment.start <= segment.end


@pytest.mark.slow
async def test_detection_reports_a_language(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language=None, initial_prompt=None)
    assert result.language


@pytest.mark.slow
async def test_silence_yields_no_segments(engine: WhisperEngine, tmp_path: Path) -> None:
    """A participant who never speaks must not produce hallucinated text.

    Whisper is known to invent text for silent input; the amplitude gate in
    `sturnus.infrastructure.speech_gate` is what prevents it — it finds no
    clips here, so the model is never called — and this test is what proves
    the gate is wired in.
    """
    import wave

    silent = tmp_path / "silence.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00" * 16_000 * 3)

    result = await engine.transcribe(silent, language="de", initial_prompt=None)
    assert [s for s in result.segments if s.text.strip()] == []


class _FakeSegment:
    """The four attributes `_transcribe` reads off a faster-whisper segment.

    `seek` is the load-bearing one and the reason this fake is not just three
    floats: it is the first log-mel frame of the encoder window the segment
    was decoded from, and it is what says which clip the segment belongs to.
    A fake that carried only times could not tell an engine that reads it
    apart from one that guesses the clip from the times -- which is exactly
    the defect `test_a_segment_that_overruns_its_clip_does_not_jump_the_gap`
    exists for.
    """

    def __init__(self, seek: int, start: float, end: float, text: str) -> None:
        self.seek = seek
        self.start = start
        self.end = end
        self.text = text


def _as_decoded_from(window_start: float, start: float, end: float, text: str) -> _FakeSegment:
    """A segment reported from the encoder window beginning at `window_start`.

    faster-whisper yields `Segment(seek=previous_seek, ...)` with
    `previous_seek` the frame the window started at, so every fake segment
    here has to name its window in the frames the library counts in. Writing
    it as "which window did this come out of" rather than as a raw number is
    what keeps these tests readable once the point is that the times a
    segment reports are *not* what decides where it lands.
    """
    return _FakeSegment(
        seek=round(window_start * _FRAMES_PER_SECOND), start=start, end=end, text=text
    )


class _FakeInfo:
    def __init__(self, language: str | None) -> None:
        self.language = language


class _RecordingModel:
    """Stands in for `WhisperModel`, recording what it is handed.

    The interesting decisions in `_transcribe` are all decisions about the
    *arguments*: which audio the model sees, which clips, and whether it is
    called at all. A fake that records them tests those decisions without
    downloading a model, which is what keeps these tests out of the `slow`
    marker while the real-inference tests above stay in it.
    """

    def __init__(
        self,
        segments: tuple[_FakeSegment, ...] = (),
        language: str | None = "de",
        segments_from: Callable[[list[float]], tuple[_FakeSegment, ...]] | None = None,
    ) -> None:
        self.segments = segments
        self.language = language
        #: Builds the segments from the `clip_timestamps` the fake was handed,
        #: for tests about *where* a segment ends up. A real model can only
        #: speak in the coordinates it was given, and since the engine now
        #: hands over concatenated speech, those coordinates are a timeline
        #: the test cannot know until the engine has computed it. Hardcoding
        #: a guess at it would be a test that pins the arithmetic against
        #: itself; asking for the middle of whatever clip the engine declared
        #: pins it against the audio the test actually built.
        self.segments_from = segments_from
        #: What `WhisperModel` publishes as the resolution of the seek grid.
        #: `_transcribe` reads it to put its own clip boundaries on that grid,
        #: so a fake that omitted it would not be exercising the conversion
        #: at all.
        self.frames_per_second = _FRAMES_PER_SECOND
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, _FakeInfo]:
        self.calls.append({"audio": audio, **kwargs})
        segments = self.segments
        if self.segments_from is not None:
            segments = self.segments_from(kwargs["clip_timestamps"])
        # faster-whisper returns a generator, so a caller that forgot to
        # consume it would see no segments; returning an iterator keeps the
        # fake honest about that.
        return iter(segments), _FakeInfo(self.language)


def _engine_with(model: _RecordingModel, default_language: str = "de") -> WhisperEngine:
    """Builds a `WhisperEngine` around a fake model without loading a real one.

    `WhisperEngine.__init__` constructs a `WhisperModel` eagerly, which
    downloads weights from HuggingFace — the thing the `slow` marker exists
    to keep off pull requests. Bypassing `__init__` is deliberate: the
    alternative would be a constructor parameter that exists only for tests
    and that production code would never pass, which is a worse trade than
    one hand-rolled fake in the same spirit as
    `tests/infrastructure/discord/test_config_commands.py`.
    """
    engine = object.__new__(WhisperEngine)
    engine._model = model
    engine._default_language = default_language
    return engine


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes((np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())


def _tone(seconds: float, amplitude: float = 0.2) -> np.ndarray:
    samples = round(seconds * 16_000)
    t = np.arange(samples, dtype=np.float32) / 16_000
    return (amplitude * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)


def _track_with_speech_at(*spoken_at: float, total: float) -> np.ndarray:
    """A padded track holding a two-second tone starting at each of `spoken_at`.

    The gaps are bit-exact zero, which is what `SpeakerWriter` writes between
    Discord packets and the only thing the gate is aimed at. Building the
    track from times the test names is what makes the timestamp assertions
    below independent: every second in them comes from this function, not
    from the code under test.
    """
    track = np.zeros(round(total * 16_000), dtype=np.float32)
    for start in spoken_at:
        tone = _tone(2.0)
        first = round(start * 16_000)
        track[first : first + tone.shape[0]] = tone
    return track


def _holds_a_silent_second(audio: np.ndarray) -> bool:
    """Whether a whole second of `audio` is bit-exact zero, as padding is.

    Checked instead of trusting a length, because it is the property that
    matters: the feature extractor allocates log-mel frames for whatever it
    is handed, and a second of digital zero in there is a second of frames
    computed for audio that was already known to be silence.
    """
    if audio.shape[0] < 16_000:
        return False
    silent = np.cumsum(np.concatenate(([0], (audio == 0.0).astype(np.int64))))
    return bool((silent[16_000:] - silent[:-16_000] == 16_000).any())


async def test_a_file_with_only_padding_never_reaches_the_model(tmp_path: Path) -> None:
    """The short circuit, which is the single most important line in the change.

    `WhisperModel.transcribe` treats an empty `clip_timestamps` list as
    "transcribe everything": `generate_segments` appends 0, then appends
    `content_frames`, yielding one clip spanning the whole file. So calling
    the model with no clips would push the entire padded recording through
    the decoder — the opposite of what the gate is for, on precisely the
    silent files that are most likely to hallucinate. The only safe thing to
    do with an empty clip list is not to call the model.
    """
    silent = tmp_path / "padding.wav"
    _write_wav(silent, np.zeros(16_000 * 3, dtype=np.float32))
    model = _RecordingModel()
    engine = _engine_with(model)

    result = await engine.transcribe(silent, language="de", initial_prompt=None)

    assert model.calls == []
    assert result.segments == ()
    assert result.language == "de"


async def test_the_model_is_handed_the_speech_and_not_the_padded_track(
    tmp_path: Path,
) -> None:
    """The array reaching the model is the concatenated speech, nothing else.

    0.5.0 handed over the whole decoded file and marked the speech with
    `clip_timestamps`. That fixed the transcripts and cost the memory:
    faster-whisper shrinks the array only in its `vad_filter` branch
    (`if vad_filter and clip_timestamps == "0"`, then
    `audio = np.concatenate(audio_chunks)`), and setting clips is precisely
    what skips that branch, so `FeatureExtractor` ran over the padding too --
    4.94 GB of log-mel frames for a 100-minute track, against 1.99 GB for the
    41 minutes of speech in it. The worker's limit had to go from 6Gi to 12Gi
    in production because of this line.

    Asserted three ways, because handing the whole array over again is
    invisible in a transcript: the array is far shorter than the file, it
    holds none of the padding that was cut out, and its length is exactly the
    duration `clip_timestamps` now describes.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(
        recording,
        np.concatenate(
            [
                np.zeros(16_000 * 2, dtype=np.float32),
                _tone(2.0),
                np.zeros(16_000 * 6, dtype=np.float32),
            ]
        ),
    )
    model = _RecordingModel()
    engine = _engine_with(model)

    await engine.transcribe(recording, language="de", initial_prompt=None)

    assert len(model.calls) == 1
    call = model.calls[0]
    audio = call["audio"]
    assert isinstance(audio, np.ndarray)
    # Two of the file's ten seconds are the tone, and the gate widens a clip
    # by a fraction of a second at each end. Ten seconds' worth of samples
    # here is the whole file again.
    assert 16_000 * 2 <= audio.shape[0] <= 16_000 * 4
    assert not _holds_a_silent_second(audio)

    clips = call["clip_timestamps"]
    # Still the flat [start, end, ...] list of seconds faster-whisper wants,
    # but on the concatenated timeline: it begins at 0.0 and ends at the
    # length of the array handed over. Leaving it in original-file seconds
    # would point the seek loop past the end of a two-and-a-half-second
    # array, and every clip after the first would decode nothing.
    assert [type(value) for value in clips] == [float, float]
    assert clips[0] == 0.0
    assert clips[1] == pytest.approx(audio.shape[0] / 16_000, abs=1e-9)


async def test_the_clip_list_is_the_concatenated_timelines_own_boundaries(
    tmp_path: Path,
) -> None:
    """Three utterances become three adjacent clips, and the joins are marked.

    Marking them is what keeps this change safe. `segment_size = min(
    nb_max_frames, content_frames - seek, seek_clip_end - seek)` caps an
    encoder window at the clip it started in, so no window is ever shown
    audio from two utterances at once and no decoded segment can span a join.
    Dropping the boundaries -- passing plain concatenated audio, which is
    what the library's own `vad_filter` path does -- was measured emitting
    one 258-second segment covering four utterances spoken minutes apart.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(10.0, 60.0, 150.0, total=200.0))
    model = _RecordingModel()
    engine = _engine_with(model)

    await engine.transcribe(recording, language="de", initial_prompt=None)

    call = model.calls[0]
    clips = call["clip_timestamps"]
    assert len(clips) == 6
    starts, ends = clips[0::2], clips[1::2]
    assert starts[0] == 0.0
    # Adjacent, exactly: clip n+1 begins where clip n ends, because in the
    # array handed over there is nothing between them any more.
    assert starts[1:] == ends[:-1]
    assert ends[-1] == pytest.approx(call["audio"].shape[0] / 16_000, abs=1e-9)
    # Each clip is the two-second tone plus a little hangover, and none of
    # them carries the fifty- and ninety-second gaps that separated them.
    assert all(2.0 <= end - start <= 3.0 for start, end in zip(starts, ends, strict=True))


def _a_model_without_weights() -> Any:
    """A real `WhisperModel` with everything the seek loop reads and no weights.

    The property the test below observes belongs to faster-whisper, not to
    us, so a fake model cannot observe it -- a fake breaks the way its author
    imagined, and the failure that matters is the one nobody imagined. What
    makes it observable without a HuggingFace download is that
    `generate_segments` computes the whole window schedule itself and only
    hands the *result* to the encoder: stub `encode`, `get_prompt` and
    `generate_with_fallback` and the seek loop, the clip walk and
    `segment_size` are all still the library's own code, running for real.

    Constructed through `object.__new__` because `WhisperModel.__init__`
    loads CTranslate2 weights. The attributes below are the ones the loop
    reads off `self`; if a release adds another, this raises `AttributeError`
    here, which is the loud failure this whole test exists to produce.
    """
    model = object.__new__(WhisperModel)
    model.feature_extractor = FeatureExtractor()
    model.frames_per_second = (
        model.feature_extractor.sampling_rate // model.feature_extractor.hop_length
    )
    model.time_precision = 0.02
    model.input_stride = 2
    model.logger = logging.getLogger("sturnus.tests.faster_whisper")
    model.encode = lambda *_args, **_kwargs: None
    model.get_prompt = lambda *_args, **_kwargs: []
    # No tokens at all, so `_split_segments_by_timestamps` takes its
    # no-timestamps branch and reports the window itself: `start` is the
    # window's `time_offset` and `end` is `time_offset + segment_duration`.
    # That is what makes each emitted segment a readable record of one
    # window, which is the thing under observation.
    model.generate_with_fallback = lambda *_args, **_kwargs: (
        SimpleNamespace(sequences_ids=[[]], no_speech_prob=0.0),
        0.0,
        0.0,
        1.0,
    )
    return model


def _decoding_options(clip_timestamps: list[float]) -> Any:
    """`TranscriptionOptions` with the fields the seek loop reads, and nothing else.

    Built from `dataclasses.fields` rather than by listing every argument, so
    an option added upstream defaults to `None` here instead of breaking a
    test that has no opinion about it. The names this test *does* have an
    opinion about are checked against the real field list, so a rename is an
    immediate failure rather than a silently ignored keyword.
    """
    chosen: dict[str, Any] = {
        "clip_timestamps": clip_timestamps,
        "multilingual": False,
        "without_timestamps": False,
        "prefix": None,
        "hotwords": None,
        "initial_prompt": None,
        "word_timestamps": False,
        "condition_on_previous_text": False,
        "prompt_reset_on_temperature": 0.5,
        # `None` skips the no-speech fast-forward, which would otherwise
        # decide what to do with a stubbed decoder's made-up probability.
        "no_speech_threshold": None,
        "log_prob_threshold": None,
    }
    names = {field.name for field in fields(TranscriptionOptions)}
    assert not set(chosen) - names, "faster-whisper renamed a decoding option"
    return TranscriptionOptions(**{name: chosen.get(name) for name in names})


def test_a_window_never_spans_two_clips() -> None:
    """The library behaviour this whole design rests on, observed on the library.

    `WhisperEngine` hands the model concatenated speech, so two utterances an
    hour apart are adjacent samples in the array. The only thing keeping an
    encoder window from covering both is `segment_size = min(nb_max_frames,
    content_frames - seek, seek_clip_end - seek)` -- an undocumented internal
    of `generate_segments`, in a loop that carries the library's own note
    that it should be rewritten as a nested loop, in a dependency pinned
    `faster-whisper>=1.1` with no upper bound and merged by Renovate on a
    green build. Nothing else in this suite would notice it going away: the
    clips would still be passed, the transcript would still be produced, and
    the timestamps would be quietly wrong by minutes. This test is that
    upper bound.

    It pins the three properties `_on_the_original_timeline` names, all
    against windows the real loop scheduled:

    * a segment's `seek` **is** the first frame of its window, since `start`
      is the window's `time_offset` and nothing else;
    * every window lies inside one clip, and the windows of a clip tile it
      exactly -- from its first frame to its last, with no gap and no
      overlap;
    * the frames a clip occupies are the ones `_on_the_frame_grid` computes,
      which is what makes attributing by `seek` land in the right clip. The
      clip boundaries here are deliberately off the 10 ms grid (2.507 s
      rounds *up* to frame 251, 5.117 s to 512), so a library that truncated
      where it now rounds would start its windows one frame early and the
      tiling would not close.

    A clip longer than the 30 s window is included so both terms of the
    `min` are exercised; `widest` asserts it really was split.
    """
    model = _a_model_without_weights()
    assert model.frames_per_second == _FRAMES_PER_SECOND

    clip_timestamps = [0.0, 2.507, 2.507, 5.117, 5.117, 41.048]
    # 80 mel bins by `content_frames + 1`, which is how `generate_segments`
    # reads a length. 45 seconds of them, comfortably past the last clip, so
    # `content_frames - seek` never binds and the clip cap is the term under
    # observation. The values are never looked at: `encode` is stubbed.
    features = np.zeros((80, 4_500 + 1), dtype=np.float32)
    tokenizer = SimpleNamespace(timestamp_begin=50_364, decode=lambda *_args: " ja")

    decoded = list(
        model.generate_segments(features, tokenizer, _decoding_options(clip_timestamps), False)
    )

    per_frame = model.feature_extractor.time_per_frame
    windows = []
    for segment in decoded:
        assert segment.start == pytest.approx(segment.seek * per_frame, abs=1e-9)
        windows.append((segment.seek, round(segment.end / per_frame)))

    boundaries = _on_the_frame_grid(clip_timestamps, model.frames_per_second)
    covered, widest = 0, 0
    for clip_start, clip_end in zip(boundaries[0::2], boundaries[1::2], strict=True):
        inside = [window for window in windows if clip_start <= window[0] < clip_end]
        assert inside, "the seek loop decoded nothing at all in a clip it was given"
        cursor = clip_start
        for window_start, window_end in inside:
            assert window_start == cursor
            assert window_end <= clip_end
            assert window_end - window_start <= model.feature_extractor.nb_max_frames
            cursor = window_end
        assert cursor == clip_end
        covered += len(inside)
        widest = max(widest, len(inside))
    # No window belonged to no clip, and the 30 s cap was reached at least
    # once -- without which the last clip would prove nothing the first two
    # do not.
    assert covered == len(windows)
    assert widest > 1


async def test_silero_is_never_asked(tmp_path: Path) -> None:
    """`vad_filter` must be off, explicitly.

    Setting `clip_timestamps` already makes it inert — faster-whisper's guard
    is `if vad_filter and clip_timestamps == "0"` — but a reader should not
    have to know that rule to see that Silero is not involved, and a future
    edit that drops `clip_timestamps` must not silently re-enable the model
    that destroyed every transcript this project ever produced.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(model := _RecordingModel())

    await engine.transcribe(recording, language="de", initial_prompt=None)

    assert model.calls[0]["vad_filter"] is False


async def test_the_decoder_side_hallucination_guards_stay_set(tmp_path: Path) -> None:
    """The gate is not a speech detector, so the second line of defence still matters.

    An amplitude test admits hum, keyboard clatter and cross-talk that Silero
    would have excluded. That is decode time wasted rather than a correctness
    bug only because these two thresholds keep filtering the decoder's own
    output afterwards (Spec 7).
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(model := _RecordingModel())

    await engine.transcribe(recording, language="de", initial_prompt=None)

    assert model.calls[0]["compression_ratio_threshold"] == 2.4
    assert model.calls[0]["no_speech_threshold"] == 0.6


def _gate_clips(recording: Path) -> tuple[tuple[float, float], ...]:
    """What the gate finds in `recording`, as the engine itself would ask it.

    The clips are an *input* to the arithmetic under test, not something that
    arithmetic defines, so recomputing an expected answer from them checks
    the offset addition rather than restating it. Asking the gate here rather
    than hardcoding 9.74 also keeps these tests out of the way of the branch
    that is changing the gate's own constants.
    """
    from faster_whisper.audio import decode_audio  # type: ignore[import-untyped]

    from sturnus.infrastructure.speech_gate import speech_clips

    return speech_clips(decode_audio(str(recording), sampling_rate=16_000))


def _assert_the_model_spoke_in_its_own_coordinates(
    call: dict[str, Any], file_seconds: float
) -> None:
    """The premise every timestamp assertion in this file rests on.

    Mapping a segment back is only meaningful if the model was speaking in
    some other timeline to begin with. 0.5.0 handed over the whole padded
    file, so its offsets were already file-relative and *every* "the times
    are right" assertion below is satisfied by doing no arithmetic at all.
    Each such test therefore states the premise instead of inheriting it from
    the test that pins the array, which a future edit could delete on its own.

    A quarter of the file is a deliberately loose ceiling -- the tracks here
    are under a twentieth speech -- because the point is to exclude the whole
    file, not to re-pin what the gate found.
    """
    audio = call["audio"]
    assert audio.shape[0] < round(file_seconds * 16_000) // 4


def _middle_of_each_clip(clips: list[float]) -> tuple[_FakeSegment, ...]:
    """One segment in the middle half of every clip the model was handed.

    Each comes out of the window that opens the clip it belongs to, which is
    what a real decoder reports for any clip shorter than the 30 s window --
    every clip in these tests.
    """
    return tuple(
        _as_decoded_from(
            start,
            start=start + (end - start) * 0.25,
            end=start + (end - start) * 0.75,
            text=f" utterance {index}",
        )
        for index, (start, end) in enumerate(zip(clips[0::2], clips[1::2], strict=True))
    )


async def test_segment_times_come_back_on_the_recordings_own_timeline(
    tmp_path: Path,
) -> None:
    """Three utterances minutes apart, each reported where it was spoken.

    This is the assertion the change stands on. The model is handed a few
    seconds of concatenated speech, so the times it returns are positions in
    *that* array -- the utterance spoken at 02:30 comes back at about five
    seconds. `sturnus.application.transcription.to_absolute` adds the
    speaker's epoch to whatever is here and `domain.transcript` sorts every
    speaker's segments together on the result, so leaving them unmapped does
    not make a slightly-wrong document: it stacks the whole meeting into its
    first ten seconds and interleaves two speakers into nonsense. A test that
    only checked the text would pass with every timestamp wrong.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(10.0, 60.0, 150.0, total=200.0))
    model = _RecordingModel(segments_from=_middle_of_each_clip)
    engine = _engine_with(model)

    result = await engine.transcribe(recording, language="de", initial_prompt=None)

    _assert_the_model_spoke_in_its_own_coordinates(model.calls[0], file_seconds=200.0)
    assert [s.text for s in result.segments] == [
        " utterance 0",
        " utterance 1",
        " utterance 2",
    ]
    # Each segment covers the middle half of its clip, and each clip is one
    # two-second tone plus hangover, so a correctly mapped segment lies
    # inside the two seconds the test placed that tone in.
    for segment, spoken_at in zip(result.segments, (10.0, 60.0, 150.0), strict=True):
        assert spoken_at <= segment.start < segment.end <= spoken_at + 2.0
    # And the silence between them is still silence: the gaps in the result
    # are the gaps in the recording, not the two-and-a-half seconds they
    # shrank to in the array the model saw.
    assert result.segments[1].start - result.segments[0].start == pytest.approx(50.0, abs=1.0)
    assert result.segments[2].start - result.segments[1].start == pytest.approx(90.0, abs=1.0)


async def test_a_segment_filling_its_clip_round_trips_within_ten_milliseconds(
    tmp_path: Path,
) -> None:
    """The mapping is exact, not approximate, and the tolerance says so.

    The only error the design admits is faster-whisper rounding each clip
    boundary onto its 10 ms frame grid (`seek_points = round(ts *
    frames_per_second)`); our own arithmetic is a difference of two integer
    sample counts and contributes nothing. Ten milliseconds is half of
    Whisper's own 20 ms timestamp grid and two orders below the "roughly the
    low hundreds of milliseconds" that cross-speaker interleave order needs,
    so it is worth pinning that the bound is this and not "somewhere in the
    right clip".

    The gate is asked for the clips separately here. They are an *input* to
    the arithmetic under test, not a constant it defines -- the engine hands
    the same array to the same function -- so recomputing the expected answer
    from them tests the offset addition rather than restating it.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(30.0, total=60.0))
    ((clip_start, clip_end),) = _gate_clips(recording)

    # Fifty milliseconds in from each end of the one clip, expressed on the
    # concatenated timeline the engine will hand over.
    model = _RecordingModel(
        segments_from=lambda clips: (
            _as_decoded_from(clips[0], clips[0] + 0.05, clips[1] - 0.05, " hallo"),
        )
    )

    result = await _engine_with(model).transcribe(recording, language="de", initial_prompt=None)

    _assert_the_model_spoke_in_its_own_coordinates(model.calls[0], file_seconds=60.0)
    (segment,) = result.segments
    assert segment.start == pytest.approx(clip_start + 0.05, abs=0.01)
    assert segment.end == pytest.approx(clip_end - 0.05, abs=0.01)


async def test_a_segment_from_a_later_window_of_a_long_clip_stays_in_that_clip(
    tmp_path: Path,
) -> None:
    """A clip longer than one encoder window, which every other test here lacks.

    The clips in this file are all a couple of seconds long, so every window
    in them begins exactly where its clip does -- and a mapping that looked
    the seek position up as a clip boundary rather than searching for the
    clip containing it would pass all of them. A speaker who talks for
    three-quarters of a minute without a two-second pause is one clip and
    several windows, and the second of those windows begins nowhere near a
    boundary.
    """
    recording = tmp_path / "speech.wav"
    track = _track_with_speech_at(300.0, total=400.0)
    speaking = _tone(45.0)
    track[round(10.0 * 16_000) : round(10.0 * 16_000) + speaking.shape[0]] = speaking
    _write_wav(recording, track)
    (first_clip_start, first_clip_end), (last_clip_start, _) = _gate_clips(recording)
    assert first_clip_end - first_clip_start > 30.0, "the fixture no longer needs two windows"

    # Half a second into the second 30-second window of the first clip.
    model = _RecordingModel(
        segments_from=lambda clips: (
            _as_decoded_from(clips[0] + 30.0, clips[0] + 30.5, clips[0] + 31.5, " later"),
        )
    )

    result = await _engine_with(model).transcribe(recording, "de", None)

    _assert_the_model_spoke_in_its_own_coordinates(model.calls[0], file_seconds=400.0)
    (segment,) = result.segments
    assert segment.start == pytest.approx(first_clip_start + 30.5, abs=0.02)
    assert segment.end == pytest.approx(first_clip_start + 31.5, abs=0.02)
    # Which is well before the second utterance, four minutes further on.
    assert segment.end < last_clip_start


async def test_a_segment_that_overruns_its_clip_does_not_jump_the_gap(
    tmp_path: Path,
) -> None:
    """The near miss, which is the one way this change fails silently.

    A decoded segment can report an `end` well past the audio its window
    actually held. `_split_segments_by_timestamps` builds it as `end_time =
    time_offset + end_timestamp_position * time_precision` and caps neither
    that nor the seek it derives at the window's content, so the tail segment
    of a clip routinely runs a few tenths of a second into nothing. Resolve
    the clip from the times -- from `end`, or from the midpoint of a segment
    that overruns by more than half its own length -- and the *next* clip's
    offset is added instead, so the text lands on the far side of all the
    removed silence. Measured on the real engine, with this branch's own
    pre-fix code, on the shape of the recording that started all this -- 100
    minutes, 187 clips, 41.2 minutes of speech: 57 of 462 segments reported
    an `end` past their clip, and 5 of those overran by more than half the
    segment, so their midpoints landed in the next clip. They came back 20 to
    31 seconds from where they were spoken, one of them carrying 14 seconds
    of text. The error is always exactly one removed gap, so on a session
    with three utterances forty minutes apart it is forty minutes. It is the
    same silent-wrong-timestamp failure the library's own
    `restore_speech_timestamps` produces, relocated into our code.

    So the clip is not resolved from the times at all: it is read off the
    window the segment came out of. The overrun here is 0.7 s against a clip
    of about 2.5 s, which puts the midpoint past the boundary -- the case a
    rule based on the times cannot survive.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(10.0, 300.0, total=320.0))
    (first_clip_start, first_clip_end), _ = _gate_clips(recording)
    # Out of the window that opens the first clip -- the only window a clip
    # of two and a half seconds has -- but reported ending 0.7 s past that
    # clip, so the midpoint sits 0.2 s beyond the boundary and the far side
    # of the boundary is five minutes of removed silence.
    model = _RecordingModel(
        segments_from=lambda clips: (
            _as_decoded_from(clips[0], clips[1] - 0.3, clips[1] + 0.7, " word"),
        )
    )

    result = await _engine_with(model).transcribe(recording, language="de", initial_prompt=None)

    _assert_the_model_spoke_in_its_own_coordinates(model.calls[0], file_seconds=320.0)
    (segment,) = result.segments
    # Where the first utterance is, not where the second one is: the gap
    # between them is 4.8 minutes, so a wrong clip is off by minutes and not
    # by a tolerance anyone could widen this assertion to absorb.
    assert first_clip_start <= segment.start < segment.end <= first_clip_end
    assert segment.start == pytest.approx(first_clip_end - 0.3, abs=0.01)
    # And it stops at the end of the clip it came from. Those 0.7 s are audio
    # the decoder was never shown -- they are on the far side of the join, in
    # silence that was cut out -- and a segment is a claim about audio that
    # exists.
    assert segment.end == pytest.approx(first_clip_end, abs=0.01)


async def test_a_segment_reported_before_its_clip_starts_is_still_inside_it(
    tmp_path: Path,
) -> None:
    """The other edge of the clip, and the other half of the clamp.

    A clip boundary reaches the seek loop as `round(ts * frames_per_second)`,
    so a boundary that rounds *down* puts the clip's first frame up to 5 ms
    before where this code says the clip begins -- and `start = time_offset +
    start_timestamp_position * time_precision` is free to be exactly that
    frame. Restored without a floor the segment claims audio from before its
    clip, which on this timeline is silence that was cut out and on the
    original one is the tail of a gap. The guarantee is that a segment lies
    inside the clip it was decoded from; this is the edge that needs the
    floor for it to hold.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(10.0, 300.0, total=320.0))
    _, (last_clip_start, last_clip_end) = _gate_clips(recording)
    # The second one lies *entirely* before the clip, which is what a
    # zero-length segment on a boundary that rounded down looks like. It is
    # the only shape in which the restored `end` would come out below the
    # restored `start`, and a segment whose end precedes its start is not
    # something any consumer of `TranscribedSegment` is prepared for.
    model = _RecordingModel(
        segments_from=lambda clips: (
            _as_decoded_from(clips[2], clips[2] - 0.005, clips[2] + 0.5, " hallo"),
            _as_decoded_from(clips[2], clips[2] - 0.005, clips[2] - 0.002, " hm"),
        )
    )

    result = await _engine_with(model).transcribe(recording, "de", None)

    _assert_the_model_spoke_in_its_own_coordinates(model.calls[0], file_seconds=320.0)
    straddling, wholly_before = result.segments
    assert last_clip_start <= straddling.start < straddling.end <= last_clip_end
    assert straddling.end == pytest.approx(last_clip_start + 0.5, abs=0.01)
    assert last_clip_start <= wholly_before.start <= wholly_before.end <= last_clip_end


async def test_a_segment_reported_at_the_very_end_lands_in_the_last_clip(
    tmp_path: Path,
) -> None:
    """A segment with no duration, sitting exactly on the final boundary.

    The decoder can emit one at the tail of a clip, and it is the shape that
    breaks every rule based on the times a segment reports: `start`, `end`
    and their midpoint all sit *on* a boundary, so a boundary search answers
    "the clip after this one" -- which past the last clip is an `IndexError`
    in a worker thread, failing a job that had already been transcribed. Read
    off the window instead, it is simply the last clip, and the clamp keeps
    the zero-length segment inside it.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(10.0, 300.0, total=320.0))
    _, (last_clip_start, last_clip_end) = _gate_clips(recording)
    model = _RecordingModel(
        segments_from=lambda clips: (_as_decoded_from(clips[-2], clips[-1], clips[-1], " ja"),)
    )

    result = await _engine_with(model).transcribe(recording, "de", None)

    (segment,) = result.segments
    # In the second utterance's clip, five minutes into the recording, not
    # in the first one and not at second zero of the concatenated array.
    assert last_clip_start <= segment.start <= segment.end <= last_clip_end


async def test_a_clip_list_that_breaks_the_gates_promise_stops_the_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three properties every line of the mapping is built on, asserted.

    `speech_clips` promises clips inside the array it was handed, ascending,
    non-overlapping, and none shorter than about 0.37 s. All three are
    load-bearing here and none of the failures is visible in a transcript: a
    clip past the end of the array slices short, so every offset after it is
    wrong by the difference; out-of-order clips make the offsets decrease, so
    segments come back in the wrong order and the protocol interleaves two
    speakers wrongly; a clip that does not end after it starts contributes a
    length of zero or less to the cumulative sum that *is* the concatenated
    timeline, so the boundaries stop increasing and both the clip list handed
    to the library and the sorted list a segment is attributed against turn
    to nonsense. The constants those promises rest on live in another module,
    so the check belongs here, where the breakage would otherwise surface.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, _track_with_speech_at(10.0, 60.0, total=80.0))

    def _clips(source: tuple[tuple[float, float], ...]) -> Callable[..., Any]:
        return lambda _audio, **_kwargs: source

    monkeypatch.setattr(
        "sturnus.infrastructure.whisper.speech_clips", _clips(((30.0, 32.0), (10.0, 12.0)))
    )
    with pytest.raises(AssertionError):
        await _engine_with(_RecordingModel()).transcribe(recording, "de", None)

    # Shorter than one sample at 16 kHz, so both bounds round to the same
    # integer: a clip of no length at all, and a boundary pair with no audio
    # behind it.
    monkeypatch.setattr("sturnus.infrastructure.whisper.speech_clips", _clips(((10.0, 10.00001),)))
    with pytest.raises(AssertionError):
        await _engine_with(_RecordingModel()).transcribe(recording, "de", None)

    # Past the end of an eighty-second file. numpy slices short rather than
    # raising, so this is the failure that would otherwise reach production
    # as nothing worse-looking than timestamps drifting after the first clip.
    monkeypatch.setattr("sturnus.infrastructure.whisper.speech_clips", _clips(((10.0, 120.0),)))
    with pytest.raises(AssertionError):
        await _engine_with(_RecordingModel()).transcribe(recording, "de", None)


async def test_an_undetected_language_falls_back_to_the_default(tmp_path: Path) -> None:
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(_RecordingModel(language=None), default_language="de")

    result = await engine.transcribe(recording, language=None, initial_prompt=None)

    assert result.language == "de"


# --- Argument-level tests ported from the transcription-quality branch ---
#
# They came with their own fake, `_ModelSpy`, which patches the name the
# module imported and so records the *constructor* arguments as well as the
# transcribe ones. `_RecordingModel` above cannot: it is handed to an engine
# built by `object.__new__`, which never runs `__init__`. The two coexist
# because they answer different questions, not because either is redundant.


class _ModelSpy:
    """Stands in for `faster_whisper.WhisperModel`, as both class and instance.

    Patched over the name `sturnus.infrastructure.whisper` imported, so
    calling it is the construction `WhisperEngine.__init__` performs and
    the object it hands back is this same spy -- which keeps the recorded
    constructor arguments and the recorded `transcribe` arguments in one
    place a test can read.
    """

    def __init__(self) -> None:
        self.construction: dict[str, Any] = {}
        self.transcription: dict[str, Any] = {}
        #: What `info.language` reports; `None` stands for detection that
        #: came up empty, which is what the adapter's own default is for.
        self.detected: str | None = "de"
        #: As on `WhisperModel`; `_transcribe` reads it before it calls the
        #: model, so a spy without it never gets as far as recording anything.
        self.frames_per_second = _FRAMES_PER_SECOND

    def __call__(self, model_size: str, **kwargs: Any) -> "_ModelSpy":
        self.construction = {"model_size": model_size, **kwargs}
        return self

    def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, _FakeInfo]:
        self.transcription = {"path": path, **kwargs}
        return iter(()), _FakeInfo(self.detected)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _ModelSpy:
    model = _ModelSpy()
    monkeypatch.setattr("sturnus.infrastructure.whisper.WhisperModel", model)
    return model


def _engine(compute_type: str = "int8_float32", default_language: str = "de") -> WhisperEngine:
    return WhisperEngine(
        model_size="large-v3",
        device="cpu",
        compute_type=compute_type,
        default_language=default_language,
    )


async def test_the_model_is_built_with_the_quantisation_it_was_given(spy: _ModelSpy) -> None:
    """`compute_type` is chosen in `sturnus.entrypoints.worker`, not here.

    It has to arrive at the library unaltered: the adapter has no business
    substituting a quantisation, and a silently-dropped one would degrade
    every transcription while every test still passed.
    """
    _engine()
    assert spy.construction == {
        "model_size": "large-v3",
        "device": "cpu",
        "compute_type": "int8_float32",
    }


async def test_the_vocabulary_prompt_reaches_the_decoder(spy: _ModelSpy) -> None:
    """`initial_prompt` is the only lever on proper nouns Sturnus has.

    Project names -- Ducula, Guira, Minestom -- are exactly what Whisper
    guesses wrong and exactly what a meeting protocol is read for. Losing
    the prompt on the way to the library costs nothing visible and every
    name in the document.
    """
    await _engine().transcribe(FIXTURE, "de", "Ducula, Guira, Minestom.")
    assert spy.transcription["initial_prompt"] == "Ducula, Guira, Minestom."


async def test_no_prompt_is_still_a_call_the_library_understands(spy: _ModelSpy) -> None:
    """A guild may have no vocabulary worth biasing towards; `None` is that."""
    await _engine().transcribe(FIXTURE, "de", None)
    assert spy.transcription["initial_prompt"] is None


async def test_a_segment_never_conditions_the_next_one(spy: _ModelSpy) -> None:
    """faster-whisper defaults `condition_on_previous_text` to `True`.

    That feeds each segment's text back in as the next one's prompt, so a
    single hallucination becomes the context every following segment is
    decoded against and the repetition runs away. `vad_filter` (below)
    makes it worse rather than better here: one speaker's track is cut
    into disconnected fragments with the silence removed, so the
    "previous text" a segment gets conditioned on is frequently from
    minutes earlier and unrelated.
    """
    await _engine().transcribe(FIXTURE, "de", None)
    assert spy.transcription["condition_on_previous_text"] is False


async def test_the_beam_is_wider_than_the_library_default(spy: _ModelSpy) -> None:
    """Costs CPU, which this deployment has, and buys accuracy it does not."""
    await _engine().transcribe(FIXTURE, "de", None)
    assert spy.transcription["beam_size"] == 8


async def test_the_hallucination_guards_are_still_in_place(spy: _ModelSpy) -> None:
    """The parameters that keep silence and repetition out (Spec 7).

    Pinned here because they are invisible in a passing test suite:
    dropping any of them produces a worker that transcribes perfectly well
    in every test and invents speech for a participant who never said a
    word in production.

    `vad_filter` is asserted **False**, which is the reverse of what this
    test said when it was written. Silero was the guard until it was found
    to be the defect: its recurrent state collapses on the bit-exact zero
    padding `SpeakerWriter` writes between packets, and it reported about
    one second of speech in two minutes of a real recording. The silence is
    now cut by `sturnus.infrastructure.speech_gate` before the decoder sees
    it, so the guard is the clip list plus the two thresholds below, and
    turning Silero back on would restore the defect rather than a
    safeguard.
    """
    await _engine().transcribe(FIXTURE, "de", None)
    assert spy.transcription["vad_filter"] is False
    # The gate's own output, in the flat [start, end, start, end, ...] form
    # faster-whisper expects. Asserted for shape rather than for values --
    # what the gate finds in the fixture belongs to the gate's own tests --
    # but asserted non-empty, because an empty list is read by
    # `WhisperModel.transcribe` as "transcribe everything" and would put the
    # padding back in front of the decoder.
    clips = spy.transcription["clip_timestamps"]
    assert clips and len(clips) % 2 == 0
    # On the concatenated timeline, so the first clip starts at 0.0. What the
    # gate found in the fixture belongs to the gate's own tests; that the
    # list is expressed in the coordinates of the array actually handed over
    # is this adapter's decision and is pinned here.
    assert clips[0] == 0.0
    assert spy.transcription["compression_ratio_threshold"] == 2.4
    assert spy.transcription["no_speech_threshold"] == 0.6


async def test_the_pinned_language_reaches_the_library(spy: _ModelSpy) -> None:
    """Passing a language is what stops faster-whisper detecting one at all."""
    await _engine().transcribe(FIXTURE, "de", None)
    assert spy.transcription["language"] == "de"


async def test_detection_is_asked_for_when_no_language_is_pinned(spy: _ModelSpy) -> None:
    await _engine().transcribe(FIXTURE, None, None)
    assert spy.transcription["language"] is None


async def test_a_detection_that_came_up_empty_falls_back_to_the_default(spy: _ModelSpy) -> None:
    """`TranscriptionResult.language` is stored and reused for the whole
    session (`sturnus.application.worker`), so it may never be `None`.
    """
    spy.detected = None
    result = await _engine(default_language="de").transcribe(FIXTURE, None, None)
    assert result.language == "de"


async def test_the_veto_on_the_no_speech_skip_is_closed_explicitly(tmp_path: Path) -> None:
    """`log_prob_threshold=None`, which is what actually catches the credits.

    Everything else in this adapter is set to a number. This one is set to
    `None`, and that is the whole point, so it needs its own test: an argument
    whose value is the absence of a value is exactly the kind that gets tidied
    away by someone who reads it as a leftover.

    What it does. In the sequential path faster-whisper decides silence like
    this (`transcribe.py:1215-1233`)::

        should_skip = result.no_speech_prob > options.no_speech_threshold
        if (options.log_prob_threshold is not None
                and avg_logprob > options.log_prob_threshold):
            should_skip = False

    `log_prob_threshold` is not a floor that rejects low-confidence output --
    on this path it is a *veto on the no-speech skip*. Its -1.0 default, which
    Sturnus used to inherit, therefore switches `no_speech_threshold` off for
    any decode fluent enough to clear -1.0. A subtitle credit is a
    high-probability token sequence -- that is why the model reaches for one
    when it has nothing to transcribe -- so `" Untertitelung des ZDF, 2020"`
    at an `avg_logprob` of about -0.88 disabled the very guard aimed at it.
    Measured through this call path: with the veto in place, 11 of 111
    non-speech inputs came back carrying invented text; with
    `log_prob_threshold=None`, 0 of 111 did.

    That also explains a dead end worth not repeating: lowering
    `no_speech_threshold` to 0.4 was measured to change nothing at all,
    because the veto fires whatever the threshold is.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(model := _RecordingModel())

    await engine.transcribe(recording, language="de", initial_prompt=None)

    assert model.calls[0]["log_prob_threshold"] is None


async def test_a_track_the_decoder_emptied_says_so_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A guard whose cost never shows up anywhere is one nobody can audit.

    This is the shape of the guard firing: the gate found audio above the
    silence floor, the model was called on it, and every decoded window came
    back judged as silence, so the speaker contributes nothing to the
    document. That is the intended outcome for a track of room tone and the
    failure mode for a track of quiet speech, and the two are indistinguishable
    from inside this adapter -- which is precisely why it has to be visible
    from outside it.

    The line carries the gated duration rather than only a count, because that
    is the number that separates the two readings: 0.9 s of room tone dropped
    is the guard working, 40 minutes dropped is an incident.
    """
    recording = tmp_path / "roomtone.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(_RecordingModel(segments=()))

    with caplog.at_level(logging.WARNING, logger="sturnus.infrastructure.whisper"):
        result = await engine.transcribe(recording, language="de", initial_prompt=None)

    assert result.segments == ()
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "roomtone.wav" in message
    # 2.3 s, and none of the other three numbers this file offers. The
    # recording is 3.0 s long and holds a 2.0 s tone; the gate widens that
    # tone by `_HANGOVER_SECONDS` at the end and clamps it at the start,
    # yielding the 2.25 s it actually handed the decoder. So this also fails
    # if the message reports the file length, the tone length, or a count of
    # clips dressed up as seconds.
    assert "2.3 s" in message, message


async def test_a_track_that_produced_text_is_not_reported_as_a_loss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The counterpart, and the reason the level is WARNING and not INFO.

    Most jobs transcribe fine. If the ordinary case warned too, the warning
    would carry no information and would be filtered out by the first person
    who reads the worker's logs -- taking the case that matters with it.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(
        _RecordingModel(segments=(_as_decoded_from(0.0, 0.0, 1.5, " Guten Morgen."),))
    )

    with caplog.at_level(logging.WARNING, logger="sturnus.infrastructure.whisper"):
        result = await engine.transcribe(recording, language="de", initial_prompt=None)

    assert [s.text for s in result.segments] == [" Guten Morgen."]
    assert caplog.records == []


async def test_the_transcribed_text_is_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """What a participant said is the one thing this adapter must not emit.

    The worker's logs are not the protocol and are not access-controlled like
    it: the document goes to a named Outline collection, the logs go wherever
    the cluster ships them. The counting added for auditability must count,
    not quote -- the same rule `infrastructure/documents/outline.py` follows
    when it logs a request without its body.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    secret = " Wir kuendigen den Vertrag mit Beispiel GmbH."
    engine = _engine_with(_RecordingModel(segments=(_as_decoded_from(0.0, 0.0, 1.5, secret),)))

    with caplog.at_level(logging.DEBUG, logger="sturnus.infrastructure.whisper"):
        await engine.transcribe(recording, language="de", initial_prompt=None)

    assert caplog.records, "the debug trace exists at all, so this is not vacuous"
    assert "Vertrag" not in caplog.text
    assert "Beispiel" not in caplog.text
