import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sturnus.infrastructure.whisper import WhisperEngine

FIXTURE = Path(__file__).parent.parent / "fixtures" / "hello.wav"


@pytest.fixture(scope="module")
def engine() -> WhisperEngine:
    # `tiny` keeps the test fast; production uses large-v3-turbo (Spec 7).
    return WhisperEngine(
        model_size="tiny", device="cpu", compute_type="int8", default_language="de"
    )


@pytest.mark.slow
async def test_transcribes_real_speech(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language="de")
    assert result.segments
    assert any(segment.text.strip() for segment in result.segments)


@pytest.mark.slow
async def test_offsets_are_within_the_recording(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language="de")
    for segment in result.segments:
        assert 0.0 <= segment.start <= segment.end


@pytest.mark.slow
async def test_detection_reports_a_language(engine: WhisperEngine) -> None:
    result = await engine.transcribe(FIXTURE, language=None)
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

    result = await engine.transcribe(silent, language="de")
    assert [s for s in result.segments if s.text.strip()] == []


class _FakeSegment:
    """The three attributes `_transcribe` reads off a faster-whisper segment."""

    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


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
        self, segments: tuple[_FakeSegment, ...] = (), language: str | None = "de"
    ) -> None:
        self.segments = segments
        self.language = language
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, _FakeInfo]:
        self.calls.append({"audio": audio, **kwargs})
        # faster-whisper returns a generator, so a caller that forgot to
        # consume it would see no segments; returning an iterator keeps the
        # fake honest about that.
        return iter(self.segments), _FakeInfo(self.language)


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

    result = await engine.transcribe(silent, language="de")

    assert model.calls == []
    assert result.segments == ()
    assert result.language == "de"


async def test_the_model_is_given_the_whole_array_and_the_clips_the_gate_found(
    tmp_path: Path,
) -> None:
    """Decode once, measure that array, and hand the model the same array.

    Passing the path instead would decode a 100-minute file twice and, worse,
    would let the clips be computed on a different copy than the one the
    model seeks through. The array must also be the *whole* file, not the
    speech spliced together: that is what makes the returned offsets absolute
    on the original timeline, so `to_absolute` keeps working untouched.
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

    await engine.transcribe(recording, language="de")

    assert len(model.calls) == 1
    call = model.calls[0]
    audio = call["audio"]
    assert isinstance(audio, np.ndarray)
    assert audio.shape[0] == pytest.approx(16_000 * 10, abs=16_000 * 0.05)

    clips = call["clip_timestamps"]
    assert [type(value) for value in clips] == [float, float]
    start, end = clips
    assert start <= 2.0
    assert end >= 4.0
    assert 0.0 <= start < end <= audio.shape[0] / 16_000


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

    await engine.transcribe(recording, language="de")

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

    await engine.transcribe(recording, language="de")

    assert model.calls[0]["compression_ratio_threshold"] == 2.4
    assert model.calls[0]["no_speech_threshold"] == 0.6


async def test_offsets_are_returned_unchanged(tmp_path: Path) -> None:
    """No offset arithmetic, deliberately.

    Unlike the `vad_filter` path, which concatenates the kept audio and
    repairs the offsets afterwards, the `clip_timestamps` path runs the
    feature extractor over the whole array and makes the seek loop jump
    between clips, so `time_offset = seek * time_per_frame` is already on the
    original timeline. Any remapping added here would shift every timestamp
    in the finished document by the length of the leading padding.
    """
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([np.zeros(16_000 * 5, dtype=np.float32), _tone(2.0)]))
    model = _RecordingModel(segments=(_FakeSegment(5.25, 6.75, " hallo"),))
    engine = _engine_with(model)

    result = await engine.transcribe(recording, language="de")

    assert [(s.start, s.end, s.text) for s in result.segments] == [(5.25, 6.75, " hallo")]


async def test_an_undetected_language_falls_back_to_the_default(tmp_path: Path) -> None:
    recording = tmp_path / "speech.wav"
    _write_wav(recording, np.concatenate([_tone(2.0), np.zeros(16_000, dtype=np.float32)]))
    engine = _engine_with(_RecordingModel(language=None), default_language="de")

    result = await engine.transcribe(recording, language=None)

    assert result.language == "de"
