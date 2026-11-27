"""Admin sections: leads, clients, chat history, knowledge base, bot settings, administrators."""

# SQLAdmin declares ModelView attributes with incomplete types (bare InstrumentedAttribute and list),
# so their type in subclasses is always «partially unknown». This can only be fixed in the library
# itself, so one rule is disabled, and only in this file.
# pyright: reportUnknownVariableType=false

import asyncio
from datetime import UTC, datetime
from typing import ClassVar, cast, final, override

from markupsafe import Markup
from sqladmin import ModelView
from sqladmin.filters import BooleanFilter, OperationColumnFilter, StaticValuesFilter
from sqladmin.i18n import get_locale_display_name
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from wtforms import Field, Form, PasswordField, SelectField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp, ValidationError

from medllmbot.admin.forms import AdminForm, CommaFloatField
from medllmbot.admin.i18n import LOCALES, gettext, lazy_gettext, lazy_text
from medllmbot.admin.live import NEW_AUDIT_EVENTS, NEW_LEADS, audit_chat_id_cell, lead_status_cell, sort_handle_cell
from medllmbot.admin.passwords import hash_password
from medllmbot.costs import format_usd, match_model
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
    Order,
    OrderStatus,
    Service,
    UsageDaily,
)
from medllmbot.operator import with_operator
from medllmbot.orders import normalize_number, order_key
from medllmbot.security import FLAG_LABELS


def _sentences(*sentences: str) -> str:
    """A form hint of several sentences: each is its own msgid, joined in the request's language."""
    return lazy_text(lambda: " ".join(map(str, sentences)))


CLIENTS = lazy_gettext("Clients")
KNOWLEDGE = lazy_gettext("Knowledge base")
SECURITY = lazy_gettext("Security")
COSTS = lazy_gettext("Costs")
# Sidebar icons of the categories. SQLAdmin takes a category icon from its first view, so every view of a category
# sets the same one
CLIENTS_ICON = "fa-solid fa-users"
KNOWLEDGE_ICON = "fa-solid fa-book-open"
SECURITY_ICON = "fa-solid fa-shield-halved"
COSTS_ICON = "fa-solid fa-coins"


# A long text in a list cell wraps (.cell-text in templates/sqladmin/layout.html). SQLAdmin's tables do not wrap,
# so a text on one line pushed the last columns out of the card
TEXT_CELL = Markup('<span class="cell-text">{}</span>')


def _text_cell(text: str | None, limit: int = 80) -> Markup | str:
    """A list cell with a long text, cut to `limit` characters. An empty text stays empty and takes no width."""
    if not text:
        return ""
    return TEXT_CELL.format(text if len(text) <= limit else text[: limit - 1] + "…")


# List column formatters: (model, attribute name) -> displayed text
def _lead_message(model: Lead, _attr: object) -> Markup | str:
    return _text_cell(model.message)


def _chat_content(model: ChatMessage, _attr: object) -> Markup | str:
    return _text_cell(model.content, 120)


def _faq_answer(model: FaqItem, _attr: object) -> Markup | str:
    return _text_cell(model.answer)


def _section_content(model: InfoSection, _attr: object) -> Markup | str:
    return _text_cell(model.content)


def _audit_text(model: AuditEvent, _attr: object) -> Markup | str:
    return _text_cell(model.user_text)


def _audit_answer(model: AuditEvent, _attr: object) -> Markup | str:
    return _text_cell(model.final_answer)


def _audit_cost(model: AuditEvent, _attr: object) -> str:
    return format_usd(model.cost_micro_usd)


def _daily_cost(model: UsageDaily, _attr: object) -> str:
    return format_usd(model.cost_micro_usd)


# SQLAdmin shows an Enum's member name (NEW, ANSWERED); show the translated label instead, as in filters and help
def _lead_status(model: Lead, _attr: object) -> str:
    return gettext(model.status.value)


def _audit_outcome(model: AuditEvent, _attr: object) -> str:
    return gettext(model.outcome.value)


def _audit_flags(model: AuditEvent, _attr: object) -> str:
    return ", ".join(gettext(FLAG_LABELS.get(code, code)) for code in model.flags.split(",") if code)


def _audit_flags_cell(model: AuditEvent, attr: object) -> Markup | str:
    return _text_cell(_audit_flags(model, attr), 200)


# A yellow badge instead of a check mark and cross: a red cross on ordinary messages looked like an alarm
SUSPICIOUS_BADGE = Markup('<span class="badge bg-warning-lt">⚠ {}</span>')


