"""Инструменты модели: поиск по каталогу, когда он не помещается в промпт."""

import logging
from collections.abc import Sequence
from functools import cache

from anthropic.types import ToolParam
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from medllmbot.db.models import BusinessProfile
from medllmbot.db.repository import CatalogPage, Repository
from medllmbot.llm import ToolOutput
from medllmbot.profiles import PROFILES
from medllmbot.prompts import service_line

log = logging.getLogger(__name__)

SEARCH_LIMIT = 10  # позиций в одном ответе инструмента: больше клиенту в чате всё равно не перечислить
MAX_PRICE = 10**9  # верхняя граница фильтра цены: защита от переполнения в SQLite

SEARCH_CATALOG_NAME = "search_catalog"


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
        """Запрос для журнала: «чистка», 500–1000 лей."""
        text = f"«{self.query}»"
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
        return ToolOutput(render_page(page), f"{name} {params.describe()} → найдено {page.total}")
