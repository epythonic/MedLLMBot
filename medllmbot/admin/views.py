"""Разделы админки: заявки, клиенты, переписка, база знаний, настройки бота."""

# SQLAdmin объявляет атрибуты ModelView с неполными типами (голые InstrumentedAttribute и list),
# поэтому их тип в наследниках всегда «partially unknown». Исправить это можно только в самой
# библиотеке, отключаем одно правило и только в этом файле.
# pyright: reportUnknownVariableType=false

from typing import ClassVar, cast, final, override

from markupsafe import Markup
from sqladmin import ModelView
from sqladmin.filters import BooleanFilter, OperationColumnFilter, StaticValuesFilter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from wtforms import Form, SelectField
from wtforms.validators import DataRequired, Length, NumberRange

from medllmbot.admin.forms import AdminForm, CommaFloatField
from medllmbot.admin.live import NEW_AUDIT_EVENTS, NEW_LEADS, audit_chat_id_cell, lead_status_cell, sort_handle_cell
from medllmbot.costs import format_usd, match_model
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
    Service,
    UsageDaily,
)
from medllmbot.security import FLAG_LABELS

CLIENTS = "Клиенты"
KNOWLEDGE = "База знаний"
SECURITY = "Безопасность"
COSTS = "Расходы"


def _short(text: str | None, limit: int = 80) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Форматтеры колонок списка: (модель, имя атрибута) -> отображаемый текст
def _lead_message(model: Lead, _attr: object) -> str:
    return _short(model.message)


def _chat_content(model: ChatMessage, _attr: object) -> str:
    return _short(model.content, 120)


def _faq_answer(model: FaqItem, _attr: object) -> str:
    return _short(model.answer)


def _section_content(model: InfoSection, _attr: object) -> str:
    return _short(model.content)


def _audit_text(model: AuditEvent, _attr: object) -> str:
    return _short(model.user_text)


def _audit_answer(model: AuditEvent, _attr: object) -> str:
    return _short(model.final_answer)


def _audit_cost(model: AuditEvent, _attr: object) -> str:
    return format_usd(model.cost_micro_usd)


def _daily_cost(model: UsageDaily, _attr: object) -> str:
    return format_usd(model.cost_micro_usd)


# SQLAdmin выводит у Enum имя элемента (NEW, ANSWERED) — показываем подпись, как в фильтрах и справке
def _lead_status(model: Lead, _attr: object) -> str:
    return model.status.value


def _audit_outcome(model: AuditEvent, _attr: object) -> str:
    return model.outcome.value


def _audit_flags(model: AuditEvent, _attr: object) -> str:
    return ", ".join(FLAG_LABELS.get(code, code) for code in model.flags.split(",") if code)


# Жёлтая метка вместо галочки и крестика: красный крестик у обычных сообщений выглядел как тревога
SUSPICIOUS_BADGE = Markup('<span class="badge bg-warning-lt">⚠ Да</span>')


def _audit_suspicious(model: AuditEvent, _attr: object) -> Markup | str:
    return SUSPICIOUS_BADGE if model.is_suspicious else ""


def _audit_tool_calls(model: AuditEvent, _attr: object) -> Markup | str:
    """Каждый вызов инструмента — с новой строки (текст экранируется)."""
    if not model.tool_calls:
        return ""
    return Markup("<br>").join(model.tool_calls.splitlines())


# ---------- Фильтры с русскими подписями ----------
# В SQLAdmin варианты «All / Yes / No» зашиты в коде фильтров и не переводятся.

ALL_LABEL = "Все"


@final
class YesNoFilter(BooleanFilter):
    @override
    async def lookups(self, request: Request, model: object, run_query: object) -> list[tuple[str, str]]:
        return [("all", ALL_LABEL), ("true", "Да"), ("false", "Нет")]


@final
class ChoiceFilter(StaticValuesFilter):
    @override
    async def lookups(self, request: Request, model: object, run_query: object) -> list[tuple[str, str]]:
        return [("__all", ALL_LABEL), *self.values]


OPERATION_LABELS = {
    "equals": "Равно",
    "contains": "Содержит",
    "starts_with": "Начинается с",
    "ends_with": "Заканчивается на",
    "greater_than": "Больше",
    "less_than": "Меньше",
}