def _audit_suspicious(model: AuditEvent, _attr: object) -> Markup | str:
    return SUSPICIOUS_BADGE.format(gettext("Yes")) if model.is_suspicious else ""


def _audit_tool_calls(model: AuditEvent, _attr: object) -> Markup | str:
    """Each tool call on a new line (the text is escaped)."""
    if not model.tool_calls:
        return ""
    return Markup("<br>").join(model.tool_calls.splitlines())


# ---------- Filters with translated labels ----------
# SQLAdmin hardcodes the «All / Yes / No» options and the operation names in English, outside its catalogs.


@final
class YesNoFilter(BooleanFilter):
    @override
    async def lookups(self, request: Request, model: object, run_query: object) -> list[tuple[str, str]]:
        return [("all", gettext("All")), ("true", gettext("Yes")), ("false", gettext("No"))]


@final
class ChoiceFilter(StaticValuesFilter):
    @override
    async def lookups(self, request: Request, model: object, run_query: object) -> list[tuple[str, str]]:
        return [("__all", gettext("All")), *self.values]


OPERATION_LABELS: dict[str, str] = {
    "equals": lazy_gettext("Equals"),
    "contains": lazy_gettext("Contains"),
    "starts_with": lazy_gettext("Starts with"),
    "ends_with": lazy_gettext("Ends with"),
    "greater_than": lazy_gettext("Greater than"),
    "less_than": lazy_gettext("Less than"),
}


@final
class OperationFilter(OperationColumnFilter):
    @override
    def get_operation_options(self, column_obj: object) -> list[tuple[str, str]]:
        return [(key, OPERATION_LABELS.get(key, label)) for key, label in super().get_operation_options(column_obj)]


# ---------- Clients ----------

# The DB stores the status name (NEW); the admin shows the label («New»), translated
STATUS_CHOICES: list[tuple[str, str]] = [(status.name, lazy_gettext(status.value)) for status in LeadStatus]


def _status_name(value: LeadStatus | str) -> str:
    return value.name if isinstance(value, LeadStatus) else str(value)


class AdminView(ModelView):
    """Common base for sections: forms with translated errors (forms.AdminForm)."""

    form_base_class: ClassVar[type[Form]] = AdminForm
    # SQLAdmin shows the exception text above the form: without catching it, that would be raw SQL in English
    integrity_error: ClassVar[str] = lazy_gettext(
        "Record not saved: it already exists or the values break database constraints."
    )

    @override
    async def insert_model(self, request: Request, data: dict[str, object]) -> object:
        try:
            # SQLAdmin left insert_model untyped (dict without parameters, returns Any)
            obj = cast(object, await super().insert_model(request, data))  # pyright: ignore[reportUnknownMemberType]
        except IntegrityError as exc:
            raise ValueError(str(self.integrity_error)) from exc
        return obj

    @override
    async def update_model(self, request: Request, pk: str, data: dict[str, object]) -> object:
        try:
            obj = cast(object, await super().update_model(request, pk, data))  # pyright: ignore[reportUnknownMemberType]
        except IntegrityError as exc:
            raise ValueError(str(self.integrity_error)) from exc
        return obj

    @override
    def search_placeholder(self) -> str:
        # SQLAdmin joins the labels with str.join, which does not accept lazy translations
        labels: dict[str, object] = self._column_labels
        return ", ".join(str(labels.get(field, field)) for field in self._search_fields)

    def async_sessions(self) -> async_sessionmaker[AsyncSession] | None:
        """Session factory for custom checks in on_model_change; None if the engine is not async (never in admin)."""
        # SQLAdmin stores sessionmaker without a type parameter; the admin always uses an async engine
        maker: object = self.session_maker  # pyright: ignore[reportUnknownMemberType]
        if not isinstance(maker, async_sessionmaker):
            return None
        return cast("async_sessionmaker[AsyncSession]", maker)


def _strip(value: object) -> object:
    """Form field filter: leading and trailing spaces are not saved, so a whitespace-only string becomes empty."""
    return value.strip() if isinstance(value, str) else value


