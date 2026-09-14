"""Alembic migration environment for the Atomic AI backend.

Reads the database DSN at runtime from the application settings
(``app.config.get_settings().DATABASE_URL``) rather than from ``alembic.ini`` so
the secret DSN never lives in a committed file or a log (Req 18.4). The DSN is
an asyncpg URL (``postgresql+asyncpg://...``), so migrations run through the
SQLAlchemy async engine.

``target_metadata`` is the shared :class:`app.db.session.Base` metadata; the
models module is imported for its side effect of registering every table and
native ``ENUM`` type on that metadata so autogenerate and ``create_all`` see the
full schema (Req 20.1, 20.2).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import the shared metadata and register every model/table on it. The models
# import is required for its side effects even though the symbol is unused.
from app.config import get_settings
from app.db.session import Base
import app.db.models  # noqa: F401  (registers tables + enums on Base.metadata)

# Alembic Config object providing access to values in alembic.ini.
config = context.config

# Configure Python logging from the ini file, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata used as the autogenerate/target reference for the schema.
target_metadata = Base.metadata


def _database_url() -> str:
    """Return the asyncpg DSN, unwrapped from settings only at call time.

    The value is never logged; it is handed directly to the engine config.
    """
    return get_settings().DATABASE_URL.get_secret_value()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL against the URL literal.

    Uses the same asyncpg DSN string; offline mode only renders SQL and does not
    open a DBAPI connection, so the async driver in the URL is inconsequential.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Configure the context on a live (sync-facing) connection and migrate."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an async engine from settings and run migrations within it."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        future=True,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
