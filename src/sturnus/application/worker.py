"""The transcription worker (Spec 5.3, Spec 7, Spec 8, Spec 12.1).

`process_one` claims one job, decrypts its audio, transcribes it, stores the
transcript, and — if it was the session's last job — creates the session's
protocol document. Every temporary file it touches is removed in a
`finally`, regardless of where processing fails: a decrypted recording left
on disk is exactly what the envelope encryption (Spec 12.1) exists to
prevent, while every other failure here is allowed to fail loudly.

Deleting the audio object itself is deliberately **not** done here, even
though it would be the obvious next step after a successful transcription.
Spec 12.2 keeps the object for `audio_retention_days` so a poor
transcription can be redone from the original audio; that deletion belongs
to the retention sweep (`sturnus.application.retention`), not to this job.

**The spectrogram, for a guild that asked for one.** `spectrograms_by_default`
makes this function draw each track's picture once, at completion, instead
of leaving the console to draw it again out of S3 on every view. It is done
here for one reason: this is the only moment in the system where the
plaintext WAV is free. It exists on disk for the length of one job and then
the `finally` removes it, and anybody who wants the same picture afterwards
has to fetch and decrypt the whole recording to get it.

That stored picture comes with a rule, which is not this module's to enforce
but is this module's to understand, because it is why the artefact's key is
written onto the job rather than derived when it is needed: **a stored
spectrogram is deleted when its audio is deleted**. The retention sweep
deletes what `transcription_job.spectrogram_key` names, in the same pass as
the recording. Nothing else would ever delete it, and a picture of when
somebody spoke and for how long that outlives their recording's retention
window is exactly the thing the window exists to end.

Language (Spec 7, Spec 11). Two things can decide what language a
recording is transcribed as, and the order between them is the whole
point. `transcription_language` is per-guild configuration and wins
outright: when a guild names a language it is handed to the engine, no
detection runs, and *nothing* is written to `detected_language`. Both
halves of that matter. A configured setting that a guess may override is
a trap, and here it would be a self-locking one -- `set_detected_language`
pins the first job's guess for the rest of the session, so the guess would
go on beating the configuration on every later job of that session, and
the column would stop meaning "what the engine detected" and start meaning
"what was configured when this session's first job ran", with no way to
tell the two apart in the data.

Detection remains available, and is what an unconfigured guild and a guild
that sets the value to `auto` (`sturnus.domain.settings.DETECT_LANGUAGE`)
both get: then, and only then, a speaker's first job asks the engine to
detect the language and persists what it found, and every later job for
that same speaker passes the stored language back in, so one protocol
never mixes languages mid-session because the engine's guess drifted.
That the guess needs pinning at all is the measure of how weak it is: it
is made on one speaker's track, which `vad_filter` has already reduced to
the fragments where that person actually spoke, so a participant whose
first contribution is a three-second agreement is close to a coin flip
between several languages -- and whichever one comes back then governs
every remaining job for them.

`transcription_prompt` (Spec 11) is the vocabulary the engine is biased
towards while decoding -- an organisation's project names, which is
precisely what a general model has never seen and will replace with
something it has. It is read here, per job, for the same reason the
document settings below are.

Dependency-rule note: this module lives in `sturnus.application`, which must
never import `sturnus.infrastructure` (tests/test_architecture.py). Every
collaborator below is therefore a narrow local `Protocol`, the same pattern
`sturnus.application.assembly` uses for `SessionReader`/`JobReader`/
`LinkReader` -- the concrete adapters that satisfy these shapes live in
`sturnus.infrastructure` and `sturnus.entrypoints.worker`, never imported
here by name. This is also why a permanently-rejected document creation is
recognised below by its exception's class *name* rather than by catching
`sturnus.infrastructure.documents.outline.PermanentDocumentError` directly:
importing that type here would be exactly the violation this rule exists
to prevent.

Once a session's last job completes, `process_one` calls
`sturnus.application.assembly.assemble` to merge *every* participant's
stored transcript -- not just the job that happened to finish last -- into
one chronological `Transcript`, and hands it to
`sturnus.application.exporting.publish_session`, which renders it once per
format and writes it to every destination the guild has enabled. **Where a
protocol goes, in what shape, and what happens when one destination fails
are that module's decisions, not this one's**; what stays here is the
assembly that produces the transcript and the session bookkeeping that
follows a successful publish. `assemble` needs a `SessionReader`
(`session_bounds`, `participant_names`, `audio_epoch`), a `JobReader`
(`transcripts_for`), and a `LinkReader` (`external_identity`). `SessionStore`
below is widened to be structurally a `SessionReader` as well as its
original language-pinning/completion shape, so the one `sessions`
collaborator satisfies both; `jobs` is threaded through `process_one` as
its own parameter, typed with `assembly`'s own `JobReader` protocol rather
than duplicating it here.

`links` is typed with this module's own `LinkRepository`, not `assembly`'s
`LinkReader`, and `config` (`ConfigReader`) is threaded through alongside
it: `transcription_language`, `transcription_prompt`, `document_target`,
`document_provider`, and `merge_gap_seconds` are all per-guild settings
(Spec 11) that this one process cannot resolve until a session -- and
therefore its guild -- is in hand. The first two are read in `process_one`
itself, just before the engine is called; the last three inside
`_create_session_document`, alongside the guild's `guild_export_target`
rows, which are what `document_target` is now only the fallback for. None
of them is read once at process start, because one worker serves every
guild. `sturnus.application.assembly.BoundLinks` adapts one call's
resolved provider back down to the plain `LinkReader` shape `assemble`
itself calls, so `assemble` stays ignorant of configuration entirely.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
import wave
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sturnus.application.assembly import (
    BoundLinks,
    JobReader,
    LinkRepository,
    assemble,
    merge_gap_from,
    serialize_transcript,
)
from sturnus.application.documents import ChannelRef
from sturnus.application.export_formats import OUTLINE, RenderRequest, format_named
from sturnus.application.exporting import (
    Destination,
    ExportPorts,
    destinations_for,
    publish_session,
)
from sturnus.application.recording import spectrogram_key
from sturnus.application.spectrogram import draw, encode_artefact
from sturnus.application.transcription import TranscriptionEngine
from sturnus.domain import settings as domain_settings
from sturnus.domain.measurements import JobMeasurements, RecordedAudio
from sturnus.observability.events import Event, log_event, log_exception

log = logging.getLogger(__name__)

#: A minimal, self-contained fallback template. It is the default
#: `template_source` for `process_one` so the twelve tests in
#: `tests/application/test_worker.py` -- none of which pass one -- can run
#: without this module reaching into `sturnus.infrastructure` for the
#: packaged `outline_template.md.j2`. Production callers
#: (`sturnus.entrypoints.worker`) load the real packaged template from disk
#: and pass its text in explicitly instead.
_FALLBACK_TEMPLATE = (
    "{% for block in blocks %}"
    "**{{ block.time }}** · {{ block.speaker.discord_display_name | md }}\n\n"
    "{{ block.text | md }}\n\n"
    "{% endfor %}"
)


class Queue(Protocol):
    """Where jobs are claimed, completed, and failed (`sturnus.infrastructure.db.queue.JobQueue`).

    `claim` deliberately returns `object | None` rather than a concrete job
    type: the real job (`ClaimedJob`) lives in `sturnus.infrastructure.db.
    queue`, which this module must never import. `process_one` narrows the
    claimed value to `_ClaimedJobShape` with `cast` immediately after.

    `lease` on `complete` and `fail` is the claim `process_one` is holding,
    handed back so the queue can refuse a worker whose job has since been
    taken over by another one -- see `sturnus.infrastructure.db.queue.
    JobQueue.complete`. `process_one` always has one, so it always passes
    one.

    `audio` travels with the transcript rather than through a write of its
    own, because it is read off the same two files in the same breath and
    is fenced by the same lease: a worker that has lost its job must not
    stamp the row with a size it measured for a copy nobody is waiting
    for. `None` when the header could not be read, which leaves the
    columns null -- see `_recorded_audio`.
    """

    async def claim(self) -> object | None: ...

    async def complete(
        self,
        job_id: int,
        transcript: str,
        measurements: JobMeasurements | None = None,
        *,
        lease: datetime | None = None,
        audio: RecordedAudio | None = None,
    ) -> bool: ...

    #: Returns whether the job is now **dead** -- out of attempts, so this
    #: recording will never be transcribed -- rather than queued for another
    #: try. Only the queue can answer that, because only the queue counts
    #: the attempts, and without the answer a caller cannot tell permanent
    #: loss from an ordinary retry: `process_one` returns `True` for both.
    async def fail(
        self, job_id: int, error: str, max_attempts: int, *, lease: datetime | None = None
    ) -> bool: ...


class AudioDownloader(Protocol):
    """Where the encrypted recording is fetched from before it can be decrypted."""

    async def get(self, key: str, target: Path) -> None: ...


class Decryptor(Protocol):
    """Unwraps the session's data key and decrypts the recording with it.

    Synchronous by design (`sturnus.infrastructure.crypto.KeyWrapper.unwrap`
    and `decrypt_file` are both CPU/IO-bound, not natively awaitable);
    `process_one` runs it through `asyncio.to_thread` itself.
    """

    def decrypt_to(self, source: Path, target: Path, wrapped: bytes, key_id: str) -> None: ...


class SpectrogramStore(Protocol):
    """Where a track's stored picture is written down and put.

    Two methods rather than one, and **an order between them that is the
    point of the port**: `record` first, `put` second. The reverse order
    has one failure mode this design cannot afford -- an object in the
    bucket that no row names. The retention sweep deletes the artefact
    `transcription_job.spectrogram_key` points at, so an artefact nothing
    points at is an artefact nothing will ever delete: a rendering of
    somebody's voice activity outliving the recording it was drawn from,
    which is precisely what the rule attached to this feature forbids.

    Written in this order the worst case is the harmless one. A `record`
    that succeeds and a `put` that fails leaves a job naming an object
    that is not there, the read path finds nothing and draws the track
    itself (`sturnus.console.routes_audio.track_spectrogram`), and the
    sweep asks the store to delete a key that was never written -- which
    an S3 `DELETE` answers successfully, exactly as it does for the audio
    object the same sweep may have deleted twice.

    Two backends behind one port because the two writes are one act. A
    port offering only half of it is a port that will eventually be used
    to do half of it.
    """

    async def record(self, job_id: int, key: str) -> None:
        """Writes the artefact's key onto the job, before the object exists."""
        ...

    async def put_sealed(self, key: str, source: Path, wrapped: bytes, key_id: str) -> None:
        """Seals `source` under the job's own data key and stores it.

        Sealed rather than stored as it is: the artefact is a rendering of
        somebody's voice activity, which is why the console puts it behind
        the same authorisation rule as the audio, and an object in this
        bucket readable by anybody holding the bucket would be the single
        exception to envelope encryption in the whole system.

        `wrapped`/`key_id` are the job's own, so the picture and the
        recording are locked with the same key and a master-key rotation
        reaches both or neither.
        """
        ...


