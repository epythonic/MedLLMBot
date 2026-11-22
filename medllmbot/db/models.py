"""DB models. Everything except the message history is managed from the web admin."""

import enum
from collections.abc import Sequence
from datetime import date, datetime
from typing import ClassVar, Self, cast, final, override

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    false,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from medllmbot.db import fts
from medllmbot.i18n import N_

# Alembic needs explicit constraint names for batch migrations in SQLite
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata: ClassVar[MetaData] = MetaData(naming_convention=NAMING_CONVENTION)


# ---------- Clients and chat history ----------


@final
class Client(Base):
    __tablename__ = "clients"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), default="", server_default="")
    is_blocked: Mapped[bool] = mapped_column(default=False, server_default=false())
    # A live operator (the admin in Telegram) handles the client: until this time (UTC) the bot stays silent
    # and relays messages to them. None means the bot replies (medllmbot/operator.py)
    operator_until: Mapped[datetime | None] = mapped_column()
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())

    messages: Mapped[list[ChatMessage]] = relationship(back_populates="client")
    leads: Mapped[list[Lead]] = relationship(back_populates="client")

    @override
    def __str__(self) -> str:
        return f"@{self.username}" if self.username else self.full_name or str(self.chat_id)


@final
class ChatMessage(Base):
    __tablename__ = "messages"
    __table_args__: tuple[Index, ...] = (Index("ix_messages_chat_id_id", "chat_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("clients.chat_id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # 'user' or 'assistant'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    client: Mapped[Client] = relationship(back_populates="messages")


# Enum values are English labels marked for the admin's catalogs (N_); the admin translates them when it shows
# them. The DB stores member names (NEW, ANSWERED…), so relabelling needs no migration.


class LeadStatus(enum.Enum):
    NEW = N_("New")
    IN_PROGRESS = N_("In progress")
    DONE = N_("Done")  # appointment booked or order placed
    REJECTED = N_("Rejected")

    @override
    def __str__(self) -> str:
        return self.value


@final
class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("clients.chat_id", ondelete="CASCADE"))
    client_name: Mapped[str] = mapped_column(String(256))
    phone: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, native_enum=False, length=16),
        default=LeadStatus.NEW,
        server_default=LeadStatus.NEW.name,
    )
    admin_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    client: Mapped[Client] = relationship(back_populates="leads")

    @override
    def __str__(self) -> str:
        return f"Заявка #{self.id} — {self.client_name}"


# ---------- Audit log ----------


class AuditOutcome(enum.Enum):
    """How processing of an incoming message ended."""

    ANSWERED = N_("Answered")
    PRICE_REPLACED = N_("Price replaced")
    LEAK_BLOCKED = N_("Prompt leak")
    REFUSAL = N_("Model refusal")
    TRUNCATED = N_("Answer cut off")  # hit max_tokens
    EMPTY = N_("Empty answer")  # the model returned no text
    LLM_ERROR = N_("API error")
    TOO_LONG = N_("Too long")
    BUDGET_EXCEEDED = N_("Spend limit")  # daily Claude budget exhausted
    GREETING = N_("Greeting")  # /start
    NOT_TEXT = N_("Not text")  # photo, voice message, sticker
    RATE_LIMITED = N_("Rate limit")
    BLOCKED = N_("Client blocked")
    HANDOFF = N_("Asked for operator")  # the client asked for a human; the bot handed the dialog to the admin
    TO_OPERATOR = N_("Sent to operator")  # the operator handles the dialog: relayed to them, Claude not called
    FROM_OPERATOR = N_("Operator reply")  # the admin replied to the client from Telegram; the text is in user_text

    @override
    def __str__(self) -> str:
        return self.value


@final
class AuditEvent(Base):
    """Every incoming message, including rejected ones, with the model's answer and attack signs.

    Unlike `messages` (context for the model), the log keeps everything and is read-only.
    """

    __tablename__ = "audit_log"
    __table_args__: tuple[Index, ...] = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_chat_id_id", "chat_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    chat_id: Mapped[int] = mapped_column(ForeignKey("clients.chat_id", ondelete="CASCADE"))
    user_text: Mapped[str] = mapped_column(Text)
    outcome: Mapped[AuditOutcome] = mapped_column(Enum(AuditOutcome, native_enum=False, length=20))
    is_suspicious: Mapped[bool] = mapped_column(default=False, server_default=false())
    flags: Mapped[str] = mapped_column(String(200), default="", server_default="")  # comma-separated
    llm_answer: Mapped[str | None] = mapped_column(Text)  # raw model answer
    final_answer: Mapped[str | None] = mapped_column(Text)  # what the client saw
    model: Mapped[str | None] = mapped_column(String(100))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    cache_read_tokens: Mapped[int | None] = mapped_column()
    cache_write_tokens: Mapped[int | None] = mapped_column()
    cost_micro_usd: Mapped[int | None] = mapped_column()  # None means no price for the model
    latency_ms: Mapped[int | None] = mapped_column()
    # Model tool calls, one line per call: «search_catalog «чистка» → найдено 2»
    tool_calls: Mapped[str | None] = mapped_column(Text)

    @classmethod
    def incoming(cls, chat_id: int, user_text: str, outcome: AuditOutcome, flags: Sequence[str] = ()) -> Self:
        """Event for an incoming message; it is suspicious if there are attack signs."""
        return cls(
            chat_id=chat_id,
            user_text=user_text,
            outcome=outcome,
            flags=",".join(flags),
            is_suspicious=bool(flags),
        )

    @override
    def __str__(self) -> str:
        return f"Событие #{self.id} — {self.outcome}"


