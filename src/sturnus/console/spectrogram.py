"""Getting a picture of a track out of the bucket, one way or the other.

What the picture *is* -- the windows, the frequency rows, the
normalisation, the stored form -- lives in
`sturnus.application.spectrogram`, because the worker draws the same
picture from the same definition. What lives here is the pair of ways the
console can obtain one, and the difference between them is entirely a
matter of cost:

**`spectrogram`** streams the encrypted track past the FFT and draws it
now. A full decrypt of the object plus `COLUMNS` transforms, per view.
That is what every view cost before a stored artefact existed, and it is
still what a guild that has not switched `spectrograms_by_default` on
pays -- along with every job transcribed before it did.

**`stored_spectrogram`** reads the artefact the worker already drew. Two
object-store requests for about a hundred kilobytes, and no FFT at all.

Both come back through the same `Spectrogram`, so the endpoint's response
does not depend on which one answered, and neither of them decides
*whether* the caller may have it: authorisation happens before either is
called and is re-decided on every request (`sturnus.console.routes_audio`).
A cache of the payload must never become a cache of the permission.

**The artefact is read exactly like the audio is** -- same envelope, same
data key, same `stream_wav`. It is sealed because it is a rendering of
somebody's voice activity: less than the audio and not nothing, by the
same argument that puts it behind the same authorisation rule. An object
in this bucket that anybody holding the bucket could read would be the one
exception, and the one exception is the whole exposure.

**Nothing here writes plaintext to disk**, for the same reason
`sturnus.console.audio` does not, and pinned by the same static test in
`tests/console/test_audio.py`, which lists this module on the serving path.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag

from sturnus.application.spectrogram import Spectrogram, decode_artefact, draw
from sturnus.console.audio import ByteRange, stored_length, stream_wav
from sturnus.console.ports import EncryptedAudioSource
from sturnus.domain.errors import CorruptRecording

#: The smallest object that could hold a WAV header at all. Checked before
#: a byte is fetched, so an object that cannot be a track is refused
#: rather than half-read.
_MIN_TRACK_BYTES = 44

#: The largest stored artefact this reader will pull into memory. A
#: picture is `COLUMNS * BINS` bytes base64'd inside a small JSON
#: envelope -- a shade over 100 kB, fixed, whatever the meeting's length.
#: The bound exists because this one *is* read whole, unlike the audio:
#: an object that has grown a thousandfold is not an artefact this build
#: wrote, and reading it to find that out is how a fixed-size read becomes
#: an unbounded one.
_MAX_ARTEFACT_BYTES = 1024 * 1024


async def spectrogram(
    source: EncryptedAudioSource,
    key: str,
    data_key: bytes,
    stored_bytes: int,
) -> Spectrogram:
    """Draws one encrypted track, now, without ever holding it.

    `stored_bytes` is the plaintext length the caller already derived from
    the object's size (`stored_length`), so this does not go back to the
    store to ask.
    """
    if stored_bytes < _MIN_TRACK_BYTES:
        raise CorruptRecording("object is too short to hold a track")

    pieces = stream_wav(source, key, data_key, ByteRange(0, stored_bytes - 1))
    try:
        return await draw(pieces)
    finally:
        # Closed here rather than in `draw`, which is shared with a caller
        # whose stream is a file: a listener who navigated away should stop
        # the transfer out of S3 in the same breath, and a suspended
        # generator nobody closed holds that connection open until
        # something else gets round to it.
        await pieces.aclose()


async def stored_spectrogram(
    source: EncryptedAudioSource,
    key: str,
    data_key: bytes,
) -> Spectrogram:
    """Reads the artefact the worker drew when the job finished.

    Raises `KeyError` when the object is not there and `CorruptRecording`
    when it is not an artefact this build can read. The caller treats both
    the same way -- draw the track instead -- because both have the same
    remedy and the person waiting is owed a picture either way.

    A failed authentication tag is folded into the same refusal rather
    than allowed out. On the audio path an `InvalidTag` means a recording
    somebody is entitled to will not decrypt, which is worth an error;
    here it means an object sealed under a key this job does not name,
    which is not an artefact this reader can use, full stop. The
    distinction between the two ways of being unreadable is not one the
    caller has any different answer for.
    """
    ciphertext_bytes = await source.size(key)
    plaintext_bytes = stored_length(ciphertext_bytes)
    if plaintext_bytes <= 0 or plaintext_bytes > _MAX_ARTEFACT_BYTES:
        raise CorruptRecording("stored spectrogram is not the size an artefact is")

    pieces = stream_wav(source, key, data_key, ByteRange(0, plaintext_bytes - 1))
    body = bytearray()
    try:
        async for piece in pieces:
            body += piece
    except InvalidTag as exc:
        raise CorruptRecording("stored spectrogram is sealed under another key") from exc
    finally:
        await pieces.aclose()
    return decode_artefact(bytes(body))
