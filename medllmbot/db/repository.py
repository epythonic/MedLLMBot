"""Все запросы бота к БД. Каждый метод открывает свою короткую сессию."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from anthropic.types import AssistantMessageParam, MessageParam, UserMessageParam
from sqlalchemy import ColumnClause, ColumnElement, String, column, delete, func, literal_column, select, table, type_coerce
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from medllmbot.costs import Price, PriceBook, TokenUsage, UsageRow
from medllmbot.db import fts
from medllmbot.db.models import (
    AuditEvent,
    AuditOutcome,
    BotConfig,
    ChatMessage,
    Client,
    FaqItem,
    InfoSection,
    Lead,
    LeadStatus,
    ModelPrice,
    Order,
    Service,
    UsageDaily,
)


@dataclass(frozen=True)
class NewRecords:
    """Сколько записей появилось после `after_id` — для плашки «новые записи» в админке."""

    last_id: int  # id последней записи (0 — записей нет): точка отсчёта для следующих проверок
    count: int
    suspicious: int = 0  # только для журнала: сколько из новых записей подозрительные


@dataclass(frozen=True)
class Knowledge:
    """Снимок базы знаний клиники: только активные записи в порядке sort_order."""

    sections: list[InfoSection]
    services: list[Service]
    faq: list[FaqItem]
    # Каталог больше порога и в `services` не загружен: модель ищет в нём инструментом search_catalog
    catalog_search: bool = False


@dataclass(frozen=True)
class CatalogPage:
    """Результат поиска по каталогу: первые позиции и сколько подошло всего."""

    services: list[Service]
    total: int


@dataclass(frozen=True)
class QuestionRow:
    """Сообщение клиента из журнала — для аналитики вопросов (`medllmbot/analytics.py`)."""

    event_id: int
    chat_id: int
    created_at: datetime  # UTC
    text: str
    outcome: AuditOutcome
    answer: str | None  # что получил клиент
    tool_calls: str | None


@dataclass(frozen=True)
class LeadRow:
    """Когда клиент оставил телефон — для доли диалогов с заявкой."""

    chat_id: int
    created_at: datetime  # UTC


_FTS = table(fts.FTS_TABLE, column("rowid"))


async def _active[M: (InfoSection, Service, FaqItem)](session: AsyncSession, model: type[M]) -> list[M]:
    """Активные записи раздела базы знаний в порядке вывода."""
    stmt = select(model).where(model.is_active.is_(True)).order_by(model.sort_order, model.id)
    return list((await session.scalars(stmt)).all())


def _to_message(role: str, content: str) -> MessageParam:
    """Строка истории из БД -> сообщение в формате Anthropic API."""
    if role == "user":
        return UserMessageParam(role="user", content=content)
    return AssistantMessageParam(role="assistant", content=content)


def _token_usage(event: AuditEvent) -> TokenUsage | None:
    """Токены из события; None, если запроса к модели не было."""
    if event.input_tokens is None or event.output_tokens is None:
        return None
    return TokenUsage(
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        cache_read_tokens=event.cache_read_tokens or 0,
        cache_write_tokens=event.cache_write_tokens or 0,
    )


def _to_price(row: ModelPrice) -> Price:
    return Price(
        model=row.model,
        from_prompt_tokens=row.from_prompt_tokens,
        input_per_mtok=row.input_per_mtok,
        output_per_mtok=row.output_per_mtok,
        cache_write_per_mtok=row.cache_write_per_mtok,
        cache_read_per_mtok=row.cache_read_per_mtok,
    )


async def _add_daily_usage(session: AsyncSession, model: str, usage: TokenUsage, cost_micro_usd: int | None) -> None:
    """Прибавляет запрос к итогам за сегодняшний день (UTC) атомарным upsert."""
    stmt = insert(UsageDaily).values(
        day=datetime.now(UTC).date(),
        model=model,
        requests=1,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_micro_usd=cost_micro_usd or 0,
        unpriced_requests=0 if cost_micro_usd is not None else 1,
    )
    counters = (
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_micro_usd",
        "unpriced_requests",
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageDaily.day, UsageDaily.model],
        set_={name: getattr(UsageDaily, name) + stmt.excluded[name] for name in counters},
    )
    await session.execute(stmt)


class Repository:
    sessions: async_sessionmaker[AsyncSession]

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    # ---------- Клиенты ----------

    async def touch_client(self, chat_id: int, username: str | None, full_name: str) -> bool:
        """Создаёт или обновляет клиента. Возвращает True, если клиент заблокирован."""
        stmt = (
            insert(Client)
            .values(chat_id=chat_id, username=username, full_name=full_name)
            .on_conflict_do_update(
                index_elements=[Client.chat_id],
                set_={"username": username, "full_name": full_name, "last_seen_at": func.now()},
            )
            .returning(Client.is_blocked)
        )
        async with self.sessions.begin() as session:
            blocked: bool = (await session.execute(stmt)).scalar_one()
        return blocked

    async def get_client(self, chat_id: int) -> Client | None:
        async with self.sessions() as session:
            return await session.get(Client, chat_id)

    async def set_client_blocked(self, chat_id: int, blocked: bool) -> Client | None:
        """Блокировка из админки. None — такого клиента нет."""
        async with self.sessions.begin() as session:
            client = await session.get(Client, chat_id)
            if client is not None:
                client.is_blocked = blocked
        return client

    # ---------- История диалога ----------

    async def load_history(self, chat_id: int, limit: int) -> list[MessageParam]:
        """Последние сообщения диалога в формате для LLM, старые — первыми."""
        stmt = (
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        async with self.sessions() as session:
            rows = (await session.execute(stmt)).all()
        history = [_to_message(role, content) for role, content in reversed(rows)]
        # При нечётном лимите срез может начаться с ответа бота, а API ждёт первым сообщение клиента
        if history and history[0]["role"] != "user":
            history = history[1:]
        return history

    async def save_pair(self, chat_id: int, question: str, answer: str) -> None:
        """Сохраняет вопрос и ответ вместе, чтобы история всегда начиналась с клиента."""
        async with self.sessions.begin() as session:
            session.add_all(
                [
                    ChatMessage(chat_id=chat_id, role="user", content=question),
                    ChatMessage(chat_id=chat_id, role="assistant", content=answer),
                ]
            )

    # ---------- Заявки ----------

    async def create_lead(self, chat_id: int, client_name: str, phone: str, message: str) -> Lead:
        lead = Lead(chat_id=chat_id, client_name=client_name, phone=phone, message=message)
        async with self.sessions.begin() as session:
            session.add(lead)
        return lead

    async def set_lead_status(self, lead_id: int, status: LeadStatus) -> Lead | None:
        """Смена статуса из админки. None — заявки нет (например, её только что удалили)."""
        async with self.sessions.begin() as session:
            lead = await session.get(Lead, lead_id)
            if lead is not None:
                lead.status = status
        return lead

    async def new_leads(self, after_id: int) -> NewRecords:
        stmt = select(func.coalesce(func.max(Lead.id), 0), func.count().filter(Lead.id > after_id))
        async with self.sessions() as session:
            last_id, count = (await session.execute(stmt)).one()
        return NewRecords(last_id, count)

    # ---------- Заказы магазина ----------

    async def find_order(self, key: str) -> Order | None:
        """Заказ по ключу номера — только буквы и цифры: «DE10023» находит «DE-10023» (orders.order_key)."""
        stored_key = func.replace(func.upper(Order.number), "-", "")
        async with self.sessions() as session:
            return await session.scalar(select(Order).where(stored_key == key.upper()).limit(1))

    # ---------- Журнал аудита ----------

    async def log_event(self, event: AuditEvent, requests: Sequence[TokenUsage] = ()) -> None:
        """Пишет событие. Если был запрос к модели — считает его стоимость и добавляет
        к дневным итогам расхода в той же транзакции.

        `requests` — токены каждого запроса, если ответ занял несколько (поиск по каталогу): тариф по длине
        промпта выбирается для каждого запроса отдельно. Без них стоимость считается по токенам события.
        """
        async with self.sessions.begin() as session:
            usage = _token_usage(event)
            if usage is not None and event.model is not None:
                prices = PriceBook(_to_price(row) for row in await session.scalars(select(ModelPrice)))
                if requests:
                    event.cost_micro_usd = prices.total_micro_usd(event.model, requests)
                else:
                    event.cost_micro_usd = prices.cost_micro_usd(event.model, usage)
                await _add_daily_usage(session, event.model, usage, event.cost_micro_usd)
            session.add(event)

    async def spent_today_micro_usd(self) -> int:
        """Расход на Claude за текущие сутки (UTC) по всем моделям — для дневного лимита."""
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(UsageDaily.cost_micro_usd), 0)).where(
                    UsageDaily.day == datetime.now(UTC).date()
                )
            )
        return int(total or 0)

    async def new_audit_events(self, after_id: int) -> NewRecords:
        newer = AuditEvent.id > after_id
        stmt = select(
            func.coalesce(func.max(AuditEvent.id), 0),
            func.count().filter(newer),
            func.count().filter(newer, AuditEvent.is_suspicious.is_(True)),
        )
        async with self.sessions() as session:
            last_id, count, suspicious = (await session.execute(stmt)).one()
        return NewRecords(last_id, count, suspicious)

    async def purge_audit(self, older_than_days: int) -> int:
        """Удаляет записи журнала старше срока хранения. Возвращает число удалённых."""
        # CURRENT_TIMESTAMP в SQLite — UTC без часового пояса, сравниваем так же
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)
        stmt = delete(AuditEvent).where(AuditEvent.created_at < cutoff).returning(AuditEvent.id)
        async with self.sessions.begin() as session:
            deleted = (await session.scalars(stmt)).all()
        return len(deleted)

    # ---------- Аналитика ----------

    async def questions_since(self, since: datetime, outcomes: Iterable[AuditOutcome]) -> list[QuestionRow]:
        """Сообщения клиентов из журнала начиная с `since` (UTC) с итогами из `outcomes`, старые — первыми.

        Сообщения с признаками атаки не входят: это не спрос клиентов, их разбирают в «Журнале».
        """
        stmt = (
            select(
                AuditEvent.id,
                AuditEvent.chat_id,
                AuditEvent.created_at,
                AuditEvent.user_text,
                AuditEvent.outcome,
                AuditEvent.final_answer,
                AuditEvent.tool_calls,
            )
            .where(
                AuditEvent.created_at >= since,
                AuditEvent.outcome.in_(list(outcomes)),
                AuditEvent.is_suspicious.is_(False),
            )
            .order_by(AuditEvent.id)
        )
        async with self.sessions() as session:
            rows = (await session.execute(stmt)).all()
        return [
            QuestionRow(event_id, chat_id, created_at, text, outcome, answer, tool_calls)
            for event_id, chat_id, created_at, text, outcome, answer, tool_calls in rows
        ]

    async def leads_since(self, since: datetime) -> list[LeadRow]:
        """Заявки начиная с `since` (UTC), старые — первыми."""
        stmt = select(Lead.chat_id, Lead.created_at).where(Lead.created_at >= since).order_by(Lead.id)
        async with self.sessions() as session:
            rows = (await session.execute(stmt)).all()
        return [LeadRow(chat_id, created_at) for chat_id, created_at in rows]

    # ---------- Расходы ----------

    async def usage_by_month(self) -> list[UsageRow]:
        """Суммы расхода по месяцам (UTC) и моделям."""
        month = type_coerce(func.strftime("%Y-%m", UsageDaily.day), String)
        stmt = (
            select(
                month,
                UsageDaily.model,
                func.sum(UsageDaily.requests),
                func.sum(UsageDaily.input_tokens),
                func.sum(UsageDaily.output_tokens),
                func.sum(UsageDaily.cache_read_tokens),
                func.sum(UsageDaily.cache_write_tokens),
                func.sum(UsageDaily.cost_micro_usd),
                func.sum(UsageDaily.unpriced_requests),
            )
            .group_by(month, UsageDaily.model)
            .order_by(month.desc(), UsageDaily.model)
        )
        async with self.sessions() as session:
            rows = (await session.execute(stmt)).all()
        return [
            UsageRow(m, model, int(req), int(inp), int(out), int(cr), int(cw), int(cost), int(unp))
            for m, model, req, inp, out, cr, cw, cost, unp in rows
        ]

    # ---------- База знаний и настройки ----------

    async def load_knowledge(self, catalog_inline_limit: int | None = None) -> Knowledge:
        """База знаний для промпта. Если активных услуг больше `catalog_inline_limit`, каталог
        не загружается целиком — модель ищет в нём через `search_catalog`. None — без порога.
        """
        async with self.sessions() as session:
            catalog_search = False
            if catalog_inline_limit is not None:
                active = await session.scalar(select(func.count()).where(Service.is_active.is_(True)))
                catalog_search = (active or 0) > catalog_inline_limit
            return Knowledge(
                sections=await _active(session, InfoSection),
                services=[] if catalog_search else await _active(session, Service),
                faq=await _active(session, FaqItem),
                catalog_search=catalog_search,
            )

    async def catalog_prices(self) -> set[int]:
        """Все цены активных услуг — для проверки цен в ответе, когда каталога нет в промпте."""
        async with self.sessions() as session:
            prices = await session.scalars(select(Service.price).where(Service.is_active.is_(True)).distinct())
            return set(prices)

    async def search_catalog(
        self,
        query: str,
        *,
        min_price: int | None = None,
        max_price: int | None = None,
        limit: int = 10,
    ) -> CatalogPage:
        """Активные услуги по словам запроса (полнотекстовый индекс) и диапазону цен.

        Самые подходящие — первыми (bm25). Запрос без слов возвращает каталог по порядку из админки.
        """
        conditions: list[ColumnElement[bool]] = [Service.is_active.is_(True)]
        if min_price is not None:
            conditions.append(Service.price >= min_price)
        if max_price is not None:
            conditions.append(Service.price <= max_price)

        found = select(Service).where(*conditions)
        match = fts.match_query(query)
        if match is None:
            found = found.order_by(Service.sort_order, Service.id)
        else:
            index: ColumnClause[object] = literal_column(fts.FTS_TABLE)  # сама таблица FTS: MATCH и bm25 берут её имя
            found = (
                found.join(_FTS, _FTS.c.rowid == Service.id)
                .where(index.op("MATCH")(match))
                # Вес совпадения в названии — 10, в описании — 1: услуга с нужным словом в названии выше
                .order_by(func.bm25(index, 10.0, 1.0), Service.sort_order, Service.id)
            )

        async with self.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(found.order_by(None).subquery()))
            services = list(await session.scalars(found.limit(limit)))
        return CatalogPage(services, total or 0)

    async def reorder[M: (InfoSection, Service, FaqItem)](self, model: type[M], ids: Sequence[int]) -> bool:
        """Порядок после перетаскивания в админке: шаг 10, чтобы вручную было куда вставить запись.

        `ids` — все записи раздела в новом порядке. False — список изменился (запись добавили
        или удалили, пока его сортировали), порядок не тронут.
        """
        async with self.sessions.begin() as session:
            rows = {row.id: row for row in await session.scalars(select(model))}
            if len(ids) != len(rows) or set(ids) != set(rows):
                return False
            for position, item_id in enumerate(ids, start=1):
                rows[item_id].sort_order = position * 10
        return True

    async def get_config(self) -> BotConfig:
        """Настройки бота; при первом обращении создаёт строку со значениями по умолчанию."""
        async with self.sessions.begin() as session:
            config = await session.get(BotConfig, 1)
            if config is None:
                config = BotConfig(id=1)
                session.add(config)
                await session.flush()
                await session.refresh(config)
            return config
