"""Process entrypoint for the `worker` deployment (Spec 5.3, Spec 13.1, Spec 13.2).

The worker owns the database schema: it runs Alembic migrations to head
before anything else starts, which is why `sturnus.entrypoints.bot` and
`sturnus.entrypoints.link` only ever wait for tables to appear rather than
create them. It then loops `process_one`
(`sturnus.application.worker.process_one`) forever -- a short sleep when
the queue was empty, straight back around when it was not -- serves the
same health endpoints the other two components serve, and on `SIGTERM`
finishes whatever job is already in flight rather than abandoning it: the
signal only sets a stop flag, and the loop checks that flag *between*
calls to `process_one`, never during one.

**Configuration is its own model, not a reuse of `sturnus.config.Settings`**
-- the same choice `sturnus.entrypoints.link.LinkSettings` makes and for
the same reason (see that module's docstring): the bot's `Settings`
requires a Discord token this process never uses, and this process needs
several fields (the document sink's credentials, the transcription
engine's model) that `Settings` does not have and that this task's brief
does not license adding there.

**Two adapters this file has to supply itself, and why.** `process_one`
needs an object that can download an encrypted recording (`get`) and one
that can read/write a speaker's pinned language and mark a session
documented (`detected_language`/`set_detected_language`/`mark_documented`)
-- neither exists on the committed `S3AudioStore` or `SessionRepository`
yet, and this task's brief does not license adding them there either
(`src/sturnus/infrastructure/objectstore.py` and
`src/sturnus/infrastructure/db/repositories.py` are outside the three
files it names). `_DownloadableAudioStore` below is a thin subclass adding
exactly the one missing method; `_WorkerSessionStore` reads and writes the
same tables `SessionRepository` already owns, through the same
SQLAlchemy 2.0 async ORM, the project's one data-access path -- narrowly
scoped to what the worker needs rather than duplicating the rest of that
repository's surface. `participant_names`/`audio_epoch`/`session_bounds`
-- the rest of the widened `SessionStore` shape `process_one` now needs to
call `sturnus.application.assembly.assemble` on a session's last job --
are the exception: `SessionRepository` already has all three, so
`_WorkerSessionStore` simply delegates to one instead of duplicating them.
`process_one`'s other two `assemble` collaborators, `JobRepository.
transcripts_for` and `AccountLinkRepository.external_identity`, likewise
already exist and are wired in directly below, with no adapter needed.

**Deployment note on the working directory (see the brief's dispatch).**
`process_one`'s scratch directory for the downloaded/decrypted audio
defaults to `/tmp`, overridable with `STURNUS_WORK_DIR`. This matches what
`charts/sturnus/values.yaml`'s `worker.tmpSizeLimit` (4Gi) already assumed
before this file existed -- that comment flagged the assumption as
unverified pending this task; it is now confirmed correct by this default.

**Deployment gap, not fixed here.** `Dockerfile` copies only `src/` into
the runtime image -- `migrations/` and `alembic.ini` are not part of it.
`_run_migrations` below will fail to find `alembic.ini` in a container
built from the current `Dockerfile`. Fixing that is a `Dockerfile` change,
outside the three files this task may touch; flagged in the task report
rather than worked around here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.resources
import logging
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.retention import sweep_expired_audio
from sturnus.application.worker import process_one, retry_pending_documents
from sturnus.config import StrictSettings
from sturnus.infrastructure.crypto import KeyWrapper, decrypt_file
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Session, SessionParticipant
from sturnus.infrastructure.db.queue import DEFAULT_LEASE_SECONDS, JobQueue
from sturnus.infrastructure.db.repositories import (
    AccountLinkRepository,
    JobRepository,
    SessionRepository,
)
from sturnus.infrastructure.documents.outline import OutlineSink
from sturnus.infrastructure.health import ReadinessState, start_health_server
from sturnus.infrastructure.objectstore import S3AudioStore
from sturnus.infrastructure.observability import init_sentry
from sturnus.infrastructure.whisper import WhisperEngine

log = logging.getLogger(__name__)

#: How long an empty claim backs off before trying again -- short enough
#: that a freshly enqueued job is picked up quickly, long enough that an
#: idle worker does not spin.
_POLL_SECONDS = 5.0

#: How often the retention sweep (Spec 12.2, Defect 3) checks for expired
#: audio. Retention is measured in days (`audio_retention_days`), so an
#: hourly check is easily frequent enough -- unlike `_POLL_SECONDS`, there
#: is no user-visible latency being traded off here.
_RETENTION_SWEEP_INTERVAL_SECONDS = 3600.0

#: How often the document-retry sweep (Defect 4) re-checks for closed
#: sessions that never got documented. Short enough that a transient
#: Outline error self-heals within minutes, not hours.
_DOCUMENT_RETRY_INTERVAL_SECONDS = 300.0

#: faster-whisper on CPU (Spec 7 sizes the deployment for CPU, not GPU --
#: see `charts/sturnus/values.yaml`'s `worker.resources`). The weights are
#: still quantised to int8, which is what keeps `large-v3` inside a
#: memory budget a CPU node will actually give it; the `_float32` half
#: names the type everything *else* runs in -- activations, accumulation,
#: the layers that are never quantised.
#:
#: Naming it is the point. Plain `int8` is an alias whose float type
#: CTranslate2 picks for the device it finds itself on, so what the
#: decoder accumulates in is decided by the node the pod landed on rather
#: than by this file; on today's x86 workers the alias resolves to exactly
#: this, and on a machine with bfloat16 support it need not. Transcription
#: quality is not something to leave to the scheduler, and CTranslate2
#: falls back silently rather than refusing a compute type it cannot
#: provide -- there would be nothing in the logs to say it had happened.
_WHISPER_COMPUTE_TYPE = "int8_float32"

#: Package and resource name of the real Outline document template. See
#: `_load_template` -- this is the packaged template `process_one` must
#: render every production document with, never the minimal
#: `sturnus.application.worker._FALLBACK_TEMPLATE`.
_TEMPLATE_PACKAGE = "sturnus.infrastructure.documents"
_TEMPLATE_RESOURCE = "outline_template.md.j2"


def _load_template() -> str:
    """Loads the packaged Outline document template's text.

    Uses `importlib.resources` rather than a path relative to the source
    tree -- this process runs from an installed wheel inside the container
    image (see `Dockerfile`), which has no `src/` layout to resolve a
    relative path against. `sturnus.application.worker.process_one`
    defaults to a minimal fallback template precisely so its own tests do
    not need this file at all; every real invocation of `process_one` --
    this one -- must pass this loader's result in as `template_source`
    instead, or a document ships with no participants list, no Outline
    mentions, and no Discord profile links: the entire visible payoff of
    account linking, silently absent.
    """
    return (
        importlib.resources.files(_TEMPLATE_PACKAGE)
        .joinpath(_TEMPLATE_RESOURCE)
        .read_text(encoding="utf-8")
    )


class WorkerSettings(StrictSettings):
    """Everything the worker process needs, and nothing it does not.

    No `discord_token`: unlike `sturnus.config.Settings`, every field here
    is something this specific process actually uses.
    """

    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    master_key: SecretStr
    master_key_id: str
    outline_base_url: str
    outline_service_key: SecretStr
    # `large-v3` rather than `large-v3-turbo`: turbo is a distillation with
    # four decoder layers instead of thirty-two, and what it gives up is
    # concentrated outside English -- which is the only place this
    # deployment operates. It buys speed, and nothing here is waiting:
    # transcription runs offline, one speaker's file at a time, after the
    # meeting is over. The cost is paid in the chart instead, in memory
    # and in a longer first start (`charts/sturnus/values.yaml`).
    whisper_model: str = "large-v3"
    # The floor under `transcription_language` (Spec 11), which is
    # per-guild and normally decides this; reached only when a guild asked
    # for detection and the engine's detection came back with nothing.
    # `en` here was a real defect, not a harmless default: every guild
    # this serves meets in German.
    whisper_default_language: str = "de"
    model_cache_dir: Path | None = None
    work_dir: Path = Path("/tmp")
    max_job_attempts: int = 3
    # How long a claimed job may stay `running` before `JobQueue.claim`
    # reclaims it (Defect 4) -- see `sturnus.infrastructure.db.queue`'s
    # `DEFAULT_LEASE_SECONDS` for why this default is as generous as it is.
    job_lease_seconds: float = DEFAULT_LEASE_SECONDS
    health_port: int = 8080


class _DownloadableAudioStore(S3AudioStore):
    """Adds the one download capability `process_one` needs to `S3AudioStore`.

    See the module docstring: `S3AudioStore` (a file this task may not
    touch) has `put`/`delete`/`exists` but no `get`. Subclassing rather
    than re-instantiating a second boto3 client keeps exactly one S3
    client, one bucket, and one set of credentials in play.
    """

    async def get(self, key: str, target: Path) -> None:
        await asyncio.to_thread(self._client.download_file, self._bucket, key, str(target))


class _KeyWrapperDecryptor:
    """Adapts `KeyWrapper.unwrap` and `decrypt_file` to `worker.Decryptor`.

    Sturnus currently issues one master key at a time (Spec 12.1 supports
    rotation by *label*, not by holding several keys live at once --
    `sturnus.infrastructure.recording_adapters.CryptoEncryptor` makes the
    same assumption on the encrypting side); `key_id` from the job is
    accepted for the interface's sake but every job is decrypted with the
    single configured `master_key`.
    """

    def __init__(self, master_key: bytes, master_key_id: str) -> None:
        self._master_key = master_key
        self._master_key_id = master_key_id

    def decrypt_to(self, source: Path, target: Path, wrapped: bytes, key_id: str) -> None:
        if key_id != self._master_key_id:
            log.warning(
                "Job was encrypted with key id %r, but only %r is configured; "
                "decrypting with the configured key anyway (no rotation support yet)",
                key_id,
                self._master_key_id,
            )
        wrapper = KeyWrapper(self._master_key, self._master_key_id)
        data_key = wrapper.unwrap(wrapped)
        decrypt_file(source, target, data_key)


class _WorkerSessionStore:
    """Adapts persistence to `sturnus.application.worker.SessionStore`.

    `SessionRepository` (`sturnus.infrastructure.db.repositories`) does not
    yet expose `detected_language`/`set_detected_language`/`mark_documented`
    -- see the module docstring for why this file supplies them directly
    instead of adding them there. Reads and writes the same
    `session_participant.detected_language` and `session.*` columns that
    repository already owns, through the same async ORM.

    `participant_names`/`audio_epoch`/`session_bounds`/`guild_id` -- the
    rest of the widened `SessionStore` shape, needed by
    `sturnus.application.assembly.assemble` and by `_create_session_document`
    to resolve per-guild configuration -- delegate to a `SessionRepository`
    instance instead of re-implementing those queries: that repository
    already owns them, and duplicating its SQL here would just be a second
    place for them to drift out of sync.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._sessions = SessionRepository(session_factory)

    async def participant_names(self, session_id: int) -> dict[int, str]:
        return await self._sessions.participant_names(session_id)

    async def audio_epoch(self, session_id: int, user_id: int) -> datetime | None:
        return await self._sessions.audio_epoch(session_id, user_id)

    async def session_bounds(self, session_id: int) -> tuple[datetime, datetime]:
        return await self._sessions.session_bounds(session_id)

    async def channel_ref(self, session_id: int) -> tuple[int, int, str | None]:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Session.guild_id, Session.channel_id, Session.channel_name).where(
                        Session.id == session_id
                    )
                )
            ).one()
            return int(row[0]), int(row[1]), row[2]

    async def guild_id(self, session_id: int) -> int:
        return await self._sessions.guild_id(session_id)

    async def closed_undocumented_sessions(self) -> list[int]:
        return await self._sessions.closed_undocumented_sessions()

    async def detected_language(self, session_id: int, user_id: int) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(SessionParticipant.detected_language).where(
                    SessionParticipant.session_id == session_id,
                    SessionParticipant.discord_user_id == user_id,
                )
            )

    async def set_detected_language(self, session_id: int, user_id: int, lang: str) -> None:
        # Only while still null: the first job to get here wins, matching
        # `SessionRepository.set_audio_epoch`'s same "first write only"
        # pattern for the same reason -- a later job for the same speaker
        # must not silently overwrite what a first job already pinned.
        async with self._session_factory() as session:
            await session.execute(
                update(SessionParticipant)
                .where(
                    SessionParticipant.session_id == session_id,
                    SessionParticipant.discord_user_id == user_id,
                    SessionParticipant.detected_language.is_(None),
                )
                .values(detected_language=lang)
            )
            await session.commit()

    async def mark_documented(self, session_id: int, doc_id: str, url: str, provider: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(
                    status="documented",
                    document_provider=provider,
                    document_id=doc_id,
                    document_url=url,
                )
            )
            await session.commit()


