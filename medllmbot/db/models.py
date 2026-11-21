"""Модели БД. Всем, кроме истории сообщений, управляют из веб-админки."""

import enum
from collections.abc import Sequence
from datetime import date, datetime
from typing import ClassVar, Self, final, override

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Явные имена ограничений нужны Alembic для batch-миграций в SQLite
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata: ClassVar[MetaData] = MetaData(naming_convention=NAMING_CONVENTION)


# ---------- Клиенты и переписка ----------


@final
class Client(Base):
    __tablename__ = "clients"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), default="", server_default="")
    is_blocked: Mapped[bool] = mapped_column(default=False, server_default=false())
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
    role: Mapped[str] = mapped_column(String(16))  # 'user' или 'assistant'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    client: Mapped[Client] = relationship(back_populates="messages")


class LeadStatus(enum.Enum):
    NEW = "Новая"
    IN_PROGRESS = "В работе"
    DONE = "Записан"
    REJECTED = "Отказ"

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


# ---------- Журнал аудита ----------


class AuditOutcome(enum.Enum):
    """Чем закончилась обработка входящего сообщения."""

    ANSWERED = "Ответ"
    PRICE_REPLACED = "Цена заменена"
    LEAK_BLOCKED = "Утечка промпта"
    REFUSAL = "Отказ модели"
    TRUNCATED = "Ответ обрезан"  # упёрся в max_tokens
    EMPTY = "Пустой ответ"  # модель не вернула текста
    LLM_ERROR = "Ошибка API"
    TOO_LONG = "Слишком длинное"
    BUDGET_EXCEEDED = "Лимит расходов"  # дневной лимит на Claude исчерпан
    GREETING = "Приветствие"  # /start
    NOT_TEXT = "Не текст"  # фото, голосовое, стикер
    RATE_LIMITED = "Rate-limit"
    BLOCKED = "Клиент заблокирован"

    @override
    def __str__(self) -> str:
        return self.value


@final
class AuditEvent(Base):
    """Каждое входящее сообщение, включая отклонённые, с ответом модели и признаками атаки.

    В отличие от `messages` (контекст для модели) журнал хранит всё и только для чтения.
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
    flags: Mapped[str] = mapped_column(String(200), default="", server_default="")  # через запятую
    llm_answer: Mapped[str | None] = mapped_column(Text)  # сырой ответ модели
    final_answer: Mapped[str | None] = mapped_column(Text)  # что увидел клиент
    model: Mapped[str | None] = mapped_column(String(100))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    cache_read_tokens: Mapped[int | None] = mapped_column()
    cache_write_tokens: Mapped[int | None] = mapped_column()
    cost_micro_usd: Mapped[int | None] = mapped_column()  # None — нет цены на модель
    latency_ms: Mapped[int | None] = mapped_column()

    @classmethod
    def incoming(cls, chat_id: int, user_text: str, outcome: AuditOutcome, flags: Sequence[str] = ()) -> Self:
        """Событие о входящем сообщении; подозрительным считается, если есть признаки атаки."""
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


# ---------- Расходы на API ----------


@final
class ModelPrice(Base):
    """Цена модели в USD за миллион токенов. Правится в админке вслед за прайсом Anthropic.

    Изменение цены не пересчитывает прошлые запросы: их стоимость зафиксирована, как в счёте.
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
    # Тариф по длине промпта (у Haiku 5.5 промпты длиннее 100 000 токенов дороже)
    from_prompt_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    input_per_mtok: Mapped[float] = mapped_column()
    output_per_mtok: Mapped[float] = mapped_column()
    cache_write_per_mtok: Mapped[float] = mapped_column()  # запись в кэш с TTL 5 минут
    cache_read_per_mtok: Mapped[float] = mapped_column()

    @override
    def __str__(self) -> str:
        return f"{self.model} (от {self.from_prompt_tokens} токенов)"


@final
class UsageDaily(Base):
    """Расход по дням (UTC) и моделям для отчёта по месяцам.

    Персональных данных здесь нет, поэтому записи не удаляются вместе со старым журналом.
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


# ---------- База знаний клиники ----------


@final
class InfoSection(Base):
    """Произвольный раздел: контакты, часы работы, оплата и т.п."""

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
    price: Mapped[int] = mapped_column()  # в леях
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")

    @override
    def __str__(self) -> str:
        return f"{self.name} — {self.price} лей"


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


# ---------- Настройки бота ----------

DEFAULT_GREETING = (
    "Здравствуйте! Я помощник клиники. Спросите о ценах, "
    "услугах или запишитесь на приём.\n\n"
    "Bună ziua! Sunt asistentul clinicii. Întrebați despre "
    "prețuri, servicii sau programați-vă."
)


@final
class BotConfig(Base):
    """Единственная строка (id = 1) с настройками, которые меняются без перезапуска."""

    __tablename__ = "bot_config"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "max_tokens > 0 AND max_len > 0 AND history_limit >= 0 AND rate_limit > 0 AND rate_window > 0",
            name="positive_limits",
        ),
        CheckConstraint("audit_retention_days > 0", name="retention_positive"),
        CheckConstraint("daily_budget_usd >= 0", name="budget_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    model: Mapped[str] = mapped_column(String(100), default="claude-haiku-5-5")
    max_tokens: Mapped[int] = mapped_column(default=1024)
    # Максимальная длина сообщения клиента, символов: подробное обращение с симптомами и телефоном
    # занимает 250–500 символов, а типичные джейлбрейки — тысячи
    max_len: Mapped[int] = mapped_column(default=1024)
    history_limit: Mapped[int] = mapped_column(default=8)  # сколько последних сообщений помнить
    rate_limit: Mapped[int] = mapped_column(default=10)  # не больше 10 сообщений...
    rate_window: Mapped[int] = mapped_column(default=60)  # ...за 60 секунд от одного клиента
    greeting: Mapped[str] = mapped_column(Text, default=DEFAULT_GREETING)
    # Сколько дней хранить журнал аудита (в нём телефоны и медицинские вопросы)
    audit_retention_days: Mapped[int] = mapped_column(default=90, server_default="90")
    # Сколько долларов в сутки (UTC) можно потратить на Claude; 0 — без лимита. Защита публичного демо
    # от спама из многих чатов: rate-limit ограничивает каждый чат, а этот лимит — все вместе
    daily_budget_usd: Mapped[int] = mapped_column(default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @override
    def __str__(self) -> str:
        return "Настройки бота"
