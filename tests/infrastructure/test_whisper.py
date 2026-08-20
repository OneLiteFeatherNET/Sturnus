from pathlib import Path

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

    result = await engine.transcribe(silent, language="de")
    assert [s for s in result.segments if s.text.strip()] == []
