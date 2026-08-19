"""Migration tests deliberately run synchronously.

`alembic.command.*` is a synchronous API; called from an `async def`
test it breaks inside the running event loop.
"""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from sturnus.infrastructure.db.models import Base

EXPECTED_TABLES = {
    "guild_config",
    "account_link",
    "consent",
    "oauth_state",
    "session",
    "session_participant",
    "transcription_job",
}


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "+psycopg")


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url(url))
    return cfg


def _table_names(url: str) -> set[str]:
    engine = create_engine(_sync_url(url))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        return {row[0] for row in rows}


def test_migration_creates_every_table(clean_database: str) -> None:
    command.upgrade(_alembic_config(clean_database), "head")
    assert EXPECTED_TABLES <= _table_names(clean_database)  # noqa: SIM300


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
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"models and migration have drifted: {diff}"
