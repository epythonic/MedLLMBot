import pytest

from medllmbot.guards import PriceChecker, RateLimiter, extract_phone
from tests.conftest import DEMO_INFO


@pytest.fixture
def prices() -> PriceChecker:
    return PriceChecker(DEMO_INFO)


class TestPriceChecker:
    def test_known_prices_parsed_from_knowledge(self, prices: PriceChecker) -> None:
        # Цены услуг и цены, упомянутые в FAQ («рассрочка от 5 000 лей»)
        assert {"800", "3500", "1200", "9500", "5000"} <= prices.known_prices

    @pytest.mark.parametrize(
        "answer",
        [
            "Чистка стоит 800 лей.",
            "Отбеливание — 3 500 лей",
            "Отбеливание — 3500 лей",
            "Curățarea costă 800 lei.",
            "Implant: 9 500 MDL",
            "Консультация 200 ЛЕЙ, снимок 100 лей.",
            "Implantul costă 9.500 lei.",  # румынский разделитель тысяч
            "Имплант — 9\u00a0500 лей",  # неразрывный пробел
            "Консультация стоит 200 лея",
        ],
    )
    def test_known_price_passes(self, prices: PriceChecker, answer: str) -> None:
        assert not prices.has_unknown_price(answer)

    @pytest.mark.parametrize(
        "answer",
        [
            "Чистка стоит 700 лей.",
            "Сейчас скидка: 640 lei!",
            "Консультация 200 лей, а чистка 750 MDL.",
            "Чистка стоит 650 леев",  # раньше не распознавалось как цена
            "Отбеливание — 3.900 lei",
        ],
    )
    def test_unknown_price_detected(self, prices: PriceChecker, answer: str) -> None:
        assert prices.has_unknown_price(answer)

    def test_text_without_prices(self, prices: PriceChecker) -> None:
        assert not prices.has_unknown_price("Мы работаем с 9:00 до 19:00, 5 дней в неделю.")


class TestPhone:
    @pytest.mark.parametrize(
        ("text", "phone"),
        [
            ("Иван, +373 69 123 456", "+373 69 123 456"),
            ("мой номер 069123456, жду звонка", "069123456"),
            ("Запишите меня: 069-12-34-56", "069-12-34-56"),
            ("Перезвоните на (069) 123 456", "(069) 123 456"),
            ("+373 (69) 12-34-56, Ирина", "+373 (69) 12-34-56"),
            ("тел. +373.69.123.456", "+373.69.123.456"),
            ("Можно 25.11? Мой номер 069123456", "069123456"),
            ("Я из Румынии: +40 721 234 567", "+40 721 234 567"),
        ],
    )
    def test_phone_extracted(self, text: str, phone: str) -> None:
        assert extract_phone(text) == phone

    @pytest.mark.parametrize(
        "text",
        [
            "Сколько стоит чистка?",
            "Чистка за 800 лей",
            "Можно на 12.11.2026 в 15:00?",
            # Ложные заявки, найденные на ревью
            "Сколько стоит имплант, 10000-15000 лей?",
            "Видел цены 1500 2000 2500",
            "Приду 2026 11 25",
            "Номер полиса 1234 5678 9012",
        ],
    )
    def test_not_a_phone(self, text: str) -> None:
        assert extract_phone(text) is None


class FakeClock:
    now: float

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestRateLimiter:
    def test_blocks_after_limit(self) -> None:
        limiter = RateLimiter(clock=FakeClock())
        results = [limiter.too_many(1, limit=10, window=60) for _ in range(11)]
        assert results == [False] * 10 + [True]

    def test_chats_are_independent(self) -> None:
        limiter = RateLimiter(clock=FakeClock())
        assert not limiter.too_many(1, limit=1, window=60)
        assert not limiter.too_many(2, limit=1, window=60)
        assert limiter.too_many(1, limit=1, window=60)

    def test_allows_again_after_window(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        for _ in range(3):
            limiter.too_many(1, limit=2, window=60)
        assert limiter.too_many(1, limit=2, window=60)

        clock.now += 61
        assert not limiter.too_many(1, limit=2, window=60)

    def test_new_limit_applies_immediately(self) -> None:
        # Лимит поменяли в админке — следующий вызов уже по новому значению
        limiter = RateLimiter(clock=FakeClock())
        for _ in range(5):
            limiter.too_many(1, limit=10, window=60)
        assert limiter.too_many(1, limit=3, window=60)

    def test_cleanup_forgets_silent_chats(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        limiter.too_many(1, limit=10, window=60)
        limiter.too_many(2, limit=10, window=60)

        clock.now += 61
        limiter.too_many(3, limit=10, window=60)
        limiter.cleanup(window=60)
        assert len(limiter) == 1

    def test_cleanup_runs_automatically(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        for chat_id in range(RateLimiter.CLEANUP_EVERY):
            clock.now += 2  # каждый чат пишет один раз и замолкает
            limiter.too_many(chat_id, limit=10, window=1)
        assert len(limiter) == 1
