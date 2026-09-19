"""Transcription against an OpenAI-compatible endpoint.

**Against a real HTTP server, not a mocked SDK.** Every test here starts a
socket and points `base_url` at it. Mocking `AsyncOpenAI` would test our
mock's idea of the protocol, and not inventing that idea is the entire
reason the official client was chosen over a hand-rolled request.

The fixtures are built from times the test names -- a tone at each of
`spoken_at`, bit-exact zero everywhere else, which is what
`sturnus.infrastructure.audio.SpeakerWriter` writes between Discord
packets. Every second in an assertion below comes from that construction
rather than from the code under test.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sturnus.domain.transcription_models import UnknownTranscriptionModel
from sturnus.infrastructure import openai_transcription
from sturnus.infrastructure.openai_transcription import (
    OpenAITranscriptionEngine,
    _plan_windows,
    _read_mono_float,
)
from sturnus.observability.events import Event

_RATE = 16_000


# ---------------------------------------------------------------------------
# A recording, built from times the test names
# ---------------------------------------------------------------------------


def _tone(seconds: float, amplitude: float = 0.2) -> np.ndarray[Any, Any]:
    samples = round(seconds * _RATE)
    t = np.arange(samples, dtype=np.float32) / _RATE
    return (amplitude * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)


def _track_with_speech_at(
    *spoken_at: float, total: float, spoken_for: float = 2.0
) -> np.ndarray[Any, Any]:
    track = np.zeros(round(total * _RATE), dtype=np.float32)
    for start in spoken_at:
        first = round(start * _RATE)
        clip = _tone(spoken_for)
        track[first : first + clip.shape[0]] = clip
    return track


def _write_wav(path: Path, samples: np.ndarray[Any, Any], rate: int = _RATE) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
    return path


def _wav_of(tmp_path: Path, samples: np.ndarray[Any, Any], name: str = "track.wav") -> Path:
    return _write_wav(tmp_path / name, samples)


def _seconds_of(wav: bytes) -> float:
    """How long an uploaded window is, read out of the bytes that were sent."""
    import io

    with wave.open(io.BytesIO(wav), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@dataclass
class _Received:
    audio: bytes
    fields: dict[str, str]

    @property
    def seconds(self) -> float:
        return _seconds_of(self.audio)


def _segment(
    index: int,
    start: float,
    end: float,
    text: str,
    *,
    no_speech_prob: float | None = 0.01,
    compression_ratio: float | None = 1.2,
    avg_logprob: float | None = -0.3,
) -> dict[str, Any]:
    """One segment as an endpoint reports it.

    Each confidence figure is omitted from the payload when it is `None`,
    which is how vLLM's `verbose_json` differs from OpenAI's -- the field is
    simply not there, rather than there and null.
    """
    body: dict[str, Any] = {
        "id": index,
        "seek": 0,
        "start": start,
        "end": end,
        "text": text,
        "tokens": [index],
        "temperature": 0.0,
    }
    if no_speech_prob is not None:
        body["no_speech_prob"] = no_speech_prob
    if compression_ratio is not None:
        body["compression_ratio"] = compression_ratio
    if avg_logprob is not None:
        body["avg_logprob"] = avg_logprob
    return body


def _verbose(*segments: dict[str, Any], language: str = "german") -> dict[str, Any]:
    return {
        "task": "transcribe",
        "language": language,
        "duration": max((segment["end"] for segment in segments), default=0.0),
        "text": " ".join(str(segment["text"]).strip() for segment in segments),
        "segments": list(segments),
    }


@dataclass
class _Endpoint:
    """A real server speaking `/audio/transcriptions`.

    `answers` is walked in order, one per request. An `int` is answered as
    that status code, so a 500 is a 500 on the wire rather than an exception
    a double decided to raise; a `float` is a delay before answering, which
    is the only way to make the client's own timeout fire.
    """

    answers: list[Any] = field(default_factory=list)
    received: list[_Received] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def answer_every_request_with(self, body: dict[str, Any]) -> None:
        self.answers = [body] * 64


def _parse_multipart(body: bytes, content_type: str) -> tuple[bytes, dict[str, str]]:
    boundary = content_type.split("boundary=")[1].split(";")[0].strip('"').encode()
    audio = b""
    fields: dict[str, str] = {}
    for part in body.split(b"--" + boundary):
        head, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        name = re.search(rb'name="([^"]+)"', head)
        if name is None:
            continue
        payload = data.rstrip(b"\r\n").removesuffix(b"--")
        if b"filename=" in head:
            audio = payload
        else:
            fields[name.group(1).decode()] = payload.decode()
    return audio, fields


@pytest.fixture
def endpoint() -> Iterator[_Endpoint]:
    state = _Endpoint()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - the stdlib's spelling
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            audio, fields = _parse_multipart(raw, self.headers.get("Content-Type", ""))
            state.received.append(_Received(audio=audio, fields=fields))

            answer = state.answers.pop(0) if state.answers else _verbose()
            if isinstance(answer, int | float) and not isinstance(answer, bool):
                if isinstance(answer, float):
                    threading.Event().wait(answer)
                    answer = _verbose()
                else:
                    self.send_response(answer)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            """Silenced: one line per request on stderr drowns pytest's output."""
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state._server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _engine(
    endpoint: _Endpoint,
    *,
    model: str = "whisper-1",
    default_language: str = "de",
    max_chunk_seconds: float = 720.0,
    timeout_seconds: float = 10.0,
    max_retries: int = 0,
) -> OpenAITranscriptionEngine:
    return OpenAITranscriptionEngine(
        base_url=endpoint.base_url,
        api_key="",
        model=model,
        default_language=default_language,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_chunk_seconds=max_chunk_seconds,
    )


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def test_a_track_is_read_as_mono_float_in_the_gate_s_range(tmp_path: Path) -> None:
    """The one thing added to how the worker already reads these files.

    `sturnus.application.worker` opens them with `wave`; the decoder this
    replaces went through `faster_whisper.audio.decode_audio`, whose only
    relevant act was dividing int16 by full scale to reach the range the
    speech gate is written against. This asserts exactly that division and
    nothing more, so a future reader can see the conversion is not a guess.
    """
    samples = _track_with_speech_at(1.0, total=4.0)
    path = _wav_of(tmp_path, samples)

    audio, rate = _read_mono_float(path)

    with wave.open(str(path), "rb") as handle:
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    assert rate == _RATE
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, raw.astype(np.float32) / 32768.0)
    assert float(np.max(np.abs(audio))) <= 1.0


