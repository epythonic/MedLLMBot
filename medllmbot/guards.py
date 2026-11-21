"""Защита: ограничение частоты сообщений, проверка цен и поиск телефона."""

import re
import time
from collections.abc import Callable
from typing import ClassVar

# Цены вида «800 лей», «1 200 lei», «12.000 lei», «500 MDL», «900 леев». Разделитель тысяч —
# пробел, неразрывный пробел или точка (румынская запись)
PRICE = re.compile(r"(\d(?:[\d \u00a0\u202f.]*\d)?)\s*(?:лей|лея|леев|lei|leu|mdl)\b", re.IGNORECASE)

# Номер телефона: молдавский (+373 69 123 456, 069123456, (069) 123 456, 069-12-34-56)
# или международный с «+». Числа вокруг не захватываем: «10000-15000 лей», даты и номера полисов — не телефоны
_SEP = r"[\s\-.]*"
PHONE = re.compile(
    rf"(?<![\d+])(?:(?:\+|00)?373{_SEP}\(?\d{{2}}\)?|\(?0\d{{2}}\)?)(?:{_SEP}\d){{6}}(?!\d)"
    rf"|(?<![\d+])\+\d(?:[\s\-.()]*\d){{9,14}}(?!\d)"
)


def find_prices(text: str) -> set[str]:
    """Все цены из текста одними цифрами: «1 200 лей» и «1.200 lei» -> «1200»."""
    return {re.sub(r"\D", "", match.group(1)) for match in PRICE.finditer(text)}


class PriceChecker:
    """Знает все цены из базы знаний и находит в ответе цены, которых там нет."""

    known_prices: set[str]

    def __init__(self, info: str) -> None:
        self.known_prices = find_prices(info)

    def has_unknown_price(self, text: str) -> bool:
        """True, если в тексте есть цена, которой нет в базе знаний."""
        return not find_prices(text) <= self.known_prices


def extract_phone(text: str) -> str | None:
    """Первый номер телефона в тексте — как его написал клиент — или None."""
    match = PHONE.search(text)
    return match.group(0).strip() if match else None


class RateLimiter:
    """Скользящее окно: не больше `limit` сообщений за `window` секунд от одного чата.

    Лимиты передаются при каждом вызове, потому что их можно поменять в админке на лету.
    Счётчики живут в памяти: это временные данные, в БД им не место.
    """

    # Как часто удалять из памяти чаты, которые давно молчат
    CLEANUP_EVERY: ClassVar[int] = 1000

    _clock: Callable[[], float]
    _seen: dict[int, list[float]]
    _calls: int

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._seen = {}
        self._calls = 0

    def too_many(self, chat_id: int, limit: int, window: float) -> bool:
        """Отмечает сообщение и возвращает True, если чат пишет слишком часто."""
        now = self._clock()
        recent = [t for t in self._seen.get(chat_id, []) if now - t < window]
        recent.append(now)
        self._seen[chat_id] = recent

        self._calls += 1
        if self._calls % self.CLEANUP_EVERY == 0:
            self.cleanup(window)

        return len(recent) > limit

    def cleanup(self, window: float) -> None:
        """Удаляет чаты без сообщений за последнее окно, чтобы словарь не рос бесконечно."""
        now = self._clock()
        self._seen = {chat_id: times for chat_id, times in self._seen.items() if times and now - times[-1] < window}

    def __len__(self) -> int:
        return len(self._seen)
