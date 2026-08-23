"""The endpoints that reach somebody's voice, and the gates in front of them.

- `GET /api/sessions/{session_id}/tracks/{discord_user_id}/audio`
- `GET /api/sessions/{session_id}/tracks/{discord_user_id}/spectrogram`
- `GET /api/sessions/{session_id}/tracks/{discord_user_id}/download`

The first two reach the same object under the same rule, so that rule is
written once, in `_authorised_track`, and each handler is what is left
after it: the shape of one HTTP response. A second copy of an
authorisation check is a second thing to keep in step with the first, and
the two only have to disagree once.

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

**Downloading is a different act, so it is a different route.** The
repository owner decided, deliberately, that an administrator of a guild
may obtain a copy of any recording of that guild, including sessions they
were not in -- a real widening of what the design used to promise, and one
that document now records rather than contradicts. It lives at its own
path rather than behind a query flag on `/audio` so that the two intents
are separable in the router, in the tests and in the audit log, and so
that nothing about playback moved to make room for it. The rule is
`_authorised_download`; the guild has to have switched the capability on
(`settings.ADMIN_AUDIO_DOWNLOAD_OFFERED`), and while it has not the route
refuses everyone, participants included.

The three routes therefore differ in exactly two things -- which rule
authorises them and which headers they answer with -- and `_resolve`
below is written so that is all that differs.

Somebody who was not in the session gets **404, not 403**, and so does
somebody who is not an administrator. A 403 confirms that the session
exists, when it happened, and that this person has a recording in it -- to
somebody the system has just decided has no business knowing any of that.
Every refusal on this path is the same answer with the same body.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from sturnus.application.spectrogram import Spectrogram
from sturnus.console.audio import (
    AudioDelivery,
    ByteRange,
    UnsatisfiableRange,
    parse_range,
    stored_length,
    stream_wav,
)
from sturnus.console.ports import Track
from sturnus.console.spectrogram import spectrogram, stored_spectrogram
from sturnus.domain.errors import CorruptRecording
from sturnus.observability.events import Event, log_event, log_exception

log = logging.getLogger(__name__)

#: Where the collaborators are found. Declared here rather than in `app`
#: because they belong to this route and nothing else reads them.
AUDIO_DELIVERY = web.AppKey("audio_delivery", AudioDelivery)

_PATH = "/api/sessions/{session_id}/tracks/{discord_user_id}/audio"
_SPECTROGRAM_PATH = "/api/sessions/{session_id}/tracks/{discord_user_id}/spectrogram"
_DOWNLOAD_PATH = "/api/sessions/{session_id}/tracks/{discord_user_id}/download"


@dataclass(frozen=True)
class _Grant:
    """A rule's verdict: the track, and what the rule found out saying so."""

    track: Track
    #: The guild the session belongs to, or `None` when the rule that
    #: granted this never had to ask. Playback does not: its whole rule is
    #: `session_participant`, and a guild it does not need is a guild it
    #: should not select.
    guild_id: int | None
    #: Whether the person asking was in the session. Always true under the
    #: playback rule, by construction; under the download rule it is what
    #: the audit line is for.
    by_participant: bool


@dataclass(frozen=True)
class _Rule:
    """One of the two ways a request can become entitled to a track.

    The whole difference between the two routes, in a value. Everything
    else about them -- the parse, the refusal, the key check, the size,
    the streaming -- is shared, and keeping the difference to this makes
    it reviewable in one place instead of by comparing two handlers.
    """

    #: Asks the directory the question this rule is. Async, because both
    #: of them are a database round trip.
    ask: Callable[[AudioDelivery, int, int, int], Awaitable[_Grant | None]]
    #: Which of the two acts this is. Read only to choose between two
    #: literal log messages, and a flag rather than the message itself
    #: because rule R1 requires every log message to be text written in
    #: the source -- a message carried on a value would not be.
    download: bool
    #: What the refusal log line says happened. Bounded literals from this
    #: source, and deliberately not fine-grained: one reason per rule, so
    #: a log cannot distinguish refusals the HTTP response must not.
    refusal_reason: str


async def _playback_grant(
    delivery: AudioDelivery, session_id: int, speaker_id: int, listener: int
) -> _Grant | None:
    track = await delivery.tracks.track_for(session_id, speaker_id, requested_by=listener)
    if track is None:
        return None
    # True without asking: `track_for` answers nothing at all to somebody
    # who was not in the session, so a track in hand *is* participation.
    return _Grant(track=track, guild_id=None, by_participant=True)