# ---------------------------------------------------------------------------
# Dividing the track
# ---------------------------------------------------------------------------


def test_every_cut_lands_inside_a_silence_the_gate_reported() -> None:
    """The load-bearing property. See the module docstring for the defect.

    A cut anywhere else means a window whose interior had silence removed,
    and then a returned segment can only be attributed by its reported
    times -- which is the thing measured at 20 to 31 seconds of error.
    """
    clips = ((0.0, 10.0), (30.0, 40.0), (60.0, 70.0), (90.0, 100.0))

    windows = _plan_windows(clips, cap_seconds=45.0)

    assert len(windows) > 1, "the fixture was meant to need dividing"
    for window in windows[:-1]:
        inside_a_clip = any(start < window.end < end for start, end in clips)
        assert not inside_a_clip, f"the cut at {window.end} falls inside speech"
        assert not window.cut_through_speech


def test_no_window_is_longer_than_the_cap() -> None:
    """A window over the endpoint's size limit is a rejected request."""
    clips = tuple((float(n * 20), float(n * 20 + 10)) for n in range(20))

    windows = _plan_windows(clips, cap_seconds=60.0)

    assert windows
    for window in windows:
        assert window.seconds <= 60.0 + 1e-9


async def test_silence_inside_a_window_is_kept_rather_than_compressed(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """The alternative is the defect this whole design avoids.

    Two clips twelve seconds apart inside one window must be *sent* with
    the padding between them intact -- asserted on the bytes the server
    received, because the offset arithmetic is one addition per window
    precisely on the promise that nothing inside one was removed.
    """
    samples = _track_with_speech_at(1.0, 14.0, total=20.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 2.0, " hallo")))

    await _engine(endpoint, max_chunk_seconds=720.0).transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    assert len(endpoint.received) == 1
    assert endpoint.received[0].seconds >= 15.0, "the padding between the clips was dropped"


