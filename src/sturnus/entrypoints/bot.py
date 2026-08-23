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
import contextlib
import logging
import signal
from datetime import UTC, datetime

import discord
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.application.ports import Clock
from sturnus.application.publishing import SessionReader, announce_ready_sessions
from sturnus.application.recovery import recover_orphans
from sturnus.config import get_settings
from sturnus.domain import settings as domain_settings
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.directory import DirectoryStore
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.models import Base
from sturnus.infrastructure.db.repositories import (
    AccountLinkRepository,
    ConsentRepository,
    JobRepository,
    SessionRepository,
)
from sturnus.infrastructure.discord.announcer import DiscordAnnouncer
from sturnus.infrastructure.discord.client import SturnusClient
from sturnus.infrastructure.discord.link_cog import PROVIDER as OUTLINE_PROVIDER
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.health import ReadinessState, start_health_server
from sturnus.infrastructure.objectstore import S3AudioStore
from sturnus.infrastructure.observability import init_sentry
from sturnus.infrastructure.recording_adapters import CryptoEncryptor, FileAudioWriterFactory
from sturnus.infrastructure.telemetry import init_telemetry, shutdown_telemetry
from sturnus.infrastructure.traced import TracedAudioStore, TracedEncryptor, TracedJobQueue
from sturnus.observability.events import Event, log_event, log_exception
from sturnus.observability.setup import (
    asyncio_exception_handler,
    configure_logging,
    install_excepthooks,
)

log = logging.getLogger(__name__)

_SCHEMA_WAIT_TIMEOUT_SECONDS = 60.0
_SCHEMA_WAIT_INTERVAL_SECONDS = 2.0

# `publish_poll_seconds` (Spec 8.5) is stored per guild, but the sweep below
# posts across every guild's ready sessions in one pass; a single process-
# wide interval, taken from the setting's own default, is a deliberate
# simplification rather than per-guild scheduling for what the spec itself
# calls "only a handful of sessions per day".
_PUBLISH_POLL_SECONDS = float(domain_settings.DEFAULTS[domain_settings.PUBLISH_POLL_SECONDS])


class SystemClock:
    """Satisfies the `Clock` port with the wall clock, always timezone-aware."""

    def now(self) -> datetime:
        return datetime.now(UTC)


async def _publish_loop(
    client: discord.Client,
    sessions: SessionReader,
    link_states: LinkStateStore,
    stop: asyncio.Event,
    poll_seconds: float = _PUBLISH_POLL_SECONDS,
) -> None:
    """Periodically posts each finished session's document link (Spec 8.5,
    Defect 3): `sturnus.application.publishing.sessions_to_announce` exists
    but nothing outside its own tests calls it -- this is that call.

    Also purges expired rows from `oauth_state` on the same interval, via
    `link_states.purge_expired` (`sturnus.infrastructure.db.link_state.
    LinkStateStore`). That method existed with its own passing unit tests
    but no caller anywhere in the process, so every abandoned `/link start`
    left its row behind forever. It belongs in this loop rather than a
    fourth one of its own: `bot.py` already constructs `LinkStateStore`
    here (to *issue* states from `/link start`) and already runs this
    exact poll/stop-event loop, so folding the purge in needs no new task
    and no new lifecycle to manage -- only one more per-guild-agnostic
    sweep alongside the one this loop already does.

    Waits for the gateway connection before its first sweep -- `get_channel`
    needs the client's channel cache populated, which only happens once
    `on_ready` has run. Both sweeps below survive their own errors
    independently, the same way `announce_ready_sessions` already survives
    one session's own failure: one sweep failing (a database hiccup) must
    not stop the other, or kill this loop outright.
    """
    await client.wait_until_ready()
    announcer = DiscordAnnouncer(client)
    # Read once, outside the loop and outside the `try` below: the count is
    # fixed for the life of the connection, and reading it inside a block
    # that swallows every exception would turn "this attribute moved" into
    # a sweep that quietly announces nothing. Taken from the client rather
    # than from `Settings` because the setting may be unset, in which case
    # Discord chose the number and only the client knows it --
    # `wait_until_ready()` above has returned, so `launch_shards` has
    # already filled it in.
    shard_count = client.shard_count
    while not stop.is_set():
        now = datetime.now(UTC)
        try:
            await announce_ready_sessions(sessions, announcer, now, shard_count=shard_count)
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.SWEEP_FAILED,
                "Publish sweep failed; will retry next interval",
                exc,
                reason="publish",
            )
        try:
            await link_states.purge_expired(now)
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.SWEEP_FAILED,
                "Expired link-state purge failed; will retry next interval",
                exc,
                reason="link_state_purge",
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


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
        log_event(
            log,
            logging.WARNING,
            Event.SCHEMA_WAITING,
            "Waiting for the worker to migrate the database schema",
            missing=sorted(missing),
        )
        await asyncio.sleep(interval_seconds)


