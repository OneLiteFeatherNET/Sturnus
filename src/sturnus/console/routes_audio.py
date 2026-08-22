"""The endpoints that reach somebody's voice, and the one gate in front of them.

- `GET /api/sessions/{session_id}/tracks/{discord_user_id}/audio`
- `GET /api/sessions/{session_id}/tracks/{discord_user_id}/spectrogram`

Both reach the same object under the same rule, so the rule is written
once, in `_authorised_track`, and each handler is what is left after it:
the shape of one HTTP response. A second copy of an authorisation check is
a second thing to keep in step with the first, and the two only have to
disagree once.

This is the console's most consequential route, and the design says why
(section 1.1): playing a recording back is a wider use of it than writing a
protocol from it, and consent for the second does not carry the first.
Three things make it defensible; exactly one of them can live in code, and
it is here:

**Only participants of a session may play its audio.** Not
administrators-in-general, not anyone holding a link. The check is a
database query against `session_participant`, scoped by the *signed-in*
Discord id -- never by anything in the URL -- and it is made on every
request. Not cached, not inferred from an earlier one: a participation that
ends must stop working immediately, and a cache is precisely a promise that
it will not.

Somebody who was not in the session gets **404, not 403**. A 403 confirms
that the session exists, when it happened, and that this person has a
recording in it -- to somebody the system has just decided has no business
knowing any of that. The refusal and "no such thing" must be the same
answer.

Everything else this handler does is arithmetic and lives in
`sturnus.console.audio`, which needs neither aiohttp nor S3 to be tested.
What is left here is the shape of the HTTP: which status, which headers,
and the order the two are decided in.

**Not compressed here.** `Content-Encoding` and `Range` interact badly --
a range is over the encoded bytes, and a compressor in this process would
make every partial response's arithmetic depend on the compressor. The
Cloudflare Tunnel in front already compresses on the wire, which is where
that belongs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiohttp import web

from sturnus.console.audio import (
    AudioDelivery,
    ByteRange,
    CorruptRecording,
    UnsatisfiableRange,
    parse_range,
    stored_length,
    stream_wav,
)
from sturnus.console.ports import Track
from sturnus.console.spectrogram import spectrogram
from sturnus.observability.events import Event, log_event, log_exception

log = logging.getLogger(__name__)

#: Where the collaborators are found. Declared here rather than in `app`
#: because they belong to this route and nothing else reads them.
AUDIO_DELIVERY = web.AppKey("audio_delivery", AudioDelivery)

_PATH = "/api/sessions/{session_id}/tracks/{discord_user_id}/audio"
_SPECTROGRAM_PATH = "/api/sessions/{session_id}/tracks/{discord_user_id}/spectrogram"


@dataclass(frozen=True)
class _AuthorisedTrack:
    """A track this request has been found entitled to, and its size."""

    track: Track
    ciphertext_bytes: int
    session_id: int
    speaker_id: int
    listener: int


async def _authorised_track(request: web.Request) -> _AuthorisedTrack | web.Response:
    """The whole access rule for audio, or the refusal to answer with.

    Everything security-relevant about these routes is in the order here.
    Authorisation happens before the object store is touched and before
    any `Range` is parsed, because a 416 carrying the length of a
    recording is still a fact about a recording -- and a stranger who asks
    for an impossible range must learn no more than a stranger who asks
    for a possible one.
    """
    # Imported here rather than at module scope: `sturnus.console.app`
    # imports this module in order to call `register`, and importing back
    # the other way at module scope is a cycle Python resolves as an
    # ImportError. This is the cheaper of the two prices -- the alternative
    # is the route living apart from the decorator that protects it.
    from sturnus.console.app import current_user

    listener = current_user(request).discord_user_id
    try:
        session_id = int(request.match_info["session_id"])
        speaker_id = int(request.match_info["discord_user_id"])
    except ValueError:
        # A path segment that is not a number names nothing, and saying so
        # is the same answer as naming something that does not exist.
        return _no_such_recording()

    delivery = request.app[AUDIO_DELIVERY]
    track = await delivery.tracks.track_for(session_id, speaker_id, requested_by=listener)
    if track is None:
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_TRACK_REFUSED,
            "Refused a recording to somebody outside the session it belongs to",
            session_id=session_id,
            discord_user_id=speaker_id,
            requested_by=listener,
            reason="not_a_participant",
        )
        return _no_such_recording()

    if track.encryption_key_id != delivery.keys.key_id:
        # Checked before anything is streamed. Going ahead would fail as an
        # authentication-tag error partway through a response that has
        # already promised a playable track with a 200.
        log_event(
            log,
            logging.ERROR,
            Event.KEY_ID_MISMATCH,
            "A recording names a master key this process does not hold",
            session_id=session_id,
            discord_user_id=speaker_id,
            key_id=track.encryption_key_id,
            configured_key_id=delivery.keys.key_id,
        )
        return _unreadable()

    try:
        ciphertext_bytes = await delivery.source.size(track.s3_key)
    except KeyError:
        # The ordinary case is a row that outlived its object: the
        # retention sweep erased the audio. Nothing is broken, so this is
        # a 404 and not a 500.
        log_event(
            log,
            logging.WARNING,
            Event.CONSOLE_TRACK_REFUSED,
            "A recording's object is no longer in the store",
            session_id=session_id,
            discord_user_id=speaker_id,
            requested_by=listener,
            reason="audio_erased",
        )
        return _no_such_recording()

    return _AuthorisedTrack(track, ciphertext_bytes, session_id, speaker_id, listener)


async def track_spectrogram(request: web.Request) -> web.StreamResponse:
    """One track as a picture of where its speech is.

    Behind the same gate as the audio itself, and that is not a formality:
    a spectrogram is a rendering of somebody's voice, and it shows when
    they spoke and for how long. It is less than the audio; it is not
    nothing, and the rule that governs the audio is the right one for it.

    Answered as a whole small JSON body rather than streamed. The payload
    is a fixed 600 by 128 bytes whatever the meeting's length, so there is
    nothing to page through -- the streaming happens on the way *in*, past
    the FFT, which is where the size of a recording actually matters.
    """
    resolved = await _authorised_track(request)
    if isinstance(resolved, web.Response):
        return resolved

    delivery = request.app[AUDIO_DELIVERY]
    data_key = delivery.keys.unwrap(resolved.track.wrapped_data_key)
    try:
        picture = await spectrogram(
            delivery.source,
            resolved.track.s3_key,
            data_key,
            stored_length(resolved.ciphertext_bytes),
        )
    except CorruptRecording as exc:
        log_exception(
            log,
            logging.ERROR,
            Event.CONSOLE_TRACK_UNREADABLE,
            "A stored recording could not be drawn",
            exc,
            session_id=resolved.session_id,
            discord_user_id=resolved.speaker_id,
            object_bytes=resolved.ciphertext_bytes,
        )
        return _unreadable()

    return web.json_response(
        {
            "columns": picture.columns,
            "bins": picture.bins,
            "sample_rate": picture.sample_rate,
            "hz_per_bin": round(picture.hz_per_bin, 4),
            "duration_seconds": picture.duration_seconds,
            "magnitudes": picture.magnitudes,
        },
        headers={"Cache-Control": "private, no-store"},
    )


async def track_audio(request: web.Request) -> web.StreamResponse:
    """Streams one speaker's recording as WAV, decrypting as it goes.

    What is left here after `_authorised_track` is the shape of the HTTP:
    which status, which headers, and the order the two are decided in.
    """
    resolved = await _authorised_track(request)
    if isinstance(resolved, web.Response):
        return resolved
    track, session_id = resolved.track, resolved.session_id
    speaker_id, listener = resolved.speaker_id, resolved.listener

    delivery = request.app[AUDIO_DELIVERY]
    try:
        total = stored_length(resolved.ciphertext_bytes)
    except CorruptRecording as exc:
        log_exception(
            log,
            logging.ERROR,
            Event.CONSOLE_TRACK_UNREADABLE,
            "A stored recording is not in the format this reader understands",
            exc,
            session_id=session_id,
            discord_user_id=speaker_id,
            object_bytes=resolved.ciphertext_bytes,
        )
        return _unreadable()

    # The stored object *is* the WAV file, header included, so the length of
    # the resource is the length of the plaintext and nothing is added to it.
    try:
        requested = parse_range(request.headers.get("Range"), total)
    except UnsatisfiableRange:
        return web.json_response(
            {"error": "range not satisfiable"},
            status=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )

    span = requested if requested is not None else ByteRange(0, total - 1)
    response = web.StreamResponse(status=206 if requested is not None else 200)
    response.content_type = "audio/wav"
    response.content_length = span.length
    response.headers["Accept-Ranges"] = "bytes"
    # Somebody's voice, decrypted, on its way to one named listener. A
    # shared cache holding a copy would hand it to the next person through
    # the same proxy -- exactly the audience the check above exists to
    # exclude.
    response.headers["Cache-Control"] = "private, no-store"
    if requested is not None:
        response.headers["Content-Range"] = f"bytes {span.first}-{span.last}/{total}"
    await response.prepare(request)

    if request.method == "HEAD":
        # Checked by hand, and it cost a debugging session to learn why:
        # `web.StreamResponse.write` does *not* suppress the body for a
        # HEAD the way `web.Response` does. Streaming the audio anyway
        # writes it after the headers, and the client reads the first bytes
        # of the WAV as the next response's status line. A HEAD is how an
        # audio element asks for the length and for `Accept-Ranges` before
        # it fetches anything, so the answer is the headers above and
        # nothing else -- and it costs no decryption at all.
        await response.write_eof()
        return response

    data_key = delivery.keys.unwrap(track.wrapped_data_key)
    try:
        async for piece in stream_wav(delivery.source, track.s3_key, data_key, span):
            await response.write(piece)
    except ConnectionError:
        # The listener went away mid-track, and that is the *ordinary* end
        # of an audio stream rather than a fault. A browser closes one
        # whenever somebody navigates away, seeks past the buffered range,
        # or lets an `<audio>` element decide it has enough -- and the
        # recording page mounts one player per speaker plus the transport,
        # so a single visit that ends normally can drop a handful at once.
        #
        # aiohttp logs the resulting `ConnectionResetError` at ERROR with a
        # full traceback if nothing catches it, which is how leaving a
        # recording page became several tracebacks in the log of a service
        # that is working perfectly. DEBUG, and no traceback: there is
        # nothing for a human to act on, and a genuinely broken transfer
        # still arrives as `CorruptRecording` below.
        #
        # `ConnectionResetError` is a subclass, so one clause covers both.
        # `asyncio.CancelledError` deliberately is *not* caught -- it
        # inherits from `BaseException`, and swallowing it would break
        # graceful shutdown.
        log.debug(
            "The listener disconnected partway through a recording (session %s, speaker %s)",
            session_id,
            speaker_id,
        )
        return response
    except CorruptRecording as exc:
        # The status line left with the headers, so there is no status to
        # change: the response ends short of its declared length, which is
        # how a client is told the transfer failed. The log line is the
        # only place an operator can learn why.
        log_exception(
            log,
            logging.ERROR,
            Event.CONSOLE_TRACK_UNREADABLE,
            "A recording failed to decrypt partway through being served",
            exc,
            session_id=session_id,
            discord_user_id=speaker_id,
            bytes=span.length,
        )
        return response

    await response.write_eof()
    # Emitted after the fact rather than before, so it records what was
    # actually delivered. This is the access log for the most sensitive
    # thing the console does -- who played whose voice back -- and it is
    # the reason `requested_by` exists as a field at all.
    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_TRACK_SERVED,
        "Served a recording to a participant of its session",
        session_id=session_id,
        discord_user_id=speaker_id,
        requested_by=listener,
        bytes=span.length,
    )
    return response


def _no_such_recording() -> web.Response:
    """The one refusal this endpoint has, for every reason it can refuse.

    "You were not in that session", "there is no such session" and "that
    person has no recording in it" are deliberately indistinguishable. See
    the module docstring.
    """
    return web.json_response({"error": "no such recording"}, status=404)


def _unreadable() -> web.Response:
    """The recording exists and this process cannot produce it.

    A fixed string, like every other error body in the console: nothing
    from the request or from the store is reflected back.
    """
    return web.json_response({"error": "this recording cannot be read"}, status=500)


def register(app: web.Application) -> None:
    """Adds the audio route to an application that already has its keys."""
    # Same cycle, same reason as in the handler. The decorator is applied
    # here rather than written above `track_audio`, which costs nothing:
    # `require_session(handler)` is exactly what `@require_session` does.
    from sturnus.console.app import require_session

    app.add_routes(
        [
            web.get(_PATH, require_session(track_audio)),
            web.get(_SPECTROGRAM_PATH, require_session(track_spectrogram)),
        ]
    )