def test_a_window_with_no_speech_in_it_is_never_sent() -> None:
    """A gap longer than the cap leaves a stretch of pure padding.

    It has to be cut somewhere, and sending it would hand a decoder nothing
    but the zero padding that is the most reliable way to make one invent
    text.
    """
    clips = ((0.0, 5.0), (400.0, 405.0))

    windows = _plan_windows(clips, cap_seconds=100.0)

    assert windows, "everything was dropped"
    for window in windows:
        assert any(start < window.end and end > window.start for start, end in clips)
    # The neighbours survive, with their own speech in them.
    assert windows[0].start == pytest.approx(0.0)
    assert windows[-1].end == pytest.approx(405.0)


def test_a_stretch_with_no_gap_is_cut_at_the_cap() -> None:
    """The one case the silence rule cannot be honoured in.

    A cut word costs one word's accuracy at one boundary; a window over the
    size limit is a rejected request and a failed job. The order of those
    two constraints is the decision, and this pins it.
    """
    clips = ((0.0, 300.0),)

    windows = _plan_windows(clips, cap_seconds=120.0)

    assert [round(window.seconds, 6) for window in windows] == [120.0, 120.0, 60.0]
    assert [window.cut_through_speech for window in windows] == [True, True, False]


async def test_a_cut_through_speech_is_logged(
    tmp_path: Path, endpoint: _Endpoint, caplog: pytest.LogCaptureFixture
) -> None:
    """A transcript with a broken sentence must be explicable from the log."""
    samples = _track_with_speech_at(0.0, total=30.0, spoken_for=30.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 5.0, " hallo")))

    with caplog.at_level(logging.WARNING):
        await _engine(endpoint, max_chunk_seconds=10.0).transcribe(
            _wav_of(tmp_path, samples), "de", None
        )

    assert "no silence to cut at" in caplog.text
    assert len(endpoint.received) >= 2


# ---------------------------------------------------------------------------
# Putting the answers back on the recording's timeline
# ---------------------------------------------------------------------------


async def test_offsets_accumulate_across_windows(tmp_path: Path, endpoint: _Endpoint) -> None:
    """Three windows, each answering in its own coordinates.

    Every window's reply starts at 0.0 as far as the endpoint is concerned;
    what makes a protocol readable is that the third window's first word is
    not reported as having been said in the first minute.
    """
    samples = _track_with_speech_at(1.0, 61.0, 121.0, total=180.0)
    endpoint.answers = [
        _verbose(_segment(0, 0.5, 2.0, " erstes")),
        _verbose(_segment(0, 0.5, 2.0, " zweites")),
        _verbose(_segment(0, 0.5, 2.0, " drittes")),
    ]

    result = await _engine(endpoint, max_chunk_seconds=50.0).transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    assert len(endpoint.received) == 3
    starts = [segment.start for segment in result.segments]
    assert starts == sorted(starts), "the windows came back out of order"
    # Where the boundaries fall is the planner's business -- it cuts in the
    # gaps, which on this fixture is nowhere near a round number. What this
    # asserts is the property that makes a protocol readable: the third
    # window's word is not reported as having been said in the first minute.
    assert starts[0] < 5.0
    assert starts[1] > 30.0
    assert starts[2] > 80.0
    assert [segment.text for segment in result.segments] == [" erstes", " zweites", " drittes"]


