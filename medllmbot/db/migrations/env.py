"""Окружение Alembic: async-движок SQLite, batch-режим для ALTER TABLE."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from medllmbot.config import AppSettings
from medllmbot.db.engine import database_url
from medllmbot.db.fts import include_name
from medllmbot.db.models import Base

config = context.config

# Логирование из alembic.ini — только при запуске из CLI, чтобы не ломать логи бота
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return config.get_main_option("sqlalchemy.url") or database_url(AppSettings().db_path)


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite не умеет большинство ALTER TABLE
        include_name=include_name,  # индекс FTS создаётся вручную, autogenerate его не трогает
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        include_name=include_name,  # индекс FTS создаётся вручную, autogenerate его не трогает
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