@final
class LeadAdmin(AdminView, model=Lead):
    name = lazy_gettext("Lead")
    name_plural = lazy_gettext("Leads")
    icon = "fa-solid fa-phone"
    category = CLIENTS
    category_icon = CLIENTS_ICON

    can_create = False  # leads are created by the bot
    # The list checks for new leads itself (HTMX); the status changes right in the row
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
    column_filters = [ChoiceFilter(Lead.status, STATUS_CHOICES, title=lazy_gettext("Status"))]
    column_formatters = {Lead.message: _lead_message, Lead.status: lead_status_cell}
    column_formatters_detail = {Lead.status: _lead_status}
    column_details_exclude_list = [Lead.client]
    form_columns = [Lead.status, Lead.admin_note]
    form_overrides = {"status": SelectField}
    form_args = {"status": {"choices": STATUS_CHOICES, "coerce": _status_name}}
    column_labels = {
        Lead.id: lazy_gettext("No."),
        Lead.created_at: lazy_gettext("Created"),
        Lead.updated_at: lazy_gettext("Updated"),
        Lead.status: lazy_gettext("Status"),
        Lead.client_name: lazy_gettext("Client"),
        Lead.phone: lazy_gettext("Phone"),
        Lead.message: lazy_gettext("Message"),
        Lead.admin_note: lazy_gettext("Administrator note"),
        Lead.chat_id: "Chat ID",
    }


def _operator_until(model: Client, _attr: object) -> str:
    """Operator period while it has not expired; an expired period means the bot is back."""
    until = model.operator_until
    if until is None or not with_operator(until, datetime.now(UTC).replace(tzinfo=None)):
        return "—"
    return f"{until:%d.%m.%Y %H:%M} UTC"


@final
class ClientAdmin(AdminView, model=Client):
    name = lazy_gettext("Client")
    name_plural = lazy_gettext("Clients")
    icon = "fa-solid fa-user"
    category = CLIENTS
    category_icon = CLIENTS_ICON

    can_create = False  # clients appear when they message the bot
    can_delete = False  # deleting would erase the history and leads
    column_list = [
        Client.chat_id,
        Client.username,
        Client.full_name,
        Client.is_blocked,
        Client.operator_until,
        Client.first_seen_at,
        Client.last_seen_at,
    ]
    column_default_sort = [(Client.last_seen_at, True)]
    # Chat ID, to find a client spotted in the audit log
    column_searchable_list = [Client.chat_id, Client.username, Client.full_name]
    column_filters = [YesNoFilter(Client.is_blocked, title=lazy_gettext("Blocked"))]
    column_details_exclude_list = [Client.messages, Client.leads]
    form_columns = [Client.is_blocked]
    column_formatters = {Client.operator_until: _operator_until}
    column_formatters_detail = {Client.operator_until: _operator_until}
    column_labels = {
        Client.chat_id: "Chat ID",
        Client.username: "Username",
        Client.full_name: lazy_gettext("Full name"),
        Client.is_blocked: lazy_gettext("Blocked"),
        Client.operator_until: lazy_gettext("With operator until"),
        Client.first_seen_at: lazy_gettext("First message"),
        Client.last_seen_at: lazy_gettext("Last message"),
    }


ORDER_STATUS_CHOICES: list[tuple[str, str]] = [(status.name, lazy_gettext(status.value)) for status in OrderStatus]


def _order_status_name(value: OrderStatus | str) -> str:
    return value.name if isinstance(value, OrderStatus) else str(value)


def _order_status(model: Order, _attr: object) -> str:
    return gettext(model.status.value)


def _order_number(value: object) -> object:
    """Order number in the form the bot searches for: «de 10023» → «DE-10023»."""
    if not isinstance(value, str):
        return value
    return normalize_number(value) or value.strip()


def _valid_order_number(_form: Form, field: Field) -> None:
    # A number the bot cannot find: the client would get «не найден» and waste attempts
    value: object = field.data  # pyright: ignore[reportAny]  # WTForms does not type field data
    if not isinstance(value, str) or normalize_number(value) != value:
        raise ValidationError(
            gettext("Order number: Latin letters, digits and hyphens, 3 to 32 characters, for example DE-10023.")
        )


