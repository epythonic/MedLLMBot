"""Инструменты модели: поиск по каталогу, когда он не помещается в промпт, и статус заказа магазина."""

import logging
from collections.abc import Sequence
from functools import cache

from anthropic.types import ToolParam
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from medllmbot.db.models import BusinessProfile
from medllmbot.db.repository import CatalogPage, Repository
from medllmbot.llm import ToolOutput, ToolRunner
from medllmbot.orders import LookupLimits, OrderBackend, OrderInfo, normalize_number, order_key, phone_matches
from medllmbot.profiles import PROFILES
from medllmbot.prompts import service_line

log = logging.getLogger(__name__)

SEARCH_LIMIT = 10  # позиций в одном ответе инструмента: больше клиенту в чате всё равно не перечислить
MAX_PRICE = 10**9  # верхняя граница фильтра цены: защита от переполнения в SQLite

SEARCH_CATALOG_NAME = "search_catalog"
# Итог поиска в журнале: «search_catalog «чистка» → найдено 2». По нему аналитика находит пустые поиски
FOUND = "→ найдено"


@cache
def search_catalog_tool(kind: BusinessProfile) -> ToolParam:
    """Описание инструмента для API. Примеры запросов — из профиля: модель ищет словами каталога."""
    examples = PROFILES[kind].search_examples
    return {
        "name": SEARCH_CATALOG_NAME,
        "description": (
            "Поиск по каталогу с ценами. Вызывай, когда клиент спрашивает о позиции каталога, её цене "
            "или о том, что есть в каталоге. Возвращает до 10 позиций в виде «название — цена лей (описание)» "
            "и сколько нашлось всего. Если ничего не нашлось, попробуй другие слова или синоним."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        f"Ключевые слова из названия на русском, как в каталоге: {examples}. "
                        "Без вопросительных слов и цен. Пустая строка — весь каталог по порядку."
                    ),
                },
                "min_price": {"type": "integer", "minimum": 0, "description": "Цена от, в леях."},
                "max_price": {"type": "integer", "minimum": 0, "description": "Цена до, в леях."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


class CatalogQuery(BaseModel):
    """Параметры search_catalog. Модель может прислать что угодно, поэтому всё проверяется."""

    query: str = Field(default="", max_length=200)
    min_price: int | None = Field(default=None, ge=0, le=MAX_PRICE)
    max_price: int | None = Field(default=None, ge=0, le=MAX_PRICE)

    def describe(self) -> str:
        """Запрос для журнала: «чистка», 500–1000 лей. Одной строкой: в журнале строка на вызов."""
        text = f"«{' '.join(self.query.split())}»"
        if self.min_price is not None or self.max_price is not None:
            low = self.min_price if self.min_price is not None else 0
            text += f", {low}–{self.max_price} лей" if self.max_price is not None else f", от {low} лей"
        return text


def render_page(page: CatalogPage) -> str:
    """Результат поиска для модели."""
    if not page.services:
        return "Ничего не нашлось. Попробуй другие слова, синоним или пустой запрос — весь каталог."
    shown = len(page.services)
    header = f"Найдено: {page.total}" + (f", показаны первые {shown}" if page.total > shown else "")
    return header + "\n" + "\n".join(service_line(service) for service in page.services)


class CatalogTools:
    """Инструменты для `ClinicLLM.ask`: модель ищет в каталоге сама."""

    repo: Repository
    kind: BusinessProfile

    def __init__(self, repo: Repository, kind: BusinessProfile = BusinessProfile.CLINIC) -> None:
        self.repo = repo
        self.kind = kind

    @property
    def definitions(self) -> Sequence[ToolParam]:
        return (search_catalog_tool(self.kind),)

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput:
        """Выполняет вызов модели. Ошибки возвращаются модели текстом: она ответит клиенту сама."""
        if name != SEARCH_CATALOG_NAME:
            return ToolOutput(f"Инструмента {name} нет.", f"{name}: неизвестный инструмент", is_error=True)
        try:
            params = CatalogQuery.model_validate(tool_input)
        except ValidationError as error:
            fields = ", ".join(str(e["loc"][0]) for e in error.errors() if e["loc"])
            return ToolOutput(f"Неверные параметры: {fields}.", f"{name}: неверные параметры ({fields})", is_error=True)
        try:
            page = await self.repo.search_catalog(
                params.query, min_price=params.min_price, max_price=params.max_price, limit=SEARCH_LIMIT
            )
        except SQLAlchemyError:
            log.exception("Поиск по каталогу не удался")
            return ToolOutput("Поиск временно недоступен.", f"{name} {params.describe()}: ошибка БД", is_error=True)
        return ToolOutput(render_page(page), f"{name} {params.describe()} {FOUND} {page.total}")


# ---------- Статус заказа ----------

ORDER_STATUS_NAME = "order_status"
ORDER_STATUS: ToolParam = {
    "name": ORDER_STATUS_NAME,
    "description": (
        "Статус заказа магазина. Вызывай, когда клиент спрашивает, где его заказ, когда доставка или сколько "
        "он стоил — каждый раз заново, даже если уже проверял в этом диалоге: статус мог измениться. "
        "Нужны номер заказа и последние 4 цифры телефона, на который оформлен заказ: без них не вызывай, "
        "сначала попроси их у клиента. Полный номер телефона не проси. "
        "Возвращает статус, состав, сумму и ожидаемую дату — дату называй числом, как в результате, — "
        "или сообщение, что заказ не найден."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "description": "Номер заказа, как назвал клиент: DE-10023."},
            "phone_last4": {
                "type": "string",
                "description": "Последние 4 цифры телефона из заказа.",
                "pattern": r"^\d{4}$",
            },
        },
        "required": ["order_number", "phone_last4"],
        "additionalProperties": False,
    },
}
# Один ответ на неверный номер и неверный телефон: по ответу нельзя узнать, что заказ существует
ORDER_NOT_FOUND = (
    "Заказ не найден: номер или последние 4 цифры телефона не совпадают. Попроси клиента проверить их. "
    "Не говори, что именно не совпало."
)
ORDER_LOCKED = (
    "Слишком много неудачных проверок. Больше не проверяй: скажи клиенту, что со статусом заказа "
    "поможет менеджер, и предложи оставить номер телефона для звонка."
)
NO_DATE = "Дата доставки пока не известна — её уточнит менеджер. Дату не называй."


