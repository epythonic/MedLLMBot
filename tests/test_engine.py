"""SQLite connection settings."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from medllmbot.db.models import Service
from tests.conftest import Sessions


async def test_pragmas(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
        assert await conn.scalar(text("PRAGMA busy_timeout")) == 5000
        # Without it, INSERT OR REPLACE breaks the catalog full-text index (test_catalog.py)
        assert await conn.scalar(text("PRAGMA recursive_triggers")) == 1


async def test_lower_understands_cyrillic(engine: AsyncEngine) -> None:
    # SQLite's built-in lower() changes case only for Latin letters
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.lower("ЧИСТКА Зубов, ЁЖ и AIR"))) == "чистка зубов, ёж и air"
        assert await conn.scalar(select(func.lower(None))) is None


@pytest.mark.usefixtures("seeded_repo")  # demo services
async def test_ilike_search_ignores_cyrillic_case(sessions: Sessions) -> None:
    # This is how SQLAdmin searches: cast(column, String).ilike('%query%')
    for query in ("чистка", "Чистка", "ЧИСТКА"):
        async with sessions() as s:
            names = (await s.scalars(select(Service.name).where(Service.name.ilike(f"%{query}%")))).all()
        assert names == ["Профессиональная чистка зубов (ультразвук + Air Flow)"], query
