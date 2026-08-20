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
repository's surface.

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
import logging
import os
import signal
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.worker import process_one
from sturnus.infrastructure.crypto import KeyWrapper, decrypt_file
from sturnus.infrastructure.db.models import Session, SessionParticipant
from sturnus.infrastructure.db.queue import JobQueue
from sturnus.infrastructure.documents.outline import OutlineSink
from sturnus.infrastructure.health import ReadinessState, start_health_server
from sturnus.infrastructure.objectstore import S3AudioStore
from sturnus.infrastructure.whisper import WhisperEngine

log = logging.getLogger(__name__)

#: How long an empty claim backs off before trying again -- short enough
#: that a freshly enqueued job is picked up quickly, long enough that an
#: idle worker does not spin.
_POLL_SECONDS = 5.0

#: faster-whisper on CPU (Spec 7 sizes the deployment for CPU, not GPU --
#: see `charts/sturnus/values.yaml`'s `worker.resources`): int8 is the
#: quantisation the chart's own model-size comment already assumes.
_WHISPER_COMPUTE_TYPE = "int8"


class WorkerSettings(BaseSettings):
    """Everything the worker process needs, and nothing it does not.

    No `discord_token`: unlike `sturnus.config.Settings`, every field here
    is something this specific process actually uses.
    """

    model_config = SettingsConfigDict(env_prefix="STURNUS_", frozen=True)

    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    master_key: SecretStr
    master_key_id: str
    outline_base_url: str
    outline_service_key: SecretStr
    outline_collection_id: str
    whisper_model: str = "large-v3-turbo"
    whisper_default_language: str = "en"
    model_cache_dir: Path | None = None
    work_dir: Path = Path("/tmp")
    max_job_attempts: int = 3
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
    """Adapts persistence for the worker's language pinning and completion bookkeeping.

    `SessionRepository` (`sturnus.infrastructure.db.repositories`) does not
    yet expose `detected_language`/`set_detected_language`/`mark_documented`
    -- see the module docstring for why this file supplies them directly
    instead of adding them there. Reads and writes the same
    `session_participant.detected_language` and `session.*` columns that
    repository already owns, through the same async ORM.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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

    async def mark_documented(self, session_id: int, doc_id: str, url: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(
                    status="documented",
                    document_provider="outline",
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


async def _run() -> None:
    logging.basicConfig(level=logging.INFO)
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

    queue = JobQueue(session_factory)
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
        collection_id=settings.outline_collection_id,
    )
    sessions = _WorkerSessionStore(session_factory)

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
                work_dir=settings.work_dir,
                max_attempts=settings.max_job_attempts,
            )
            if not did_work:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
    finally:
        log.info("Shutdown requested: worker stopping after its current job")
        await health_runner.cleanup()
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