@final
class OperationFilter(OperationColumnFilter):
    @override
    def get_operation_options(self, column_obj: object) -> list[tuple[str, str]]:
        return [(key, OPERATION_LABELS.get(key, label)) for key, label in super().get_operation_options(column_obj)]


# ---------- Клиенты ----------

# В БД хранится имя статуса (NEW), в админке показываем подпись («Новая»)
STATUS_CHOICES = [(status.name, status.value) for status in LeadStatus]


def _status_name(value: LeadStatus | str) -> str:
    return value.name if isinstance(value, LeadStatus) else str(value)


class AdminView(ModelView):
    """Общая основа разделов: формы с ошибками на русском (forms.AdminForm)."""

    form_base_class: ClassVar[type[Form]] = AdminForm
    # SQLAdmin показывает текст исключения над формой: без перехвата это был бы сырой SQL на английском
    integrity_error: ClassVar[str] = "Запись не сохранена: такая уже есть или значения нарушают ограничения базы."

    @override
    async def insert_model(self, request: Request, data: dict[str, object]) -> object:
        try:
            # SQLAdmin не типизировал insert_model (dict без параметров, возвращает Any)
            obj = cast(object, await super().insert_model(request, data))  # pyright: ignore[reportUnknownMemberType]
        except IntegrityError as exc:
            raise ValueError(self.integrity_error) from exc
        return obj

    @override
    async def update_model(self, request: Request, pk: str, data: dict[str, object]) -> object:
        try:
            obj = cast(object, await super().update_model(request, pk, data))  # pyright: ignore[reportUnknownMemberType]
        except IntegrityError as exc:
            raise ValueError(self.integrity_error) from exc
        return obj


def _strip(value: object) -> object:
    """Фильтр поля формы: пробелы по краям не сохраняем — строка из пробелов становится пустой."""
    return value.strip() if isinstance(value, str) else value


@final
class LeadAdmin(AdminView, model=Lead):
    name = "Заявка"
    name_plural = "Заявки"
    icon = "fa-solid fa-phone"
    category = CLIENTS

    can_create = False  # заявки создаёт бот
    # Список сам проверяет новые заявки (HTMX), статус меняется прямо в строке
    list_template = "live/list.html"
    live_route = NEW_LEADS
    column_list = [
        Lead.id,
        Lead.created_at,
        Lead.status,
        Lead.client_name,
        Lead.phone,
        Lead.message,
    ]
    column_default_sort = [(Lead.created_at, True)]
    column_searchable_list = [Lead.client_name, Lead.phone, Lead.message]
    column_filters = [ChoiceFilter(Lead.status, STATUS_CHOICES, title="Статус")]
    column_formatters = {Lead.message: _lead_message, Lead.status: lead_status_cell}
    column_formatters_detail = {Lead.status: _lead_status}
    column_details_exclude_list = [Lead.client]
    form_columns = [Lead.status, Lead.admin_note]
    form_overrides = {"status": SelectField}
    form_args = {"status": {"choices": STATUS_CHOICES, "coerce": _status_name}}
    column_labels = {
        Lead.id: "№",
        Lead.created_at: "Создана",
        Lead.updated_at: "Изменена",
        Lead.status: "Статус",
        Lead.client_name: "Клиент",
        Lead.phone: "Телефон",
        Lead.message: "Сообщение",
        Lead.admin_note: "Заметка администратора",
        Lead.chat_id: "Chat ID",
    }


@final
class ClientAdmin(AdminView, model=Client):
    name = "Клиент"
    name_plural = "Клиенты"
    icon = "fa-solid fa-user"
    category = CLIENTS

    can_create = False  # клиенты появляются, когда пишут боту
    can_delete = False  # удаление стёрло бы историю и заявки
    column_list = [
        Client.chat_id,
        Client.username,
        Client.full_name,
        Client.is_blocked,
        Client.first_seen_at,
        Client.last_seen_at,
    ]
    column_default_sort = [(Client.last_seen_at, True)]
    # Chat ID — чтобы найти клиента, которого заметили в журнале
    column_searchable_list = [Client.chat_id, Client.username, Client.full_name]
    column_filters = [YesNoFilter(Client.is_blocked, title="Заблокирован")]
    column_details_exclude_list = [Client.messages, Client.leads]
    form_columns = [Client.is_blocked]
    column_labels = {
        Client.chat_id: "Chat ID",
        Client.username: "Username",
        Client.full_name: "Имя",
        Client.is_blocked: "Заблокирован",
        Client.first_seen_at: "Первое сообщение",
        Client.last_seen_at: "Последнее сообщение",
    }