async def test_a_segment_overrunning_its_window_is_clamped(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """Routine, and the reason attribution had to be made unnecessary.

    A decoded `end` past the window it came from must not be allowed to
    claim time in the next one -- that is the mechanism that moved text 20
    to 31 seconds away from where it was spoken.
    """
    samples = _track_with_speech_at(1.0, 61.0, total=120.0)
    endpoint.answers = [
        _verbose(_segment(0, 0.5, 900.0, " ueberlang")),
        _verbose(_segment(0, 0.5, 2.0, " zweites")),
    ]

    result = await _engine(endpoint, max_chunk_seconds=50.0).transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    first, second = result.segments
    assert first.end <= second.start, "the overrun swallowed the next window"
    assert first.end <= 50.0


# ---------------------------------------------------------------------------
# Refusing what the model invented
# ---------------------------------------------------------------------------


async def test_the_measured_hallucination_is_dropped_whatever_its_avg_logprob(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """The regression test for the defect this rule exists for.

    `no_speech_prob` 0.7 with `avg_logprob` -0.88 is the measured shape of
    `" Untertitelung des ZDF, 2020"`: an invented line is *fluent*, so its
    log-probability is high, and a rule that consulted it would keep
    exactly the segment it was meant to refuse.
    """
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(
        _verbose(
            _segment(
                0,
                0.0,
                3.0,
                " Untertitelung des ZDF, 2020",
                no_speech_prob=0.7,
                avg_logprob=-0.88,
                compression_ratio=0.69,
            ),
            _segment(1, 3.0, 5.0, " echte worte"),
        )
    )

    result = await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert [segment.text for segment in result.segments] == [" echte worte"]


async def test_a_low_log_probability_alone_keeps_its_segment(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """No rule may read `avg_logprob`, and this is what asserts it.

    A quiet, hesitant, perfectly real sentence has a poor log-probability.
    Dropping it would lose speech, and the figure carries no information
    about invention that the other two do not carry better.
    """
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(
        _verbose(_segment(0, 0.0, 3.0, " genuschelt", avg_logprob=-3.5))
    )

    result = await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert [segment.text for segment in result.segments] == [" genuschelt"]


async def test_a_repetitive_segment_is_dropped_independently(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """The second rule catches a model stuck in a loop.

    Independent of the first: this segment reports a perfectly ordinary
    no-speech probability, and the measurement behind these thresholds is
    explicit that neither rule catches what the other does.
    """
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(
        _verbose(
            _segment(0, 0.0, 3.0, " ja ja ja ja ja", no_speech_prob=0.01, compression_ratio=9.9),
            _segment(1, 3.0, 5.0, " echte worte"),
        )
    )

    result = await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert [segment.text for segment in result.segments] == [" echte worte"]


@pytest.mark.parametrize("omitted", ["no_speech_prob", "compression_ratio"])
async def test_a_response_omitting_a_figure_keeps_its_segments(
    omitted: str, tmp_path: Path, endpoint: _Endpoint
) -> None:
    """vLLM omits `no_speech_prob`, and others will omit other fields.

    A missing figure means the rule that reads it does not apply -- never
    that the segment is dropped, and never that the job fails. A deployment
    against such an endpoint keeps the speech gate and `temperature=0`,
    which `docs/operations.md` states rather than leaving to be discovered.
    """
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 3.0, " hallo", **{omitted: None})))

    result = await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert [segment.text for segment in result.segments] == [" hallo"]


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


async def test_a_language_name_comes_back_as_a_code(tmp_path: Path, endpoint: _Endpoint) -> None:
    """`whisper-1` answers `"german"`; every column here stores `de`."""
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 3.0, " hallo"), language="german"))

    result = await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), None, None)

    assert result.language == "de"