class ConfigReader(Protocol):
    """Where per-guild runtime configuration is read from (Spec 11).

    Matches `sturnus.infrastructure.db.config_store.ConfigStore.get`
    structurally: falls back to that key's entry in `sturnus.domain.
    settings.DEFAULTS` when nothing is stored, and to `None` for a key
    with no default (e.g. `document_target`) that a guild never set.
    """

    async def get(self, guild_id: int, key: str) -> str | None: ...


class SessionStore(Protocol):
    """The session-scoped bookkeeping this job needs.

    Covers the original language-pinning/completion bookkeeping
    (`detected_language`/`set_detected_language`/`mark_documented`) *and*
    everything `sturnus.application.assembly.assemble` needs to know about
    a session's participants (`participant_names`/`audio_epoch`/
    `session_bounds`) -- widened to that full shape so the one `sessions`
    collaborator `process_one` already receives can also be passed to
    `assemble` as its `SessionReader`, structurally, without a separate
    parameter.
    """

    async def detected_language(self, session_id: int, user_id: int) -> str | None: ...

    async def set_detected_language(self, session_id: int, user_id: int, lang: str) -> None: ...

    async def mark_documented(
        self, session_id: int, doc_id: str, url: str, provider: str
    ) -> None: ...

    async def participant_names(self, session_id: int) -> dict[int, str]: ...

    async def audio_epoch(self, session_id: int, user_id: int) -> datetime | None: ...

    async def session_bounds(self, session_id: int) -> tuple[datetime, datetime]: ...

    async def channel_ref(self, session_id: int) -> tuple[int, int, str | None]:
        """`(guild_id, channel_id, channel_name)` for the protocol's heading.

        The name is whatever the bot saw when the session opened, and may
        be `None` for sessions recorded before it was captured.
        """
        ...

    async def guild_id(self, session_id: int) -> int:
        """The guild a session belongs to.

        Needed to resolve per-guild configuration (Spec 11) twice per job:
        `transcription_language` and `transcription_prompt` before the
        engine is called (`process_one`), and `document_target`,
        `document_provider` and `merge_gap_seconds` when a session's last
        job creates the document (`_create_session_document`).
        """
        ...

    async def closed_undocumented_sessions(self) -> list[int]:
        """Closed sessions whose jobs are all terminal but which never got documented.

        Used by `retry_pending_documents`, not by `process_one` itself --
        see that function's docstring for why a session can end up here at
        all despite `process_one` already trying once.
        """
        ...

    async def sessions_with_unpublished_targets(self) -> list[int]:
        """Sessions that reached some destinations of their guild but not all.

        The candidate set a second destination made necessary. A session
        whose primary destination succeeded is `documented` and therefore
        invisible to `closed_undocumented_sessions` -- so a Markdown export
        that failed beside a successful Outline document would never be
        retried, and the guild would be missing an artefact with nothing
        anywhere saying so.

        Answering it needs both `guild_export_target` and
        `session_document`, which is why it is the store's question rather
        than one this module could derive: "every enabled target of this
        session's guild that has no row for this session" is one statement
        in SQL and three round trips per session otherwise.
        """
        ...