ROLE_CHOICES = [("user", "user — клиент"), ("assistant", "assistant — бот")]


@final
class ChatMessageAdmin(AdminView, model=ChatMessage):
    name = "Сообщение"
    name_plural = "Переписка"
    icon = "fa-solid fa-comments"
    category = CLIENTS

    # История — журнал, его только читают
    can_create = False
    can_edit = False
    can_delete = False
    column_list = [
        ChatMessage.id,
        ChatMessage.created_at,
        ChatMessage.chat_id,
        ChatMessage.role,
        ChatMessage.content,
    ]
    column_default_sort = [(ChatMessage.id, True)]
    column_searchable_list = [ChatMessage.content]
    column_filters = [
        OperationFilter(ChatMessage.chat_id, title="Chat ID"),
        ChoiceFilter(ChatMessage.role, ROLE_CHOICES, title="Роль"),
    ]
    column_formatters = {ChatMessage.content: _chat_content}
    column_details_exclude_list = [ChatMessage.client]
    column_labels = {
        ChatMessage.id: "№",
        ChatMessage.created_at: "Время",
        ChatMessage.chat_id: "Chat ID",
        ChatMessage.role: "Роль",
        ChatMessage.content: "Текст",
    }


# ---------- База знаний ----------

SORT_HINT = {"description": "Меньше — выше в списке. В списке порядок удобнее менять перетаскиванием"}
# Порядок меняют перетаскиванием, а оно работает, только когда весь раздел на одной странице
SORTABLE_LIST = "live/sortable_list.html"
SORTABLE_PAGE_SIZE = 100


@final
class ServiceAdmin(AdminView, model=Service):
    # Названия без предметной области: тот же раздел — услуги клиники или товары магазина
    name = "Позиция"
    name_plural = "Каталог и цены"
    icon = "fa-solid fa-list"
    category = KNOWLEDGE

    column_list = [Service.sort_order, Service.name, Service.price, Service.is_active]
    column_default_sort = [(Service.sort_order, False)]
    list_template = SORTABLE_LIST
    page_size = SORTABLE_PAGE_SIZE
    page_size_options = [SORTABLE_PAGE_SIZE]
    column_searchable_list = [Service.name]
    column_filters = [YesNoFilter(Service.is_active, title="Активна")]
    column_formatters = {Service.sort_order: sort_handle_cell}
    form_columns = [
        Service.name,
        Service.price,
        Service.description,
        Service.is_active,
        Service.sort_order,
    ]
    form_args = {
        "price": {"validators": [NumberRange(min=0)], "description": "В леях"},
        "description": {"description": "Необязательно: уточнение для бота"},
        "sort_order": SORT_HINT,
    }
    column_labels = {
        Service.sort_order: "Порядок",
        Service.name: "Название",
        Service.price: "Цена, лей",
        Service.description: "Описание",
        Service.is_active: "Активна",
    }


@final
class FaqAdmin(AdminView, model=FaqItem):
    name = "Вопрос"
    name_plural = "Частые вопросы"
    icon = "fa-solid fa-circle-question"
    category = KNOWLEDGE

    column_list = [FaqItem.sort_order, FaqItem.question, FaqItem.answer, FaqItem.is_active]
    column_default_sort = [(FaqItem.sort_order, False)]
    list_template = SORTABLE_LIST
    page_size = SORTABLE_PAGE_SIZE
    page_size_options = [SORTABLE_PAGE_SIZE]
    column_searchable_list = [FaqItem.question, FaqItem.answer]
    column_formatters = {FaqItem.answer: _faq_answer, FaqItem.sort_order: sort_handle_cell}
    form_columns = [FaqItem.question, FaqItem.answer, FaqItem.is_active, FaqItem.sort_order]
    form_args = {"sort_order": SORT_HINT}
    column_labels = {
        FaqItem.sort_order: "Порядок",
        FaqItem.question: "Вопрос",
        FaqItem.answer: "Ответ",
        FaqItem.is_active: "Активен",
    }