def _run_migrations(database_url: str) -> None:
    """Runs Alembic to head. The worker owns the schema (Spec 13.1) --
    `sturnus.entrypoints.bot` and `sturnus.entrypoints.link` only ever wait
    for tables to appear, never create them.

    Resolved relative to the current working directory, matching how
    `alembic` is invoked from the command line elsewhere in this project
    (`alembic.ini`'s `script_location = %(here)s/migrations`). See the
    module docstring's "Deployment gap" note: the current `Dockerfile`
    does not copy `alembic.ini`/`migrations/` into the runtime image, so
    this will raise there until that is fixed.
    """
    config_path = Path.cwd() / "alembic.ini"
    if not config_path.is_file():
        raise RuntimeError(
            f"alembic.ini not found at {config_path}; the worker cannot run migrations "
            "without it. See sturnus.entrypoints.worker's module docstring: the current "
            "Dockerfile does not copy migrations/ or alembic.ini into the runtime image."
        )
    cfg = Config(str(config_path))
    # `migrations/env.py` reads `sqlalchemy.url` from this config (falling
    # back to `DATABASE_URL` if unset) and converts `+asyncpg` to
    # `+psycopg` itself before running -- passing the same asyncpg URL
    # `Settings` already uses keeps that one substitution rule in one
    # place instead of duplicating it here.
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