class _ClaimedJobShape(Protocol):
    """The attributes `process_one` reads off whatever `Queue.claim` returns.

    Matches `sturnus.infrastructure.db.queue.ClaimedJob` structurally
    without importing it (see `Queue.claim`'s docstring).
    """

    id: int
    session_id: int
    discord_user_id: int
    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes
    #: When this claim was stamped, and the token that proves the job is
    #: still this worker's when it reports back.
    claimed_at: datetime
    #: `None` for every job nobody asked a question about, which is
    #: almost all of them.
    requested_model: str | None


def _configured_language(configured: str | None) -> str | None:
    """The language a guild named, or `None` when it asked for detection.

    Three stored values mean "detect", and the caller has no reason to
    tell them apart: `auto` (`sturnus.domain.settings.DETECT_LANGUAGE`),
    nothing at all, and blank. The last two are unreachable through
    `/config` -- the key has a default and clearing restores it -- but
    neither is unreachable in practice: `ConfigReader` is a protocol, and
    `guild_config` is a table an operator is told they may edit with SQL
    (`docs/operations.md` section 4.1), which `ConfigStore.set`'s
    validation never sees. A blank value has to mean *something*, and the
    alternative is passing `""` to the engine, which rejects it -- turning
    one careless `UPDATE` into every job of that guild failing.

    Surrounding whitespace is stripped for the same reason: `" de "` is
    not a language code faster-whisper knows, and a value typed with a
    trailing space is not a decision to fail every job.
    """
    if configured is None:
        return None
    named = configured.strip()
    if not named or named.casefold() == domain_settings.DETECT_LANGUAGE:
        return None
    return named