@final
class InfoSectionAdmin(AdminView, model=InfoSection):
    name = "Раздел"
    name_plural = "О компании"
    icon = "fa-solid fa-building"
    category = KNOWLEDGE

    column_list = [
        InfoSection.sort_order,
        InfoSection.title,
        InfoSection.content,
        InfoSection.is_active,
    ]
    column_default_sort = [(InfoSection.sort_order, False)]
    list_template = SORTABLE_LIST
    page_size = SORTABLE_PAGE_SIZE
    page_size_options = [SORTABLE_PAGE_SIZE]
    column_formatters = {InfoSection.content: _section_content, InfoSection.sort_order: sort_handle_cell}
    form_columns = [
        InfoSection.title,
        InfoSection.content,
        InfoSection.is_active,
        InfoSection.sort_order,
    ]
    form_args = {"sort_order": SORT_HINT}
    column_labels = {
        InfoSection.sort_order: "Порядок",
        InfoSection.title: "Заголовок",
        InfoSection.content: "Текст",
        InfoSection.is_active: "Активен",
    }


# ---------- Безопасность ----------

OUTCOME_CHOICES = [(outcome.name, outcome.value) for outcome in AuditOutcome]


@final
class AuditEventAdmin(AdminView, model=AuditEvent):
    name = "Событие"
    name_plural = "Журнал"
    icon = "fa-solid fa-shield-halved"
    category = SECURITY

    # Журнал аудита только читают; старые записи удаляет бот по сроку хранения
    can_create = False
    can_edit = False
    can_delete = False
    # Список сам проверяет новые события (HTMX)
    list_template = "live/list.html"
    live_route = NEW_AUDIT_EVENTS
    column_list = [
        AuditEvent.id,
        AuditEvent.created_at,
        AuditEvent.chat_id,
        AuditEvent.outcome,
        AuditEvent.is_suspicious,
        AuditEvent.flags,
        AuditEvent.user_text,
        AuditEvent.final_answer,
        AuditEvent.cost_micro_usd,
    ]
    column_default_sort = [(AuditEvent.id, True)]
    column_searchable_list = [AuditEvent.user_text, AuditEvent.llm_answer]
    column_filters = [
        YesNoFilter(AuditEvent.is_suspicious, title="Подозрительное"),
        ChoiceFilter(AuditEvent.outcome, OUTCOME_CHOICES, title="Итог"),
        OperationFilter(AuditEvent.chat_id, title="Chat ID"),
    ]
    column_formatters = {
        AuditEvent.user_text: _audit_text,
        AuditEvent.final_answer: _audit_answer,
        AuditEvent.flags: _audit_flags,
        AuditEvent.cost_micro_usd: _audit_cost,
        AuditEvent.outcome: _audit_outcome,
        AuditEvent.is_suspicious: _audit_suspicious,
    }
    column_formatters_detail = {
        AuditEvent.chat_id: audit_chat_id_cell,  # с кнопкой блокировки клиента
        AuditEvent.outcome: _audit_outcome,
        AuditEvent.flags: _audit_flags,
        AuditEvent.cost_micro_usd: _audit_cost,
        AuditEvent.tool_calls: _audit_tool_calls,
    }
    column_labels = {
        AuditEvent.id: "№",
        AuditEvent.created_at: "Время",
        AuditEvent.chat_id: "Chat ID",
        AuditEvent.outcome: "Итог",
        AuditEvent.is_suspicious: "Подозрительное",
        AuditEvent.flags: "Признаки атаки",
        AuditEvent.user_text: "Сообщение клиента",
        AuditEvent.llm_answer: "Ответ модели",
        AuditEvent.final_answer: "Отправлено клиенту",
        AuditEvent.model: "Модель",
        AuditEvent.stop_reason: "stop_reason",
        AuditEvent.input_tokens: "Токены на входе",
        AuditEvent.output_tokens: "Токены на выходе",
        AuditEvent.cache_read_tokens: "Токены из кэша",
        AuditEvent.cache_write_tokens: "Токены в кэш",
        AuditEvent.cost_micro_usd: "Стоимость, USD",
        AuditEvent.latency_ms: "Задержка, мс",
        AuditEvent.tool_calls: "Поиск по каталогу",
    }


# ---------- Расходы ----------

# Потолок — защита от опечатки: самые дорогие модели стоят десятки долларов за миллион токенов
PRICE_HINT = {"validators": [NumberRange(min=0, max=1000)], "description": "USD за миллион токенов, можно с запятой: 0,15"}


