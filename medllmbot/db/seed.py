"""Demo data: a fictional dental clinic (default) or electronics shop (`--profile shop`).

- `python -m medllmbot.db.seed` fills the knowledge base only if it is empty, and adds fictional clients with
  their chats, leads and log entries (`demo_activity.py`) only if there are no clients yet: re-running is safe,
  and real conversations are never mixed with made-up ones.
- `python -m medllmbot.db.seed --reset` returns the demo server to its initial state after a showing: deletes
  clients, chat history, leads, orders and the audit log, restores the knowledge base, demo shop orders, the model
  price list, the bot settings and the fictional activity with fresh dates.
  Spending totals and the daily budget stay, otherwise a reset would zero out the day's spending.
- `--reset --profile shop` switches the demo to the shop: a different bot profile, catalog and greeting.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from medllmbot.config import AppSettings
from medllmbot.db.demo_activity import DEMO_CHATS, build_activity
from medllmbot.db.demo_shop import (
    SHOP_FAQ,
    SHOP_GREETING,
    SHOP_GREETING_RO,
    SHOP_ORDERS,
    SHOP_PRODUCTS,
    SHOP_SECTIONS,
)
from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.models import (
    DEFAULT_GREETING,
    DEFAULT_GREETING_RO,
    AuditEvent,
    BotConfig,
    BusinessProfile,
    ChatMessage,
    Client,
    FaqItem,
    InfoSection,
    Lead,
    ModelPrice,
    Order,
    OrderStatus,
    Service,
)
from medllmbot.text import Language

log = logging.getLogger(__name__)

DEMO_SECTIONS: list[tuple[str, str]] = [
    (
        "О клинике",
        "Demo Dental Clinic — вымышленная стоматология для демонстрации бота. "
        "Все данные придуманы и не относятся к реальной клинике.",
    ),
    (
        "Контакты",
        "Адрес: Кишинёв, ул. Примерная, 1 (демо-адрес).\nТелефон регистратуры: +373 22 000 000 (демо-номер).",
    ),
    (
        "Часы работы",
        "пн–пт 9:00–19:00, сб 9:00–14:00, вс — выходной.\nАдминистратор отвечает в Telegram в рабочее время.",
    ),
    (
        "Цены",
        "Цены указаны ориентировочно. Точную стоимость лечения врач называет после осмотра.",
    ),
]

DEMO_SERVICES: list[tuple[str, int, str | None]] = [
    ("Консультация стоматолога", 200, "Бесплатна при лечении в тот же день"),
    ("Профессиональная чистка зубов (ультразвук + Air Flow)", 800, "Около 60 минут"),
    ("Отбеливание зубов ZOOM", 3500, None),
    ("Лечение кариеса, светоотверждаемая пломба", 900, None),
    ("Лечение пульпита (один канал)", 1200, None),
    ("Удаление зуба простое", 500, None),
    ("Удаление зуба мудрости", 1500, None),
    ("Металлокерамическая коронка", 2800, None),
    ("Циркониевая коронка", 5000, None),
    ("Имплант (без коронки)", 9500, None),
    ("Детская консультация", 150, None),
    ("Прицельный рентгеновский снимок", 100, None),
]

DEMO_FAQ: list[tuple[str, str]] = [
    (
        "Как записаться?",
        "Оставьте имя и номер телефона в этом чате — администратор перезвонит и подберёт время.",
    ),
    ("Можно ли оплатить картой?", "Да, принимаем наличные и банковские карты."),
    ("Есть ли парковка?", "Да, бесплатная парковка у входа."),
    ("Лечите ли детей?", "Да, принимаем детей с 3 лет."),
    (
        "Больно ли лечить зубы?",
        "Все процедуры проводятся под местной анестезией, она входит в стоимость лечения.",
    ),
    (
        "Есть ли рассрочка?",
        "Да, на имплантацию и протезирование от 5 000 лей — уточняйте у администратора.",
    ),
    (
        "Что делать при острой боли?",
        "Позвоните в регистратуру — постараемся принять в тот же день.",
    ),
]


# Initial price list, the same one migration a6cc85d9a7c3 adds (a test checks they match).
# (model, from prompt length, input, cache write, cache read, output), USD per million tokens
DEMO_PRICES: list[tuple[str, int, float, float, float, float]] = [
    ("claude-haiku-5-5", 0, 0.10, 0.125, 0.01, 0.50),
    ("claude-haiku-5-5", 100_001, 0.50, 0.625, 0.05, 2.50),
    ("claude-haiku-4-5", 0, 1.00, 1.25, 0.10, 5.00),
    ("claude-sonnet-5-5", 0, 2.00, 2.50, 0.10, 10.00),
    ("claude-sonnet-5", 0, 2.00, 2.50, 0.20, 10.00),
    ("claude-sonnet-4-6", 0, 3.00, 3.75, 0.30, 15.00),
    ("claude-opus-5-5", 0, 4.00, 5.00, 0.20, 20.00),
    ("claude-opus-5", 0, 5.00, 6.25, 0.50, 25.00),
    ("claude-fable-5-2", 0, 8.00, 10.00, 0.20, 40.00),
    ("claude-fable-5-1", 0, 10.00, 12.50, 0.25, 50.00),
]


@dataclass(frozen=True)
class DemoSet:
    """Demo knowledge base and greetings for one business profile."""

    sections: list[tuple[str, str]]
    catalog: list[tuple[str, int, str | None]]
    faq: list[tuple[str, str]]
    greeting: str
    greeting_ro: str
    # Shop orders: (number, name, phone, status, items, total, delivery date as days from today)
    orders: list[tuple[str, str, str, OrderStatus, str, int, int | None]] = field(
        default_factory=list[tuple[str, str, str, OrderStatus, str, int, int | None]]
    )


DEMOS: dict[BusinessProfile, DemoSet] = {
    BusinessProfile.CLINIC: DemoSet(DEMO_SECTIONS, DEMO_SERVICES, DEMO_FAQ, DEFAULT_GREETING, DEFAULT_GREETING_RO),
    BusinessProfile.SHOP: DemoSet(SHOP_SECTIONS, SHOP_PRODUCTS, SHOP_FAQ, SHOP_GREETING, SHOP_GREETING_RO, SHOP_ORDERS),
}


def _add_knowledge(session: AsyncSession, demo: DemoSet) -> None:
    session.add_all(
        InfoSection(title=title, content=content, sort_order=i) for i, (title, content) in enumerate(demo.sections)
    )
    session.add_all(
        Service(name=name, price=price, description=description, sort_order=i)
        for i, (name, price, description) in enumerate(demo.catalog)
    )
    session.add_all(FaqItem(question=question, answer=answer, sort_order=i) for i, (question, answer) in enumerate(demo.faq))
    today = datetime.now(UTC).date()
    session.add_all(
        Order(
            number=number,
            customer_name=name,
            phone=phone,
            status=status,
            items=items,
            total=total,
            delivery_date=today + timedelta(days=days) if days is not None else None,
        )
        for number, name, phone, status, items, total, days in demo.orders
    )


async def seed_demo(sessions: async_sessionmaker[AsyncSession], kind: BusinessProfile = BusinessProfile.CLINIC) -> bool:
    """Fills an empty knowledge base with the profile's demo data. Returns True if anything was added."""
    demo = DEMOS[kind]
    async with sessions.begin() as session:
        knowledge_models = (InfoSection, Service, FaqItem, Order)
        counts = [await session.scalar(select(func.count()).select_from(model)) for model in knowledge_models]
        if any(counts):
            return False
        _add_knowledge(session, demo)
        # The profile matches the loaded data. Greetings change only together with the profile:
        # the admin's own greetings survive re-seeding the same demo base
        config = await session.get(BotConfig, 1)
        if config is None:
            session.add(BotConfig(id=1, profile=kind, greeting=demo.greeting, greeting_ro=demo.greeting_ro))
        elif config.profile != kind:
            config.profile = kind
            config.greeting, config.greeting_ro = demo.greeting, demo.greeting_ro
    return True


