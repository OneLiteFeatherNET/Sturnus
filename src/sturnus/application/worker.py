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
one chronological `Transcript`, then renders it through
`sturnus.application.documents.render_transcript`/`document_title` before
handing the result to `documents.create`. `assemble` needs a `SessionReader`
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
`_create_session_document`. None of them is read once at process start,
because one worker serves every guild. `_BoundLinks`
adapts one call's resolved provider back down to the plain `LinkReader`
shape `assemble` itself calls, so `assemble` stays ignorant of
configuration entirely.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sturnus.application.assembly import JobReader, assemble, serialize_transcript
from sturnus.application.documents import (
    ChannelRef,
    CreatedDocument,
    DocumentSink,
    document_title,
    render_transcript,
)
from sturnus.application.transcription import TranscriptionEngine
from sturnus.domain import settings as domain_settings
from sturnus.domain.transcript import DEFAULT_MERGE_GAP
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
    """

    async def claim(self) -> object | None: ...

    async def complete(self, job_id: int, transcript: str) -> bool: ...

    #: Returns whether the job is now **dead** -- out of attempts, so this
    #: recording will never be transcribed -- rather than queued for another
    #: try. Only the queue can answer that, because only the queue counts
    #: the attempts, and without the answer a caller cannot tell permanent
    #: loss from an ordinary retry: `process_one` returns `True` for both.
    async def fail(self, job_id: int, error: str, max_attempts: int) -> bool: ...


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


class ConfigReader(Protocol):
    """Where per-guild runtime configuration is read from (Spec 11).

    Matches `sturnus.infrastructure.db.config_store.ConfigStore.get`
    structurally: falls back to that key's entry in `sturnus.domain.
    settings.DEFAULTS` when nothing is stored, and to `None` for a key
    with no default (e.g. `document_target`) that a guild never set.
    """

    async def get(self, guild_id: int, key: str) -> str | None: ...


class LinkRepository(Protocol):
    """Where a speaker's external identity is read from, keyed by provider.

    Unlike `sturnus.application.assembly.LinkReader`, `provider` is a
    parameter of `external_identity` here rather than fixed once at
    construction: the worker serves every guild from one process, and
    which provider's account-link mapping applies is itself per-guild
    configuration (Spec 11's `document_provider`) that cannot be resolved
    until a session's guild is known. `_BoundLinks` below adapts one
    resolved provider back down to the narrower `LinkReader` shape
    `assemble` actually calls.
    """

    async def external_identity(
        self, discord_user_id: int, provider: str
    ) -> tuple[str, str] | None: ...


class _BoundLinks:
    """Adapts `LinkRepository` to `sturnus.application.assembly.LinkReader`
    for one already-resolved provider, so `assemble` -- which knows
    nothing about per-guild configuration -- can keep calling
    `external_identity` with just a Discord user id.
    """

    def __init__(self, links: LinkRepository, provider: str) -> None:
        self._links = links
        self._provider = provider

    async def external_identity(self, discord_user_id: int) -> tuple[str, str] | None:
        return await self._links.external_identity(discord_user_id, self._provider)


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


async def _create_session_document(
    documents: DocumentSink,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkRepository,
    config: ConfigReader,
    session_id: int,
    template_source: str,
) -> None:
    """Assembles, renders, and creates the document for a session's last job.

    Calls `sturnus.application.assembly.assemble` to merge every
    participant's stored transcript -- not just the job that happened to
    complete the session -- into one chronological `Transcript`, then
    renders it through `render_transcript`/`document_title`. Both are
    localised to UTC: no timezone configuration exists anywhere in this
    codebase yet (`sturnus.entrypoints.worker.WorkerSettings` has no such
    field), so this keeps the same UTC anchor the previous, single-speaker
    version of this function already used rather than inventing one.

    Also resolves three guild-scoped settings (Spec 11) here, at
    document-creation time, rather than once at process start -- the
    worker serves every guild from one process and cannot know which
    guild's values apply until a session, and therefore its guild, is
    known:

    - `document_target`: where `documents.create` writes the document (an
      Outline collection id today). Required, with no default -- a guild
      that never configured it has nothing to create a document *into*, so
      this raises rather than guessing. The caller's own retry path
      (`process_one`'s document-creation handler, `retry_pending_documents`)
      already treats any non-`PermanentDocumentError` failure here as
      transient and tries again later, which is exactly right for "an
      administrator has not configured this yet" -- unlike a rejected
      token or a deleted collection, this can and does resolve itself.
    - `document_provider`: which provider's account-link mapping a
      speaker's external identity is read from, via `_BoundLinks`.
    - `merge_gap_seconds`: how long a pause may be before one speaker's
      blocks split, forwarded to `assemble`.
    """
    guild = await sessions.guild_id(session_id)

    target = await config.get(guild, domain_settings.DOCUMENT_TARGET)
    if target is None:
        raise RuntimeError(
            f"guild {guild} has no {domain_settings.DOCUMENT_TARGET!r} configured; "
            "cannot create a document until an administrator sets it"
        )

    provider = await config.get(guild, domain_settings.DOCUMENT_PROVIDER)
    # `DEFAULTS` supplies "outline" when a guild never set this explicitly
    # (see `ConfigReader`'s docstring) -- unlike `document_target`, this
    # key always resolves to a value.
    assert provider is not None

    merge_gap_value = await config.get(guild, domain_settings.MERGE_GAP_SECONDS)
    merge_gap = (
        timedelta(seconds=int(merge_gap_value))
        if merge_gap_value is not None
        else DEFAULT_MERGE_GAP
    )

    transcript = await assemble(
        session_id, sessions, jobs, _BoundLinks(links, provider), UTC, merge_gap
    )

    # `assemble` works in UTC deliberately -- ordering and merging must not
    # depend on a local offset -- and only the rendering is localised.
    tz = await _guild_timezone(config, guild)
    ref_guild, ref_channel, ref_name = await sessions.channel_ref(session_id)
    channel = ChannelRef(ref_guild, ref_channel, ref_name)
    body = render_transcript(transcript, template_source, tz, channel)
    title = document_title(transcript, tz)
    try:
        created: CreatedDocument = await documents.create(title, body, target)
    except Exception as exc:
        # Recognised by class name, not by `except PermanentDocumentError`:
        # that type lives in `sturnus.infrastructure.documents.outline`,
        # which this module must never import (see the module docstring).
        if type(exc).__name__ != "PermanentDocumentError":
            raise
        # ERROR, not WARNING: a permanent rejection is the end of the road
        # for this session's document. No sweep will fix it, so it needs a
        # human -- unlike every other document failure here, which
        # `retry_pending_documents` picks up on its own schedule.
        log_event(
            log,
            logging.ERROR,
            Event.SESSION_DOCUMENT_REJECTED,
            "Document sink permanently rejected creation; no retry will succeed",
            session_id=session_id,
        )
        return
    await sessions.mark_documented(session_id, created.id, created.url, provider)
    log_event(
        log,
        logging.INFO,
        Event.SESSION_DOCUMENT_CREATED,
        "Created the session protocol document",
        session_id=session_id,
        document_id=created.id,
        provider=provider,
        collection_id=target,
        participants=len(transcript.participants),
        blocks=len(transcript.blocks),
        body_bytes=len(body.encode("utf-8")),
    )


async def process_one(
    queue: Queue,
    engine: TranscriptionEngine,
    store: AudioDownloader,
    crypto: Decryptor,
    documents: DocumentSink,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkRepository,
    config: ConfigReader,
    work_dir: Path,
    max_attempts: int,
    template_source: str = _FALLBACK_TEMPLATE,
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
    6. If it was: assemble every participant's stored transcript into one
       document (`_create_session_document`, `sturnus.application.assembly.
       assemble`) and mark the session documented.
    7. Every temporary file made in steps 2-3 is removed in a `finally`, so
       a failure anywhere above never leaves decrypted speech on disk. The
       audio object in S3 is left alone deliberately -- see the module
       docstring.

    `jobs` and `links` are only ever read from in step 6, but are accepted
    as parameters up front (rather than constructed lazily) so every
    collaborator `process_one` needs is visible in its signature, matching
    `sessions`/`documents`/`queue` and the rest.

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

    Step 6 (document creation) is deliberately handled by a *separate*
    `try`/`except`, outside the one above: by the time it runs, `queue.
    complete` has already succeeded and the job is `done` -- calling
    `queue.fail` on it would incorrectly return an already-transcribed job
    to the queue for no reason. A transient document-sink failure here is
    instead only logged; `retry_pending_documents` is what actually retries
    document creation, on its own schedule, independent of any one job.
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
                result = await engine.transcribe(wav_path, pinned_language, prompt)
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
                await queue.fail(job.id, str(exc), max_attempts)
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

            is_last = await queue.complete(job.id, serialize_transcript(result))
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
            await queue.fail(job.id, str(exc), max_attempts)
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
                documents, sessions, jobs, links, config, job.session_id, template_source
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
    documents: DocumentSink,
    sessions: SessionStore,
    jobs: JobReader,
    links: LinkRepository,
    config: ConfigReader,
    template_source: str = _FALLBACK_TEMPLATE,
) -> None:
    """Retries document creation for closed sessions that never got documented.

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

    Survives its own errors per session, same as `process_one` does for
    document creation: one session still failing (Outline still down, or
    a rejection that never becomes `PermanentDocumentError`) must not stop
    every other session in the same sweep from being tried.
    """
    for session_id in await sessions.closed_undocumented_sessions():
        try:
            await _create_session_document(
                documents, sessions, jobs, links, config, session_id, template_source
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