async def _download_grant(
    delivery: AudioDelivery, session_id: int, speaker_id: int, listener: int
) -> _Grant | None:
    found = await delivery.tracks.downloadable_track_for(
        session_id, speaker_id, requested_by=listener
    )
    if found is None:
        return None
    return _Grant(
        track=found.track,
        guild_id=found.guild_id,
        by_participant=found.by_participant,
    )


#: Participants of the session, and nobody else. Unchanged, and the
#: download route was built beside it rather than through it precisely so
#: that it stays unchanged.
_PLAYBACK = _Rule(ask=_playback_grant, download=False, refusal_reason="not_a_participant")

#: Participants, plus administrators of the guild, and only while that
#: guild has switched the capability on. One reason for all three ways to
#: fail: the response cannot tell them apart and neither should a reader
#: of the log conclude the response did.
_DOWNLOAD = _Rule(ask=_download_grant, download=True, refusal_reason="download_not_permitted")


@dataclass(frozen=True)
class _AuthorisedTrack:
    """A track this request has been found entitled to, and its size.

    The last two carry the granting rule's findings through to the audit
    line, which is written after the last byte has left. Recovering them
    there instead would mean asking the database a second question about
    an authorisation that has already been decided.
    """

    track: Track
    ciphertext_bytes: int
    session_id: int
    speaker_id: int
    listener: int
    #: `None` under the playback rule, which never asks about a guild.
    guild_id: int | None
    by_participant: bool


async def _authorised_track(request: web.Request) -> _AuthorisedTrack | web.Response:
    """The access rule for playing a recording back, or the refusal."""
    return await _resolve(request, _PLAYBACK)


async def _authorised_download(request: web.Request) -> _AuthorisedTrack | web.Response:
    """The access rule for taking a copy away, or the refusal."""
    return await _resolve(request, _DOWNLOAD)


async def _resolve(request: web.Request, rule: _Rule) -> _AuthorisedTrack | web.Response:
    """Applies one rule, or produces the refusal to answer with.

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
    granted = await rule.ask(delivery, session_id, speaker_id, listener)
    if granted is None:
        log_event(
            log,
            logging.INFO,
            Event.CONSOLE_TRACK_REFUSED,
            "Refused a copy of a recording to somebody not entitled to take one"
            if rule.download
            else "Refused a recording to somebody outside the session it belongs to",
            session_id=session_id,
            discord_user_id=speaker_id,
            requested_by=listener,
            reason=rule.refusal_reason,
        )
        return _no_such_recording()

    track = granted.track
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

    return _AuthorisedTrack(
        track,
        ciphertext_bytes,
        session_id,
        speaker_id,
        listener,
        granted.guild_id,
        granted.by_participant,
    )


async def _stored_picture(
    delivery: AudioDelivery, resolved: _AuthorisedTrack, data_key: bytes
) -> Spectrogram | None:
    """The artefact the worker drew, or `None` to draw the track instead.

    `None` covers three cases the caller must treat identically: this job
    has no stored picture, the object it names is not in the bucket, and
    the object is not a picture this build can read. All three have the
    same remedy and the same cost, and none of them is a reason to refuse
    somebody a view they are entitled to.

    Deliberately not `CorruptRecording`-transparent. A stored artefact
    that will not decode is *not* the same event as a stored recording
    that will not decrypt: the recording is the only copy of what somebody
    said and its loss needs a human, while the artefact is a derived
    convenience the next line of this handler recreates. Letting the
    exception out would have answered 500 for a request that can be
    answered perfectly well.
    """
    key = resolved.track.spectrogram_key
    if key is None:
        return None
    try:
        return await stored_spectrogram(delivery.source, key, data_key)
    except (KeyError, CorruptRecording) as exc:
        log_exception(
            log,
            logging.INFO,
            Event.CONSOLE_SPECTROGRAM_REDRAWN,
            "A stored spectrogram could not be used; drawing this track instead",
            exc,
            session_id=resolved.session_id,
            discord_user_id=resolved.speaker_id,
        )
        return None


async def track_spectrogram(request: web.Request) -> web.StreamResponse:
    """One track as a picture of where its speech is.

    Behind the same gate as the audio itself, and that is not a formality:
    a spectrogram is a rendering of somebody's voice, and it shows when
    they spoke and for how long. It is less than the audio; it is not
    nothing, and the rule that governs the audio is the right one for it.

    **The gate is in front of the artefact too, and stays there.** A guild
    that switched `spectrograms_by_default` on has a picture the worker
    already drew, and answering from it makes this endpoint cheap; it must
    not make it *open*. `_authorised_track` runs first and runs on every
    request -- it is the same `session_participant` query, decided again,
    never cached -- and only then does anything look for a stored picture.
    A cache of the payload must never quietly become a cache of the
    permission, and the ordering here is what keeps those two apart.

    It also runs first for a second reason: `_authorised_track` is what
    refuses a track whose recording the retention sweep has erased. A
    swept job offers no picture either, because the sweep deleted both --
    and because the row it would have come from no longer names one.

    **Either source answers the same thing.** A job from before the
    setting was enabled, a guild that never enabled it, an artefact that
    is missing or that a later build cannot read: all of them fall through
    to drawing the track, which is what every view cost before artefacts
    existed. The contract does not depend on which happened.

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
        picture = await _stored_picture(delivery, resolved, data_key)
        if picture is None:
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
    return await _stream_track(request, resolved, _PLAYBACK)