@final
class OperatorRelay(Base):
    """A bot message in the admin chat that refers to a client: a reply to it goes to that client.

    Message IDs are unique only within a chat, so the key is chat plus ID: if ADMIN_ID changes,
    a reply from the new admin will not reach a client from the old admin's relays.
    """

    __tablename__ = "operator_relays"

    admin_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    admin_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(ForeignKey("clients.chat_id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------- API costs ----------


@final
class ModelPrice(Base):
    """Model price in USD per million tokens. Edited in the admin to follow Anthropic's price list.

    A price change does not recalculate past requests: their cost is fixed, as on an invoice.
    """

    __tablename__ = "model_prices"
    __table_args__: tuple[UniqueConstraint | CheckConstraint, ...] = (
        UniqueConstraint("model", "from_prompt_tokens"),
        CheckConstraint(
            "from_prompt_tokens >= 0 AND input_per_mtok >= 0 AND output_per_mtok >= 0 "
            "AND cache_write_per_mtok >= 0 AND cache_read_per_mtok >= 0",
            name="non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    # Tier by prompt length (Haiku 5.5 prompts longer than 100,000 tokens cost more)
    from_prompt_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    input_per_mtok: Mapped[float] = mapped_column()
    output_per_mtok: Mapped[float] = mapped_column()
    cache_write_per_mtok: Mapped[float] = mapped_column()  # cache write with a 5-minute TTL
    cache_read_per_mtok: Mapped[float] = mapped_column()

    @override
    def __str__(self) -> str:
        return f"{self.model} (от {self.from_prompt_tokens} токенов)"


@final
class UsageDaily(Base):
    """Spending per day (UTC) and model for the monthly report.

    There is no personal data here, so rows are not deleted along with the old audit log.
    """

    __tablename__ = "llm_usage_daily"
    __table_args__: tuple[UniqueConstraint, ...] = (UniqueConstraint("day", "model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column()
    model: Mapped[str] = mapped_column(String(100))
    requests: Mapped[int] = mapped_column(default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cost_micro_usd: Mapped[int] = mapped_column(default=0, server_default="0")
    unpriced_requests: Mapped[int] = mapped_column(default=0, server_default="0")

    @override
    def __str__(self) -> str:
        return f"{self.day} — {self.model}"


# ---------- Clinic knowledge base ----------


@final
class InfoSection(Base):
    """Free-form section: contacts, opening hours, payment, etc."""

    __tablename__ = "info_sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")

    @override
    def __str__(self) -> str:
        return self.title


@final
class Service(Base):
    __tablename__ = "services"
    __table_args__: tuple[CheckConstraint, ...] = (CheckConstraint("price >= 0", name="price_non_negative"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[int] = mapped_column()  # in lei
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")

    @override
    def __str__(self) -> str:
        return f"{self.name} — {self.price} лей"


# Catalog full-text index for create_all (in tests); in the production DB a migration creates it
_services = cast(Table, Service.__table__)
for _statement in fts.CREATE:
    event.listen(_services, "after_create", DDL(_statement))
for _statement in fts.DROP:
    event.listen(_services, "before_drop", DDL(_statement))


@final
class FaqItem(Base):
    __tablename__ = "faq"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(500))
    answer: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")

    @override
    def __str__(self) -> str:
        return self.question


# ---------- Shop orders ----------


class OrderStatus(enum.Enum):
    """Admin labels. The wording the model sees is Russian and lives in orders.STATUS_TEXT."""

    NEW = N_("Accepted")
    CONFIRMED = N_("Confirmed")
    SHIPPED = N_("Out for delivery")
    READY = N_("Ready for pickup")
    DELIVERED = N_("Delivered")
    CANCELLED = N_("Cancelled")

    @override
    def __str__(self) -> str:
        return self.value


@final
class Order(Base):
    """Shop order, a demo stand-in for the shop API (CMS, 1C): the bot gets the status via `OrderBackend`.

    The phone is used for verification: the status is shown only to someone who gives the order number
    and the last digits of the phone from the order.
    """

    __tablename__ = "orders"
    __table_args__: tuple[CheckConstraint, ...] = (CheckConstraint("total >= 0", name="total_non_negative"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)  # as on the receipt: DE-10023
    customer_name: Mapped[str] = mapped_column(String(256))
    phone: Mapped[str] = mapped_column(String(32))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16),
        default=OrderStatus.NEW,
        server_default=OrderStatus.NEW.name,
    )
    items: Mapped[str] = mapped_column(Text)  # what was ordered, for the reply to the client
    total: Mapped[int] = mapped_column()  # in lei
    delivery_date: Mapped[date | None] = mapped_column()  # expected delivery or pickup date
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @override
    def __str__(self) -> str:
        return f"Заказ {self.number}"


# ---------- Bot settings ----------


class BusinessProfile(enum.Enum):
    """Whose bot this is: the profile sets the role and domain rules in the prompt (medllmbot/profiles.py)."""

    CLINIC = N_("Dental clinic")
    SHOP = N_("Electronics store")

    @override
    def __str__(self) -> str:
        return self.value


# Russian and Romanian greetings: the bot picks one by the client's Telegram language, otherwise sends both
DEFAULT_GREETING = "Здравствуйте! Я помощник клиники. Спросите о ценах, услугах или запишитесь на приём."
DEFAULT_GREETING_RO = "Bună ziua! Sunt asistentul clinicii. Întrebați despre prețuri, servicii sau programați-vă."


@final
class BotConfig(Base):
    """The single row (id = 1) with settings that change without a restart."""

    __tablename__ = "bot_config"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "max_tokens > 0 AND max_len > 0 AND history_limit >= 0 AND rate_limit > 0 AND rate_window > 0",
            name="positive_limits",
        ),
        CheckConstraint("audit_retention_days > 0", name="retention_positive"),
        CheckConstraint("daily_budget_usd >= 0", name="budget_non_negative"),
        CheckConstraint("catalog_inline_limit >= 0", name="catalog_limit_non_negative"),
        CheckConstraint("operator_hours > 0", name="operator_hours_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # Changes together with the knowledge base: python -m medllmbot.db.seed --reset --profile shop
    profile: Mapped[BusinessProfile] = mapped_column(
        Enum(BusinessProfile, native_enum=False, length=16),
        default=BusinessProfile.CLINIC,
        server_default=BusinessProfile.CLINIC.name,
    )
    model: Mapped[str] = mapped_column(String(100), default="claude-haiku-5-5")
    max_tokens: Mapped[int] = mapped_column(default=1024)
    # Maximum client message length, in characters: a detailed request with symptoms and a phone number
    # takes 250–500 characters, while typical jailbreaks take thousands
    max_len: Mapped[int] = mapped_column(default=1024)
    history_limit: Mapped[int] = mapped_column(default=8)  # how many recent messages to remember
    rate_limit: Mapped[int] = mapped_column(default=10)  # no more than 10 messages...
    rate_window: Mapped[int] = mapped_column(default=60)  # ...per 60 seconds from one client
    greeting: Mapped[str] = mapped_column(Text, default=DEFAULT_GREETING)
    # Romanian greeting. Empty means all clients get `greeting`: that is how settings worked before this field,
    # and it may contain both languages at once
    greeting_ro: Mapped[str | None] = mapped_column(Text, default=DEFAULT_GREETING_RO)
    # How many days to keep the audit log (it contains phone numbers and medical questions)
    audit_retention_days: Mapped[int] = mapped_column(default=90, server_default="90")
    # How many dollars per day (UTC) may be spent on Claude; 0 means no limit. Protects the public demo
    # from spam across many chats: rate-limit caps each chat, while this limit caps them all together
    daily_budget_usd: Mapped[int] = mapped_column(default=0, server_default="0")
    # A catalog of up to this many items goes into the prompt whole; beyond that the model searches it
    # with the search_catalog tool. A small catalog in the prompt is cheaper and faster, a large one
    # will not fit; 0 means always search
    catalog_inline_limit: Mapped[int] = mapped_column(default=50, server_default="50")
    # How many hours the bot stays silent after a client asks for a human, if the operator has not replied;
    # each operator reply extends the period
    operator_hours: Mapped[int] = mapped_column(default=12, server_default="12")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @override
    def __str__(self) -> str:
        return "Настройки бота"
