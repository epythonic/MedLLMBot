"""Статус заказа для инструмента order_status: интерфейс к системе магазина и защита от подбора.

Бот не знает, где магазин хранит заказы. Он спрашивает `OrderBackend`:
- в демо это `DatabaseOrders` — таблица `orders`, которую правят в админке;
- в проекте для магазина здесь будет запрос к его CMS или 1С с тем же методом `find`.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar, Protocol

from medllmbot.db.models import OrderStatus
from medllmbot.db.repository import Repository

LAST_DIGITS = 4  # сколько последних цифр телефона называет клиент

# Номер заказа в каноническом виде: латинские буквы и цифры, части через дефис — DE-10023
ORDER_NUMBER = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
NUMBER_LENGTH = range(3, 33)
# Кириллица, похожая на латиницу: «DЕ-10023» с русской «Е» → «DE-10023». У «Д» латинской пары нет —
# такой номер не подходит, и админка его не сохранит
_LOOKALIKES = str.maketrans("АВЕКМНОРСТХУ", "ABEKMHOPCTXY")
# Пробелы и любые тире — разделители: «DE 10023», «DE–10023» (автозамена на телефоне), «DE - 10023»
_SEPARATORS = re.compile(r"[\s\-\u2010-\u2015\u2212]+")


@dataclass(frozen=True)
class OrderInfo:
    number: str
    status: OrderStatus
    items: str
    total: int  # в леях
    delivery_date: date | None
    phone: str = field(repr=False)  # для проверки; не показывается клиенту, модели и в логах


class OrderBackend(Protocol):
    """Источник заказов. Реализация для магазина ходит в его систему учёта.

    `number` уже приведён к каноническому виду (`normalize_number`), но клиент мог не написать дефис:
    сравнивать стоит по `order_key` — только буквы и цифры.
    """

    async def find(self, number: str) -> OrderInfo | None: ...


class DatabaseOrders:
    """Демо-источник: заказы из таблицы `orders` (раздел админки «Заказы»)."""

    repo: Repository

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def find(self, number: str) -> OrderInfo | None:
        order = await self.repo.find_order(order_key(number))
        if order is None:
            return None
        return OrderInfo(order.number, order.status, order.items, order.total, order.delivery_date, order.phone)


def normalize_number(text: str) -> str | None:
    """Номер заказа как на чеке: «de 10023», «№ DE–10023», «#de-10023» → «DE-10023».

    None — на номер заказа не похоже (другие символы, кириллица без латинской пары, длина).
    """
    cleaned = text.strip().upper().translate(_LOOKALIKES).lstrip("#№").strip()
    number = _SEPARATORS.sub("-", cleaned).strip("-")
    if not ORDER_NUMBER.fullmatch(number) or len(number) not in NUMBER_LENGTH:
        return None
    return number


def order_key(number: str) -> str:
    """Ключ для сравнения: только буквы и цифры. «DE10023» и «DE-10023» — один заказ."""
    return number.replace("-", "")


def phone_matches(phone: str, last_digits: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return len(last_digits) == LAST_DIGITS and last_digits.isdigit() and digits.endswith(last_digits)


class FailedLookups:
    """Не больше `max_failures` неудачных проверок заказа за `window` секунд на один ключ.

    Ключ — чат (перебор из одного аккаунта) или номер заказа (перебор одного заказа с разных аккаунтов).
    Попытка занимается до запроса к магазину и возвращается при успехе: иначе параллельные сообщения
    проскочили бы лимит, пока магазин отвечает. Счётчики в памяти, как у rate-limit: после перезапуска
    бота попытки начинаются заново — это допустимо.
    """

    # Как часто удалять из памяти ключи без свежих неудач
    CLEANUP_EVERY: ClassVar[int] = 1000

    max_failures: int
    window: float
    _clock: Callable[[], float]
    _failures: dict[int | str, list[float]]
    _calls: int

    def __init__(self, max_failures: int = 5, window: float = 3600, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = max_failures
        self.window = window
        self._clock = clock
        self._failures = {}
        self._calls = 0

    def _recent(self, key: int | str) -> list[float]:
        now = self._clock()
        return [t for t in self._failures.get(key, []) if now - t < self.window]

    def blocked(self, key: int | str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def try_begin(self, key: int | str) -> bool:
        """Занимает попытку. False — лимит исчерпан, проверять нельзя."""
        self._calls += 1
        if self._calls % self.CLEANUP_EVERY == 0:
            self.cleanup()
        recent = self._recent(key)
        if len(recent) >= self.max_failures:
            return False
        self._failures[key] = [*recent, self._clock()]
        return True

    def succeeded(self, key: int | str) -> None:
        """Проверка прошла — занятая попытка неудачей не считается."""
        if times := self._failures.get(key):
            times.pop()

    def cleanup(self) -> None:
        self._failures = {key: recent for key in list(self._failures) if (recent := self._recent(key))}

    def __len__(self) -> int:
        return len(self._failures)


def _order_limit() -> FailedLookups:
    return FailedLookups(max_failures=10)


@dataclass(frozen=True)
class LookupLimits:
    """Два лимита неудачных проверок: из одного чата и по одному номеру заказа со всех чатов.

    Номера заказов идут подряд, поэтому одного лимита на чат мало: с десятка аккаунтов
    цифры телефона подбирались бы вдесятеро быстрее.
    """

    by_chat: FailedLookups = field(default_factory=FailedLookups)
    by_order: FailedLookups = field(default_factory=_order_limit)
