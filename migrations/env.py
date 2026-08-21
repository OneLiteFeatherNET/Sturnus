from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

import os  # noqa: E402

from sturnus.infrastructure.db.models import Base  # noqa: E402
from sturnus.observability.setup import logging_is_configured  # noqa: E402

# Interpret the config file for Python logging -- but only when nothing else
# owns logging yet.
#
# `fileConfig` is not additive. It *replaces* `root.handlers` with
# alembic.ini's bare stderr `StreamHandler` and, with its default
# `disable_existing_loggers=True`, sets `disabled = True` on every logger the
# ini does not name -- which is all of `sturnus.*`.
#
# `sturnus.entrypoints.worker` calls `configure_logging("worker")` in
# `main()` and then `_run_migrations` a few lines into `_run()`. Without this
# guard, the worker's single filtered JSON handler is torn out seconds after
# startup and every later line goes to stderr unstructured and, more
# importantly, unfiltered by `SturnusFilter` -- or is dropped entirely
# because its logger was disabled. That is the "one handler, one exit"
# guarantee in `sturnus.observability.setup` silently undone by a call in
# another file.
#
# Running Alembic standalone (`alembic upgrade head` from a shell) still gets
# the ini's logging, because nothing has configured logging in that process --
# and that is the case `disable_existing_loggers=False` is for. The guard and
# the argument fix two different halves of the same regression and neither
# replaces the other:
#
# - The guard stops `root.handlers` being *replaced*, which the argument does
#   nothing about. That is the half that matters once `configure_logging()`
#   has installed the single filtered JSON handler.
# - `disable_existing_loggers` defaults to True, which sets `disabled = True`
#   on every logger not named in `alembic.ini` -- and `Logger.handle` returns
#   immediately for a disabled logger, so even Sentry stops seeing them
#   (`LoggingIntegration` hooks `Logger.callHandlers`, which is downstream).
#   That is the half that still bites on the standalone path the guard lets
#   through: `alembic upgrade head` imports `sturnus.*` for `Base.metadata`
#   above, so those loggers already exist by the time this line runs.
#
# Drop either one and a process goes quiet: without the guard the worker
# loses its filter seconds after startup, without the argument standalone
# Alembic disables every logger it did not name. See
# `tests/infrastructure/test_migrations.py` and
# `tests/test_logging_discipline.py`.
if config.config_file_name is not None and not logging_is_configured():
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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
