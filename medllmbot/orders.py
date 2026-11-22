"""Order status for the order_status tool: the interface to the shop system and brute-force protection.

The bot does not know where the shop stores its orders. It asks an `OrderBackend`:
- in the demo this is `DatabaseOrders`, the `orders` table edited in the admin panel;
- in a real shop project this would be a query to its CMS or 1C with the same `find` method.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar, Protocol

from medllmbot.db.models import OrderStatus
from medllmbot.db.repository import Repository

LAST_DIGITS = 4  # how many last phone digits the client gives
# How the model and the audit log name a status. Russian like the rest of the prompt, independent of the admin's
# translated labels (OrderStatus values)
STATUS_TEXT: dict[OrderStatus, str] = {
    OrderStatus.NEW: "Принят",
    OrderStatus.CONFIRMED: "Подтверждён",
    OrderStatus.SHIPPED: "Передан в доставку",
    OrderStatus.READY: "Ждёт в пункте самовывоза",
    OrderStatus.DELIVERED: "Получен",
    OrderStatus.CANCELLED: "Отменён",
}

# Order number in canonical form: Latin letters and digits, parts joined by hyphens, e.g. DE-10023
ORDER_NUMBER = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
NUMBER_LENGTH = range(3, 33)
# Cyrillic letters that look Latin: «DЕ-10023» with a Russian «Е» → «DE-10023». «Д» has no Latin twin,
# so such a number does not match, and the admin panel will not save it
_LOOKALIKES = str.maketrans("АВЕКМНОРСТХУ", "ABEKMHOPCTXY")
# Spaces and any dashes are separators: «DE 10023», «DE–10023» (phone autocorrect), «DE - 10023»
_SEPARATORS = re.compile(r"[\s\-\u2010-\u2015\u2212]+")


@dataclass(frozen=True)
class OrderInfo:
    number: str
    status: OrderStatus
    items: str
    total: int  # in lei
    delivery_date: date | None
    phone: str = field(repr=False)  # for verification; never shown to the client, the model or in logs


class OrderBackend(Protocol):
    """Order source. A real shop implementation queries the shop's accounting system.

    `number` is already in canonical form (`normalize_number`), but the client may have omitted the hyphen:
    compare by `order_key`, letters and digits only.
    """

    async def find(self, number: str) -> OrderInfo | None: ...


class DatabaseOrders:
    """Demo source: orders from the `orders` table (the «Заказы» admin section)."""

    repo: Repository

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def find(self, number: str) -> OrderInfo | None:
        order = await self.repo.find_order(order_key(number))
        if order is None:
            return None
        return OrderInfo(order.number, order.status, order.items, order.total, order.delivery_date, order.phone)


def normalize_number(text: str) -> str | None:
    """Order number as on the receipt: «de 10023», «№ DE–10023», «#de-10023» → «DE-10023».

    None if it does not look like an order number (other characters, Cyrillic without a Latin twin, length).
    """
    cleaned = text.strip().upper().translate(_LOOKALIKES).lstrip("#№").strip()
    number = _SEPARATORS.sub("-", cleaned).strip("-")
    if not ORDER_NUMBER.fullmatch(number) or len(number) not in NUMBER_LENGTH:
        return None
    return number


def order_key(number: str) -> str:
    """Comparison key: letters and digits only. «DE10023» and «DE-10023» are the same order."""
    return number.replace("-", "")


def phone_matches(phone: str, last_digits: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return len(last_digits) == LAST_DIGITS and last_digits.isdigit() and digits.endswith(last_digits)


class FailedLookups:
    """At most `max_failures` failed order checks per `window` seconds for one key.

    The key is a chat (brute force from one account) or an order number (one order brute-forced from many accounts).
    An attempt is reserved before the shop request and released on success: otherwise parallel messages
    would slip past the limit while the shop responds. Counters are in memory, like the rate limit: after a bot
    restart the attempts start over, which is acceptable.
    """

    # How often to drop keys without recent failures from memory
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
        """Reserves an attempt. False means the limit is exhausted and checking is not allowed."""
        self._calls += 1
        if self._calls % self.CLEANUP_EVERY == 0:
            self.cleanup()
        recent = self._recent(key)
        if len(recent) >= self.max_failures:
            return False
        self._failures[key] = [*recent, self._clock()]
        return True

    def succeeded(self, key: int | str) -> None:
        """The check passed, so the reserved attempt does not count as a failure."""
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
    """Two limits on failed checks: from one chat and for one order number across all chats.

    Order numbers are sequential, so a per-chat limit alone is not enough: with a dozen accounts
    the phone digits would be guessed ten times faster.
    """

    by_chat: FailedLookups = field(default_factory=FailedLookups)
    by_order: FailedLookups = field(default_factory=_order_limit)