async def test_an_unrecognised_language_falls_back_to_the_default(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """A transcript with an odd language beats no transcript."""
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(
        _verbose(_segment(0, 0.0, 3.0, " hallo"), language="klingon")
    )

    result = await _engine(endpoint, default_language="de").transcribe(
        _wav_of(tmp_path, samples), None, None
    )

    assert result.language == "de"


async def test_the_first_window_s_language_governs_the_rest(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """One speaker's track must not change language halfway through.

    The same pinning `set_detected_language` performs across a session's
    jobs, one level down: a quieter stretch is where a detector guesses
    differently, and the guild has not changed language because of it.
    """
    samples = _track_with_speech_at(1.0, 61.0, total=120.0)
    endpoint.answers = [
        _verbose(_segment(0, 0.0, 2.0, " hallo"), language="german"),
        _verbose(_segment(0, 0.0, 2.0, " weiter"), language="dutch"),
    ]

    result = await _engine(endpoint, max_chunk_seconds=50.0).transcribe(
        _wav_of(tmp_path, samples), None, None
    )

    assert result.language == "de"
    assert endpoint.received[1].fields.get("language") == "de", (
        "the second window was left to detect its own language"
    )


async def test_a_pinned_language_is_passed_through_untouched(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """A guild that configured a language has decided; nothing overrides it."""
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(
        _verbose(_segment(0, 0.0, 3.0, " hallo"), language="english")
    )

    result = await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert result.language == "de"
    assert endpoint.received[0].fields.get("language") == "de"


# ---------------------------------------------------------------------------
# What is never sent at all
# ---------------------------------------------------------------------------


async def test_a_track_the_gate_finds_nothing_in_makes_no_request(
    tmp_path: Path, endpoint: _Endpoint, caplog: pytest.LogCaptureFixture
) -> None:
    """A silent participant must produce no segments, and cost nothing.

    Pushing a whole recording of padding through a decoder is both slow and
    the worst case for invention, and it is exactly what a track of nothing
    but packet padding is.
    """
    silent = _wav_of(tmp_path, np.zeros(round(30.0 * _RATE), dtype=np.float32))

    with caplog.at_level(logging.INFO):
        result = await _engine(endpoint).transcribe(silent, "de", None)

    assert result.segments == ()
    assert endpoint.received == [], "silence was uploaded"
    assert any(
        getattr(line, "sturnus_event", None) == str(Event.TRANSCRIPTION_SKIPPED)
        for line in caplog.records
    )


async def test_a_model_outside_the_registry_is_refused_before_any_upload(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """Before a sample is read, let alone a hundred minutes of it uploaded.

    The refusal is here as well as at the re-queue boundary because a row
    can be written by hand, and an operator reading the job's `error`
    column deserves this message rather than whatever the endpoint says
    about a model it does not serve.
    """
    samples = _track_with_speech_at(1.0, total=10.0)

    with pytest.raises(UnknownTranscriptionModel):
        await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None, model="large-v3")

    assert endpoint.received == []


async def test_the_worker_s_own_configured_model_is_always_allowed(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """An operator who pointed this deployment at a privately named model
    has said so deliberately, and a worker refusing its own configuration
    would be refusing the only thing it was told."""
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 2.0, " hallo")))

    result = await _engine(endpoint, model="a-private-conversion").transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    assert result.segments
    assert endpoint.received[0].fields.get("model") == "a-private-conversion"


# ---------------------------------------------------------------------------
# What the job measured, and what it reported while running
# ---------------------------------------------------------------------------


async def test_the_measurements_are_the_gate_s_and_not_the_endpoint_s(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """`audio_seconds` is the file as written, silence and all.

    The endpoint reports a duration of what it was sent, which is the
    windows rather than the recording. Taking that number would make a
    100-minute track with two minutes of speech look like a two-minute
    recording, and the invariant on `JobMeasurements` exists because that
    confusion has happened.
    """
    samples = _track_with_speech_at(1.0, 61.0, total=120.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 2.0, " hallo")))

    result = await _engine(endpoint, max_chunk_seconds=50.0).transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    assert result.measurements is not None
    assert result.measurements.audio_seconds == pytest.approx(120.0, abs=0.01)
    assert result.measurements.speech_seconds < result.measurements.audio_seconds
    assert result.measurements.speech_seconds > 0.0
    assert result.measurements.segment_count == len(result.segments)
    assert result.measurements.model == "whisper-1"


