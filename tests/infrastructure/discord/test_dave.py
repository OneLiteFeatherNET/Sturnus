"""The end-to-end layer, and the three things that may happen to a frame.

This is the defect that made every recording noise: Discord encrypts voice
twice, and `discord-ext-voice-recv` only ever undid the outer layer. What
reached the Opus decoder was DAVE ciphertext, which libopus decoded
without complaint into white noise.

So the tests below are mostly about *not* repeating that. The decisive one
is `test_ciphertext_never_reaches_the_decoder_when_decryption_fails`:
handing a frame on when it could not be decrypted is precisely the bug,
and a decryptor that failed open would reproduce it exactly.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sturnus.infrastructure.discord.dave import DaveDecryptor, session_source_for

FRAME = b"\x7c" + b"\x11" * 40
PLAIN = b"\x7c" + b"\x22" * 40
ANNA = 100


class FakeSession:
    """Stands in for `davey.DaveSession` without the native library."""

    def __init__(
        self,
        *,
        ready: bool = True,
        plaintext: bytes | None = PLAIN,
        raises: Exception | None = None,
    ) -> None:
        self._ready = ready
        self._plaintext = plaintext
        self._raises = raises
        self.calls: list[tuple[int, bytes]] = []

    @property
    def ready(self) -> bool:
        return self._ready

    def decrypt(self, user_id: int, media_type: Any, packet: bytes) -> bytes:
        del media_type
        self.calls.append((user_id, packet))
        if self._raises is not None:
            raise self._raises
        assert self._plaintext is not None
        return self._plaintext


def _decryptor(session: object | None, monkeypatch: pytest.MonkeyPatch) -> DaveDecryptor:
    """A decryptor over a fixed session, with `davey` stubbed out."""
    monkeypatch.setattr("sturnus.infrastructure.discord.dave.audio_media_type", lambda: "audio")
    return DaveDecryptor(lambda: session)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The three outcomes
# ---------------------------------------------------------------------------


def test_a_frame_is_decrypted_before_anything_decodes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix itself: what leaves here is plaintext Opus, not ciphertext."""
    session = FakeSession()
    decryptor = _decryptor(session, monkeypatch)

    assert decryptor.decrypt(ANNA, FRAME) == PLAIN
    assert session.calls == [(ANNA, FRAME)]
    assert decryptor.frames_decrypted == 1


def test_a_channel_without_end_to_end_encryption_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every channel uses DAVE, and refusing those would break them.

    No session is the ordinary state before a connection settles and in a
    channel where the layer is not in use; the frame is already plain Opus.
    """
    decryptor = _decryptor(None, monkeypatch)
    assert decryptor.decrypt(ANNA, FRAME) == FRAME
    assert decryptor.frames_passed_through == 1
    assert decryptor.frames_decrypted == 0


def test_ciphertext_never_reaches_the_decoder_when_decryption_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, as a test.

    Passing the frame on would hand libopus ciphertext -- which it decodes
    without error into noise, which is exactly how this defect stayed
    invisible for as long as it did. `None` means "write nothing", and
    `SpeakerWriter` places audio by RTP time, so the gap becomes real
    silence and nothing after it shifts.
    """
    decryptor = _decryptor(FakeSession(raises=ValueError("NoDecryptorForUser")), monkeypatch)

    assert decryptor.decrypt(ANNA, FRAME) is None
    assert decryptor.frames_failed == 1


