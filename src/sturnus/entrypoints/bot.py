"""Process entrypoint for the `bot` deployment (Spec 4.1).

Builds the dependency graph, waits for a schema the bot itself never
creates (the worker owns migrations, Spec 13.1), recovers whatever a
previous process left on disk, starts the health server, and runs the
Discord client until `SIGTERM` or `SIGINT` asks it to stop -- at which
point it closes every in-progress session before disconnecting, so a
routine deploy never discards a recording (Spec 6.4).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import signal
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.application.ports import Clock
from sturnus.application.recovery import recover_orphans
from sturnus.config import get_settings
from sturnus.domain import settings as domain_settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.repositories import (
    ConsentRepository,
    JobRepository,
    SessionRepository,
)
from sturnus.infrastructure.discord.client import SturnusClient
from sturnus.infrastructure.health import ReadinessState, start_health_server
from sturnus.infrastructure.objectstore import S3AudioStore
from sturnus.infrastructure.recording_adapters import CryptoEncryptor, FileAudioWriterFactory

log = logging.getLogger(__name__)

_SCHEMA_WAIT_TIMEOUT_SECONDS = 60.0
_SCHEMA_WAIT_INTERVAL_SECONDS = 2.0


class SystemClock:
    """Satisfies the `Clock` port with the wall clock, always timezone-aware."""

    def now(self) -> datetime:
        return datetime.now(UTC)


async def _wait_for_schema(
    engine: AsyncEngine,
    timeout_seconds: float = _SCHEMA_WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = _SCHEMA_WAIT_INTERVAL_SECONDS,
) -> None:
    """Polls until every table the models declare exists, or fails loudly.

    The bot never runs Alembic itself; the worker owns the schema
    (Spec 13.1). At boot the schema may briefly not exist yet -- a fresh
    deploy racing the worker's migration -- or may be missing outright, a
    misconfiguration. Waiting a bounded amount of time covers the former;
    raising instead of starting against a schema that doesn't match the
    models covers the latter.
    """
    required = {table.name for table in Base.metadata.sorted_tables}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        async with engine.connect() as conn:
            existing: set[str] = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        missing = required - existing
        if not missing:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"Database schema is missing required table(s): {sorted(missing)}. "
                "The worker owns migrations and must run them before the bot can start."
            )
        log.warning("Waiting for the database schema; missing table(s): %s", sorted(missing))
        await asyncio.sleep(interval_seconds)


async def _run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _wait_for_schema(engine)

    config_store = ConfigStore(session_factory)
    consent_repo = ConsentRepository(session_factory)
    session_repo = SessionRepository(session_factory)
    job_repo = JobRepository(session_factory)

    audio_store = S3AudioStore(
        settings.s3_endpoint,
        settings.s3_bucket,
        settings.s3_access_key.get_secret_value(),
        settings.s3_secret_key.get_secret_value(),
    )
    encryptor = CryptoEncryptor(
        base64.b64decode(settings.master_key.get_secret_value()), settings.master_key_id
    )
    writer_factory = FileAudioWriterFactory(settings.recording_dir)
    clock: Clock = SystemClock()

    # Recovery has no guild to read a per-guild retention override from --
    # only a session id parsed off the filesystem -- so it falls back to
    # the global default rather than guessing at any one guild's setting.
    default_retention_days = int(domain_settings.DEFAULTS[domain_settings.AUDIO_RETENTION_DAYS])
    recovered = await recover_orphans(
        settings.recording_dir,
        session_repo,
        job_repo,
        audio_store,
        encryptor,
        default_retention_days,
        clock.now(),
    )
    if recovered:
        log.warning(
            "Recovered %d orphaned recording(s) left behind by a previous process",
            len(recovered),
        )

    readiness = ReadinessState()

    async def database_ping() -> bool:
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    health_runner = await start_health_server(readiness, settings.health_port)

    client = SturnusClient(
        clock=clock,
        config_store=config_store,
        consent_repo=consent_repo,
        session_repo=session_repo,
        job_repo=job_repo,
        audio_store=audio_store,
        writer_factory=writer_factory,
        encryptor=encryptor,
        readiness=readiness,
        database_ping=database_ping,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    client_task = asyncio.create_task(client.start(settings.discord_token.get_secret_value()))
    try:
        await stop.wait()
    finally:
        log.info("Shutdown requested: closing every active session before disconnecting")
        await client.graceful_shutdown()
        await client.close()
        await client_task
        await health_runner.cleanup()
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
