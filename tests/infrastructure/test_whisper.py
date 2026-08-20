"""The faster-whisper adapter, from two sides.

The `slow` tests at the bottom run real inference and are the only proof
that the adapter produces usable text at all; they download a model, so
they are deselected on pull requests (see `pyproject.toml`'s marker) and
cannot be where a decoding parameter is pinned -- a wrong `beam_size` still
transcribes "hello" perfectly.

Everything above them therefore drives a fake `WhisperModel` and asserts on
what actually reaches the library. Every one of those parameters is a
quality decision that is invisible in the output of a two-second fixture
and expensive in a real meeting, so each is pinned here rather than trusted
to survive a refactor.
"""

from pathlib import Path
from typing import Any

import pytest

from sturnus.infrastructure.whisper import WhisperEngine

FIXTURE = Path(__file__).parent.parent / "fixtures" / "hello.wav"


class _FakeInfo:
    """The half of faster-whisper's `(segments, info)` the adapter reads."""

    def __init__(self, language: str | None) -> None:
        self.language = language


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
    """The three parameters that keep silence and repetition out (Spec 7).

    Pinned here because they are invisible in a passing test suite:
    dropping any of them produces a worker that transcribes perfectly well
    in every test and invents speech for a participant who never said a
    word in production.
    """
    await _engine().transcribe(FIXTURE, "de", None)
    assert spy.transcription["vad_filter"] is True
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


@pytest.fixture(scope="module")
def engine() -> WhisperEngine:
    # `tiny` keeps the test fast; production uses large-v3 (Spec 7).
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

    Whisper is known to invent text for silent input; `vad_filter` is what
    prevents it, and this test is what proves the filter is enabled.
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