async def _run() -> None:
    settings = get_settings()

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _wait_for_schema(engine)

    config_store = ConfigStore(session_factory)
    consent_repo = ConsentRepository(session_factory)
    session_repo = SessionRepository(session_factory)
    job_repo = JobRepository(session_factory)
    link_states = LinkStateStore(session_factory)
    # Written here, read by the console's API process -- which has no
    # gateway to ask Discord who holds `admin_role_id`, and must not be
    # given one (Spec 13.2): it already holds S3 and the master key.
    admin_mirror = AdminMemberStore(session_factory)
    # The same arrangement, one step wider: the console makes an
    # administrator paste snowflakes into `voice_channel_id`,
    # `consent_role_id` and `admin_role_id` and then shows those
    # snowflakes back, because `api` cannot ask what they are called
    # either. This is where the names come from.
    directory_mirror = DirectoryStore(session_factory)
    # `provider` fixed at construction: the bot only ever reads its own
    # Outline mapping back (`/link status`), never another provider's --
    # see `AccountLinkRepository`'s class docstring for why the read and
    # write sides differ here.
    account_links = AccountLinkRepository(session_factory, OUTLINE_PROVIDER)
    outline_oauth = OutlineOAuth(
        base_url=settings.outline_base_url,
        client_id=settings.outline_client_id,
        # Deliberately empty: this process never exchanges a code for a
        # token (that is the `link` deployment's job), so it never calls
        # `identity_from_code`, the only method that reads this value. The
        # real secret must never enter the bot process (Spec 13.2) -- see
        # `sturnus.config.Settings`'s comment on the same split.
        client_secret="",
        redirect_uri=settings.outline_redirect_uri,
    )

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

    # Tracing is applied here, on the way into `SturnusClient` and therefore
    # into `RecordingService`. Each wrapper satisfies the same port the plain
    # adapter does, so `sturnus.application.recording` gains a span per
    # encrypt/upload/enqueue without importing OpenTelemetry -- which it may
    # not do (`tests/test_architecture.py`). See
    # `sturnus.infrastructure.traced`.
    #
    # `audio_store` and `encryptor` are wrapped *after* `recover_orphans`
    # has used the plain ones below: recovery runs once at startup, outside
    # any session, and its spans would be orphaned roots carrying nothing
    # a log line does not already say.
    traced_audio_store = TracedAudioStore(audio_store)
    traced_encryptor = TracedEncryptor(encryptor)
    traced_job_repo = TracedJobQueue(job_repo)

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
        log_event(
            log,
            logging.WARNING,
            Event.SESSION_RECOVERED,
            "Recovered orphaned recordings left behind by a previous process",
            count=len(recovered),
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
        admin_mirror=admin_mirror,
        directory_mirror=directory_mirror,
        consent_repo=consent_repo,
        session_repo=session_repo,
        job_repo=traced_job_repo,
        audio_store=traced_audio_store,
        writer_factory=writer_factory,
        encryptor=traced_encryptor,
        readiness=readiness,
        database_ping=database_ping,
        session_factory=session_factory,
        outline_oauth=outline_oauth,
        link_states=link_states,
        account_links=account_links,
        capture_diagnostics=settings.capture_diagnostics,
        shard_count=settings.shard_count,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(asyncio_exception_handler)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    client_task = asyncio.create_task(client.start(settings.discord_token.get_secret_value()))
    publish_task = asyncio.create_task(_publish_loop(client, session_repo, link_states, stop))
    try:
        await stop.wait()
    finally:
        log_event(
            log,
            logging.INFO,
            Event.SHUTDOWN_BEGIN,
            "Shutdown requested: closing every active session before disconnecting",
        )
        publish_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publish_task
        await client.graceful_shutdown()
        await client.close()
        await client_task
        await health_runner.cleanup()
        await engine.dispose()
        # Last: flushes the spans describing this very shutdown, which is
        # exactly the batch someone will be looking for after a deploy that
        # lost a session.
        shutdown_telemetry()
        log_event(log, logging.INFO, Event.SHUTDOWN_COMPLETE, "Bot stopped")


def main() -> None:
    # All four run before `_run`, and so before `get_settings()` reads the
    # environment: with a DSN configured, a settings `ValidationError` is
    # then itself reported instead of being the one failure Sentry can never
    # see. Without a DSN, `init_sentry` returns having touched nothing at all
    # -- see `sturnus.infrastructure.observability`.
    # `configure_logging` first of all: it installs the handler that formats
    # and redacts everything the other three might have to report, and
    # `install_excepthooks` is what stops a settings `ValidationError` --
    # whose pydantic message embeds the raw environment dict, token prefix
    # and all -- reaching stderr unredacted.
    configure_logging("bot")
    install_excepthooks()
    init_sentry("bot")
    init_telemetry("bot")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
