"""Стоимость запросов к Claude: прайс-лист, расчёт по токенам и отчёт по месяцам.

Деньги хранятся в целых микродолларах (1 µUSD = $0.000001): суммы за месяц складываются
без ошибок округления. Удобное совпадение: цена «$X за миллион токенов» — это ровно
X микродолларов за токен.
"""

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int  # вход без кэша
    output_tokens: int  # включая токены размышлений модели
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def prompt_tokens(self) -> int:
        """Длина промпта целиком — по ней выбирается тариф (у Haiku 5.5 он зависит от длины)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class Price:
    """Цены за миллион токенов в USD."""

    model: str
    from_prompt_tokens: int  # тариф действует для промптов не короче этого значения
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float  # запись в кэш с TTL 5 минут
    cache_read_per_mtok: float

    def cost_micro_usd(self, usage: TokenUsage) -> int:
        return round(
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
        )


def match_model(model: str, priced: Iterable[str]) -> str | None:
    """ID из прайс-листа, по которому считается модель: точное совпадение или тот же ID без даты снимка."""
    candidates = [m for m in priced if m == model or re.fullmatch(rf"{re.escape(m)}-\d{{8}}", model)]
    return max(candidates, key=len) if candidates else None


# Предел INTEGER в SQLite: стоимость больше — заведомо ошибка в прайс-листе
MAX_MICRO_USD = 2**63 - 1


class PriceBook:
    """Прайс-лист. Ищет цену по точному ID модели, а для датированных снимков
    (`claude-haiku-4-5-20251001`) — по ID без даты. Другая версия модели (`claude-sonnet-5-6`)
    цену `claude-sonnet-5` не наследует: такие запросы попадают в «без цены».
    """

    _tiers: dict[str, list[Price]]

    def __init__(self, prices: Iterable[Price]) -> None:
        self._tiers = {}
        for price in prices:
            self._tiers.setdefault(price.model, []).append(price)
        for tiers in self._tiers.values():
            tiers.sort(key=lambda p: p.from_prompt_tokens)

    def find(self, model: str, prompt_tokens: int) -> Price | None:
        priced = match_model(model, self._tiers)
        if priced is None:
            return None
        suitable = [p for p in self._tiers[priced] if p.from_prompt_tokens <= prompt_tokens]
        return suitable[-1] if suitable else None

    def cost_micro_usd(self, model: str, usage: TokenUsage) -> int | None:
        """Стоимость запроса или None, если цены на модель нет в прайс-листе."""
        price = self.find(model, usage.prompt_tokens)
        if price is None:
            return None
        try:
            cost = price.cost_micro_usd(usage)
        except OverflowError, ValueError:  # бесконечность или NaN в прайс-листе
            return None
        return cost if cost <= MAX_MICRO_USD else None


def format_usd(micro_usd: int | None) -> str:
    if micro_usd is None:
        return "—"
    return f"${micro_usd / 1_000_000:,.4f}"


# ---------- Отчёт по месяцам ----------


@dataclass(frozen=True)
class UsageRow:
    month: str  # «2026-11»
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_micro_usd: int
    unpriced_requests: int  # запросы, для модели которых не нашлось цены


@dataclass(frozen=True)
class MonthReport:
    month: str
    rows: list[UsageRow]
    total: UsageRow
    # Для текущего месяца: расход на конец месяца при сохранении текущего темпа
    forecast_micro_usd: int | None


def _total(month: str, rows: list[UsageRow]) -> UsageRow:
    return UsageRow(
        month=month,
        model="Итого",
        requests=sum(r.requests for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cache_read_tokens=sum(r.cache_read_tokens for r in rows),
        cache_write_tokens=sum(r.cache_write_tokens for r in rows),
        cost_micro_usd=sum(r.cost_micro_usd for r in rows),
        unpriced_requests=sum(r.unpriced_requests for r in rows),
    )


def build_report(rows: Iterable[UsageRow], today: date) -> list[MonthReport]:
    """Группирует строки по месяцам (новые сверху) и считает прогноз для текущего месяца."""
    by_month: dict[str, list[UsageRow]] = {}
    for row in rows:
        by_month.setdefault(row.month, []).append(row)

    current = f"{today.year:04d}-{today.month:02d}"
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    reports: list[MonthReport] = []
    for month in sorted(by_month, reverse=True):
        month_rows = sorted(by_month[month], key=lambda r: r.cost_micro_usd, reverse=True)
        total = _total(month, month_rows)
        forecast = round(total.cost_micro_usd / today.day * days_in_month) if month == current else None
        reports.append(MonthReport(month, month_rows, total, forecast))
    return reports