@final
class OrderAdmin(AdminView, model=Order):
    name = lazy_gettext("Order")
    name_plural = lazy_gettext("Orders")
    icon = "fa-solid fa-box"
    category = CLIENTS
    category_icon = CLIENTS_ICON
    integrity_error = lazy_gettext("An order with this number already exists: open it and edit it instead.")

    column_list = [
        Order.number,
        Order.created_at,
        Order.status,
        Order.customer_name,
        Order.phone,
        Order.total,
        Order.delivery_date,
    ]
    column_default_sort = [(Order.created_at, True)]
    column_searchable_list = [Order.number, Order.customer_name, Order.phone]
    column_filters = [ChoiceFilter(Order.status, ORDER_STATUS_CHOICES, title=lazy_gettext("Status"))]
    column_formatters = {Order.status: _order_status}
    column_formatters_detail = {Order.status: _order_status}
    form_columns = [
        Order.number,
        Order.status,
        Order.customer_name,
        Order.phone,
        Order.items,
        Order.total,
        Order.delivery_date,
    ]
    form_overrides = {"status": SelectField}
    form_args = {
        "number": {
            "validators": [DataRequired(), _valid_order_number],
            "filters": [_order_number],
            "description": _sentences(
                lazy_gettext("As on the receipt, for example DE-10023."),
                lazy_gettext("The customer gives it to the bot"),
            ),
        },
        "status": {"choices": ORDER_STATUS_CHOICES, "coerce": _order_status_name},
        "customer_name": {"validators": [DataRequired()], "filters": [_strip]},
        "phone": {
            "validators": [DataRequired(), Length(max=32)],
            "filters": [_strip],
            "description": lazy_gettext(
                "The bot shows the status only if the customer gives the last 4 digits of this number"
            ),
        },
        "items": {"validators": [DataRequired()], "filters": [_strip]},
        "total": {"validators": [NumberRange(min=0, max=10**9)], "description": lazy_gettext("Lei, a whole number")},
        "delivery_date": {
            "description": lazy_gettext("When delivery or pickup is expected; empty means the date is not known yet")
        },
    }
    column_labels = {
        Order.id: "ID",
        Order.number: lazy_gettext("Number"),
        Order.created_at: lazy_gettext("Placed"),
        Order.updated_at: lazy_gettext("Last updated"),
        Order.status: lazy_gettext("Status"),
        Order.customer_name: lazy_gettext("Customer"),
        Order.phone: lazy_gettext("Phone"),
        Order.items: lazy_gettext("Items"),
        Order.total: lazy_gettext("Total, lei"),
        Order.delivery_date: lazy_gettext("Delivery date"),
    }

    @override
    async def on_model_change(self, data: dict[str, object], model: object, is_created: bool, request: Request) -> None:
        # The bot treats «DE10023» and «DE-10023» as one order; a second such number would make the answer random
        number = data.get("number")
        own_id = None if is_created or not isinstance(model, Order) else model.id
        if isinstance(number, str) and await self._number_taken(number, own_id):
            raise ValueError(str(self.integrity_error))

    async def _number_taken(self, number: str, own_id: int | None) -> bool:
        sessions = self.async_sessions()
        if sessions is None:
            return False
        async with sessions() as session:
            other = await session.scalar(
                select(Order.id).where(
                    func.replace(func.upper(Order.number), "-", "") == order_key(number), Order.id != own_id
                )
            )
        return other is not None


ROLE_CHOICES = [("user", lazy_gettext("user — client")), ("assistant", lazy_gettext("assistant — bot"))]


@final
class ChatMessageAdmin(AdminView, model=ChatMessage):
    name = lazy_gettext("Message")
    name_plural = lazy_gettext("Chat history")
    icon = "fa-solid fa-comments"
    category = CLIENTS
    category_icon = CLIENTS_ICON

    # The history is a log; it is read-only
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
        ChoiceFilter(ChatMessage.role, ROLE_CHOICES, title=lazy_gettext("Role")),
    ]
    column_formatters = {ChatMessage.content: _chat_content}
    column_details_exclude_list = [ChatMessage.client]
    column_labels = {
        ChatMessage.id: lazy_gettext("No."),
        ChatMessage.created_at: lazy_gettext("Time"),
        ChatMessage.chat_id: "Chat ID",
        ChatMessage.role: lazy_gettext("Role"),
        ChatMessage.content: lazy_gettext("Text"),
    }


# ---------- Knowledge base ----------

SORT_HINT = {
    "description": _sentences(
        lazy_gettext("Lower numbers come first."),
        lazy_gettext("In the list it is easier to change the order by dragging"),
    )
}
# The order is changed by dragging, which only works when the whole section is on one page
SORTABLE_LIST = "live/sortable_list.html"
SORTABLE_PAGE_SIZE = 100