@final
class ModelPriceAdmin(AdminView, model=ModelPrice):
    name = "Цена модели"
    name_plural = "Цены моделей"
    icon = "fa-solid fa-tags"
    category = COSTS
    integrity_error = "Для этой модели уже есть тариф с таким значением «От длины промпта» — измените существующую строку."
    # Поле для float-колонок задаём сами: SQLAdmin 0.32 ищет для них конвертер «Numeric», а в SQLAlchemy 2.1
    # Float больше не наследует Numeric — без явного поля форма падает с NoConverterFound
    form_overrides = dict.fromkeys(
        ["input_per_mtok", "output_per_mtok", "cache_write_per_mtok", "cache_read_per_mtok"], CommaFloatField
    )

    column_list = [
        ModelPrice.model,
        ModelPrice.from_prompt_tokens,
        ModelPrice.input_per_mtok,
        ModelPrice.output_per_mtok,
        ModelPrice.cache_write_per_mtok,
        ModelPrice.cache_read_per_mtok,
    ]
    column_default_sort = [(ModelPrice.model, False), (ModelPrice.from_prompt_tokens, False)]
    column_searchable_list = [ModelPrice.model]
    form_columns = [
        ModelPrice.model,
        ModelPrice.from_prompt_tokens,
        ModelPrice.input_per_mtok,
        ModelPrice.output_per_mtok,
        ModelPrice.cache_write_per_mtok,
        ModelPrice.cache_read_per_mtok,
    ]
    form_args = {
        "model": {"description": "ID модели, как в настройках бота, например claude-haiku-5-5"},
        "from_prompt_tokens": {
            "validators": [NumberRange(min=0)],
            "description": "Тариф для промптов не короче этого числа токенов; обычно 0",
        },
        "input_per_mtok": PRICE_HINT,
        "output_per_mtok": PRICE_HINT,
        "cache_write_per_mtok": PRICE_HINT,
        "cache_read_per_mtok": PRICE_HINT,
    }
    column_labels = {
        ModelPrice.model: "Модель",
        ModelPrice.from_prompt_tokens: "От длины промпта",
        ModelPrice.input_per_mtok: "Вход",
        ModelPrice.output_per_mtok: "Выход",
        ModelPrice.cache_write_per_mtok: "Запись в кэш",
        ModelPrice.cache_read_per_mtok: "Чтение из кэша",
    }


@final
class UsageDailyAdmin(AdminView, model=UsageDaily):
    name = "День"
    name_plural = "Расход по дням"
    icon = "fa-solid fa-calendar-day"
    category = COSTS

    # Итоги пишет бот, вручную их не правят
    can_create = False
    can_edit = False
    can_delete = False
    column_list = [
        UsageDaily.day,
        UsageDaily.model,
        UsageDaily.requests,
        UsageDaily.input_tokens,
        UsageDaily.output_tokens,
        UsageDaily.cache_read_tokens,
        UsageDaily.cache_write_tokens,
        UsageDaily.cost_micro_usd,
        UsageDaily.unpriced_requests,
    ]
    column_default_sort = [(UsageDaily.day, True)]
    column_formatters = {UsageDaily.cost_micro_usd: _daily_cost}
    column_formatters_detail = {UsageDaily.cost_micro_usd: _daily_cost}
    column_labels = {
        UsageDaily.day: "День (UTC)",
        UsageDaily.model: "Модель",
        UsageDaily.requests: "Запросов",
        UsageDaily.input_tokens: "Вход",
        UsageDaily.output_tokens: "Выход",
        UsageDaily.cache_read_tokens: "Из кэша",
        UsageDaily.cache_write_tokens: "В кэш",
        UsageDaily.cost_micro_usd: "Стоимость, USD",
        UsageDaily.unpriced_requests: "Без цены",
    }


# ---------- Настройки ----------


