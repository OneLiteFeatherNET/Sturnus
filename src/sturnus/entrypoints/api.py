"""Process entrypoint for the `api` deployment: the console's back end.

Serves the JSON API at `sturnus.onelitefeather.dev/api/*` behind an OAuth
session. See `docs/superpowers/specs/2026-08-21-sturnus-console-design.md`.

**What this process holds, and what it must never hold.** It has the
database, the OAuth client secret, S3 and the master key -- the last two
because it decrypts audio on the way to the browser
(`sturnus.console.routes_audio`). It has no Discord token, and must not be
given one: a process that can read every recording ever made is not one to
also hand the ability to act as the bot (Spec 13.2). Whether somebody
administers a guild is therefore read from `admin_member`, which the bot
mirrors, rather than asked of Discord here.

That makes this the second process holding the master key, and the first
one reachable through a browser. What keeps that defensible is not the key
handling -- it is that the key never unwraps anything the requester was not
in the room for, which is decided one layer up, per request, against
`session_participant`.

Like `link`, this starts listening *before* waiting for the worker's
migrations. Waiting first leaves the health port closed for as long as the
wait takes, and the liveness probe kills the pod while it is doing exactly
what it should.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import signal
from datetime import UTC, datetime, timedelta

from aiohttp import web
from pydantic import SecretStr, field_validator
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.config import StrictSettings
from sturnus.console.adapters import (
    ConsoleCollectionNames,
    ConsoleConsentDirectory,
    ConsoleGuildNames,
    ConsoleGuildOAuthClients,
    ConsoleGuildReports,
    ConsoleGuildSetup,
    ConsoleLinkDirectory,
    ConsolePersonalConsents,
    ConsoleProfileDirectory,
    ConsoleQueueControl,
    ConsoleQueueOverview,
    ConsoleSessionDocuments,
    ConsoleSessionNaming,
    ConsoleStateStore,
    ConsoleTagWriter,
    ConsoleTrackDirectory,
    ConsoleTranscripts,
    GuildSignInClients,
)
from sturnus.console.app import build_api
from sturnus.console.audio import AudioDelivery
from sturnus.console.queries import ConsoleQueries
from sturnus.console.session import SessionCookie
from sturnus.domain.onboarding import invite_url
from sturnus.infrastructure.crypto import KeyWrapper
from sturnus.infrastructure.db.admin_members import AdminMemberStore
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.export_targets import ExportTargetStore
from sturnus.infrastructure.db.guild_oauth import GuildOAuthClientStore
from sturnus.infrastructure.db.models import (
    AccountLink,
    AdminMember,
    ConsoleState,
    GuildChannel,
    GuildConfig,
    GuildExportTarget,
    GuildMember,
    GuildRole,
    GuildSetupIntent,
    OutlineCollection,
    SessionDocument,
    UserPreference,
)
from sturnus.infrastructure.db.models import GuildOAuthClient as GuildOAuthClientRow
from sturnus.infrastructure.db.preferences import PreferenceStore
from sturnus.infrastructure.db.setup_intents import SetupIntentStore
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth
from sturnus.infrastructure.objectstore import S3AudioStore, S3DocumentStore
from sturnus.infrastructure.observability import init_sentry
from sturnus.infrastructure.telemetry import init_telemetry, shutdown_telemetry
from sturnus.observability.events import Event, log_event
from sturnus.observability.setup import (
    asyncio_exception_handler,
    configure_logging,
    install_excepthooks,
)

log = logging.getLogger(__name__)

_SCHEMA_WAIT_INTERVAL_SECONDS = 2.0

#: How long a console session lasts. Long enough for a working day, short
#: enough that a tab left open overnight is not a standing grant.
_SESSION_LIFETIME = timedelta(hours=12)

#: The tables this process reads. Narrower than the whole schema on
#: purpose: waiting for tables it never touches would tie its readiness to
#: migrations that have nothing to do with it.
_REQUIRED_TABLES = frozenset(
    {
        AccountLink.__tablename__,
        AdminMember.__tablename__,
        ConsoleState.__tablename__,
        # The per-guild sign-in clients. On the list because the *login*
        # route reads it on every request that carries `?guild=`, so a
        # `/readyz` that passed without it would be a console signing
        # people in through the environment client while a guild's own
        # link 500s.
        GuildOAuthClientRow.__tablename__,
        # The settings section reads and writes this one. Without it here,
        # `/readyz` would pass while the first settings page 500s.
        GuildConfig.__tablename__,
        # A person's own preferences, and the four mirrors the console
        # resolves ids through. Every one of them is read while a page is
        # being rendered, so leaving them out would let `/readyz` pass
        # while the first profile menu 500s -- the same argument
        # `guild_config` is on this list for.
        UserPreference.__tablename__,
        GuildChannel.__tablename__,
        GuildRole.__tablename__,
        GuildMember.__tablename__,
        OutlineCollection.__tablename__,
        # Where a guild publishes, and what a session published. The
        # export section reads the first and the protocol route reads the
        # second, both while a page is being rendered -- the same argument
        # `guild_config` is on this list for.
        GuildExportTarget.__tablename__,
        SessionDocument.__tablename__,
        # Where onboarding is written down. On the list for the same
        # reason `guild_config` is: without it `/readyz` would pass while
        # the first person trying to set a guild up got a 500 for their
        # trouble.
        GuildSetupIntent.__tablename__,
        "session",
        "session_participant",
        "transcription_job",
    }
)


class ApiSettings(StrictSettings):
    """Everything this process needs, and nothing it does not.

    No `discord_token`: see the module docstring. `session_secret` signs
    the console's session cookies and is refused below thirty-two bytes by
    `SessionCookie` itself, so a placeholder fails at startup rather than
    serving forgeable sessions.

    The S3 credentials and the master key are here because audio playback
    decrypts in this process. They are required rather than optional, so a
    deployment missing them fails while an operator is still looking at it
    -- the alternative is a console that signs people in, shows them their
    sessions, and answers every play button with a 500.
    """

    database_url: str
    outline_base_url: str
    outline_client_id: str
    outline_client_secret: SecretStr
    #: The console's own callback, which is *not* the account-link one --
    #: they are different flows on different paths, and registering the
    #: same URI for both would send an account link to the console.
    console_redirect_uri: str
    session_secret: SecretStr
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    #: Base64-encoded 32 bytes, and the id of the key it is. A recording
    #: names the master key that wrapped its data key, so the id is what
    #: lets a mismatch be reported as the configuration error it is rather
    #: than as an authentication-tag failure mid-response.
    master_key: SecretStr
    master_key_id: str
    health_port: int = 8080
    console_origin: str = "https://sturnus.onelitefeather.dev"
    #: This deployment's Discord application id, used for one thing: the
    #: `bot`-scope invite link the console offers. Public by design -- it
    #: appears in every invite URL ever clicked -- so it travels as plain
    #: configuration and never through the Secret, and it is emphatically
    #: not a token: it grants this process nothing (Spec 13.2).
    #:
    #: Optional, so a deployment that has not set it yet starts and serves
    #: everything else; the invite endpoint then answers `url: null`,
    #: which the console renders as "this deployment has no invite link
    #: configured" rather than as an error.
    discord_client_id: str | None = None

    @field_validator("discord_client_id", mode="after")
    @classmethod
    def _client_id_is_a_snowflake(cls, value: str | None) -> str | None:
        """Blank is absent; anything that is not digits fails at startup.

        The alternative is a console that signs people in, offers an
        invite button, and hands whoever clicks it a Discord page that
        cannot say what application it is being asked to authorise.
        Failing here names the variable while an operator is still looking
        at the deployment.
        """
        if value is None or not value.strip():
            return None
        # Raises `ValueError` for anything that is not a snowflake, with
        # the reason argued where the rule lives.
        invite_url(value.strip())
        return value.strip()


async def _wait_for_schema(engine: AsyncEngine) -> None:
    """Polls until every table this process reads exists.

    Unbounded, unlike the bot's equivalent: this process serves a web
    console, and a console that is 503 while the schema catches up is a
    better outcome than a pod that gives up and crash-loops. `/readyz`
    reports the wait, so Kubernetes holds traffic back rather than
    restarting anything.
    """
    while True:
        async with engine.connect() as conn:
            existing: set[str] = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        missing = _REQUIRED_TABLES - existing
        if not missing:
            return
        log_event(
            log,
            logging.WARNING,
            Event.SCHEMA_WAITING,
            "Waiting for the worker to migrate the database schema",
            missing=sorted(missing),
        )
        await asyncio.sleep(_SCHEMA_WAIT_INTERVAL_SECONDS)


async def _run() -> None:
    settings = ApiSettings()

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    oauth = OutlineOAuth(
        base_url=settings.outline_base_url,
        client_id=settings.outline_client_id,
        client_secret=settings.outline_client_secret.get_secret_value(),
        redirect_uri=settings.console_redirect_uri,
    )

    admins = AdminMemberStore(session_factory)
    config = ConfigStore(session_factory)
    # The master key is this process's and only this process's. `link`
    # does not hold one -- the chart's `_helpers.tpl` refuses to render
    # it there -- which is exactly why per-guild OAuth is available to
    # the console sign-in and not to the Discord account-link flow, and
    # why an export target's credential is unwrappable here and nowhere
    # else.
    keys = KeyWrapper(
        base64.b64decode(settings.master_key.get_secret_value()),
        settings.master_key_id,
    )
    oauth_clients = GuildOAuthClientStore(session_factory, keys)

    audio = AudioDelivery(
        # The configuration store, because the download route's rule
        # includes a per-guild switch the guild has to have turned on
        # (`settings.ADMIN_AUDIO_DOWNLOAD_OFFERED`). Playback's rule is
        # unchanged and reads none of it.
        tracks=ConsoleTrackDirectory(session_factory, config),
        source=S3AudioStore(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
        ),
        keys=keys,
    )

    def now() -> datetime:
        """The one clock this process reads.

        Named rather than repeated as a lambda at each call site: two
        collaborators that each build their own would be two clocks a test
        has to pin separately, and one of them would be missed.
        """
        return datetime.now(UTC)

    schema_ready = False
    app = build_api(
        # Not the client itself any more: which client a sign-in runs
        # against is a per-sign-in question now, and `oauth` above is
        # what a sign-in with no guild resolves to.
        clients=GuildSignInClients(
            oauth, oauth_clients, redirect_uri=settings.console_redirect_uri
        ),
        states=ConsoleStateStore(session_factory),
        links=ConsoleLinkDirectory(session_factory),
        admins=admins,
        config=config,
        reads=ConsoleQueries(session_factory),
        sessions=SessionCookie(settings.session_secret.get_secret_value(), _SESSION_LIFETIME),
        now=now,
        schema_ready=lambda: schema_ready,
        console_origin=settings.console_origin,
        audio=audio,
        queue=ConsoleQueueControl(session_factory, admins),
        tags=ConsoleTagWriter(session_factory),
        queues=ConsoleQueueOverview(session_factory, admins, now),
        consents=ConsoleConsentDirectory(session_factory, admins, config, now),
        own_consents=ConsolePersonalConsents(session_factory, config, now),
        reports=ConsoleGuildReports(session_factory, admins, config),
        profile=ConsoleProfileDirectory(session_factory),
        prefs=PreferenceStore(session_factory),
        names=ConsoleGuildNames(session_factory, admins),
        collections=ConsoleCollectionNames(session_factory, admins),
        # The configuration store again, because a transcript is
        # assembled under the same guild's `merge_gap_seconds` and
        # `document_provider` the published protocol was built with --
        # anything else would be a console disagreeing with the document
        # about the same meeting.
        transcripts=ConsoleTranscripts(session_factory, config),
        naming=ConsoleSessionNaming(session_factory),
        # The real store, not a narrowed copy of it. The port
        # (`sturnus.console.ports.ExportTargets`) is what keeps
        # `secret_for` out of a handler's reach: it is not on the port, so
        # nothing typed against the port can call it, and the store needs
        # no second class to say so.
        exports=ExportTargetStore(session_factory, keys),
        documents=ConsoleSessionDocuments(session_factory),
        artefacts=S3DocumentStore(
            settings.s3_endpoint,
            settings.s3_bucket,
            settings.s3_access_key.get_secret_value(),
            settings.s3_secret_key.get_secret_value(),
        ),
        oauth_clients=ConsoleGuildOAuthClients(oauth_clients, admins),
        setup=ConsoleGuildSetup(session_factory, admins, SetupIntentStore(session_factory)),
        discord_client_id=settings.discord_client_id,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    # Bound to `0.0.0.0`: reached through a Kubernetes Service and the
    # Cloudflare Tunnel in front of it, never directly (Spec 13.5).
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    log_event(
        log,
        logging.INFO,
        Event.CONSOLE_STARTED,
        "Console API listening",
        count=settings.health_port,
    )

    await _wait_for_schema(engine)
    schema_ready = True
    log.info("Database schema is present; ready to serve the console")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(asyncio_exception_handler)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log_event(
            log,
            logging.INFO,
            Event.SHUTDOWN_BEGIN,
            "Shutdown requested: stopping the console API",
        )
        await runner.cleanup()
        await engine.dispose()
        shutdown_telemetry()
        log_event(log, logging.INFO, Event.SHUTDOWN_COMPLETE, "Console API stopped")


def main() -> None:
    # The same four, in the same order and for the same reasons as every
    # other entrypoint: `configure_logging` installs the handler that
    # redacts what the others report, and `install_excepthooks` is what
    # stops a settings `ValidationError` -- whose pydantic message embeds
    # the raw environment dict, secret prefixes and all -- reaching stderr
    # unredacted.
    configure_logging("api")
    install_excepthooks()
    init_sentry("api")
    init_telemetry("api")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
