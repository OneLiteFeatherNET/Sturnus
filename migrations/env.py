from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
#
# `disable_existing_loggers=False` is load-bearing, not a preference.
# `fileConfig` defaults it to True, which sets `disabled = True` on every
# logger that already exists and is not named in `alembic.ini` -- and
# `logging.Logger.handle` returns immediately for a disabled logger. This
# module is not only imported by the `alembic` CLI: the worker runs
# migrations in-process at startup (`_run_migrations` in
# `sturnus.entrypoints.worker`), by which point every `sturnus.*` logger
# already exists. With the default, the worker would fall silent for the
# rest of its life the moment it finished migrating -- and Sentry with it,
# because `LoggingIntegration` hooks `Logger.callHandlers`, which a disabled
# logger never reaches. See `tests/infrastructure/test_migrations.py`.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

import os  # noqa: E402

from sturnus.infrastructure.db.models import Base  # noqa: E402

target_metadata = Base.metadata


def _resolve_url() -> str:
    """URL from -x url=..., else from DATABASE_URL. asyncpg becomes psycopg."""
    supplied = context.get_x_argument(as_dictionary=True).get("url")
    url = supplied or config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("no database url: pass -x url=... or set DATABASE_URL")
    return url.replace("+asyncpg", "+psycopg")


config.set_main_option("sqlalchemy.url", _resolve_url())

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
