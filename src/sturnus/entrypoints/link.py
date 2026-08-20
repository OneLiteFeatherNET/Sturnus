"""Process entrypoint for the `link` deployment (Spec 8.4, Spec 13.2).

This is the only publicly reachable process in the system, and its
configuration is deliberately its own, separate model rather than a reuse
of `sturnus.config.Settings`: the bot's `Settings` requires a Discord
token, S3 credentials and the audio master key, and reusing it here would
force this process to hold all three just to satisfy validation, exactly
the blast radius the separate deployment exists to avoid (Spec 13.2). This
process holds an OAuth client secret and a database connection, and
nothing else.

Builds the OAuth client, the state store and the account-link repository,
waits for the two tables it reads and writes (`oauth_state`,
`account_link`) to exist -- like the bot, this process never runs
migrations itself; the worker owns the schema (Spec 13.1) -- then serves
`build_app`'s three routes until `SIGTERM` or `SIGINT`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from aiohttp import web
from pydantic import SecretStr
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.config import StrictSettings
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.models import AccountLink, OAuthState
from sturnus.infrastructure.db.repositories import AccountLinkRepository
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.linkserver import build_app
from sturnus.infrastructure.observability import init_sentry

log = logging.getLogger(__name__)

_SCHEMA_WAIT_TIMEOUT_SECONDS = 60.0
_SCHEMA_WAIT_INTERVAL_SECONDS = 2.0

_REQUIRED_TABLES = {OAuthState.__tablename__, AccountLink.__tablename__}


class LinkSettings(StrictSettings):
    """Everything this process needs, and nothing it does not.

    No `discord_token`, no `s3_*` credentials, no `master_key`: unlike
    `sturnus.config.Settings`, every field here is something this specific
    process actually uses.
    """

    database_url: str
    outline_base_url: str
    outline_client_id: str
    outline_client_secret: SecretStr
    outline_redirect_uri: str
    health_port: int = 8080


async def _wait_for_schema(
    engine: AsyncEngine,
    timeout_seconds: float = _SCHEMA_WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = _SCHEMA_WAIT_INTERVAL_SECONDS,
) -> None:
    """Polls until `oauth_state` and `account_link` both exist, or fails loudly.

    This process never runs Alembic itself; the worker owns the schema
    (Spec 13.1). At boot the schema may briefly not exist yet -- a fresh
    deploy racing the worker's migration -- or may be missing outright, a
    misconfiguration. Waiting a bounded amount of time covers the former;
    raising instead of starting against a schema that doesn't match covers
    the latter.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        async with engine.connect() as conn:
            existing: set[str] = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        missing = _REQUIRED_TABLES - existing
        if not missing:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"Database schema is missing required table(s): {sorted(missing)}. "
                "The worker owns migrations and must run them before the link "
                "service can start."
            )
        log.warning("Waiting for the database schema; missing table(s): %s", sorted(missing))
        await asyncio.sleep(interval_seconds)


async def _run() -> None:
    settings = LinkSettings()

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    states = LinkStateStore(session_factory)
    links = AccountLinkRepository(session_factory)
    oauth = OutlineOAuth(
        base_url=settings.outline_base_url,
        client_id=settings.outline_client_id,
        client_secret=settings.outline_client_secret.get_secret_value(),
        redirect_uri=settings.outline_redirect_uri,
    )

    # The wait for the worker's migrations happens *after* this server is
    # listening, not before. Waiting first leaves the health port closed for
    # as long as the wait takes, and the liveness probe kills the pod while
    # it is doing exactly what it should -- which is what happened on the
    # first deployment, where the tables did not exist until the worker had
    # run. `/healthz` now answers immediately and `/readyz` reports 503
    # until the schema is there, so Kubernetes holds traffic back without
    # restarting anything.
    schema_ready = False

    app = build_app(
        oauth=oauth,
        states=states,
        links=links,
        now=lambda: datetime.now(UTC),
        schema_ready=lambda: schema_ready,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    # Bound to `0.0.0.0`: this is the one process meant to be reached from
    # outside the pod network, but the actual public exposure is a
    # Kubernetes Service and Cloudflare Tunnel in front of it, not this
    # bind itself (Spec 13.5).
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    log.info("Link service listening on port %d", settings.health_port)

    await _wait_for_schema(engine)
    schema_ready = True
    log.info("Database schema is present; ready to serve account links")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("Shutdown requested: stopping the link service")
        await runner.cleanup()
        await engine.dispose()


def main() -> None:
    # Both run before `_run`, and so before `LinkSettings()` reads the
    # environment: with a DSN configured, a settings `ValidationError` is
    # then itself reported instead of being the one failure Sentry can never
    # see. Without a DSN, `init_sentry` returns having touched nothing at all
    # -- see `sturnus.infrastructure.observability`.
    logging.basicConfig(level=logging.INFO)
    init_sentry("link")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