def _recorded_audio(plaintext: Path, stored: Path) -> RecordedAudio | None:
    """What this track is, read off the two files the job already has.

    The one moment in the system where both exist at once: the encrypted
    object has just been downloaded and the plaintext WAV has just been
    decrypted out of it, and both are deleted a few lines later. Every
    later reader -- the spectrogram, a metadata tab -- would otherwise pay
    a ranged GET and a chunk decrypt to walk the same RIFF header, plus a
    second round trip to ask S3 how big the object is.

    Read with `wave` rather than by walking the chunk list as
    `sturnus.console.spectrogram.parse_track_format` does, because the
    file is on local disk here and the standard library is already the
    writer: `sturnus.infrastructure.audio.SpeakerWriter` produced this
    file through `wave`. The streaming reader exists because a console
    request has no file, not because two parsers were wanted.

    `None` rather than an exception for anything unreadable. This is
    metadata about a recording whose transcript is the point, and failing
    a job -- and eventually killing it after `max_attempts` -- over a
    header nobody can parse would trade the words for the file size. A
    null column says "nobody could look", which is the truth.
    """
    try:
        with wave.open(str(plaintext), "rb") as track:
            return RecordedAudio(
                sample_rate=track.getframerate(),
                channels=track.getnchannels(),
                stored_bytes=stored.stat().st_size,
            )
    except (OSError, wave.Error, ValueError, EOFError):
        return None


async def _guild_timezone(config: ConfigReader, guild: int) -> tzinfo:
    """The timezone the protocol's times are written in (Spec 11).

    Falls back to UTC on an unusable value rather than failing the job: a
    protocol with the wrong offset is a smaller loss than no protocol at
    all, and the log line says which guild to go and fix. The default is
    Europe/Berlin, so reaching UTC here means someone set something odd.
    """
    name = await config.get(guild, domain_settings.TIMEZONE)
    if name is None:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "Guild %d has an unusable %s (%r); writing this protocol in UTC",
            guild,
            domain_settings.TIMEZONE,
            name,
        )
        return UTC


async def _legacy_destination(
    config: ConfigReader, session_id: int, guild: int
) -> Destination | None:
    """The single Outline destination a guild's `guild_config` describes.

    What every guild running today is configured with, and what they keep:
    there is no migration that moves `document_target` into
    `guild_export_target`, so a guild with no rows in that table publishes
    exactly where it always did. See
    `sturnus.application.exporting.destinations_for` -- this is its
    `fallback`, used only when the table has nothing for the guild.

    `None` for a guild that never set `document_target`. It used to raise,
    which was right when it was the only destination there could be and is
    wrong now: a guild that configured a destination in the table and never
    touched the legacy key must not have its publish fail on the absence of
    a setting it deliberately does not use. The caller reports "nowhere to
    publish" once, having seen both.
    """
    target = await config.get(guild, domain_settings.DOCUMENT_TARGET)
    if target is None:
        return None
    provider = await config.get(guild, domain_settings.DOCUMENT_PROVIDER)
    # `DEFAULTS` supplies "outline" when a guild never set this explicitly
    # (see `ConfigReader`'s docstring) -- unlike `document_target`, this
    # key always resolves to a value.
    assert provider is not None
    entry = format_named(OUTLINE)
    # The registry always holds `outline`; it is the entry the rest of this
    # system was built around. Asserting keeps the signature honest instead
    # of widening it for a case that cannot happen.
    assert entry is not None
    return Destination(
        session_id=session_id,
        target_id=None,
        format=entry,
        target=target,
        # The configured `document_provider` verbatim, not the format name:
        # this is what `session.document_provider` has always been written
        # with, and nothing about an existing guild's rows changes here.
        provider=provider,
    )