def test_an_empty_plaintext_is_a_failure_rather_than_an_empty_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`decode(b"")` raises `OpusError(-1)`, so an empty result must not be
    handed on as though it were audio."""
    decryptor = _decryptor(FakeSession(plaintext=b""), monkeypatch)
    assert decryptor.decrypt(ANNA, FRAME) is None
    assert decryptor.frames_failed == 1


# ---------------------------------------------------------------------------
# Epochs: the reason the session is read per frame
# ---------------------------------------------------------------------------


def test_the_session_is_read_again_for_every_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAVE is MLS: the group key changes whenever somebody joins or leaves.

    A cached session would decrypt against a retired epoch and produce
    exactly the noise this module removes -- silently, and only for part
    of a meeting.
    """
    monkeypatch.setattr("sturnus.infrastructure.discord.dave.audio_media_type", lambda: "audio")
    first, second = FakeSession(), FakeSession()
    sessions = iter([first, second, second])
    decryptor = DaveDecryptor(lambda: next(sessions))

    decryptor.decrypt(ANNA, FRAME)
    decryptor.decrypt(ANNA, FRAME)

    assert len(first.calls) == 1, "the first epoch's session was reused after it was replaced"
    assert len(second.calls) == 1


def test_a_session_that_is_not_ready_yet_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-handshake there is a session object but no usable key.

    Treated as "no session", so the frame passes through rather than being
    dropped: the alternative loses the first seconds of every recording.
    """
    session = FakeSession(ready=False)
    decryptor = _decryptor(session, monkeypatch)

    assert decryptor.decrypt(ANNA, FRAME) == FRAME
    assert session.calls == []


# ---------------------------------------------------------------------------
# Nothing may escape towards the packet-router thread
# ---------------------------------------------------------------------------


def test_a_broken_session_source_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RecordingSink.write` runs on the packet-router thread, where an
    escaping exception ends capture for every speaker at once."""
    monkeypatch.setattr("sturnus.infrastructure.discord.dave.audio_media_type", lambda: "audio")

    def source() -> Any:
        raise RuntimeError("client gone")

    assert DaveDecryptor(source).decrypt(ANNA, FRAME) == FRAME


def test_a_session_whose_readiness_raises_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hostile:
        @property
        def ready(self) -> bool:
            raise RuntimeError("no")

    decryptor = _decryptor(Hostile(), monkeypatch)
    assert decryptor.decrypt(ANNA, FRAME) == FRAME


def test_a_lost_frame_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """`b""` is the library's manufactured packet for a gap; the decoder
    routes it to concealment and there is nothing to decrypt."""
    session = FakeSession()
    decryptor = _decryptor(session, monkeypatch)

    assert decryptor.decrypt(ANNA, b"") == b""
    assert decryptor.decrypt(ANNA, None) is None
    assert session.calls == []


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_a_run_of_failures_is_logged_once_rather_than_per_frame(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """At 50 frames a second an unrate-limited line is its own outage, and
    a key rotation legitimately costs a handful of frames."""
    decryptor = _decryptor(FakeSession(raises=ValueError("boom")), monkeypatch)
    with caplog.at_level(logging.WARNING):
        for _ in range(120):
            decryptor.decrypt(ANNA, FRAME)

    assert len(caplog.records) == 1
    assert decryptor.frames_failed == 120


def test_the_failure_run_resets_after_a_frame_decrypts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A second rotation must be reported like the first, not swallowed by
    the rate limit of the one before it."""
    monkeypatch.setattr("sturnus.infrastructure.discord.dave.audio_media_type", lambda: "audio")
    failing, working = FakeSession(raises=ValueError("boom")), FakeSession()
    sessions = iter([failing, working, failing])
    decryptor = DaveDecryptor(lambda: next(sessions))

    with caplog.at_level(logging.WARNING):
        decryptor.decrypt(ANNA, FRAME)
        decryptor.decrypt(ANNA, FRAME)
        decryptor.decrypt(ANNA, FRAME)

    assert len(caplog.records) == 2


# ---------------------------------------------------------------------------
# Reaching the session
# ---------------------------------------------------------------------------


def test_the_session_is_read_from_the_voice_client() -> None:
    """`discord.py` exposes it nowhere public; `VoiceClient.voice_privacy_code`
    reaches through the same attribute."""

    session = FakeSession()

    class Connection:
        dave_session = session

    class Client:
        _connection = Connection()

    assert session_source_for(Client())() is session


def test_a_client_without_a_connection_yields_no_session() -> None:
    """Before `connect()` and after a drop there is no state to read."""
    assert session_source_for(object())() is None