async def _retention_sweep_loop(
    jobs: JobRepository, store: _DownloadableAudioStore, stop: asyncio.Event
) -> None:
    """Periodically deletes expired audio and stamps `audio_deleted_at`
    (Spec 12.2, Defect 3): `sturnus.application.retention.expired_jobs`
    exists but nothing outside its own tests calls it -- this is that
    call, on `_RETENTION_SWEEP_INTERVAL_SECONDS`.

    `sweep_expired_audio` already survives one job's own failure and
    continues past it; the `try`/`except` here is one layer up, for a
    failure reading candidates in the first place (a database hiccup) --
    without it, that would kill this loop outright instead of simply
    trying again on the next interval.
    """
    while not stop.is_set():
        try:
            await sweep_expired_audio(jobs, store, datetime.now(UTC))
        except Exception as exc:
            log.warning("Retention sweep failed; will retry next interval: %s", exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_RETENTION_SWEEP_INTERVAL_SECONDS)


async def _document_retry_loop(
    documents: OutlineSink,
    sessions: _WorkerSessionStore,
    jobs: JobRepository,
    links: AccountLinkRepository,
    config: ConfigStore,
    template_source: str,
    stop: asyncio.Event,
) -> None:
    """Periodically retries document creation for closed, undocumented
    sessions (Defect 4) on `_DOCUMENT_RETRY_INTERVAL_SECONDS`. See
    `sturnus.application.worker.retry_pending_documents`'s docstring for
    why a session can need this at all.
    """
    while not stop.is_set():
        try:
            await retry_pending_documents(documents, sessions, jobs, links, config, template_source)
        except Exception as exc:
            log.warning("Document retry sweep failed; will retry next interval: %s", exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_DOCUMENT_RETRY_INTERVAL_SECONDS)


