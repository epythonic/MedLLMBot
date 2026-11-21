"""Настройки соединения с SQLite."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from medllmbot.db.models import Service
from tests.conftest import Sessions


async def test_pragmas(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
        assert await conn.scalar(text("PRAGMA busy_timeout")) == 5000


async def test_lower_understands_cyrillic(engine: AsyncEngine) -> None:
    # Встроенный lower() в SQLite меняет регистр только у латиницы
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.lower("ЧИСТКА Зубов, ЁЖ и AIR"))) == "чистка зубов, ёж и air"
        assert await conn.scalar(select(func.lower(None))) is None


@pytest.mark.usefixtures("seeded_repo")  # демо-услуги
async def test_ilike_search_ignores_cyrillic_case(sessions: Sessions) -> None:
    # Так ищет SQLAdmin: cast(колонка, String).ilike('%запрос%')
    for query in ("чистка", "Чистка", "ЧИСТКА"):
        async with sessions() as s:
            names = (await s.scalars(select(Service.name).where(Service.name.ilike(f"%{query}%")))).all()
        assert names == ["Профессиональная чистка зубов (ультразвук + Air Flow)"], query
