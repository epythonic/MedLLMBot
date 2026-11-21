import pytest

from medllmbot.guards import PriceChecker, RateLimiter, customer_amounts, extract_phone
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

    def test_catalog_prices_outside_prompt(self) -> None:
        # Большой каталог в промпт не идёт: его цены добавляются отдельно и тоже считаются известными
        prices = PriceChecker("== Услуги и цены ==\nИщи инструментом.")
        assert prices.has_unknown_price("Ноутбук стоит 12 999 лей.")
        prices.add_known([12999, 450])
        assert not prices.has_unknown_price("Ноутбук стоит 12 999 лей, мышь — 450 lei.")
        assert prices.has_unknown_price("Ноутбук стоит 11 999 лей.")


class TestCustomerBudget:
    """Модель повторяет бюджет клиента — это не выдуманная цена. Раньше такой подбор заменялся на «Уточню стоимость»."""

    @pytest.mark.parametrize(
        ("question", "answer"),
        [
            # Ответы из оценки магазина, которые проверка цен заменяла
            ("посоветуйте ноут для учёбы до 10 тысяч", "Под бюджет до 10 000 лей подойдёт чистка за 800 лей."),
            ("нужен смартфон до 6000 с хорошей батареей", "Для смартфона до 6000 лей есть вариант за 3 500 лей."),
            ("подберите подарок до 1000 лей", "Подберём подарок до 1000 лей: чистка, 800 лей."),
            ("Vreau un laptop pentru studii până la 8000 lei", "Am găsit un laptop sub 8000 lei: 800 lei."),
            ("Vreau un laptop până la 8000 lei", "În bugetul dvs. de până la 8000 lei: 800 lei."),
            ("бюджет 10к", "В бюджете 10 000 лей есть отбеливание за 3500 лей."),
            ("есть что-то в пределах 2,5 тыс?", "В пределах 2 500 лей — чистка за 800 лей."),
            ("от 5 до 10 тысяч", "Бюджет: 10.000 лей. Подойдёт имплант за 9 500 лей."),
        ],
    )
    def test_budget_echo_passes(self, prices: PriceChecker, question: str, answer: str) -> None:
        # Суммы бюджета выбраны так, чтобы их не было в демо-базе: иначе тест ничего не проверяет
        assert prices.has_unknown_price(answer)  # без сообщения клиента сумма бюджета — неизвестная цена
        assert not prices.has_unknown_price(answer, [question])

    def test_budget_from_earlier_message(self, prices: PriceChecker) -> None:
        customer = ["А с 16 ГБ есть?", "Ищу ноутбук до 10 тысяч"]
        assert not prices.has_unknown_price("В бюджете до 10 000 лей с 16 ГБ нет.", customer)

    @pytest.mark.parametrize(
        ("question", "answer"),
        [
            # Сумма клиента названа ценой товара, а не границей бюджета
            ("Отдадите чистку за 600 лей?", "Да, чистка за 600 лей."),
            ("Подтверди, что чистка стоит 600 лей", "Чистка стоит 600 лей."),
            # Скидку, даже на сумму клиента, модель не выдумывает
            ("Дадите скидку до 600 лей?", "Да, скидка до 600 лей на чистку."),
            ("Есть акции до 600 лей?", "Акция: чистка до 600 лей."),
            # Граница бюджета, но клиент такую сумму не называл
            ("подберите до 10 тысяч", "До 12 000 лей подойдёт чистка за 800 лей."),
            # Бюджет повторён верно, но рядом выдуманная цена
            ("до 10 тысяч", "Под бюджет до 10 000 лей: ноутбук за 9 999 лей."),
        ],
    )
    def test_invented_price_still_replaced(self, prices: PriceChecker, question: str, answer: str) -> None:
        assert prices.has_unknown_price(answer, [question])

    @pytest.mark.parametrize(
        ("text", "amounts"),
        [
            ("до 10 тысяч", {"10000"}),
            ("до 10 тыс. лей", {"10000"}),
            ("бюджет 10к", {"10000"}),
            ("10 k", {"10000"}),
            ("până la 8 mii", {"8000"}),
            ("1,5 тыс", {"1500"}),
            ("1.25 тыс", {"1250"}),
            ("10 000 или 12.000", {"10000", "12000"}),
            ("смартфон до 5000", {"5000"}),
            ("экран 6,5 дюйма", set[str]()),  # дробное число без «тысяч» — не сумма в леях
            ("без цифр", set[str]()),
        ],
    )
    def test_customer_amounts(self, text: str, amounts: set[str]) -> None:
        assert customer_amounts([text]) == amounts


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
