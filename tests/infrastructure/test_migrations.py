"""Migration tests deliberately run synchronously.

`alembic.command.*` is a synchronous API; called from an `async def`
test it breaks inside the running event loop.
"""

import logging
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from sturnus.infrastructure.db.models import Base
from sturnus.observability import setup

EXPECTED_TABLES = {
    "guild_config",
    "account_link",
    "consent",
    "oauth_state",
    "session",
    "session_participant",
    "transcription_job",
    "admin_member",
    "console_state",
    "user_preference",
    "guild_channel",
    "guild_role",
    "guild_member",
    "outline_collection",
    "guild",
    "guild_export_target",
    "session_document",
    "guild_setup_intent",
}

#: The revision before the one that drops the per-guild sign-in schema.
PREVIOUS_HEAD = "0013"

#: Fixed rather than read from the clock, so a seeded row is the same on
#: every run.
SEEDED_AT = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "+psycopg")


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url(url))
    return cfg


def _table_names(url: str) -> set[str]:
    engine = create_engine(_sync_url(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def _console_state_columns(url: str) -> set[str]:
    engine = create_engine(_sync_url(url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'console_state'"
                )
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def _guild_sign_in_client_count(url: str) -> int:
    engine = create_engine(_sync_url(url))
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM guild_oauth_client")).scalar_one())
    finally:
        engine.dispose()


def _seed_previous_head(url: str) -> None:
    """One per-guild client and one sign-in state that names its guild."""
    engine = create_engine(_sync_url(url))
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO guild_oauth_client (guild_id, slug, provider, base_url, "
                    "client_id, wrapped_client_secret, encryption_key_id, redirect_uri, "
                    "created_at, updated_at) "
                    "VALUES (4711, 'acme', 'outline', 'https://outline.example', 'client-id', "
                    ":secret, 'master-1', NULL, :at, :at)"
                ),
                {"secret": b"wrapped-secret", "at": SEEDED_AT},
            )
            conn.execute(
                text(
                    "INSERT INTO console_state (state, created_at, expires_at, guild_id) "
                    "VALUES ('pending', :at, :at, 4711)"
                ),
                {"at": SEEDED_AT},
            )
    finally:
        engine.dispose()


def test_migration_creates_every_table(clean_database: str) -> None:
    command.upgrade(_alembic_config(clean_database), "head")
    assert _table_names(clean_database) >= EXPECTED_TABLES


def test_downgrade_removes_the_tables(clean_database: str) -> None:
    cfg = _alembic_config(clean_database)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert not (EXPECTED_TABLES & _table_names(clean_database))


def test_models_and_migration_do_not_drift(clean_database: str) -> None:
    """After `upgrade head`, an autogenerate must find nothing left to do.

    Without this test, a model change with no matching migration goes
    unnoticed until it shows up in production.
    """
    command.upgrade(_alembic_config(clean_database), "head")

    engine = create_engine(_sync_url(clean_database))
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"models and migration have drifted: {diff}"


def test_running_migrations_does_not_silence_the_application_loggers(
    clean_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker migrates in-process, and `fileConfig` is a global.

    `migrations/env.py` calls `logging.config.fileConfig`, whose default
    `disable_existing_loggers=True` sets `disabled = True` on every logger
    not named in `alembic.ini`. `sturnus.entrypoints.worker._run_migrations`
    runs this at worker startup, when every `sturnus.*` logger already
    exists -- so with the default the worker logs nothing for the rest of
    its life, and reports nothing to Sentry either, since
    `LoggingIntegration` hooks `Logger.callHandlers` and
    `Logger.handle` never gets there for a disabled logger.

    `env.py` now reaches `fileConfig` only when nothing else owns logging
    yet, so `_configured` is forced back to False for the duration: any test
    that ran `configure_logging()` earlier in the session would otherwise
    close the guard, skip `fileConfig` altogether, and leave this test
    passing without ever exercising the call it exists to constrain. The
    guard is the other half of the same regression -- see
    `tests/test_logging_discipline.py` -- and neither half may be tested by
    accidentally disabling the other.
    """
    monkeypatch.setattr(setup, "_configured", False)

    application_logger = logging.getLogger("sturnus.infrastructure.observability")
    assert application_logger.disabled is False

    command.upgrade(_alembic_config(clean_database), "head")

    assert application_logger.disabled is False, (
        "alembic's fileConfig disabled the application loggers; the worker "
        "runs migrations in-process and would go silent after startup"
    )
    assert logging.getLogger("asyncio").disabled is False


def test_there_is_exactly_one_migration_head(clean_database: str) -> None:
    """Two heads would make `upgrade head` pick one at random, and a branch that
    forgot to rebase onto the new revision would leave two.
    """
    heads = ScriptDirectory.from_config(_alembic_config(clean_database)).get_heads()
    assert len(heads) == 1, f"expected one migration head, found {heads}"


def test_upgrade_removes_the_guild_sign_in_table_and_column(clean_database: str) -> None:
    command.upgrade(_alembic_config(clean_database), "head")
    assert "guild_oauth_client" not in _table_names(clean_database)
    assert "guild_id" not in _console_state_columns(clean_database)


def test_upgrade_removes_a_populated_guild_sign_in_client(clean_database: str) -> None:
    """The drop is exercised on data, not on the empty table a fresh schema has."""
    cfg = _alembic_config(clean_database)
    command.upgrade(cfg, PREVIOUS_HEAD)
    _seed_previous_head(clean_database)

    command.upgrade(cfg, "head")

    assert "guild_oauth_client" not in _table_names(clean_database)
    assert "guild_id" not in _console_state_columns(clean_database)


def test_downgrade_one_step_restores_the_empty_shapes(clean_database: str) -> None:
    """Downgrade can walk the graph back, but it cannot bring rows back."""
    cfg = _alembic_config(clean_database)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    assert "guild_oauth_client" in _table_names(clean_database)
    assert _guild_sign_in_client_count(clean_database) == 0
    assert "guild_id" in _console_state_columns(clean_database)