@final
class ServiceAdmin(AdminView, model=Service):
    # Domain-neutral names: the same section holds clinic services or shop products
    name = lazy_gettext("Item")
    name_plural = lazy_gettext("Catalog and prices")
    icon = "fa-solid fa-list"
    category = KNOWLEDGE
    category_icon = KNOWLEDGE_ICON

    column_list = [Service.sort_order, Service.name, Service.price, Service.is_active]
    column_default_sort = [(Service.sort_order, False)]
    list_template = SORTABLE_LIST
    page_size = SORTABLE_PAGE_SIZE
    page_size_options = [SORTABLE_PAGE_SIZE]
    column_searchable_list = [Service.name]
    column_filters = [YesNoFilter(Service.is_active, title=lazy_gettext("Active"))]
    column_formatters = {Service.sort_order: sort_handle_cell}
    form_columns = [
        Service.name,
        Service.price,
        Service.description,
        Service.is_active,
        Service.sort_order,
    ]
    form_args = {
        "price": {"validators": [NumberRange(min=0)], "description": lazy_gettext("In lei")},
        "description": {"description": lazy_gettext("Optional: a note for the bot")},
        "sort_order": SORT_HINT,
    }
    column_labels = {
        Service.sort_order: lazy_gettext("Sort order"),
        Service.name: lazy_gettext("Name"),
        Service.price: lazy_gettext("Price, lei"),
        Service.description: lazy_gettext("Description"),
        Service.is_active: lazy_gettext("Active"),
    }


@final
class FaqAdmin(AdminView, model=FaqItem):
    name = lazy_gettext("Question")
    name_plural = lazy_gettext("FAQ")
    icon = "fa-solid fa-circle-question"
    category = KNOWLEDGE
    category_icon = KNOWLEDGE_ICON

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
        FaqItem.sort_order: lazy_gettext("Sort order"),
        FaqItem.question: lazy_gettext("Question"),
        FaqItem.answer: lazy_gettext("Answer"),
        FaqItem.is_active: lazy_gettext("Enabled"),
    }


@final
class InfoSectionAdmin(AdminView, model=InfoSection):
    name = lazy_gettext("Section")
    name_plural = lazy_gettext("About the business")
    icon = "fa-solid fa-building"
    category = KNOWLEDGE
    category_icon = KNOWLEDGE_ICON

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
        InfoSection.sort_order: lazy_gettext("Sort order"),
        InfoSection.title: lazy_gettext("Title"),
        InfoSection.content: lazy_gettext("Text"),
        InfoSection.is_active: lazy_gettext("Enabled"),
    }


# ---------- Security ----------

OUTCOME_CHOICES: list[tuple[str, str]] = [(outcome.name, lazy_gettext(outcome.value)) for outcome in AuditOutcome]


@final
class AuditEventAdmin(AdminView, model=AuditEvent):
    name = lazy_gettext("Event")
    name_plural = lazy_gettext("Log")
    icon = "fa-solid fa-clipboard-list"
    category = SECURITY
    category_icon = SECURITY_ICON

    # The audit log is read-only; the bot deletes old records per the retention period
    can_create = False
    can_edit = False
    can_delete = False
    # The list checks for new events itself (HTMX)
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
        YesNoFilter(AuditEvent.is_suspicious, title=lazy_gettext("Suspicious")),
        ChoiceFilter(AuditEvent.outcome, OUTCOME_CHOICES, title=lazy_gettext("Outcome")),
        OperationFilter(AuditEvent.chat_id, title="Chat ID"),
    ]
    column_formatters = {
        AuditEvent.user_text: _audit_text,
        AuditEvent.final_answer: _audit_answer,
        AuditEvent.flags: _audit_flags_cell,
        AuditEvent.cost_micro_usd: _audit_cost,
        AuditEvent.outcome: _audit_outcome,
        AuditEvent.is_suspicious: _audit_suspicious,
    }
    column_formatters_detail = {
        AuditEvent.chat_id: audit_chat_id_cell,  # with a button to block the client
        AuditEvent.outcome: _audit_outcome,
        AuditEvent.flags: _audit_flags,
        AuditEvent.cost_micro_usd: _audit_cost,
        AuditEvent.tool_calls: _audit_tool_calls,
    }
    column_labels = {
        AuditEvent.id: lazy_gettext("No."),
        AuditEvent.created_at: lazy_gettext("Time"),
        AuditEvent.chat_id: "Chat ID",
        AuditEvent.outcome: lazy_gettext("Outcome"),
        AuditEvent.is_suspicious: lazy_gettext("Suspicious"),
        AuditEvent.flags: lazy_gettext("Attack signs"),
        AuditEvent.user_text: lazy_gettext("Client message"),
        AuditEvent.llm_answer: lazy_gettext("Model answer"),
        AuditEvent.final_answer: lazy_gettext("Sent to client"),
        AuditEvent.model: lazy_gettext("Model"),
        AuditEvent.stop_reason: "stop_reason",
        AuditEvent.input_tokens: lazy_gettext("Input tokens"),
        AuditEvent.output_tokens: lazy_gettext("Output tokens"),
        AuditEvent.cache_read_tokens: lazy_gettext("Cache read tokens"),
        AuditEvent.cache_write_tokens: lazy_gettext("Cache write tokens"),
        AuditEvent.cost_micro_usd: lazy_gettext("Cost, USD"),
        AuditEvent.latency_ms: lazy_gettext("Latency, ms"),
        AuditEvent.tool_calls: lazy_gettext("What the bot searched for"),
    }