class OrderQuery(BaseModel):
    order_number: str = Field(max_length=64)
    phone_last4: str = Field(pattern=r"^\d{4}$")


def render_order(order: OrderInfo) -> str:
    """Заказ для модели. Телефон не передаётся: модели он не нужен."""
    date_line = (
        f"Ожидаемая дата доставки или выдачи: {order.delivery_date:%d.%m.%Y}." if order.delivery_date is not None else NO_DATE
    )
    return "\n".join(
        [f"Заказ {order.number}: {order.status}.", f"Состав: {order.items.strip()}", f"Сумма: {order.total} лей.", date_line]
    )


class OrderTools:
    """Инструмент order_status для одного чата: проверяет владельца заказа и ограничивает перебор."""

    backend: OrderBackend
    chat_id: int
    limits: LookupLimits

    def __init__(self, backend: OrderBackend, chat_id: int, limits: LookupLimits) -> None:
        self.backend = backend
        self.chat_id = chat_id
        self.limits = limits

    @property
    def definitions(self) -> Sequence[ToolParam]:
        return (ORDER_STATUS,)

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput:
        if name != ORDER_STATUS_NAME:
            return ToolOutput(f"Инструмента {name} нет.", f"{name}: неизвестный инструмент", is_error=True)
        try:
            params = OrderQuery.model_validate(tool_input)
        except ValidationError as error:
            fields = ", ".join(str(e["loc"][0]) for e in error.errors() if e["loc"])
            return ToolOutput(f"Неверные параметры: {fields}.", f"{name}: неверные параметры ({fields})", is_error=True)
        number = normalize_number(params.order_number)
        if number is None:
            # В систему магазина такой номер не уходит, поэтому и попыткой не считается
            return ToolOutput(ORDER_NOT_FOUND, f"{name} {params.order_number!r} → не похоже на номер заказа")

        key = order_key(number)
        by_chat, by_order = self.limits.by_chat, self.limits.by_order
        # Попытки занимаются до запроса к магазину: параллельные сообщения не проскочат лимит
        if not by_chat.try_begin(self.chat_id):
            return ToolOutput(ORDER_LOCKED, f"{name}: попытки этого чата исчерпаны", is_error=True)
        if not by_order.try_begin(key):
            by_chat.succeeded(self.chat_id)
            return ToolOutput(ORDER_LOCKED, f"{name} {number}: проверки этого заказа временно закрыты", is_error=True)
        try:
            order = await self.backend.find(number)
        except BaseException:
            # Сбой системы магазина — не ошибка клиента: попытки возвращаются
            by_chat.succeeded(self.chat_id)
            by_order.succeeded(key)
            raise

        if order is None or not phone_matches(order.phone, params.phone_last4):
            reason = "не найден" if order is None else "телефон не совпал"
            return ToolOutput(ORDER_NOT_FOUND, f"{name} {number} → {reason}")
        by_chat.succeeded(self.chat_id)
        by_order.succeeded(key)
        return ToolOutput(render_order(order), f"{name} {order.number} → {order.status}", prices=(order.total,))


class Toolbox:
    """Несколько наборов инструментов как один: описания подряд, вызов — тому, чьё это имя."""

    runners: tuple[ToolRunner, ...]

    def __init__(self, *runners: ToolRunner) -> None:
        self.runners = runners

    @property
    def definitions(self) -> Sequence[ToolParam]:
        return tuple(tool for runner in self.runners for tool in runner.definitions)

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput:
        for runner in self.runners:
            if any(tool["name"] == name for tool in runner.definitions):
                return await runner.run(name, tool_input)
        return ToolOutput(f"Инструмента {name} нет.", f"{name}: неизвестный инструмент", is_error=True)
