"""Cost of Claude requests: the price list, per-token calculation and the monthly report.

Money is stored in whole microdollars (1 µUSD = $0.000001), so monthly totals add up
without rounding errors. A handy coincidence: a price of "$X per million tokens" is exactly
X microdollars per token.
"""

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from medllmbot.i18n import N_


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int  # input without cache
    output_tokens: int  # including the model's thinking tokens
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def prompt_tokens(self) -> int:
        """Full prompt length; it selects the pricing tier (for Haiku 5.5 the tier depends on length)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class Price:
    """Prices per million tokens in USD."""

    model: str
    from_prompt_tokens: int  # the tier applies to prompts at least this long
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float  # cache write with a 5-minute TTL
    cache_read_per_mtok: float

    def cost_micro_usd(self, usage: TokenUsage) -> int:
        return round(
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
        )


def match_model(model: str, priced: Iterable[str]) -> str | None:
    """Price list ID used to price the model: an exact match or the same ID without the snapshot date."""
    candidates = [m for m in priced if m == model or re.fullmatch(rf"{re.escape(m)}-\d{{8}}", model)]
    return max(candidates, key=len) if candidates else None


# SQLite INTEGER limit: a larger cost is certainly an error in the price list
MAX_MICRO_USD = 2**63 - 1


class PriceBook:
    """Price list. Looks up a price by the exact model ID, and for dated snapshots
    (`claude-haiku-4-5-20251001`) by the ID without the date. Another model version (`claude-sonnet-5-6`)
    does not inherit the `claude-sonnet-5` price: such requests end up in «без цены» (unpriced).
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
        """Request cost, or None if the model has no price in the price list."""
        price = self.find(model, usage.prompt_tokens)
        if price is None:
            return None
        try:
            cost = price.cost_micro_usd(usage)
        except OverflowError, ValueError:  # infinity or NaN in the price list
            return None
        return cost if cost <= MAX_MICRO_USD else None

    def total_micro_usd(self, model: str, requests: Iterable[TokenUsage]) -> int | None:
        """Cost of a reply made of several requests (catalog search): the tier follows each request's length,
        as in the Anthropic bill. None if at least one request cannot be priced.
        """
        total = 0
        for usage in requests:
            cost = self.cost_micro_usd(model, usage)
            if cost is None:
                return None
            total += cost
        return total if total <= MAX_MICRO_USD else None


def format_usd(micro_usd: int | None) -> str:
    if micro_usd is None:
        return "—"
    return f"${micro_usd / 1_000_000:,.4f}"


# ---------- Monthly report ----------


@dataclass(frozen=True)
class UsageRow:
    month: str  # "2026-11"
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_micro_usd: int
    unpriced_requests: int  # requests whose model has no price


@dataclass(frozen=True)
class MonthReport:
    month: str
    rows: list[UsageRow]
    total: UsageRow
    # For the current month: projected spending at month end if the current pace holds
    forecast_micro_usd: int | None


# The label of the sum row; a msgid, the admin translates it
TOTAL = N_("Total")


def _total(month: str, rows: list[UsageRow]) -> UsageRow:
    return UsageRow(
        month=month,
        model=TOTAL,
        requests=sum(r.requests for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cache_read_tokens=sum(r.cache_read_tokens for r in rows),
        cache_write_tokens=sum(r.cache_write_tokens for r in rows),
        cost_micro_usd=sum(r.cost_micro_usd for r in rows),
        unpriced_requests=sum(r.unpriced_requests for r in rows),
    )


def build_report(rows: Iterable[UsageRow], today: date) -> list[MonthReport]:
    """Groups rows by month (newest first) and computes the forecast for the current month."""
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