# ---------- Costs ----------

# The cap guards against typos: the most expensive models cost tens of dollars per million tokens
PRICE_HINT = {
    "validators": [NumberRange(min=0, max=1000)],
    "description": lazy_gettext("USD per million tokens, a decimal comma is fine: 0,15"),
}


@final
class ModelPriceAdmin(AdminView, model=ModelPrice):
    name = lazy_gettext("Model price")
    name_plural = lazy_gettext("Model prices")
    icon = "fa-solid fa-tags"
    category = COSTS
    category_icon = COSTS_ICON
    integrity_error = lazy_gettext(
        "This model already has a rate with this «From prompt length» value: edit the existing row."
    )
    # The field for float columns is set explicitly: SQLAdmin 0.32 looks for a «Numeric» converter for them,
    # but in SQLAlchemy 2.1 Float no longer inherits Numeric; without an explicit field the form fails
    # with NoConverterFound
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
        "model": {"description": lazy_gettext("Model ID as in the bot settings, for example claude-haiku-5-5")},
        "from_prompt_tokens": {
            "validators": [NumberRange(min=0)],
            "description": lazy_gettext("Rate for prompts of at least this many tokens; usually 0"),
        },
        "input_per_mtok": PRICE_HINT,
        "output_per_mtok": PRICE_HINT,
        "cache_write_per_mtok": PRICE_HINT,
        "cache_read_per_mtok": PRICE_HINT,
    }
    column_labels = {
        ModelPrice.model: lazy_gettext("Model"),
        ModelPrice.from_prompt_tokens: lazy_gettext("From prompt length"),
        ModelPrice.input_per_mtok: lazy_gettext("Input"),
        ModelPrice.output_per_mtok: lazy_gettext("Output"),
        ModelPrice.cache_write_per_mtok: lazy_gettext("Writing to cache"),
        ModelPrice.cache_read_per_mtok: lazy_gettext("Reading from cache"),
    }


@final
class UsageDailyAdmin(AdminView, model=UsageDaily):
    name = lazy_gettext("Day")
    name_plural = lazy_gettext("Daily spend")
    icon = "fa-solid fa-calendar-day"
    category = COSTS
    category_icon = COSTS_ICON

    # The bot writes the totals; they are not edited by hand
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
        UsageDaily.day: lazy_gettext("Day (UTC)"),
        UsageDaily.model: lazy_gettext("Model"),
        UsageDaily.requests: lazy_gettext("Requests"),
        UsageDaily.input_tokens: lazy_gettext("Input"),
        UsageDaily.output_tokens: lazy_gettext("Output"),
        UsageDaily.cache_read_tokens: lazy_gettext("Cache read"),
        UsageDaily.cache_write_tokens: lazy_gettext("Cache write"),
        UsageDaily.cost_micro_usd: lazy_gettext("Cost, USD"),
        UsageDaily.unpriced_requests: lazy_gettext("Unpriced"),
    }


# ---------- Settings ----------


# Languages by their own names, like in the language switcher: «Русский», «Română», «English»
ADMIN_LANGUAGE_CHOICES = [(code, get_locale_display_name(code)) for code in LOCALES]


def _admin_language(model: BotConfig, _attr: object) -> str:
    return get_locale_display_name(model.admin_language)


def _profile(model: BotConfig, _attr: object) -> str:
    return gettext(model.profile.value)