async def _create_session_document(
    exports: ExportPorts,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkRepository,
    config: ConfigReader,
    session_id: int,
    template_source: str,
    now: datetime,
) -> None:
    """Assembles the transcript and publishes it to every destination.

    Calls `sturnus.application.assembly.assemble` to merge every
    participant's stored transcript -- not just the job that happened to
    complete the session -- into one chronological `Transcript`, then hands
    it to `sturnus.application.exporting.publish_session`. Rendering, the
    choice of destinations, the survival of one that fails and the
    de-duplication that keeps a retry from republishing what worked are all
    that module's; what is left here is the assembly and the session
    bookkeeping that follows.

    Resolves the guild-scoped settings (Spec 11) here, at publication time,
    rather than once at process start -- the worker serves every guild from
    one process and cannot know which guild's values apply until a session,
    and therefore its guild, is known:

    - the guild's `guild_export_target` rows: where the protocol goes, and
      in what shape. A guild with none falls back to `document_target`.
    - `document_provider`: which provider's account-link mapping a
      speaker's external identity is read from, via `BoundLinks`. Still
      guild configuration and still one per session, because it decides how
      a *speaker* is identified in the transcript rather than where the
      transcript is sent -- one transcript is assembled and every
      destination receives the same people.
    - `merge_gap_seconds`: how long a pause may be before one speaker's
      blocks split, forwarded to `assemble`.

    `session.document_url` is stamped from the primary destination alone,
    which is what keeps the announcement path and everything else already
    reading a session working unchanged.
    """
    guild = await sessions.guild_id(session_id)

    provider = await config.get(guild, domain_settings.DOCUMENT_PROVIDER)
    assert provider is not None

    destinations = destinations_for(
        session_id,
        await exports.targets.enabled_for(guild),
        await _legacy_destination(config, session_id, guild),
    )
    if not destinations:
        raise RuntimeError(
            f"guild {guild} has no enabled export target and no "
            f"{domain_settings.DOCUMENT_TARGET!r} configured; cannot publish a protocol "
            "until an administrator configures a destination"
        )

    merge_gap = merge_gap_from(await config.get(guild, domain_settings.MERGE_GAP_SECONDS))

    transcript = await assemble(
        session_id, sessions, jobs, BoundLinks(links, provider), UTC, merge_gap
    )

    # `assemble` works in UTC deliberately -- ordering and merging must not
    # depend on a local offset -- and only the rendering is localised.
    tz = await _guild_timezone(config, guild)
    ref_guild, ref_channel, ref_name = await sessions.channel_ref(session_id)
    report = await publish_session(
        session_id,
        destinations,
        RenderRequest(
            transcript=transcript,
            tz=tz,
            channel=ChannelRef(ref_guild, ref_channel, ref_name),
            outline_template=template_source,
        ),
        exports,
        now,
    )
    if report.primary is None:
        return
    await sessions.mark_documented(
        session_id,
        report.primary.document.id,
        report.primary.document.url,
        report.primary.destination.provider,
    )
    log_event(
        log,
        logging.INFO,
        Event.SESSION_DOCUMENT_CREATED,
        "Published the session protocol",
        session_id=session_id,
        document_id=report.primary.document.id,
        provider=report.primary.destination.provider,
        count=len(destinations),
        failed=report.failed,
        skipped=report.skipped,
        participants=len(transcript.participants),
        blocks=len(transcript.blocks),
    )


#: How much of the plaintext WAV is handed to the FFT at a time. The
#: number is not about memory -- `draw` keeps one window regardless -- it
#: is about giving the event loop the chance to run between pieces.
#: Drawing an hour-long track is on the order of a second of arithmetic,
#: and a worker that did all of it without an `await` in the middle would
#: stop answering its own health probe while it did.
_WAV_PIECE_BYTES = 256 * 1024


async def _wav_pieces(path: Path) -> AsyncGenerator[bytes, None]:
    """Yields the decrypted WAV off the scratch disk, a piece at a time.

    Each read is a thread hop for the same reason every other blocking
    call in this process is (`sturnus.infrastructure.objectstore`): the
    file is on a pod's ephemeral disk, and a synchronous read of it inside
    the loop is a stall nothing can preempt.
    """
    with path.open("rb") as handle:
        while piece := await asyncio.to_thread(handle.read, _WAV_PIECE_BYTES):
            yield piece