async def current_profile(sessions: async_sessionmaker[AsyncSession]) -> BusinessProfile | None:
    async with sessions() as session:
        config = await session.get(BotConfig, 1)
        return config.profile if config else None


async def reset_demo(sessions: async_sessionmaker[AsyncSession], kind: BusinessProfile = BusinessProfile.CLINIC) -> None:
    """Returns the profile demo to its initial state in one transaction. Leaves spending and the daily budget."""
    demo = DEMOS[kind]
    async with sessions.begin() as session:
        config = await session.get(BotConfig, 1)
        budget = config.daily_budget_usd if config else 0
        for model in (Lead, ChatMessage, AuditEvent, Client, InfoSection, Service, FaqItem, Order, ModelPrice, BotConfig):
            await session.execute(delete(model))
        _add_knowledge(session, demo)
        session.add_all(
            ModelPrice(
                model=model,
                from_prompt_tokens=from_tokens,
                input_per_mtok=input_price,
                cache_write_per_mtok=cache_write,
                cache_read_per_mtok=cache_read,
                output_per_mtok=output_price,
            )
            for model, from_tokens, input_price, cache_write, cache_read, output_price in DEMO_PRICES
        )
        session.add(
            BotConfig(id=1, profile=kind, greeting=demo.greeting, greeting_ro=demo.greeting_ro, daily_budget_usd=budget)
        )


