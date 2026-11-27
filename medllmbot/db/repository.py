"""All bot queries to the DB. Each method opens its own short session."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from anthropic.types import AssistantMessageParam, MessageParam, UserMessageParam
from sqlalchemy import (
    ColumnClause,
    ColumnElement,
    String,
    column,
    delete,
    func,
    literal_column,
    select,
    table,
    type_coerce,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from medllmbot.costs import Price, PriceBook, TokenUsage, UsageRow
from medllmbot.db import fts
from medllmbot.db.models import (
    AdminUser,
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
    OperatorRelay,
    Order,
    Service,
    UsageDaily,
)


@dataclass(frozen=True)
class NewRecords:
    """How many records appeared after `after_id`, for the new-records badge in the admin."""

    last_id: int  # id of the last record (0 means no records): the baseline for subsequent checks
    count: int
    suspicious: int = 0  # audit log only: how many of the new records are suspicious


@dataclass(frozen=True)
class Knowledge:
    """Snapshot of the clinic knowledge base: only active records, in sort_order."""

    sections: list[InfoSection]
    services: list[Service]
    faq: list[FaqItem]
    # The catalog exceeds the threshold and is not loaded into `services`: the model searches it with search_catalog
    catalog_search: bool = False


@dataclass(frozen=True)
class CatalogPage:
    """Catalog search result: the top items and the total number of matches."""

    services: list[Service]
    total: int


@dataclass(frozen=True)
class QuestionRow:
    """A client message from the audit log, for question analytics (`medllmbot/analytics.py`)."""

    event_id: int
    chat_id: int
    created_at: datetime  # UTC
    text: str
    outcome: AuditOutcome
    answer: str | None  # what the client received
    tool_calls: str | None


@dataclass(frozen=True)
class LeadRow:
    """When the client left a phone number, for the share of dialogs with a lead."""

    chat_id: int
    created_at: datetime  # UTC


_FTS = table(fts.FTS_TABLE, column("rowid"))


async def _active[M: (InfoSection, Service, FaqItem)](session: AsyncSession, model: type[M]) -> list[M]:
    """Active records of a knowledge base section, in display order."""
    stmt = select(model).where(model.is_active.is_(True)).order_by(model.sort_order, model.id)
    return list((await session.scalars(stmt)).all())


def _to_message(role: str, content: str) -> MessageParam:
    """History row from the DB -> message in Anthropic API format."""
    if role == "user":
        return UserMessageParam(role="user", content=content)
    return AssistantMessageParam(role="assistant", content=content)


def _pair(chat_id: int, question: str, answer: str) -> list[ChatMessage]:
    return [
        ChatMessage(chat_id=chat_id, role="user", content=question),
        ChatMessage(chat_id=chat_id, role="assistant", content=answer),
    ]


def _token_usage(event: AuditEvent) -> TokenUsage | None:
    """Tokens from the event; None if there was no model request."""
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
    """Adds the request to today's (UTC) totals with an atomic upsert."""
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


async def _add_event(session: AsyncSession, event: AuditEvent, requests: Sequence[TokenUsage]) -> None:
    """Adds the event; if there was a model request, its cost goes into the event and the daily totals."""
    usage = _token_usage(event)
    if usage is not None and event.model is not None:
        prices = PriceBook(_to_price(row) for row in await session.scalars(select(ModelPrice)))
        if requests:
            event.cost_micro_usd = prices.total_micro_usd(event.model, requests)
        else:
            event.cost_micro_usd = prices.cost_micro_usd(event.model, usage)
        await _add_daily_usage(session, event.model, usage, event.cost_micro_usd)
    session.add(event)


