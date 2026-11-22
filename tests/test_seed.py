"""Demo data: filling an empty database and resetting the demo server after a demo."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, update

from medllmbot.db.models import (
    DEFAULT_GREETING,
    AuditEvent,
    AuditOutcome,
    Base,
    BotConfig,
    ChatMessage,
    Client,
    Lead,
    ModelPrice,
    Service,
    UsageDaily,
)
from medllmbot.db.repository import Repository
from medllmbot.db.seed import DEMO_PRICES, DEMO_SERVICES, reset_demo
from tests.conftest import CHAT_ID, Sessions

MIGRATIONS = Path(__file__).parents[1] / "medllmbot" / "db" / "migrations" / "versions"


def test_demo_prices_match_migration() -> None:
    # Reset restores the same price list that installation sets
    spec = importlib.util.spec_from_file_location("llm_costs", MIGRATIONS / "a6cc85d9a7c3_llm_costs.py")
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    prices: object = migration.PRICES_2026_11_10
    assert prices == DEMO_PRICES


async def count(sessions: Sessions, model: type[Base]) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_reset_returns_demo_to_initial_state(seeded_repo: Repository, sessions: Sessions) -> None:
    # What a demo could leave behind: messages, a lead, audit log, edits to the knowledge base, prices and settings
    await seeded_repo.save_pair(CHAT_ID, "Сколько стоит чистка?", "800 лей")
    await seeded_repo.create_lead(CHAT_ID, "@ivan", "069123456", "Запишите меня")
    await seeded_repo.log_event(AuditEvent.incoming(CHAT_ID, "Привет", AuditOutcome.ANSWERED))
    async with sessions.begin() as session:
        await session.execute(update(Service).values(price=1))
        await session.execute(update(BotConfig).values(greeting="Изменено заказчиком", daily_budget_usd=3))
        session.add(UsageDaily(day=datetime.now(UTC).date(), model="claude-haiku-5-5", requests=1, cost_micro_usd=500))

    await reset_demo(sessions)

    for model in (Client, ChatMessage, Lead, AuditEvent):
        assert await count(sessions, model) == 0, model.__name__
    knowledge = await seeded_repo.load_knowledge()
    assert [(s.name, s.price) for s in knowledge.services] == [(name, price) for name, price, _ in DEMO_SERVICES]
    assert await count(sessions, ModelPrice) == len(DEMO_PRICES)
    config = await seeded_repo.get_config()
    assert config.greeting == DEFAULT_GREETING
    # Spending and the limit remain: otherwise a reset would zero out the day's spending
    assert config.daily_budget_usd == 3
    assert await seeded_repo.spent_today_micro_usd() == 500


async def test_reset_works_on_empty_database(repo: Repository, sessions: Sessions) -> None:
    await reset_demo(sessions)
    assert len((await repo.load_knowledge()).services) == len(DEMO_SERVICES)
    assert (await repo.get_config()).daily_budget_usd == 0
