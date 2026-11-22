"""SQLite connection via SQLAlchemy (async, aiosqlite driver)."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry


def database_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


def _sqlite_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    # SQLite does not enforce foreign keys by default
    cursor.execute("PRAGMA foreign_keys = ON")
    # WAL lets the bot write while the admin reads (two processes, one DB)
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA busy_timeout = 5000")
    # INSERT OR REPLACE deletes the old row; without this flag the delete trigger does not fire
    # and the catalog full-text index (db/fts.py) drifts from the table
    cursor.execute("PRAGMA recursive_triggers = ON")
    cursor.close()


@runtime_checkable
class _SupportsFunctions(Protocol):
    def create_function(
        self, name: str, num_params: int, func: Callable[[str | None], str | None], *, deterministic: bool = False
    ) -> None: ...


def _unicode_lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _unicode_functions(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    # SQLite's built-in lower() only lowercases Latin letters, but admin search
    # (ILIKE → lower(a) LIKE lower(b)) must find «Чистка» for the query «чистка».
    # A custom function with the same name replaces the built-in one for this connection.
    if isinstance(dbapi_connection, _SupportsFunctions):
        dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)


def create_engine(db_path: Path) -> AsyncEngine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(database_url(db_path))
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    event.listen(engine.sync_engine, "connect", _unicode_functions)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