class Repository:
    sessions: async_sessionmaker[AsyncSession]

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    # ---------- Clients ----------

    async def touch_client(self, chat_id: int, username: str | None, full_name: str) -> bool:
        """Creates or updates the client. Returns True if the client is blocked."""
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
        """Blocking from the admin. None means there is no such client."""
        async with self.sessions.begin() as session:
            client = await session.get(Client, chat_id)
            if client is not None:
                client.is_blocked = blocked
        return client

    # ---------- Dialog history ----------

    async def load_history(self, chat_id: int, limit: int) -> list[MessageParam]:
        """Latest dialog messages in LLM format, oldest first."""
        stmt = (
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        async with self.sessions() as session:
            rows = (await session.execute(stmt)).all()
        history = [_to_message(role, content) for role, content in reversed(rows)]
        # The slice may start with bot replies (odd limit, several operator replies in a row),
        # but the API expects the client's message first
        while history and history[0]["role"] != "user":
            history = history[1:]
        return history

    async def save_pair(self, chat_id: int, question: str, answer: str) -> None:
        """Saves the question and answer together so the history always starts with the client."""
        async with self.sessions.begin() as session:
            session.add_all(_pair(chat_id, question, answer))

    # ---------- Leads ----------

    async def create_lead(self, chat_id: int, client_name: str, phone: str, message: str) -> Lead:
        lead = Lead(chat_id=chat_id, client_name=client_name, phone=phone, message=message)
        async with self.sessions.begin() as session:
            session.add(lead)
        return lead

    async def set_lead_status(self, lead_id: int, status: LeadStatus) -> Lead | None:
        """Status change from the admin. None means there is no lead (e.g. it was just deleted)."""
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

    # ---------- Live operator ----------

    async def operator_until(self, chat_id: int) -> datetime | None:
        """Until when (UTC) the operator handles the client; None means the bot replies. It may have expired."""
        async with self.sessions() as session:
            return await session.scalar(select(Client.operator_until).where(Client.chat_id == chat_id))

    async def start_operator(self, chat_id: int, until: datetime) -> None:
        """Hands the client to the operator until `until` (UTC): the bot stays silent until then."""
        async with self.sessions.begin() as session:
            await session.execute(update(Client).where(Client.chat_id == chat_id).values(operator_until=until))

    async def end_operator(self, chat_id: int, now: datetime) -> bool:
        """Returns the client to the bot. True if the operator was handling the client and it had not expired."""
        async with self.sessions.begin() as session:
            client = await session.get(Client, chat_id)
            if client is None or client.operator_until is None:
                return False
            active = client.operator_until > now
            client.operator_until = None
        return active

    async def add_relay(self, admin_chat_id: int, admin_message_id: int, chat_id: int) -> None:
        """Remembers a bot message in the admin chat: a reply to it goes to client `chat_id`."""
        stmt = (
            insert(OperatorRelay)
            .values(admin_chat_id=admin_chat_id, admin_message_id=admin_message_id, chat_id=chat_id)
            .on_conflict_do_update(
                index_elements=[OperatorRelay.admin_chat_id, OperatorRelay.admin_message_id], set_={"chat_id": chat_id}
            )
        )
        async with self.sessions.begin() as session:
            await session.execute(stmt)

    async def relay_chat(self, admin_chat_id: int, admin_message_id: int) -> int | None:
        """The client a bot message in the admin chat refers to; None if none."""
        stmt = select(OperatorRelay.chat_id).where(
            OperatorRelay.admin_chat_id == admin_chat_id, OperatorRelay.admin_message_id == admin_message_id
        )
        async with self.sessions() as session:
            return await session.scalar(stmt)

    async def save_message(self, chat_id: int, role: str, content: str) -> None:
        """A single message into the history: the client writes to the operator, the operator replies,
        without the bot's question-answer pair.

        Empty messages are not saved: the Claude API rejects messages without text.
        """
        if not content.strip():
            return
        async with self.sessions.begin() as session:
            session.add(ChatMessage(chat_id=chat_id, role=role, content=content))

    # ---------- Shop orders ----------

    async def find_order(self, key: str) -> Order | None:
        """Order by number key, letters and digits only: «DE10023» finds «DE-10023» (orders.order_key)."""
        stored_key = func.replace(func.upper(Order.number), "-", "")
        async with self.sessions() as session:
            return await session.scalar(select(Order).where(stored_key == key.upper()).limit(1))

    # ---------- Audit log ----------

    async def log_event(self, event: AuditEvent, requests: Sequence[TokenUsage] = ()) -> None:
        """Writes the event. If there was a model request, computes its cost and adds it
        to the daily spending totals in the same transaction.

        `requests` holds the tokens of each request if the answer took several (catalog search): the prompt
        length tier is chosen for each request separately. Without them the cost is computed from the event tokens.
        """
        async with self.sessions.begin() as session:
            await _add_event(session, event, requests)

    async def log_event_with_pair(
        self, event: AuditEvent, question: str, answer: str, requests: Sequence[TokenUsage] = ()
    ) -> None:
        """`log_event` plus `save_pair` in one transaction: either both are written or neither.

        If the log write fails (the DB stays busy), the client gets an error message instead of the answer,
        so the history must not get that answer either.
        """
        async with self.sessions.begin() as session:
            await _add_event(session, event, requests)
            session.add_all(_pair(event.chat_id, question, answer))

    async def spent_today_micro_usd(self) -> int:
        """Claude spending for the current day (UTC) across all models, for the daily budget."""
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
        """Deletes audit log records older than the retention period. Returns the number deleted."""
        # CURRENT_TIMESTAMP in SQLite is UTC without a time zone, so compare the same way
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)
        stmt = delete(AuditEvent).where(AuditEvent.created_at < cutoff).returning(AuditEvent.id)
        async with self.sessions.begin() as session:
            deleted = (await session.scalars(stmt)).all()
        return len(deleted)

    # ---------- Analytics ----------

    async def questions_since(self, since: datetime, outcomes: Iterable[AuditOutcome]) -> list[QuestionRow]:
        """Client messages from the audit log since `since` (UTC) with outcomes in `outcomes`, oldest first.

        Messages with attack signs are excluded: they are not client demand and are reviewed in «Журнал».
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
        """Leads since `since` (UTC), oldest first."""
        stmt = select(Lead.chat_id, Lead.created_at).where(Lead.created_at >= since).order_by(Lead.id)
        async with self.sessions() as session:
            rows = (await session.execute(stmt)).all()
        return [LeadRow(chat_id, created_at) for chat_id, created_at in rows]

    async def purge_relays(self, older_than_days: int) -> int:
        """Deletes old operator relay links per the audit log retention period. Returns the number deleted."""
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)
        stmt = delete(OperatorRelay).where(OperatorRelay.created_at < cutoff).returning(OperatorRelay.admin_message_id)
        async with self.sessions.begin() as session:
            deleted = (await session.scalars(stmt)).all()
        return len(deleted)

    # ---------- Costs ----------

    async def usage_by_month(self) -> list[UsageRow]:
        """Spending totals per month (UTC) and model."""
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

    # ---------- Knowledge base and settings ----------

    async def load_knowledge(self, catalog_inline_limit: int | None = None) -> Knowledge:
        """Knowledge base for the prompt. If there are more active services than `catalog_inline_limit`,
        the catalog is not loaded whole and the model searches it via `search_catalog`. None means no threshold.
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
        """All prices of active services, for checking prices in the answer when the catalog is not in the prompt."""
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
        """Active services matching the query words (full-text index) and price range.

        Best matches first (bm25). A query without words returns the catalog in the admin order.
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
            index: ColumnClause[object] = literal_column(fts.FTS_TABLE)  # the FTS table: MATCH and bm25 take its name
            found = (
                found.join(_FTS, _FTS.c.rowid == Service.id)
                .where(index.op("MATCH")(match))
                # Match weight: 10 in the name, 1 in the description; a service with the word in its name ranks higher
                .order_by(func.bm25(index, 10.0, 1.0), Service.sort_order, Service.id)
            )

        async with self.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(found.order_by(None).subquery()))
            services = list(await session.scalars(found.limit(limit)))
        return CatalogPage(services, total or 0)

    async def reorder[M: (InfoSection, Service, FaqItem)](self, model: type[M], ids: Sequence[int]) -> bool:
        """Order after drag-and-drop in the admin: step 10, so there is room to insert a record by hand.

        `ids` are all records of the section in the new order. False means the list changed (a record was added
        or deleted while it was being sorted); the order is left untouched.
        """
        async with self.sessions.begin() as session:
            rows = {row.id: row for row in await session.scalars(select(model))}
            if len(ids) != len(rows) or set(ids) != set(rows):
                return False
            for position, item_id in enumerate(ids, start=1):
                rows[item_id].sort_order = position * 10
        return True

    # ---------- Admin panel accounts ----------

    async def admin_user_by_email(self, email: str) -> AdminUser | None:
        """The account with this login; the email is compared without regard to case and surrounding spaces."""
        async with self.sessions() as session:
            return await session.scalar(select(AdminUser).where(AdminUser.email == AdminUser.normalize_email(email)))

    async def get_admin_user(self, user_id: int) -> AdminUser | None:
        async with self.sessions() as session:
            return await session.get(AdminUser, user_id)

    async def mark_admin_login(self, user_id: int) -> None:
        """The «Last login» time in the «Administrators» section."""
        async with self.sessions.begin() as session:
            await session.execute(update(AdminUser).where(AdminUser.id == user_id).values(last_login_at=func.now()))

    async def get_config(self) -> BotConfig:
        """Bot settings; on first access creates the row with default values."""
        async with self.sessions.begin() as session:
            config = await session.get(BotConfig, 1)
            if config is None:
                config = BotConfig(id=1)
                session.add(config)
                await session.flush()
                await session.refresh(config)
            return config
