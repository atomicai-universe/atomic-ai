"""Async database engine and session factory.

Builds the SQLAlchemy 2.0 async engine from ``DATABASE_URL`` (a
:class:`pydantic.SecretStr`, read via ``.get_secret_value()`` so the DSN never
appears in logs) and exposes an :class:`async_sessionmaker` session factory.

The declarative :class:`Base` lives here so both the models module and the
engine share a single :class:`~sqlalchemy.orm.registry`/``MetaData``.

Requirements: 20.1 (models/metadata), 20.3 (referential integrity via the ORM
mapping), 18.4 (never emit the secret DSN).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model and the engine's metadata."""


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, created on first use.

    The DSN is pulled from settings and unwrapped with ``get_secret_value()``
    only at engine-construction time; it is never logged.
    """
    settings = get_settings()
    dsn = settings.DATABASE_URL.get_secret_value()
    return create_async_engine(
        dsn,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory, created on first use."""
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session that commits/rolls back cleanly.

    Commits on success, rolls back on any exception, and always closes the
    session::

        async with session_scope() as session:
            session.add(obj)
    """
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped :class:`AsyncSession`.

    Rolls back on error and always closes the session. Commit is left to the
    caller/route so read-only handlers incur no write.
    """
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def loop_local_session_scope() -> AsyncIterator[AsyncSession]:
    """A session backed by a THROWAWAY NullPool engine, created + disposed here.

    Why this exists: some code runs async DB work from a SHORT-LIVED event loop
    created per call (e.g. the Strands agent's ``before_tool_call`` approval hook
    is driven by ``strands_engine._run_async``, which spins up a fresh
    ``asyncio`` loop, runs the coroutine, then closes the loop). The process-wide
    pooled engine (:func:`get_engine`) keeps connections bound to whatever loop
    first opened them; reusing one from a different, ephemeral loop makes
    asyncpg raise ``RuntimeError: got Future ... attached to a different loop``
    when the loop tears the connection down — which crashed the ``gmail_reply``
    approval path so no approval was ever created.

    This context manager builds a dedicated engine with ``NullPool`` (no pooled
    connections to leak across loops), yields a committing session, and disposes
    the engine in ``finally`` so the connection is fully closed on THIS loop.
    Slightly more overhead per call than the pooled engine, but correct and
    loop-safe — the right trade-off for the low-frequency approval-gate path.
    """
    settings = get_settings()
    dsn = settings.DATABASE_URL.get_secret_value()
    engine = create_async_engine(dsn, echo=False, future=True, poolclass=NullPool)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
        await engine.dispose()