async def test_progress_advances_once_per_completed_window(
    tmp_path: Path, endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One window is one request with no intermediate signal.

    This is what coarsens the stall clock, and it is asserted rather than
    assumed because the alert that pages on
    `seconds_since_progress` has to be set above a window's round trip --
    see the change's `design.md`, Decision 8.
    """

    class _Recorder:
        def __init__(self) -> None:
            self.positions: list[float] = []
            self.total: float | None = None
            self.began: str | None = None
            self.ended = 0

        def begin(self, model: str) -> None:
            self.began = model

        def set_total(self, seconds: float) -> None:
            self.total = seconds

        def advance(self, position_seconds: float) -> None:
            self.positions.append(position_seconds)

        def end(self) -> None:
            self.ended += 1

    recorder = _Recorder()
    monkeypatch.setattr(openai_transcription, "TRANSCRIPTION_PROGRESS", recorder)

    samples = _track_with_speech_at(1.0, 61.0, 121.0, total=180.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 2.0, " hallo")))

    await _engine(endpoint, max_chunk_seconds=50.0).transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    assert recorder.began == "whisper-1"
    assert len(recorder.positions) == len(endpoint.received) == 3
    assert recorder.positions == sorted(recorder.positions)
    assert recorder.total is not None
    assert recorder.positions[-1] == pytest.approx(recorder.total)
    assert recorder.ended == 1


async def test_a_failed_request_still_ends_the_job_in_flight(
    tmp_path: Path, endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a failure looks exactly like a wedge.

    `seconds_since_progress` would climb past every threshold while the
    worker moved cheerfully on to the next job.
    """
    ended = []

    class _Recorder:
        def begin(self, model: str) -> None:
            del model

        def set_total(self, seconds: float) -> None:
            del seconds

        def advance(self, position_seconds: float) -> None:
            del position_seconds

        def end(self) -> None:
            ended.append(True)

    monkeypatch.setattr(openai_transcription, "TRANSCRIPTION_PROGRESS", _Recorder())
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answers = [500]

    with pytest.raises(Exception):  # noqa: B017 - the SDK's own error type
        await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert ended == [True]


# ---------------------------------------------------------------------------
# Failure reaches the queue
# ---------------------------------------------------------------------------


async def test_a_server_error_raises_so_the_job_records_an_attempt(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """Swallowing it would produce a partial transcript that looks complete.

    `worker.process_one` owns what happens next: an attempt is recorded,
    and at `max_job_attempts` the job is dead with an error an operator can
    read.
    """
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answers = [500]

    with pytest.raises(Exception) as raised:  # noqa: B017 - the SDK's own error type
        await _engine(endpoint).transcribe(_wav_of(tmp_path, samples), "de", None)

    assert "500" in str(raised.value) or "server" in str(raised.value).lower()


async def test_a_timeout_raises_rather_than_returning_what_arrived(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """The client's own timeout, fired by a server that answers too late."""
    samples = _track_with_speech_at(1.0, total=10.0)
    endpoint.answers = [2.0]

    with pytest.raises(Exception):  # noqa: B017 - the SDK's own error type
        await _engine(endpoint, timeout_seconds=0.25).transcribe(
            _wav_of(tmp_path, samples), "de", None
        )


# ---------------------------------------------------------------------------
# What actually goes on the wire
# ---------------------------------------------------------------------------


async def test_every_window_is_sent_as_its_own_request_under_the_cap(
    tmp_path: Path, endpoint: _Endpoint
) -> None:
    """The size limit is the reason any of this exists.

    Asserted on the bytes the server received rather than on a plan, so a
    window that was computed correctly and then sliced wrongly still fails
    this test.
    """
    samples = _track_with_speech_at(1.0, 61.0, 121.0, total=180.0)
    endpoint.answer_every_request_with(_verbose(_segment(0, 0.0, 2.0, " hallo")))

    await _engine(endpoint, max_chunk_seconds=50.0).transcribe(
        _wav_of(tmp_path, samples), "de", None
    )

    assert len(endpoint.received) == 3
    for received in endpoint.received:
        assert received.seconds <= 50.0 + 1e-6
        assert received.fields.get("response_format") == "verbose_json"
        assert received.fields.get("temperature") == "0"
