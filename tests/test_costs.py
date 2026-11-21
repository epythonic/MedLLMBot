import dataclasses
from datetime import date

import pytest

from medllmbot.costs import Price, PriceBook, TokenUsage, UsageRow, build_report, format_usd

HAIKU_4_5 = Price("claude-haiku-4-5", 0, 1.00, 5.00, 1.25, 0.10)
HAIKU_5_5 = Price("claude-haiku-5-5", 0, 0.10, 0.50, 0.125, 0.01)
HAIKU_5_5_LONG = Price("claude-haiku-5-5", 100_001, 0.50, 2.50, 0.625, 0.05)
SONNET_5 = Price("claude-sonnet-5", 0, 2.00, 10.00, 2.50, 0.20)
SONNET_5_5 = Price("claude-sonnet-5-5", 0, 2.00, 10.00, 2.50, 0.10)


@pytest.fixture
def book() -> PriceBook:
    return PriceBook([HAIKU_4_5, HAIKU_5_5_LONG, HAIKU_5_5, SONNET_5, SONNET_5_5])


def usage(input_tokens: int = 0, output: int = 0, cache_read: int = 0, cache_write: int = 0) -> TokenUsage:
    return TokenUsage(input_tokens, output, cache_read, cache_write)


class TestPrice:
    def test_cost_counts_every_token_kind(self) -> None:
        # $/MTok == µUSD/токен: 1000·1 + 200·5 + 500·1.25 + 3000·0.1
        cost = HAIKU_4_5.cost_micro_usd(usage(1000, 200, cache_read=3000, cache_write=500))
        assert cost == 1000 + 1000 + 625 + 300

    def test_prompt_tokens_include_cache(self) -> None:
        assert usage(10, 99, cache_read=20, cache_write=30).prompt_tokens == 60


class TestPriceBook:
    def test_exact_model(self, book: PriceBook) -> None:
        assert book.find("claude-sonnet-5", 100) == SONNET_5

    def test_dated_snapshot_uses_alias_price(self, book: PriceBook) -> None:
        assert book.find("claude-haiku-4-5-20251001", 100) == HAIKU_4_5

    def test_longest_prefix_wins(self, book: PriceBook) -> None:
        assert book.find("claude-sonnet-5-5-20261001", 100) == SONNET_5_5

    def test_prefix_must_end_on_dash(self, book: PriceBook) -> None:
        assert book.find("claude-sonnet-50", 100) is None

    def test_other_model_version_is_not_priced(self, book: PriceBook) -> None:
        # Новая версия модели не должна тихо считаться по цене старой — её место в «без цены»
        assert book.find("claude-sonnet-5-5-6", 100) is None
        assert book.find("claude-haiku-5-5-preview", 100) is None

    @pytest.mark.parametrize("bad_price", [float("inf"), float("nan"), 1e30])
    def test_broken_price_gives_no_cost(self, bad_price: float) -> None:
        broken = PriceBook([dataclasses.replace(HAIKU_5_5, input_per_mtok=bad_price)])
        assert broken.cost_micro_usd("claude-haiku-5-5", usage(1000)) is None

    def test_unknown_model(self, book: PriceBook) -> None:
        assert book.find("gpt-5", 100) is None
        assert book.cost_micro_usd("gpt-5", usage(1000)) is None

    @pytest.mark.parametrize(
        ("prompt_tokens", "expected"),
        [
            (0, HAIKU_5_5),
            (100_000, HAIKU_5_5),
            (100_001, HAIKU_5_5_LONG),
            (900_000, HAIKU_5_5_LONG),
        ],
    )
    def test_long_context_tier(self, book: PriceBook, prompt_tokens: int, expected: Price) -> None:
        assert book.find("claude-haiku-5-5", prompt_tokens) == expected

    def test_several_requests_priced_one_by_one(self, book: PriceBook) -> None:
        # Ответ с поиском по каталогу: два запроса по 60 000 токенов. Вместе 120 000, но тариф —
        # по каждому запросу, как в счёте Anthropic, поэтому дорогой тариф не применяется
        requests = [usage(60_000, 100), usage(60_000, 100)]
        assert book.total_micro_usd("claude-haiku-5-5", requests) == 2 * round(60_000 * 0.10 + 100 * 0.50)
        assert book.cost_micro_usd("claude-haiku-5-5", usage(120_000, 200)) == round(120_000 * 0.50 + 200 * 2.50)

    def test_several_requests_without_price(self, book: PriceBook) -> None:
        assert book.total_micro_usd("claude-unknown", [usage(10), usage(10)]) is None
        assert book.total_micro_usd("claude-haiku-5-5", []) == 0

    def test_tier_counts_cached_tokens(self, book: PriceBook) -> None:
        # Порог считается по всему промпту, включая чтение из кэша
        cost = book.cost_micro_usd("claude-haiku-5-5", usage(1, cache_read=100_000))
        assert cost == round(1 * 0.50 + 100_000 * 0.05)


@pytest.mark.parametrize(
    ("micro", "text"),
    [(None, "—"), (0, "$0.0000"), (2925, "$0.0029"), (12_345_678, "$12.3457")],
)
def test_format_usd(micro: int | None, text: str) -> None:
    assert format_usd(micro) == text


def row(month: str, model: str, cost: int, requests: int = 1, unpriced: int = 0) -> UsageRow:
    return UsageRow(month, model, requests, 100, 10, 0, 0, cost, unpriced)


class TestBuildReport:
    def test_months_newest_first_with_totals(self) -> None:
        rows = [
            row("2026-10", "claude-haiku-4-5", 300),
            row("2026-11", "claude-haiku-4-5", 100, requests=2),
            row("2026-11", "claude-sonnet-5", 500, unpriced=1),
        ]

        [nov, oct_] = build_report(rows, today=date(2026, 11, 10))

        assert (nov.month, oct_.month) == ("2026-11", "2026-10")
        assert [r.model for r in nov.rows] == [
            "claude-sonnet-5",
            "claude-haiku-4-5",
        ]  # дороже — выше
        assert (nov.total.requests, nov.total.cost_micro_usd) == (3, 600)
        assert nov.total.unpriced_requests == 1
        assert nov.total.input_tokens == 200

    def test_forecast_only_for_current_month(self) -> None:
        rows = [row("2026-11", "m", 1_000_000), row("2026-10", "m", 5)]

        [nov, oct_] = build_report(rows, today=date(2026, 11, 10))

        # $1 за 10 дней ноября (30 дней) → $3 к концу месяца
        assert nov.forecast_micro_usd == 3_000_000
        assert oct_.forecast_micro_usd is None

    def test_empty(self) -> None:
        assert build_report([], today=date(2026, 11, 10)) == []
