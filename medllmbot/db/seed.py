"""Демо-данные: вымышленная стоматология (по умолчанию) или магазин электроники (`--profile shop`).

- `python -m medllmbot.db.seed` заполняет базу знаний, только если она пустая: повторный запуск безопасен.
- `python -m medllmbot.db.seed --reset` возвращает демо-сервер в исходное состояние после показа: удаляет
  клиентов, переписку, заявки, заказы и журнал, восстанавливает базу знаний, демо-заказы магазина, прайс-лист
  моделей и настройки бота.
  Итоги расходов и дневной лимит остаются — иначе сброс обнулял бы потраченное за сутки.
- `--reset --profile shop` переключает демо на магазин: другой профиль бота, каталог и приветствие.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from medllmbot.config import AppSettings
from medllmbot.db.demo_shop import SHOP_FAQ, SHOP_GREETING, SHOP_ORDERS, SHOP_PRODUCTS, SHOP_SECTIONS
from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.models import (
    DEFAULT_GREETING,
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


# Стартовый прайс-лист — тот же, что добавляет миграция a6cc85d9a7c3 (совпадение проверяет тест).
# (модель, от длины промпта, вход, запись в кэш, чтение из кэша, выход), USD за миллион токенов
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
    """База знаний и приветствие демо для одного профиля бизнеса."""

    sections: list[tuple[str, str]]
    catalog: list[tuple[str, int, str | None]]
    faq: list[tuple[str, str]]
    greeting: str
    # Заказы магазина: (номер, имя, телефон, статус, состав, сумма, дата доставки — дней от сегодня)
    orders: list[tuple[str, str, str, OrderStatus, str, int, int | None]] = field(
        default_factory=list[tuple[str, str, str, OrderStatus, str, int, int | None]]
    )


DEMOS: dict[BusinessProfile, DemoSet] = {
    BusinessProfile.CLINIC: DemoSet(DEMO_SECTIONS, DEMO_SERVICES, DEMO_FAQ, DEFAULT_GREETING),
    BusinessProfile.SHOP: DemoSet(SHOP_SECTIONS, SHOP_PRODUCTS, SHOP_FAQ, SHOP_GREETING, SHOP_ORDERS),
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
    """Заполняет пустую базу знаний демо-данными профиля. Возвращает True, если что-то добавлено."""
    demo = DEMOS[kind]
    async with sessions.begin() as session:
        knowledge_models = (InfoSection, Service, FaqItem, Order)
        counts = [await session.scalar(select(func.count()).select_from(model)) for model in knowledge_models]
        if any(counts):
            return False
        _add_knowledge(session, demo)
        # Профиль — под залитые данные. Приветствие меняется только вместе с профилем:
        # своё приветствие администратора при повторном заполнении той же демо-базы остаётся
        config = await session.get(BotConfig, 1)
        if config is None:
            session.add(BotConfig(id=1, profile=kind, greeting=demo.greeting))
        elif config.profile != kind:
            config.profile = kind
            config.greeting = demo.greeting
    return True


async def current_profile(sessions: async_sessionmaker[AsyncSession]) -> BusinessProfile | None:
    async with sessions() as session:
        config = await session.get(BotConfig, 1)
        return config.profile if config else None


async def reset_demo(sessions: async_sessionmaker[AsyncSession], kind: BusinessProfile = BusinessProfile.CLINIC) -> None:
    """Возвращает демо профиля в исходное состояние одной транзакцией. Расход и дневной лимит не трогает."""
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
        session.add(BotConfig(id=1, profile=kind, greeting=demo.greeting, daily_budget_usd=budget))


# Значение флага --profile → профиль
PROFILE_OPTIONS: dict[str, BusinessProfile] = {kind.name.lower(): kind for kind in BusinessProfile}


async def main(reset: bool, kind: BusinessProfile) -> None:
    engine = create_engine(AppSettings().db_path)
    try:
        sessions = create_sessionmaker(engine)
        if reset:
            await reset_demo(sessions, kind)
            log.info(
                "Демо сброшено (%s): база знаний, цены и настройки — как после установки, клиенты и журнал удалены", kind
            )
        elif await seed_demo(sessions, kind):
            log.info("Демо-данные добавлены (%s)", kind)
        else:
            log.info("База знаний не пустая — демо-данные не добавлялись")
            current = await current_profile(sessions)
            if current is not None and current != kind:
                log.warning(
                    "Профиль не изменён (сейчас: %s). Чтобы переключить демо: --reset --profile %s",
                    current,
                    kind.name.lower(),
                )
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> tuple[bool, BusinessProfile]:
    parser = argparse.ArgumentParser(description="Демо-данные: вымышленная стоматология или магазин электроники")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="удалить клиентов, переписку, заявки, заказы и журнал и вернуть базу знаний, цены и настройки к демо",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_OPTIONS),
        default=BusinessProfile.CLINIC.name.lower(),
        help="какой бизнес показывать: clinic — стоматология, shop — магазин электроники",
    )
    args = parser.parse_args(argv)
    reset: bool = args.reset  # pyright: ignore[reportAny]  # argparse.Namespace не типизирован
    profile: str = args.profile  # pyright: ignore[reportAny]  # argparse.Namespace не типизирован
    return reset, PROFILE_OPTIONS[profile]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(*parse_args()))
