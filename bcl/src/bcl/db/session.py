"""Async SQLAlchemy engine and session factory for SQLite + aiosqlite.

SQLAlchemy 2.0 async style. AsyncSession everywhere.
PRAGMAs are applied on every connection via an `event.listens_for`
hook — SQLite re-applies pragmas per-connection, not globally.

There is exactly one engine per process. Sessions are short-lived,
created per-request via `get_session()` dependency.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from bcl.config import Settings, get_settings


def _make_engine(settings: Settings) -> AsyncEngine:
    """Build the engine and register the SQLite PRAGMA hook."""
    # Ensure the directory exists before SQLite tries to open the file
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        settings.database_url,
        echo=settings.environment == "dev" and settings.log_level == "DEBUG",
        # SQLite-specific connect args via the underlying aiosqlite connection
        connect_args={"check_same_thread": False, "timeout": 30.0},
        future=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        """Apply pragmas on every new physical connection.

        SQLite's pragmas are connection-scoped, so we reapply on every connect.
        """
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"PRAGMA journal_mode={settings.sqlite_journal_mode}")
            cursor.execute(f"PRAGMA synchronous={settings.sqlite_synchronous}")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
            if settings.sqlite_foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


# Process-singleton engine. Built lazily.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings())
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, commits on clean exit, rolls back on error.

    Usage:
        @app.get("/topologies")
        async def list_topologies(session: Annotated[AsyncSession, Depends(get_session)]):
            ...
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            # Caller is responsible for committing — we do NOT auto-commit reads.
            # State-changing endpoints commit explicitly inside their handlers.
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the engine and all its connections. Called on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