async def _store_spectrogram(
    spectrograms: SpectrogramStore,
    config: ConfigReader,
    job: _ClaimedJobShape,
    guild: int,
    wav_path: Path,
) -> None:
    """Draws this track's picture and stores it, if the guild asked for one.

    **Never raises.** A transcription that succeeded and could not be
    drawn has done the valuable part: the transcript is stored, the job is
    `done`, and the session's document will be written from it. Letting a
    failed artefact reach `process_one`'s handler would return an
    already-transcribed job to the queue and transcribe it again -- minutes
    of GPU-less inference -- to retry a picture the console can draw for
    itself in a second. So this logs and moves on, and the read path falls
    back to computing.

    Drawn here rather than by a later sweep because *here* is the only
    place the plaintext is free. `process_one`'s `finally` deletes the
    decrypted WAV within milliseconds of this returning, and every other
    place in the system that wants this picture has to fetch and decrypt
    the whole object again to get it.
    """
    try:
        offered = await config.get(guild, domain_settings.SPECTROGRAMS_BY_DEFAULT)
        if not domain_settings.is_true(offered):
            return

        pieces = _wav_pieces(wav_path)
        try:
            picture = await draw(pieces)
        finally:
            await pieces.aclose()

        # Beside the WAV in the job's scratch directory, so the `finally`
        # that removes decrypted speech removes this too -- an artefact is
        # a rendering of the same voice and gets the same treatment.
        artefact = wav_path.with_name("spectrogram.json")
        body = encode_artefact(picture)
        await asyncio.to_thread(artefact.write_bytes, body)

        key = spectrogram_key(job.session_id, job.discord_user_id)
        # Recorded before it is stored. See `SpectrogramStore`: an object
        # no row names is an object the retention sweep cannot delete.
        await spectrograms.record(job.id, key)
        await spectrograms.put_sealed(key, artefact, job.wrapped_data_key, job.encryption_key_id)
        log_event(
            log,
            logging.INFO,
            Event.SPECTROGRAM_STORED,
            "Stored a spectrogram beside a recording",
            job_id=job.id,
            session_id=job.session_id,
            discord_user_id=job.discord_user_id,
            bytes=len(body),
        )
    except Exception as exc:
        log_exception(
            log,
            logging.WARNING,
            Event.SPECTROGRAM_FAILED,
            "Could not store a spectrogram; the console will draw this track on demand",
            exc,
            job_id=job.id,
            session_id=job.session_id,
            stage="spectrogram",
        )


