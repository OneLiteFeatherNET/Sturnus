from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """One container for the whole test run — starting it up costs seconds."""
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture
def clean_database(postgres_url: str) -> str:
    """Fully resets the schema before every test.

    Necessary because all tests share one container, and tables can come
    from two paths: via Alembic (with `alembic_version`) and via
    `create_all` (without). A plain `drop_all` would leave Alembic's
    bookkeeping in place, causing a later `upgrade head` to fail on tables
    that already exist. Dropping the schema handles both cases.
    """
    engine = create_engine(postgres_url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return postgres_url