async def _run() -> None:
    settings = WorkerSettings()

    if settings.model_cache_dir is not None:
        # faster-whisper never sets `download_root` itself
        # (sturnus/infrastructure/whisper.py); it falls through to
        # huggingface_hub, whose cache directory resolves from `HF_HOME`.
        # Set before `WhisperEngine` (and therefore `WhisperModel`) is
        # constructed below, so the very first model load already lands on
        # the persistent volume rather than the image's baked-in default.
        os.environ["HF_HOME"] = str(settings.model_cache_dir)

    await asyncio.to_thread(_run_migrations, settings.database_url)

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    queue = JobQueue(session_factory, lease_seconds=settings.job_lease_seconds)
    transcription_engine = WhisperEngine(
        settings.whisper_model,
        "cpu",
        _WHISPER_COMPUTE_TYPE,
        settings.whisper_default_language,
    )
    store = _DownloadableAudioStore(
        settings.s3_endpoint,
        settings.s3_bucket,
        settings.s3_access_key.get_secret_value(),
        settings.s3_secret_key.get_secret_value(),
    )
    crypto = _KeyWrapperDecryptor(
        base64.b64decode(settings.master_key.get_secret_value()), settings.master_key_id
    )
    documents = OutlineSink(
        base_url=settings.outline_base_url,
        api_token=settings.outline_service_key.get_secret_value(),
    )
    sessions = _WorkerSessionStore(session_factory)
    jobs = JobRepository(session_factory)
    # No fixed provider: `document_provider` is per-guild configuration
    # (Spec 11), read per document -- see
    # `sturnus.application.worker._create_session_document` -- rather than
    # assumed to be "outline" for every guild this one process serves.
    links = AccountLinkRepository(session_factory)
    config_store = ConfigStore(session_factory)
    template_source = _load_template()

    readiness = ReadinessState(discord_connected=True)  # this process has no gateway to wait on

    async def database_ping() -> bool:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    health_runner = await start_health_server(readiness, settings.health_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # Two background sweeps, independent of the main claim/transcribe loop
    # below and of each other -- each survives its own errors (see their
    # own docstrings) so a failure in one never stops the other or the
    # main loop.
    retention_task = asyncio.create_task(_retention_sweep_loop(jobs, store, stop))
    document_retry_task = asyncio.create_task(
        _document_retry_loop(documents, sessions, jobs, links, config_store, template_source, stop)
    )

    log.info("Worker started; polling the transcription queue")
    try:
        while not stop.is_set():
            readiness.database_reachable = await database_ping()
            # `process_one` runs to completion before `stop.is_set()` is
            # checked again -- a SIGTERM during a job lets that job finish
            # rather than abandoning it mid-decrypt (see the module
            # docstring).
            did_work = await process_one(
                queue=queue,
                engine=transcription_engine,
                store=store,
                crypto=crypto,
                documents=documents,
                sessions=sessions,
                jobs=jobs,
                links=links,
                config=config_store,
                work_dir=settings.work_dir,
                max_attempts=settings.max_job_attempts,
                template_source=template_source,
            )
            if not did_work:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
    finally:
        log.info("Shutdown requested: worker stopping after its current job")
        for task in (retention_task, document_retry_task):
            task.cancel()
        for task in (retention_task, document_retry_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await health_runner.cleanup()
        await engine.dispose()


def main() -> None:
    # Both run before `_run`, and so before `WorkerSettings()` reads the
    # environment: with a DSN configured, a settings `ValidationError` is
    # then itself reported instead of being the one failure Sentry can never
    # see. Without a DSN, `init_sentry` returns having touched nothing at all
    # -- see `sturnus.infrastructure.observability`.
    logging.basicConfig(level=logging.INFO)
    init_sentry("worker")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