async def process_one(
    queue: Queue,
    engine: TranscriptionEngine,
    store: AudioDownloader,
    crypto: Decryptor,
    exports: ExportPorts,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkRepository,
    config: ConfigReader,
    spectrograms: SpectrogramStore,
    work_dir: Path,
    max_attempts: int,
    template_source: str = _FALLBACK_TEMPLATE,
    now: datetime | None = None,
) -> bool:
    """Processes one claimed job end to end. Returns `False` if the queue was empty.

    Order, and why it is this order:

    1. Claim -- nothing claimed means there is no work; the caller backs off.
    2. Download the encrypted object to a scratch directory under `work_dir`.
    3. Unwrap the data key and decrypt to a plaintext WAV, still on disk.
    4. Resolve the guild's `transcription_language` and
       `transcription_prompt` (Spec 11), then transcribe -- configured
       language first, detection and per-speaker pinning only when the
       guild asked for it (Spec 7; see the module docstring for the order
       and why it is that way round).
    5. Store the transcript on the job; ask whether it was the session's last.
    6. If the guild asked for it (`spectrograms_by_default`): draw this
       track's spectrogram from the WAV that is still on disk and store it
       beside the audio (`_store_spectrogram`). Between step 5 and step 7
       because that is the only window in which the plaintext exists and
       the transcript is already safe.
    7. If it was the session's last: assemble every participant's stored
       transcript into one transcript (`_create_session_document`,
       `sturnus.application.assembly.assemble`), publish it to every
       destination the guild has enabled
       (`sturnus.application.exporting.publish_session`), and mark the
       session documented from the primary one.
    8. Every temporary file made in steps 2-3 is removed in a `finally`, so
       a failure anywhere above never leaves decrypted speech on disk. The
       audio object in S3 is left alone deliberately -- see the module
       docstring.

    `jobs` and `links` are only ever read from in step 7, but are accepted
    as parameters up front (rather than constructed lazily) so every
    collaborator `process_one` needs is visible in its signature, matching
    `sessions`/`exports`/`queue` and the rest.

    `now` is the instant a published destination is recorded with, and
    defaults to the clock rather than being required: this function is
    already called from a loop that has no clock of its own, and a caller
    that pins it (every test of this function) gets a deterministic
    `session_document.created_at`.

    **Error handling (Defect 4).** Steps 2-5 are wrapped in a `try`/`except`
    that routes *any* failure -- a failed S3 download, a decrypt error, a
    database error storing the transcript, anything at all other than the
    transcription failure already handled by its own narrower `except`
    below -- through `queue.fail`, exactly like a transcription failure is.
    Without this, such a failure propagated straight out of `process_one`;
    the entrypoint has no handler either, so the whole worker process died,
    and the job it was holding stayed `running` forever (`claim` only ever
    selects `pending` jobs -- see `sturnus.infrastructure.db.queue.JobQueue`
    for the lease that also reclaims a job stranded this way).

    Step 7 (publication) is deliberately handled by a *separate*
    `try`/`except`, outside the one above: by the time it runs, `queue.
    complete` has already succeeded and the job is `done` -- calling
    `queue.fail` on it would incorrectly return an already-transcribed job
    to the queue for no reason. A transient sink failure here is instead
    only logged; `retry_pending_documents` is what actually retries
    publication, on its own schedule, independent of any one job -- and it
    retries only the destinations that failed, never the ones that
    succeeded.
    """
    claimed = await queue.claim()
    if claimed is None:
        return False
    job = cast(_ClaimedJobShape, claimed)
    log_event(
        log,
        logging.INFO,
        Event.JOB_CLAIMED,
        "Claimed a transcription job",
        job_id=job.id,
        session_id=job.session_id,
        discord_user_id=job.discord_user_id,
        key_id=job.encryption_key_id,
    )

    job_dir = work_dir / f"job-{job.id}-{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)
    encrypted_path = job_dir / "audio.enc"
    wav_path = job_dir / "audio.wav"

    is_last = False
    try:
        try:
            await store.get(job.s3_key, encrypted_path)
            await asyncio.to_thread(
                crypto.decrypt_to,
                encrypted_path,
                wav_path,
                job.wrapped_data_key,
                job.encryption_key_id,
            )

            # Both settings are the guild's (Spec 11), so the guild has to
            # be resolved first: one worker process serves all of them and
            # only the session names one. Two extra reads per job, against
            # a transcription measured in minutes.
            guild = await sessions.guild_id(job.session_id)
            configured_language = await config.get(guild, domain_settings.TRANSCRIPTION_LANGUAGE)
            prompt = await config.get(guild, domain_settings.TRANSCRIPTION_PROMPT)

            # A configured language beats a stored detection outright, and
            # the stored detection is not even read when there is one --
            # see this module's docstring for why that order is the point
            # rather than a detail.
            named_language = _configured_language(configured_language)
            pinned_language = (
                named_language
                if named_language is not None
                else await sessions.detected_language(job.session_id, job.discord_user_id)
            )

            # Started here rather than before the two config reads above, so
            # `realtime_factor` stays a measurement of the model and not of
            # a database round-trip. Spec 15 wants that number compared
            # against real material, and a number that quietly includes
            # whatever the config store was doing is not comparable.
            started = time.monotonic()
            try:
                result = await engine.transcribe(
                    wav_path, pinned_language, prompt, job.requested_model
                )
            except Exception as exc:
                log_exception(
                    log,
                    logging.WARNING,
                    Event.JOB_FAILED,
                    "Transcription failed",
                    exc,
                    job_id=job.id,
                    session_id=job.session_id,
                    stage="transcribe",
                    max_attempts=max_attempts,
                )
                await queue.fail(job.id, str(exc), max_attempts, lease=job.claimed_at)
                return True

            wall_seconds = time.monotonic() - started
            audio_seconds = max((segment.end for segment in result.segments), default=0.0)
            # Counts and durations, never text. `realtime_factor` is the
            # number Spec 15 says must be measured against real material
            # before rollout rather than estimated -- this is that
            # measurement, on every job, forever.
            log_event(
                log,
                logging.INFO,
                Event.JOB_TRANSCRIBED,
                "Transcribed a recording",
                job_id=job.id,
                session_id=job.session_id,
                segments=len(result.segments),
                audio_seconds=round(audio_seconds, 3),
                wall_seconds=round(wall_seconds, 3),
                realtime_factor=round(wall_seconds / audio_seconds, 3) if audio_seconds else None,
                language=result.language,
            )

            # Reached only when the guild asked for detection *and* this is
            # the first job for this speaker: a named language is never
            # `None`, which is exactly what keeps configuration out of
            # `detected_language`. Dropping the condition altogether would
            # write the configured language into that column on every job
            # and pin it there, which is the trap the docstring describes.
            if pinned_language is None:
                await sessions.set_detected_language(
                    job.session_id, job.discord_user_id, result.language
                )

            # The engine's own measurements, straight through: the worker
            # neither recomputes nor second-guesses them. `audio_seconds`
            # above is the end of the last segment, which answers "how long
            # did this take per minute of speech" for the log line and is
            # the wrong number for the database -- on a track whose speaker
            # fell silent halfway through it is nowhere near the length of
            # the recording.
            # `lease` is this worker's claim, handed back so the queue can
            # refuse the write if the job was taken over while it ran. A
            # transcription that outlives the lease is not an error and is
            # not rare on a long track; what must not happen is two workers
            # each storing a transcript for it and each reporting the
            # session's last job, which creates the protocol twice.
            # Read before the `finally` below removes both files, and
            # written in the same call as the transcript so one lease
            # fences both. See `_recorded_audio` for why an unreadable
            # header leaves the columns null rather than failing the job.
            is_last = await queue.complete(
                job.id,
                serialize_transcript(result),
                result.measurements,
                lease=job.claimed_at,
                audio=_recorded_audio(wav_path, encrypted_path),
            )

            # After the transcript is safely stored, and before the
            # `finally` deletes the plaintext it needs. Both halves of
            # that sentence are load-bearing: drawn any earlier, a
            # failure here would travel to the handler below and re-queue
            # a job that has already been transcribed; drawn any later,
            # there is nothing left on disk to draw. `_store_spectrogram`
            # swallows its own failures, which is what makes the first
            # half true.
            await _store_spectrogram(spectrograms, config, job, guild, wav_path)
        except Exception as exc:
            # Everything other than the transcription failure already
            # handled above: a failed download, a decrypt error, a
            # database error. See this function's docstring's "Error
            # handling (Defect 4)" note -- without this, the exception
            # propagated out of `process_one` and killed the worker
            # process, stranding this job `running` forever.
            # `stage` is what this line was missing: it covered download,
            # decrypt *and* the transcript write with one message and no
            # timing for any of them. The stage now says which, and the
            # matching `job.process` trace times all three.
            log_exception(
                log,
                logging.WARNING,
                Event.JOB_FAILED,
                "Job failed outside transcription",
                exc,
                job_id=job.id,
                session_id=job.session_id,
                stage="pipeline",
                max_attempts=max_attempts,
            )
            await queue.fail(job.id, str(exc), max_attempts, lease=job.claimed_at)
            return True
    finally:
        # Runs whether processing succeeded, the transcription failed, or
        # something above raised outright: decrypted speech (and the
        # encrypted copy fetched to build it) must never survive this
        # function, regardless of how it exits.
        shutil.rmtree(job_dir, ignore_errors=True)

    if is_last:
        try:
            await _create_session_document(
                exports,
                sessions,
                jobs,
                links,
                config,
                job.session_id,
                template_source,
                now or datetime.now(UTC),
            )
        except Exception as exc:
            # The job itself already completed successfully -- see this
            # function's docstring for why this is a separate, narrower
            # handler that never calls `queue.fail`. Left for
            # `retry_pending_documents` to pick up: the session stays
            # `closed` and never becomes `documented`, which is exactly
            # what that sweep looks for.
            # Never `%s` on `exc`: `_create_session_document` renders the
            # assembled transcript through Jinja and posts it through httpx,
            # so a `jinja2.UndefinedError` or an `httpx.HTTPStatusError`
            # raised in that path can carry template context or request
            # content -- and `%s` would print it verbatim.
            log_exception(
                log,
                logging.WARNING,
                Event.SESSION_DOCUMENT_RETRY_FAILED,
                "Document creation failed; the retry sweep will try again",
                exc,
                session_id=job.session_id,
            )

    return True


