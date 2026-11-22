"""Order status: owner check, brute-force protection, the order_status tool and the assistant's reply."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from medllmbot.assistant import ClinicAssistant
from medllmbot.db.demo_shop import SHOP_ORDERS
from medllmbot.db.models import AuditOutcome, BusinessProfile, Order, OrderStatus
from medllmbot.db.repository import Repository
from medllmbot.db.seed import reset_demo, seed_demo
from medllmbot.llm import ClinicLLM, ToolOutput
from medllmbot.orders import (
    DatabaseOrders,
    FailedLookups,
    LookupLimits,
    OrderInfo,
    normalize_number,
    order_key,
    phone_matches,
)
from medllmbot.profiles import PROFILES
from medllmbot.prompts import rules
from medllmbot.tools import NO_DATE, ORDER_LOCKED, ORDER_NOT_FOUND, ORDER_STATUS, CatalogTools, OrderTools, Toolbox
from tests.conftest import CHAT_ID, CLINIC_TOOL, FakeClient, Sessions, load_audit, make_response, make_tool_use

ORDER = OrderInfo(
    number="DE-10022",
    status=OrderStatus.SHIPPED,
    items="Ноутбук Voltix Air 14, беспроводная мышь Nordwave Silent",
    total=11_848,
    delivery_date=date(2026, 11, 23),
    phone="+373 69 000 102",
)


class FakeOrders:
    """Shop system: orders keyed by number; remembers which numbers were asked for.

    `delay` is the shop's response time (CMS and 1C take hundreds of milliseconds), `error` is its system failure.
    """

    def __init__(self, *orders: OrderInfo, delay: float = 0, error: Exception | None = None) -> None:
        self.orders: dict[str, OrderInfo] = {order_key(order.number): order for order in orders}
        self.asked: list[str] = []
        self.delay: float = delay
        self.error: Exception | None = error

    async def find(self, number: str) -> OrderInfo | None:
        self.asked.append(number)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.orders.get(order_key(number))


class FakeClock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("DE-10022", "DE-10022"),
        ("de-10022", "DE-10022"),
        (" de 10022 ", "DE-10022"),
        ("DE - 10022", "DE-10022"),
        ("DE\u201310022", "DE-10022"),  # dash from phone autocorrect
        ("#DE-10022", "DE-10022"),
        ("№ DE-10022", "DE-10022"),
        ("D\u0415-10022", "DE-10022"),  # Cyrillic «Е» instead of the Latin one
        ("DE10022", "DE10022"),
        ("10022", "10022"),
    ],
)
def test_order_number_is_normalized(text: str, number: str) -> None:
    assert normalize_number(text) == number


@pytest.mark.parametrize("text", ["", "?", "DE", "заказ 5", "DE_10022", "ДЕ-10022", "x" * 40])
def test_not_an_order_number(text: str) -> None:
    assert normalize_number(text) is None


def test_number_with_and_without_dash_is_one_order() -> None:
    assert order_key("DE-10022") == order_key("DE10022") == "DE10022"


def test_phone_is_hidden_from_repr() -> None:
    assert "000 102" not in repr(ORDER)


def test_phone_matches_last_digits() -> None:
    assert phone_matches("+373 69 000 102", "0102")
    assert phone_matches("069-000-102", "0102")
    assert not phone_matches("+373 69 000 102", "0103")
    assert not phone_matches("+373 69 000 102", "102")  # exactly 4 digits
    assert not phone_matches("+373 69 000 102", "+102")


class TestFailedLookups:
    def test_blocks_after_limit_and_forgets_after_window(self) -> None:
        clock = FakeClock()
        failures = FailedLookups(max_failures=3, window=60, clock=clock)
        for _ in range(3):
            assert failures.try_begin(CHAT_ID)
        assert not failures.try_begin(CHAT_ID)
        assert failures.blocked(CHAT_ID)
        assert failures.try_begin(CHAT_ID + 1)  # another chat is unaffected
        clock.now += 61
        assert not failures.blocked(CHAT_ID)

    def test_success_gives_attempt_back(self) -> None:
        failures = FailedLookups(max_failures=1)
        for _ in range(3):
            assert failures.try_begin(CHAT_ID)
            failures.succeeded(CHAT_ID)
        assert not failures.blocked(CHAT_ID)

    def test_cleanup_forgets_quiet_keys(self) -> None:
        clock = FakeClock()
        failures = FailedLookups(window=60, clock=clock)
        failures.try_begin(CHAT_ID)
        failures.try_begin("DE10022")
        clock.now += 61
        failures.cleanup()
        assert len(failures) == 0


def _tools(backend: FakeOrders, limits: LookupLimits | None = None, chat_id: int = CHAT_ID) -> OrderTools:
    return OrderTools(backend, chat_id, limits or LookupLimits())


def _limits(per_chat: int = 5, per_order: int = 10) -> LookupLimits:
    return LookupLimits(FailedLookups(max_failures=per_chat), FailedLookups(max_failures=per_order))


class TestOrderTools:
    async def test_found_order(self) -> None:
        output = await _tools(FakeOrders(ORDER)).run("order_status", {"order_number": "de-10022", "phone_last4": "0102"})
        assert not output.is_error
        assert output.text.splitlines() == [
            "Заказ DE-10022: Передан в доставку.",
            "Состав: Ноутбук Voltix Air 14, беспроводная мышь Nordwave Silent",
            "Сумма: 11848 лей.",
            "Ожидаемая дата доставки или выдачи: 23.11.2026.",
        ]
        assert "000 102" not in output.text  # the phone is not passed to the model
        assert output.summary == "order_status DE-10022 → Передан в доставку"
        assert output.prices == (11_848,)

    async def test_wrong_phone_and_wrong_number_look_the_same(self) -> None:
        # The reply does not reveal whether the order exists
        tools = _tools(FakeOrders(ORDER))
        wrong_phone = await tools.run("order_status", {"order_number": "DE-10022", "phone_last4": "9999"})
        wrong_number = await tools.run("order_status", {"order_number": "DE-99999", "phone_last4": "0102"})
        assert wrong_phone.text == wrong_number.text == ORDER_NOT_FOUND
        assert wrong_phone.prices == wrong_number.prices == ()
        # The admin sees the reason in the audit log
        assert wrong_phone.summary == "order_status DE-10022 → телефон не совпал"
        assert wrong_number.summary == "order_status DE-99999 → не найден"

    async def test_guessing_is_stopped(self) -> None:
        backend = FakeOrders(ORDER)
        tools = _tools(backend, _limits(per_chat=5))
        for guess in range(5):
            await tools.run("order_status", {"order_number": "DE-10022", "phone_last4": f"{guess:04}"})
        # Even the correct digits are no longer checked: guessing is stopped
        output = await tools.run("order_status", {"order_number": "DE-10022", "phone_last4": "0102"})
        assert (output.text, output.is_error) == (ORDER_LOCKED, True)
        assert len(backend.asked) == 5

    async def test_success_does_not_count_as_failure(self) -> None:
        limits = _limits(per_chat=1, per_order=1)
        tools = _tools(FakeOrders(ORDER), limits)
        for _ in range(3):
            await tools.run("order_status", {"order_number": "DE-10022", "phone_last4": "0102"})
        assert not limits.by_chat.blocked(CHAT_ID)
        assert not limits.by_order.blocked("DE10022")

    async def test_parallel_messages_do_not_bypass_limit(self) -> None:
        # While the slow shop system is responding, the chat's other messages are already rejected
        backend = FakeOrders(ORDER, delay=0.05)
        tools = _tools(backend, _limits(per_chat=5))
        guesses: list[dict[str, object]] = [{"order_number": "DE-10022", "phone_last4": f"{g:04}"} for g in range(10)]
        outputs = await asyncio.gather(*(tools.run("order_status", guess) for guess in guesses))
        assert len(backend.asked) == 5
        assert sum(output.text == ORDER_LOCKED for output in outputs) == 5

    async def test_one_order_is_protected_across_chats(self) -> None:
        # Order numbers are sequential: guessing one order from different accounts also hits the limit
        backend = FakeOrders(ORDER)
        limits = _limits(per_chat=5, per_order=10)
        for chat in range(12):
            await _tools(backend, limits, chat_id=chat).run(
                "order_status", {"order_number": "DE-10022", "phone_last4": f"{chat:04}"}
            )
        assert len(backend.asked) == 10
        # The limit belongs to that order: another order is checked as usual
        other = OrderInfo("DE-5", OrderStatus.NEW, "Чайник", 399, None, "069000005")
        output = await _tools(FakeOrders(other), limits, chat_id=99).run(
            "order_status", {"order_number": "DE-5", "phone_last4": "0005"}
        )
        assert output.summary == "order_status DE-5 → Принят"

    async def test_store_failure_is_not_customer_failure(self) -> None:
        limits = _limits(per_chat=1)
        tools = _tools(FakeOrders(ORDER, error=ConnectionError("1С недоступна")), limits)
        with pytest.raises(ConnectionError):
            await tools.run("order_status", {"order_number": "DE-10022", "phone_last4": "0102"})
        assert not limits.by_chat.blocked(CHAT_ID)

    async def test_number_formats_find_order(self) -> None:
        for written in ("DE10022", "de 10022", "№ DE\u201310022"):
            output = await _tools(FakeOrders(ORDER)).run("order_status", {"order_number": written, "phone_last4": "0102"})
            assert output.summary == "order_status DE-10022 → Передан в доставку", written

    @pytest.mark.parametrize(
        "tool_input",
        [{"order_number": "DE-10022"}, {"order_number": "DE-10022", "phone_last4": "01a2"}, {"phone_last4": "0102"}],
    )
    async def test_invalid_input(self, tool_input: dict[str, object]) -> None:
        backend = FakeOrders(ORDER)
        output = await _tools(backend).run("order_status", tool_input)
        assert output.is_error
        assert backend.asked == []

    async def test_garbage_number_is_not_looked_up(self) -> None:
        # Such a number is not sent to the shop and is not an attempt: a typo won't lock out an honest client
        backend = FakeOrders(ORDER)
        limits = _limits(per_chat=1)
        output = await _tools(backend, limits).run("order_status", {"order_number": "мой заказ", "phone_last4": "0102"})
        assert output.text == ORDER_NOT_FOUND
        assert output.summary == "order_status 'мой заказ' → не похоже на номер заказа"
        assert backend.asked == []
        assert not limits.by_chat.blocked(CHAT_ID)

    async def test_order_without_date(self) -> None:
        order = OrderInfo("DE-1", OrderStatus.NEW, "Чайник", 399, None, "069000001")
        output = await _tools(FakeOrders(order)).run("order_status", {"order_number": "DE-1", "phone_last4": "0001"})
        assert "Ожидаемая дата" not in output.text
        assert output.text.endswith(NO_DATE)


class TestToolbox:
    async def test_dispatches_by_name(self, seeded_repo: Repository) -> None:
        toolbox = Toolbox(CatalogTools(seeded_repo), _tools(FakeOrders(ORDER)))
        assert [tool["name"] for tool in toolbox.definitions] == ["search_catalog", "order_status"]
        assert toolbox.definitions == (CLINIC_TOOL, ORDER_STATUS)

        found = await toolbox.run("search_catalog", {"query": "коронка"})
        order = await toolbox.run("order_status", {"order_number": "DE-10022", "phone_last4": "0102"})
        assert found.summary.startswith("search_catalog")
        assert order.summary.startswith("order_status")

    async def test_unknown_tool(self) -> None:
        output = await Toolbox(_tools(FakeOrders())).run("search_catalog", {"query": "x"})
        assert output == ToolOutput(
            "Инструмента search_catalog нет.", "search_catalog: неизвестный инструмент", is_error=True
        )


async def test_database_orders_ignore_case_and_dash(repo: Repository, sessions: Sessions) -> None:
    async with sessions.begin() as s:
        s.add(Order(number="DE-7", customer_name="Ион", phone="069000007", items="Чайник", total=399))
    for written in ("DE-7", "de-7", "DE7"):
        found = await DatabaseOrders(repo).find(written)
        assert found is not None, written
        assert (found.number, found.status, found.total) == ("DE-7", OrderStatus.NEW, 399)
    assert await DatabaseOrders(repo).find("DE-8") is None


class TestShopAssistant:
    @pytest.fixture
    async def shop(self, repo: Repository, sessions: Sessions) -> Repository:
        await seed_demo(sessions, BusinessProfile.SHOP)
        await repo.touch_client(CHAT_ID, "ivan", "Иван")
        return repo

    async def test_order_status_answer(self, fake_client: FakeClient, shop: Repository, sessions: Sessions) -> None:
        fake_client.messages.responses = [
            make_tool_use({"order_number": "DE-10022", "phone_last4": "0102"}, name="order_status"),
            make_response("Заказ DE-10022 передан в доставку, сумма 11 848 лей, ожидается 23.11."),
        ]
        assistant = ClinicAssistant(ClinicLLM(fake_client.as_anthropic()), shop)
        reply = await assistant.reply(CHAT_ID, "Где мой заказ DE-10022? Последние цифры 0102", await shop.get_config())

        # 11 848 лей is not in the catalog: it is the order total from the shop system, the price check skips it
        assert reply.outcome is AuditOutcome.ANSWERED
        tool_result = str(fake_client.messages.calls[1].messages[-1]["content"])
        assert "Заказ DE-10022: Передан в доставку." in tool_result
        [event] = await load_audit(sessions)
        assert event.tool_calls == "order_status DE-10022 → Передан в доставку"

    async def test_invented_order_sum_is_still_replaced(self, fake_client: FakeClient, shop: Repository) -> None:
        fake_client.messages.responses = [
            make_tool_use({"order_number": "DE-10022", "phone_last4": "9999"}, name="order_status"),
            make_response("Ваш заказ на 12 000 лей уже в пути."),
        ]
        assistant = ClinicAssistant(ClinicLLM(fake_client.as_anthropic()), shop)
        reply = await assistant.reply(CHAT_ID, "Где заказ DE-10022, цифры 9999", await shop.get_config())
        assert reply.outcome is AuditOutcome.PRICE_REPLACED

    async def test_follow_up_about_sum_needs_fresh_check(self, fake_client: FakeClient, shop: Repository) -> None:
        # An order total is allowed only if order_status returned it in this reply: an old answer from history
        # may be outdated. That is why the shop rule asks to call the tool again
        assistant = ClinicAssistant(ClinicLLM(fake_client.as_anthropic()), shop)
        config = await shop.get_config()
        fake_client.messages.responses = [
            make_tool_use({"order_number": "DE-10022", "phone_last4": "0102"}, name="order_status"),
            make_response("Сумма заказа — 11 848 лей."),
        ]
        first = await assistant.reply(CHAT_ID, "Где DE-10022? Цифры 0102", config)
        assert first.outcome is AuditOutcome.ANSWERED

        fake_client.messages.response = make_response("Напоминаю: 11 848 лей.")
        from_history = await assistant.reply(CHAT_ID, "Напомните сумму", config)
        assert from_history.outcome is AuditOutcome.PRICE_REPLACED

        fake_client.messages.responses = [
            make_tool_use({"order_number": "DE-10022", "phone_last4": "0102"}, name="order_status"),
            make_response("Сумма заказа — 11 848 лей."),
        ]
        fresh = await assistant.reply(CHAT_ID, "А ещё раз сумму?", config)
        assert fresh.outcome is AuditOutcome.ANSWERED
        assert "Статус, сумму и дату бери только из order_status в этом ответе" in rules(BusinessProfile.SHOP)

    async def test_lookups_are_limited_across_messages(self, fake_client: FakeClient, shop: Repository) -> None:
        # The failure counter lives in the assistant, not in one question: guessing across messages hits the limit too
        assistant = ClinicAssistant(ClinicLLM(fake_client.as_anthropic()), shop)
        config = await shop.get_config()
        for guess in range(6):
            fake_client.messages.responses = [
                make_tool_use({"order_number": "DE-10022", "phone_last4": f"{guess:04}"}, name="order_status"),
                make_response("Заказ не найден, проверьте номер."),
            ]
            await assistant.reply(CHAT_ID, f"DE-10022, цифры {guess:04}", config)
        last_result = str(fake_client.messages.calls[-1].messages[-1]["content"])
        assert ORDER_LOCKED in last_result

    async def test_store_system_is_pluggable(self, fake_client: FakeClient, shop: Repository) -> None:
        # A shop plugs in its own back-office system through the same interface
        backend = FakeOrders(ORDER)
        fake_client.messages.responses = [
            make_tool_use({"order_number": "DE-10022", "phone_last4": "0102"}, name="order_status"),
            make_response("Передан в доставку."),
        ]
        assistant = ClinicAssistant(ClinicLLM(fake_client.as_anthropic()), shop, orders=backend)
        await assistant.reply(CHAT_ID, "Где DE-10022? 0102", await shop.get_config())
        assert backend.asked == ["DE-10022"]

    def test_shop_rules_mention_orders(self) -> None:
        assert "order_status" in rules(BusinessProfile.SHOP)
        assert PROFILES[BusinessProfile.SHOP].orders
        assert not PROFILES[BusinessProfile.CLINIC].orders


async def test_clinic_has_no_order_tool(fake_client: FakeClient, seeded_repo: Repository) -> None:
    assistant = ClinicAssistant(ClinicLLM(fake_client.as_anthropic()), seeded_repo)
    await assistant.reply(CHAT_ID, "Где мой заказ?", await seeded_repo.get_config())
    [call] = fake_client.messages.calls
    assert call.tools is None  # catalog is in the prompt and the clinic has no orders, so no tools are needed
    assert "order_status" not in call.system[0]["text"]


class TestDemoOrders:
    async def test_seeded_with_fresh_dates(self, sessions: Sessions) -> None:
        await seed_demo(sessions, BusinessProfile.SHOP)
        async with sessions() as s:
            orders = {o.number: o for o in await s.scalars(select(Order))}
        assert set(orders) == {number for number, *_ in SHOP_ORDERS}
        today = datetime.now(UTC).date()
        assert orders["DE-10022"].delivery_date == today + timedelta(days=1)
        assert orders["DE-10025"].delivery_date is None

    async def test_demo_phones_are_fake(self) -> None:
        assert all(phone.startswith("+373 69 000 ") for _, _, phone, *_ in SHOP_ORDERS)

    async def test_reset_to_clinic_removes_orders(self, repo: Repository, sessions: Sessions) -> None:
        await reset_demo(sessions, BusinessProfile.SHOP)
        await reset_demo(sessions, BusinessProfile.CLINIC)
        assert await DatabaseOrders(repo).find("DE-10022") is None
