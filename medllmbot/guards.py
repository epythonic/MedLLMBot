"""Safeguards: message rate limiting, price checks and phone number detection."""

import re
import time
from collections.abc import Callable, Iterable
from typing import ClassVar

# Prices like «800 лей», «1 200 lei», «12.000 lei», «500 MDL», «900 леев». The thousands separator is
# a space, a non-breaking space or a dot (Romanian notation)
PRICE = re.compile(r"(\d(?:[\d \u00a0\u202f.]*\d)?)\s*(?:лей|лея|леев|lei|leu|mdl)\b", re.IGNORECASE)

# Phone number: Moldovan (+373 69 123 456, 069123456, (069) 123 456, 069-12-34-56)
# or international with «+». Surrounding digits are not captured: «10000-15000 лей», dates and policy numbers
# are not phones
_SEP = r"[\s\-.]*"
PHONE = re.compile(
    rf"(?<![\d+])(?:(?:\+|00)?373{_SEP}\(?\d{{2}}\)?|\(?0\d{{2}}\)?)(?:{_SEP}\d){{6}}(?!\d)"
    rf"|(?<![\d+])\+\d(?:[\s\-.()]*\d){{9,14}}(?!\d)"
)


# Amount in a client message: «10 000», «10.000», «5000», «10 тысяч», «10 тыс.», «10к», «1,5 тыс», «8 mii»
_AMOUNT = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[   .]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)"
    r"(?:\s*(тыс\w*|к|k|mii)(?![^\W\d_]))?",
    re.IGNORECASE,
)
# Words before an amount that make it a budget bound rather than a product price: «до 10 000 лей»,
# «в бюджете 5000 лей», «până la 8000 lei», «sub 8000 lei»
_BUDGET_BOUND = re.compile(
    r"(?<!\w)(?:до|от|в пределах|не дороже|не больше|не более|дешевле|бюджет\w*(?:\s+(?:в|до|от))?"
    r"|p[âa]n[ăa] la|sub|de la|maxim(?:um)?|buget\w*(?:\s+(?:de|p[âa]n[ăa] la))?)\s*[:—–-]?\s*$",
    re.IGNORECASE,
)
# A discount is not a budget: «скидка до 500 лей» is made up by the model, even if the client named the amount
_DISCOUNT = re.compile(r"скидк|акци|распрода|reducer|discount|promo", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?\n]")


def find_prices(text: str) -> set[str]:
    """All prices in the text as bare digits: «1 200 лей» and «1.200 lei» -> «1200»."""
    return {re.sub(r"\D", "", match.group(1)) for match in PRICE.finditer(text)}


def customer_amounts(texts: Iterable[str]) -> set[str]:
    """Amounts the client named, as bare digits: «до 10 тысяч» -> «10000», «1,5 тыс» -> «1500»."""
    amounts: set[str] = set()
    for text in texts:
        for match in _AMOUNT.finditer(text):
            whole, fraction, thousands = match.groups()
            digits = re.sub(r"\D", "", whole)
            if thousands:
                amounts.add(str(int(digits) * 1000 + int((fraction or "").ljust(3, "0"))))
            elif fraction is None:
                amounts.add(str(int(digits)))
    return amounts


def _is_budget(text: str, start: int) -> bool:
    """The price starting at `start` follows budget-bound words and is not in a sentence about a discount."""
    clause = _SENTENCE_END.split(text[:start])[-1]
    return _BUDGET_BOUND.search(clause) is not None and _DISCOUNT.search(clause) is None


class PriceChecker:
    """Knows all prices from the knowledge base and finds prices in a reply that are not there."""

    known_prices: set[str]

    def __init__(self, info: str) -> None:
        self.known_prices = find_prices(info)

    def add_known(self, prices: Iterable[int]) -> None:
        """Prices missing from the knowledge base text: the catalog the model searches with a tool."""
        self.known_prices |= {str(price) for price in prices}

    def has_unknown_price(self, text: str, customer: Iterable[str] = ()) -> bool:
        """True if the text has a price that is not in the knowledge base.

        `customer` is the client's messages in this dialog. Their own budget in the reply is not a made-up price:
        «подберите до 10 тысяч» -> «под бюджет до 10 000 лей есть…». Only an amount the client named themselves
        is allowed, and only as a budget bound: «S8 стоит 3000 лей» with the client's amount is still replaced.
        """
        budgets = customer_amounts(customer)
        for match in PRICE.finditer(text):
            price = re.sub(r"\D", "", match.group(1))
            if price in self.known_prices or (price in budgets and _is_budget(text, match.start())):
                continue
            return True
        return False


def extract_phone(text: str) -> str | None:
    """The first phone number in the text, as the client wrote it, or None."""
    match = PHONE.search(text)
    return match.group(0).strip() if match else None


class RateLimiter:
    """Sliding window: at most `limit` messages per `window` seconds from one chat.

    Limits are passed on every call because they can be changed in the admin panel on the fly.
    Counters live in memory: this is transient data that does not belong in the DB.
    """

    # How often to drop chats that have been silent for a while from memory
    CLEANUP_EVERY: ClassVar[int] = 1000

    _clock: Callable[[], float]
    _seen: dict[int, list[float]]
    _calls: int

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._seen = {}
        self._calls = 0

    def too_many(self, chat_id: int, limit: int, window: float) -> bool:
        """Records a message and returns True if the chat writes too often."""
        now = self._clock()
        recent = [t for t in self._seen.get(chat_id, []) if now - t < window]
        recent.append(now)
        self._seen[chat_id] = recent

        self._calls += 1
        if self._calls % self.CLEANUP_EVERY == 0:
            self.cleanup(window)

        return len(recent) > limit

    def cleanup(self, window: float) -> None:
        """Drops chats with no messages in the last window so the dict does not grow forever."""
        now = self._clock()
        self._seen = {chat_id: times for chat_id, times in self._seen.items() if times and now - times[-1] < window}

    def __len__(self) -> int:
        return len(self._seen)