async def seed_activity(sessions: async_sessionmaker[AsyncSession], now: datetime | None = None) -> bool:
    """Adds the fictional clients, chats, leads and log entries of the current profile. Returns True if added.

    Only into a database without clients: a demo where someone already wrote to the bot keeps its real
    conversations. Dates count back from `now` (naive UTC), so «Analytics» shows them in its periods.
    """
    async with sessions.begin() as session:
        if await session.scalar(select(func.count()).select_from(Client)):
            return False
        config = await session.get(BotConfig, 1)
        kind = config.profile if config else BusinessProfile.CLINIC
        demo = DEMOS[kind]
        greeting = config.greeting if config else demo.greeting
        greeting_ro = (config.greeting_ro if config else demo.greeting_ro) or greeting
        moment = now or datetime.now(UTC).replace(tzinfo=None)
        activity = build_activity(DEMO_CHATS[kind], kind, {Language.RU: greeting, Language.RO: greeting_ro}, moment)
        session.add_all(activity.clients)
        await session.flush()
        session.add_all(activity.records)
    return True


# --profile flag value → profile
PROFILE_OPTIONS: dict[str, BusinessProfile] = {kind.name.lower(): kind for kind in BusinessProfile}


async def main(reset: bool, kind: BusinessProfile) -> None:
    engine = create_engine(AppSettings().db_path)
    try:
        sessions = create_sessionmaker(engine)
        if reset:
            await reset_demo(sessions, kind)
            log.info("Demo reset (%s): knowledge base, prices and settings as after install; clients and log deleted", kind)
        elif await seed_demo(sessions, kind):
            log.info("Demo data added (%s)", kind)
        else:
            log.info("Knowledge base is not empty, demo data not added")
            current = await current_profile(sessions)
            if current is not None and current != kind:
                log.warning(
                    "Profile not changed (current: %s). To switch the demo: --reset --profile %s",
                    current.name.lower(),  # the --profile option value, not the Russian display name
                    kind.name.lower(),
                )
        if await seed_activity(sessions):
            log.info("Demo activity added: fictional clients, chats, leads and log entries")
        else:
            log.info("The database already has clients, demo activity not added")
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> tuple[bool, BusinessProfile]:
    parser = argparse.ArgumentParser(description="Demo data: a fictional dental clinic or electronics shop")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete clients, chat history, leads, orders and the log; reset knowledge base, prices, settings and "
        "the fictional clients to demo",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_OPTIONS),
        default=BusinessProfile.CLINIC.name.lower(),
        help="which business to show: clinic is the dental clinic, shop is the electronics shop",
    )
    args = parser.parse_args(argv)
    reset: bool = args.reset  # pyright: ignore[reportAny]  # argparse.Namespace is untyped
    profile: str = args.profile  # pyright: ignore[reportAny]  # argparse.Namespace is untyped
    return reset, PROFILE_OPTIONS[profile]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(*parse_args()))