@final
class BotConfigAdmin(AdminView, model=BotConfig):
    name = lazy_gettext("Bot settings")
    name_plural = lazy_gettext("Bot settings")
    icon = "fa-solid fa-gear"

    @override
    async def on_model_change(self, data: dict[str, object], model: object, is_created: bool, request: Request) -> None:
        # A model without a price means untracked spending and, most often, a typo in the ID that stops the bot
        # from answering
        name = data.get("model")
        if isinstance(name, str) and not await self._is_priced(name):
            no_price = gettext("No price for model «%(model)s» in «Costs → Model prices».") % {"model": name}
            raise ValueError(" ".join((no_price, gettext("Check the model ID or add its price first."))))

    async def _is_priced(self, name: str) -> bool:
        sessions = self.async_sessions()
        if sessions is None:
            return True
        async with sessions() as session:
            priced = await session.scalars(select(ModelPrice.model))
            return match_model(name, priced) is not None

    # The single row: created automatically, cannot be deleted
    can_create = False
    can_delete = False
    # The profile changes only together with the knowledge base (seed --reset --profile), so it is not in the form
    column_formatters = {BotConfig.profile: _profile, BotConfig.admin_language: _admin_language}
    column_formatters_detail = {BotConfig.profile: _profile, BotConfig.admin_language: _admin_language}
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
        BotConfig.operator_hours,
        BotConfig.admin_language,
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
        BotConfig.greeting_ro,
        BotConfig.audit_retention_days,
        BotConfig.daily_budget_usd,
        BotConfig.catalog_inline_limit,
        BotConfig.operator_hours,
        BotConfig.admin_language,
    ]
    form_overrides = {"admin_language": SelectField}
    form_args = {
        "model": {
            "validators": [DataRequired(), Length(max=100)],
            "filters": [_strip],
            "description": _sentences(
                lazy_gettext("Claude model ID, for example claude-haiku-5-5 or claude-sonnet-5-5."),
                lazy_gettext("The model needs a price in «Costs → Model prices»"),
            ),
        },
        "greeting": {
            "validators": [DataRequired()],
            "filters": [_strip],
            "description": _sentences(
                lazy_gettext("In Russian."),
                lazy_gettext(
                    "Clients whose Telegram is in neither Russian nor Romanian get it together with the Romanian one"
                ),
            ),
        },
        "greeting_ro": {
            "filters": [_strip],
            "description": _sentences(
                lazy_gettext("For clients whose Telegram is in Romanian."),
                lazy_gettext("Empty: everyone gets the greeting above"),
            ),
        },
        "max_tokens": {
            "validators": [NumberRange(min=1, max=64000)],
            "description": lazy_gettext("Length of the bot's answer, not of the client's question"),
        },
        "max_len": {
            "validators": [NumberRange(min=1)],
            "description": _sentences(
                lazy_gettext("Characters."),
                lazy_gettext("To a longer message the bot replies how many characters are allowed"),
            ),
        },
        "history_limit": {
            "validators": [NumberRange(min=0, max=100)],
            "description": lazy_gettext("How many recent messages to pass to the model"),
        },
        "rate_limit": {"validators": [NumberRange(min=1)]},
        "rate_window": {"validators": [NumberRange(min=1)], "description": lazy_gettext("Seconds")},
        "daily_budget_usd": {
            "validators": [NumberRange(min=0, max=10_000)],
            "description": _sentences(
                lazy_gettext("Dollars per day (UTC), 0 means no limit."),
                lazy_gettext("Past the limit the bot answers without Claude; leads are still saved"),
            ),
        },
        "catalog_inline_limit": {
            "validators": [NumberRange(min=0, max=1000)],
            "description": _sentences(
                lazy_gettext("A catalog with more active items than this is searched instead of read whole."),
                lazy_gettext("0 means always search"),
            ),
        },
        "admin_language": {
            "choices": ADMIN_LANGUAGE_CHOICES,
            "description": lazy_gettext(
                "Language of the bot's Telegram messages to you: new leads, alerts, the live operator. "
                "The admin panel's own language is chosen with the switcher in the menu"
            ),
        },
        "operator_hours": {
            "validators": [NumberRange(min=1, max=720)],
            "description": _sentences(
                lazy_gettext(
                    "For this many hours the bot stays silent after a client asks for a human, if the operator has not replied."
                ),
                lazy_gettext("Each operator reply extends the period"),
            ),
        },
        "audit_retention_days": {
            "validators": [NumberRange(min=1, max=3650)],
            "description": lazy_gettext("Log records older than this are deleted automatically"),
        },
    }
    column_labels = {
        BotConfig.profile: lazy_gettext("Business profile"),
        BotConfig.model: lazy_gettext("Model"),
        BotConfig.max_tokens: lazy_gettext("Answer token limit"),
        BotConfig.max_len: lazy_gettext("Max client message length"),
        BotConfig.history_limit: lazy_gettext("History depth"),
        BotConfig.rate_limit: lazy_gettext("Messages per window"),
        BotConfig.rate_window: lazy_gettext("Rate-limit window, s"),
        BotConfig.greeting: lazy_gettext("Greeting (/start)"),
        BotConfig.greeting_ro: lazy_gettext("Greeting in Romanian"),
        BotConfig.audit_retention_days: lazy_gettext("Keep log, days"),
        BotConfig.daily_budget_usd: lazy_gettext("Daily spend limit, $"),
        BotConfig.catalog_inline_limit: lazy_gettext("Catalog in prompt, up to items"),
        BotConfig.operator_hours: lazy_gettext("Operator handles chat, hours"),
        BotConfig.admin_language: lazy_gettext("Administrator language"),
        BotConfig.updated_at: lazy_gettext("Last changed"),
    }