async def track_download(request: web.Request) -> web.StreamResponse:
    """The same recording, as a file to keep rather than a stream to play.

    Everything about producing the bytes is `_stream_track`, shared with
    playback down to the `Range` arithmetic: a second copy of the
    decryption path is a second place for the last byte of a partial
    response to go missing, and a download of a long meeting is exactly
    the transfer that gets interrupted and resumed.

    The two differences are the rule that got here -- administrators of
    the guild as well as participants, and only where the guild has
    switched the capability on -- and one header.
    """
    resolved = await _authorised_download(request)
    if isinstance(resolved, web.Response):
        return resolved
    return await _stream_track(request, resolved, _DOWNLOAD)


def _attachment_filename(session_id: int, speaker_id: int) -> str:
    """What the file is called once it has left the console.

    **The filename names a speaker, and that is itself a disclosure.** It
    is the one part of this response that outlives the request: it lands
    in a Downloads folder, gets attached to a mail, gets read over a
    shoulder, appears in a screenshot of a file listing -- with none of
    the context the console gave it and none of its access control.

    So it names the speaker by **Discord snowflake and never by display
    name**. A name in a filename is a statement about a person to
    everybody who ever sees the file; a snowflake says nothing to a
    bystander and is exact for anybody entitled to resolve it, which is
    the same reasoning that puts display names in
    `sturnus.observability.fields.DENIED_NAMES` for logs. It also means
    this route needs no name lookup at all, so a display name is not
    merely omitted here -- it is not in this process's hands.

    The session id is included because a speaker has one recording per
    meeting and a folder of `speaker-100.wav` files is a folder of files
    nobody can tell apart.

    Both values are `int`, parsed out of the path by `_resolve` before
    anything reached this function, so nothing a client sends can be
    written into a response header as text.
    """
    return f"sturnus-session-{session_id}-speaker-{speaker_id}.wav"


async def _stream_track(
    request: web.Request, resolved: _AuthorisedTrack, rule: _Rule
) -> web.StreamResponse:
    """The bytes, and the shape of the HTTP around them.

    Shared by both routes on purpose: which status, which headers and the
    order the two are decided in is the same problem whether somebody is
    playing a recording or keeping it, and the parts that genuinely differ
    -- one header and one audit line -- are the only places `rule` is
    read.
    """
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
    if rule.download:
        # The whole of what makes this response a download rather than a
        # stream. `attachment` is what stops a browser playing it inline
        # and starts it writing a file; the name is `_attachment_filename`'s
        # decision, and its docstring is where that decision is argued.
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{_attachment_filename(session_id, speaker_id)}"'
        )
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
    # thing the console does -- who reached whose voice -- and it is the
    # reason `requested_by` exists as a field at all.
    if rule.download:
        # WARNING, not INFO, and not because a download is a failure. A
        # copy in a Downloads folder outlives every control this system
        # has: retention cannot sweep it, a withdrawn consent cannot
        # reach it, and nothing but this line records that it was ever
        # made. `by_participant` is what separates the two acts the route
        # can perform -- somebody keeping a copy of their own meeting, and
        # an administrator obtaining a recording of a meeting they were
        # not in, which is the one read in this system that reaches
        # another person's voice without the reader having been in the
        # room with them.
        #
        # No display name: it is in `DENIED_NAMES`, and this process does
        # not hold one anyway (see `_attachment_filename`).
        log_event(
            log,
            logging.WARNING,
            Event.CONSOLE_TRACK_DOWNLOADED,
            "A copy of a recording left the console as a file",
            session_id=session_id,
            guild_id=resolved.guild_id,
            discord_user_id=speaker_id,
            requested_by=listener,
            by_participant=resolved.by_participant,
            bytes=span.length,
        )
    else:
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
            # A path of its own rather than `?download=1` on the one above.
            # The two are different acts under different rules, and a
            # router that can show which one a request was is a router an
            # access log, a rate limit and a reverse proxy can all agree
            # with.
            web.get(_DOWNLOAD_PATH, require_session(track_download)),
        ]
    )
