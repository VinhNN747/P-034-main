import asyncio
import selectors
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

import src.shared.models  # noqa: F401
from alembic import context
from src.shared.config import get_settings
from src.shared.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    configured_url = get_settings().database_url
    if configured_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + configured_url.removeprefix("postgresql://")
    if configured_url.startswith("postgres://"):
        return "postgresql+psycopg://" + configured_url.removeprefix("postgres://")
    if configured_url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + configured_url.removeprefix("sqlite:///")
    return configured_url


def ensure_sqlite_parent(url: str) -> None:
    parsed_url = make_url(url)
    if parsed_url.get_backend_name() == "sqlite" and parsed_url.database:
        Path(parsed_url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = database_url()
    ensure_sqlite_parent(url)
    connectable = async_engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        do_run_migrations(supplied_connection)
        return
    if sys.platform == "win32":
        # Psycopg async requires add_reader/add_writer, which are unavailable
        # on Windows' default ProactorEventLoop.
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_async_migrations())
        finally:
            loop.close()
            asyncio.set_event_loop(None)
        return

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