# ---------- Administrators ----------

# A light check: the login is compared as typed, a mistyped address simply does not log in
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_PASSWORD_LENGTH = 12


def _email(value: object) -> object:
    return AdminUser.normalize_email(value) if isinstance(value, str) else value


@final
class AdminUserAdmin(AdminView, model=AdminUser):
    """Extra accounts with their own email and password. The owner's login from .env is not listed here."""

    name = lazy_gettext("Administrator")
    name_plural = lazy_gettext("Administrators")
    icon = "fa-solid fa-user-shield"
    integrity_error = lazy_gettext("An administrator with this email already exists.")
    can_export = False  # the export would include the password hashes

    column_list = [AdminUser.email, AdminUser.is_active, AdminUser.created_at, AdminUser.last_login_at]
    column_details_list = [AdminUser.email, AdminUser.is_active, AdminUser.created_at, AdminUser.last_login_at]
    column_default_sort = [(AdminUser.email, False)]
    column_searchable_list = [AdminUser.email]
    column_filters = [YesNoFilter(AdminUser.is_active, title=lazy_gettext("Active"))]
    form_columns = [AdminUser.email, AdminUser.is_active]
    # A new account is active; whether it is active is chosen when editing. The password field is added in scaffold_form
    form_create_rules = ["email", "password"]
    form_edit_rules = ["email", "is_active", "password"]
    form_args = {
        "email": {
            "validators": [
                DataRequired(),
                Length(max=254),
                Regexp(EMAIL_PATTERN, message=lazy_gettext("Enter an email address, for example name@example.com.")),
            ],
            "filters": [_email],
            "description": lazy_gettext("The login for the admin panel, in any case"),
            "render_kw": {"autocomplete": "off"},
        },
        "is_active": {
            "description": lazy_gettext("Off: the administrator cannot log in, and their open sessions end at once")
        },
    }
    column_labels = {
        AdminUser.email: lazy_gettext("Email"),
        AdminUser.is_active: lazy_gettext("Active"),
        AdminUser.created_at: lazy_gettext("Created"),
        AdminUser.last_login_at: lazy_gettext("Last login"),
    }

    @override
    async def scaffold_form(self, rules: list[str] | None = None) -> type[Form]:
        model_form = await super().scaffold_form(rules)

        @final
        class AdminUserForm(model_form):
            # Not a model column: on_model_change turns it into password_hash. «new-password» stops the browser
            # from filling in the saved password of the person editing the form
            password = PasswordField(
                lazy_gettext("Password"),
                validators=[Optional(), Length(min=MIN_PASSWORD_LENGTH, max=128)],
                description=_sentences(
                    lazy_gettext("At least 12 characters."),
                    lazy_gettext("When editing, leave it empty to keep the current password"),
                ),
                render_kw={"autocomplete": "new-password"},
            )

        return AdminUserForm

    @override
    async def on_model_change(self, data: dict[str, object], model: object, is_created: bool, request: Request) -> None:
        # The password itself is never saved: only its scrypt hash. Empty when editing means «keep the current one»
        password = data.pop("password", None)
        if isinstance(password, str) and password:
            data["password_hash"] = await asyncio.to_thread(hash_password, password)
        elif is_created:
            raise ValueError(gettext("Set a password for the new administrator."))


VIEWS = [
    LeadAdmin,
    OrderAdmin,
    ClientAdmin,
    ChatMessageAdmin,
    ServiceAdmin,
    FaqAdmin,
    InfoSectionAdmin,
    AuditEventAdmin,
    ModelPriceAdmin,
    UsageDailyAdmin,
    BotConfigAdmin,
    AdminUserAdmin,
]