@final
class BotConfigAdmin(AdminView, model=BotConfig):
    name = "Настройки бота"
    name_plural = "Настройки бота"
    icon = "fa-solid fa-gear"

    @override
    async def on_model_change(self, data: dict[str, object], model: object, is_created: bool, request: Request) -> None:
        # Модель без цены — это и неучтённые расходы, и чаще всего опечатка в ID, на которой бот перестанет отвечать
        name = data.get("model")
        if isinstance(name, str) and not await self._is_priced(name):
            raise ValueError(
                f"Для модели «{name}» нет цены в «Расходы → Цены моделей». Проверьте ID модели или сначала добавьте цену."
            )

    async def _is_priced(self, name: str) -> bool:
        # SQLAdmin хранит sessionmaker без параметра типа; админка всегда работает с async-движком
        maker: object = self.session_maker  # pyright: ignore[reportUnknownMemberType]
        if not isinstance(maker, async_sessionmaker):
            return True
        sessions = cast("async_sessionmaker[AsyncSession]", maker)
        async with sessions() as session:
            priced = await session.scalars(select(ModelPrice.model))
            return match_model(name, priced) is not None

    # Единственная строка: создаётся автоматически, удалять нельзя
    can_create = False
    can_delete = False
    # Профиль меняется только вместе с базой знаний (seed --reset --profile), поэтому в форме его нет
    column_list = [
        BotConfig.profile,
        BotConfig.model,
        BotConfig.max_tokens,
        BotConfig.max_len,
        BotConfig.history_limit,
        BotConfig.rate_limit,
        BotConfig.rate_window,
        BotConfig.daily_budget_usd,
        BotConfig.catalog_inline_limit,
        BotConfig.updated_at,
    ]
    form_columns = [
        BotConfig.model,
        BotConfig.max_tokens,
        BotConfig.max_len,
        BotConfig.history_limit,
        BotConfig.rate_limit,
        BotConfig.rate_window,
        BotConfig.greeting,
        BotConfig.audit_retention_days,
        BotConfig.daily_budget_usd,
        BotConfig.catalog_inline_limit,
    ]
    form_args = {
        "model": {
            "validators": [DataRequired(), Length(max=100)],
            "filters": [_strip],
            "description": (
                "ID модели Claude, например claude-haiku-5-5 или claude-sonnet-5-5. "
                "Цена модели должна быть в «Расходы → Цены моделей»"
            ),
        },
        "greeting": {"validators": [DataRequired()], "filters": [_strip]},
        "max_tokens": {
            "validators": [NumberRange(min=1, max=64000)],
            "description": "Длина ответа бота, а не вопроса клиента",
        },
        "max_len": {
            "validators": [NumberRange(min=1)],
            "description": "Символов. На более длинное сообщение бот ответит, сколько символов можно",
        },
        "history_limit": {
            "validators": [NumberRange(min=0, max=100)],
            "description": "Сколько последних сообщений передавать модели",
        },
        "rate_limit": {"validators": [NumberRange(min=1)]},
        "rate_window": {"validators": [NumberRange(min=1)], "description": "Секунд"},
        "daily_budget_usd": {
            "validators": [NumberRange(min=0, max=10_000)],
            "description": "Долларов в сутки (UTC), 0 — без лимита. После лимита бот отвечает без Claude, заявки сохраняются",
        },
        "catalog_inline_limit": {
            "validators": [NumberRange(min=0, max=1000)],
            "description": "Каталог больше этого числа активных услуг бот не читает целиком, а ищет в нём. 0 — всегда поиск",
        },
        "audit_retention_days": {
            "validators": [NumberRange(min=1, max=3650)],
            "description": "Записи журнала старше этого срока удаляются автоматически",
        },
    }
    column_labels = {
        BotConfig.profile: "Профиль бизнеса",
        BotConfig.model: "Модель",
        BotConfig.max_tokens: "Лимит токенов ответа",
        BotConfig.max_len: "Макс. длина сообщения клиента",
        BotConfig.history_limit: "Глубина истории",
        BotConfig.rate_limit: "Сообщений в окне",
        BotConfig.rate_window: "Окно rate-limit, с",
        BotConfig.greeting: "Приветствие (/start)",
        BotConfig.audit_retention_days: "Хранить журнал, дней",
        BotConfig.daily_budget_usd: "Лимит расходов в сутки, $",
        BotConfig.catalog_inline_limit: "Каталог в промпте, до позиций",
        BotConfig.updated_at: "Изменено",
    }


VIEWS = [
    LeadAdmin,
    ClientAdmin,
    ChatMessageAdmin,
    ServiceAdmin,
    FaqAdmin,
    InfoSectionAdmin,
    AuditEventAdmin,
    ModelPriceAdmin,
    UsageDailyAdmin,
    BotConfigAdmin,
]
