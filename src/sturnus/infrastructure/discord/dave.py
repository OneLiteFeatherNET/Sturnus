"""Undoing Discord's end-to-end encryption before the Opus decoder sees a frame.

**This is the defect that made every recording noise.** Discord encrypts
voice twice: the transport layer (`aead_xchacha20_poly1305_rtpsize`, which
`discord-ext-voice-recv` decrypts correctly) and, underneath it, DAVE --
Discord Audio and Video End-to-End Encryption, an MLS-based layer between
the participants themselves. `discord.py` implements DAVE and encrypts
everything it *sends* through `dave_session.encrypt_opus`.
`discord-ext-voice-recv` never learned the receiving half: the string
"dave" does not appear anywhere in it.

So what arrived at `ResilientOpusDecoder` was DAVE ciphertext, and every
symptom follows from that one fact:

- The transport decryption succeeded, because that layer was never the
  problem. Its authentication tag verified, which is why nothing raised.
- The payload arithmetic was exact -- `16 + 4n` bytes trimmed, every time.
  The boundary was never wrong.
- The bytes were high-entropy, because ciphertext is.
- libopus read a random TOC byte off each one and **reported no error**:
  almost every byte value is a formally valid Opus configuration. It
  returned noise, `frames_decoded` climbed, and nothing anywhere looked
  broken.
- The original incident -- `OpusError: corrupted stream` killing the
  packet-router thread -- was the same bytes, occasionally landing on a
  configuration libopus *did* reject.

Everything previously investigated (the reader's format, per-frame
resampling, frame stitching, packet offsets) sat below this layer and
could not have been the cause.

**The fix is to call the decryption that already exists.** `davey`, the
library `discord.py` depends on, exposes `DaveSession.decrypt(user_id,
media_type, packet)`. It is simply never called on the receive path.
Sturnus decodes its own Opus (`RecordingSink.wants_opus` returns `True`),
so it holds the frame at exactly the right moment to do it.

Three properties this module has to have, each for a reason:

**The session is fetched per frame, never cached.** DAVE is MLS: the group
key changes whenever somebody joins or leaves, and `VoiceConnectionState`
replaces or `reinit`s its session at each transition. A cached reference
would decrypt against a retired epoch and produce exactly the noise this
module exists to remove -- silently, and only for part of a meeting.

**A frame that will not decrypt is dropped, not passed on.** Handing
ciphertext to the Opus decoder is what produced the original defect;
`ResilientOpusDecoder` already treats `None` as "write nothing", and
`SpeakerWriter` places audio by RTP time, so a dropped frame becomes its
own duration of real silence and nothing after it shifts.

**Nothing here may raise.** It runs on the packet-router thread inside
`RecordingSink.write`, whose module docstring explains at length why an
exception escaping there ends capture for every speaker at once.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: How often a run of decryption failures is logged. At 50 frames a second
#: per speaker an unrate-limited line is its own outage, and a key rotation
#: legitimately costs a handful of frames.
FAILURE_LOG_EVERY = 500


class DaveSession(Protocol):
    """The slice of `davey.DaveSession` this module uses.

    Spelled as a Protocol so the whole policy is testable without the
    native library, and so the two attributes that decide whether to
    decrypt at all are named rather than duck-typed at the call site.
    """

    @property
    def ready(self) -> bool: ...

    def decrypt(self, user_id: int, media_type: Any, packet: bytes) -> bytes: ...


class SessionSource(Protocol):
    """Where the current session comes from, asked per frame.

    A callable rather than a value: see the module docstring on epochs.
    Returns `None` whenever there is no active end-to-end session, which
    is both the pre-connection state and a channel where DAVE is not in
    use.
    """

    def __call__(self) -> DaveSession | None: ...


def audio_media_type() -> Any:
    """`davey.MediaType.audio`, imported at call time.

    Deferred so that importing this module does not require the native
    library: `sturnus.infrastructure.discord.sink` imports it
    unconditionally, and the test suite runs without `davey` present.
    """
    import davey

    return davey.MediaType.audio


class DaveDecryptor:
    """Turns a DAVE-encrypted frame back into an Opus packet.

    Constructed with a way to reach the current session rather than with
    the session itself, and consulted on every frame.
    """

    def __init__(self, session_source: SessionSource) -> None:
        self._session_source = session_source
        self._media_type: Any = None
        self.frames_decrypted = 0
        self.frames_passed_through = 0
        self.frames_failed = 0
        self._consecutive_failures = 0

    @property
    def active(self) -> bool:
        """Whether an end-to-end session is currently carrying the audio."""
        session = self._current_session()
        return session is not None

    def decrypt(self, user_id: int, frame: bytes | None) -> bytes | None:
        """One frame, decrypted; unchanged if there is no session; `None` if it fails.

        The three outcomes are deliberately distinct. Passing a frame
        through when no session exists is correct -- a channel without
        DAVE sends plain Opus, and refusing it would break every such
        recording. Dropping one that will not decrypt is also correct:
        handing ciphertext to libopus is the defect this module removes.
        """
        if not frame:
            # A lost frame; the decoder routes it to concealment. There is
            # nothing to decrypt and nothing to report.
            return frame

        session = self._current_session()
        if session is None:
            self.frames_passed_through += 1
            return frame

        try:
            if self._media_type is None:
                self._media_type = audio_media_type()
            plaintext = session.decrypt(user_id, self._media_type, frame)
        except Exception as error:
            self._note_failure(user_id, error)
            return None

        if not plaintext:
            self._note_failure(user_id, None)
            return None

        self.frames_decrypted += 1
        self._consecutive_failures = 0
        return plaintext

    def _current_session(self) -> DaveSession | None:
        """The session as it is *now*, or `None`.

        Never raises: reaching for it crosses into the voice client, and a
        client that is mid-reconnect is an ordinary state rather than an
        error.
        """
        try:
            session = self._session_source()
        except Exception:
            log.debug("Could not reach the end-to-end session", exc_info=True)
            return None
        if session is None:
            return None
        try:
            return session if session.ready else None
        except Exception:
            log.debug("End-to-end session did not report readiness", exc_info=True)
            return None

    def _note_failure(self, user_id: int, error: BaseException | None) -> None:
        """Books one frame that could not be decrypted, and says so sparsely.

        A handful in a row is ordinary: an MLS epoch changes whenever
        somebody joins or leaves, and frames already in flight were sealed
        under the previous key. A sustained run is not, and is what the
        rate-limited line below is for.
        """
        self.frames_failed += 1
        self._consecutive_failures += 1
        if self._consecutive_failures == 1 or self._consecutive_failures % FAILURE_LOG_EVERY == 0:
            log.warning(
                "Could not decrypt %d consecutive end-to-end frames for discord_user_id=%s; "
                "they are dropped rather than decoded, because handing ciphertext to the Opus "
                "decoder is what produced silent noise before. A few in a row are an ordinary "
                "key rotation; a sustained run means this session is not in the group.",
                self._consecutive_failures,
                user_id,
                exc_info=error if self._consecutive_failures == 1 else None,
            )


def session_source_for(voice_client: Any) -> SessionSource:
    """A `SessionSource` reading the client's current DAVE session.

    Reaches through `voice_client._connection`, and that underscore is
    load-bearing rather than careless: `discord.py` exposes the session
    nowhere else. `VoiceClient.voice_privacy_code` reads it the same way,
    which is the closest thing to a public accessor there is.

    `sturnus.infrastructure.discord.sink` otherwise touches only the
    library's documented surface, so this is the one exception and it is
    named here rather than buried in the sink.
    """

    def source() -> DaveSession | None:
        connection = getattr(voice_client, "_connection", None)
        session: DaveSession | None = getattr(connection, "dave_session", None)
        return session

    return source