async def retry_pending_documents(
    exports: ExportPorts,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkRepository,
    config: ConfigReader,
    template_source: str = _FALLBACK_TEMPLATE,
    now: datetime | None = None,
) -> None:
    """Retries publication for sessions whose protocol did not reach everywhere.

    `_create_session_document` (called from `process_one`, above) only
    ever fires once, off the one job that happens to complete a session
    last -- and by the time it runs that job is already `done`, so nothing
    else naturally re-triggers it if the attempt fails (Defect 4). This
    re-derives the same "every job of this session is terminal" condition
    independently and after the fact, from `sessions.
    closed_undocumented_sessions`, and tries again. It also serves as the
    safety net for the residual timing gap `sturnus.infrastructure.db.
    queue.JobQueue.complete`'s own docstring describes under "Defect 5":
    a session whose last job happens to complete a moment *before*
    `close_session` commits reports "not last" at that moment, and nothing
    else would ever revisit it without this sweep.

    **Two candidate sets, because a session can now be half-published.**
    `closed_undocumented_sessions` is the original one: nothing reached
    the primary destination, so the session never became `documented`. It
    misses the case a second destination introduced -- Outline succeeded,
    the session *is* `documented`, and the Markdown export failed.
    `sessions_with_unpublished_targets` is that case, and without it a
    failed secondary would never be retried at all. The two are unioned
    rather than merged into one query because they are two different
    questions about a session, and a session that answers both must still
    be published exactly once per sweep.

    **A retry publishes only what failed.** `publish_session` skips every
    destination already in `session_document`, which is what keeps a
    destination that stays down from reprinting the Outline document every
    five minutes -- a hundred real documents in somebody's wiki by
    lunchtime, none of them removable by anything in this system.

    Survives its own errors per session, same as `process_one` does for
    publication: one session still failing (Outline still down, or a
    rejection that never becomes `PermanentDocumentError`) must not stop
    every other session in the same sweep from being tried.
    """
    stamp = now or datetime.now(UTC)
    # `dict.fromkeys` rather than a set: a session that answers both
    # queries must be published once, and the sweep's order should stay
    # the order the database reported rather than a hash order that
    # changes between runs.
    pending = dict.fromkeys(
        [
            *await sessions.closed_undocumented_sessions(),
            *await sessions.sessions_with_unpublished_targets(),
        ]
    )
    for session_id in pending:
        try:
            await _create_session_document(
                exports,
                sessions,
                jobs,
                links,
                config,
                session_id,
                template_source,
                stamp,
            )
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.SESSION_DOCUMENT_RETRY_FAILED,
                "Retrying document creation failed; will try again next sweep",
                exc,
                session_id=session_id,
            )
