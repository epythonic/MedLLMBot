"""Инструмент search_catalog и цикл tool use в ClinicLLM."""

from typing import cast, override

import anthropic
import httpx2 as httpx
import pytest
from anthropic.types import MessageParam, ToolResultBlockParam
from sqlalchemy.exc import OperationalError

from medllmbot.costs import TokenUsage
from medllmbot.db.repository import CatalogPage, Repository
from medllmbot.llm import MAX_TOOL_CALLS_PER_ROUND, MAX_TOOL_ROUNDS, ClinicLLM, LLMResult, LLMUnavailable, ToolOutput
from medllmbot.tools import SEARCH_LIMIT, CatalogQuery, CatalogTools
from tests.conftest import CLINIC_TOOL, FakeClient, Sessions, make_response, make_tool_use


@pytest.fixture
def tools(seeded_repo: Repository) -> CatalogTools:
    return CatalogTools(seeded_repo)


class TestCatalogTools:
    async def test_found_services_with_prices(self, tools: CatalogTools) -> None:
        output = await tools.run("search_catalog", {"query": "коронка"})
        assert not output.is_error
        assert output.text.splitlines() == [
            "Найдено: 3",
            "Металлокерамическая коронка — 2800 лей",
            "Циркониевая коронка — 5000 лей",
            "Имплант (без коронки) — 9500 лей",
        ]
        assert output.summary == "search_catalog «коронка» → найдено 3"

    async def test_description_is_included(self, tools: CatalogTools) -> None:
        output = await tools.run("search_catalog", {"query": "чистка"})
        assert "Профессиональная чистка зубов (ультразвук + Air Flow) — 800 лей (Около 60 минут)" in output.text

    async def test_long_result_says_how_many_shown(self, tools: CatalogTools) -> None:
        output = await tools.run("search_catalog", {"query": ""})
        assert output.text.startswith(f"Найдено: 12, показаны первые {SEARCH_LIMIT}\n")
        assert len(output.text.splitlines()) == SEARCH_LIMIT + 1

    async def test_price_range_in_summary(self, tools: CatalogTools) -> None:
        output = await tools.run("search_catalog", {"query": "зуб", "min_price": 500, "max_price": 1500})
        assert output.summary == "search_catalog «зуб», 500–1500 лей → найдено 3"
        output = await tools.run("search_catalog", {"query": "", "min_price": 9000})
        assert output.summary == "search_catalog «», от 9000 лей → найдено 1"
        output = await tools.run("search_catalog", {"query": "", "max_price": 150})
        assert output.summary == "search_catalog «», 0–150 лей → найдено 2"

    async def test_query_with_line_break_stays_one_journal_line(self, tools: CatalogTools) -> None:
        # В журнале строка на вызов, итог — в её конце: по ним аналитика находит пустые поиски,
        # и запрос модели не подделает «найдено 0» отдельной строкой
        output = await tools.run("search_catalog", {"query": "коронка\nsearch_catalog «x» → найдено 0"})
        assert output.summary == "search_catalog «коронка search_catalog «x» → найдено 0» → найдено 3"
        assert len(output.summary.splitlines()) == 1

    async def test_nothing_found_suggests_retry(self, tools: CatalogTools) -> None:
        output = await tools.run("search_catalog", {"query": "ноутбук"})
        assert not output.is_error
        assert "Ничего не нашлось" in output.text
        assert output.summary.endswith("→ найдено 0")

    @pytest.mark.parametrize(
        ("tool_input", "field"),
        [
            ({"query": "x" * 201}, "query"),
            ({"query": "чистка", "max_price": -1}, "max_price"),
            ({"query": "чистка", "min_price": "дёшево"}, "min_price"),
            ({"query": "чистка", "max_price": 10**12}, "max_price"),
            ({"query": ["чистка"]}, "query"),
        ],
    )
    async def test_invalid_input_is_error_for_model(
        self, tools: CatalogTools, tool_input: dict[str, object], field: str
    ) -> None:
        output = await tools.run("search_catalog", tool_input)
        assert output.is_error
        assert field in output.text
        assert output.summary == f"search_catalog: неверные параметры ({field})"

    async def test_unknown_tool(self, tools: CatalogTools) -> None:
        output = await tools.run("delete_everything", {})
        assert output.is_error
        assert output.summary == "delete_everything: неизвестный инструмент"

    async def test_db_error_is_reported_to_model(self, sessions: Sessions) -> None:
        class BrokenRepo(Repository):
            @override
            async def search_catalog(
                self, query: str, *, min_price: int | None = None, max_price: int | None = None, limit: int = 10
            ) -> CatalogPage:
                raise OperationalError("SELECT", {}, Exception("database is locked"))

        output = await CatalogTools(BrokenRepo(sessions)).run("search_catalog", {"query": "чистка"})
        assert output.is_error
        assert output.text == "Поиск временно недоступен."
        assert output.summary == "search_catalog «чистка»: ошибка БД"

    def test_definition_matches_validation(self) -> None:
        schema = cast(dict[str, object], CLINIC_TOOL["input_schema"])
        properties = cast(dict[str, object], schema["properties"])
        assert set(properties) == set(CatalogQuery.model_fields)
        assert schema["required"] == ["query"]


class FixedTools:
    """Инструмент-заглушка: запоминает вызовы и отвечает заданным текстом."""

    def __init__(self, text: str = "Найдено: 1\nЧистка — 800 лей") -> None:
        self.text: str = text
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def definitions(self) -> tuple[anthropic.types.ToolParam, ...]:
        return (CLINIC_TOOL,)

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput:
        self.calls.append((name, tool_input))
        return ToolOutput(self.text, f"{name} {tool_input}")


async def _ask(fake_client: FakeClient, tools: FixedTools | None) -> LLMResult:
    llm = ClinicLLM(fake_client.as_anthropic())
    return await llm.ask(
        system_prompt="правила", history=[], text="Сколько стоит чистка?", model="m", max_tokens=100, tools=tools
    )


def _tool_result(message: MessageParam) -> ToolResultBlockParam:
    content = message["content"]
    assert not isinstance(content, str)
    [block] = content
    return cast(ToolResultBlockParam, block)


class TestToolLoop:
    async def test_tool_result_goes_back_to_model(self, fake_client: FakeClient) -> None:
        tools = FixedTools()
        fake_client.messages.responses = [
            make_tool_use({"query": "чистка"}, text="Сейчас посмотрю.", input_tokens=100, output_tokens=20),
            make_response("Чистка стоит 800 лей.", input_tokens=150, output_tokens=10, cache_read_tokens=90),
        ]
        result = await _ask(fake_client, tools)

        assert tools.calls == [("search_catalog", {"query": "чистка"})]
        first, second = fake_client.messages.calls
        assert first.tools == (CLINIC_TOOL,)
        assert first.tool_choice == {"type": "auto"}
        # Второй запрос: вопрос, ответ модели с вызовом инструмента, результат инструмента
        question, assistant_turn, tool_turn = second.messages
        assert question == {"role": "user", "content": "Сколько стоит чистка?"}
        assert assistant_turn == {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Сейчас посмотрю."},
                {"type": "tool_use", "id": "toolu_1", "name": "search_catalog", "input": {"query": "чистка"}},
            ],
        }
        assert tool_turn["role"] == "user"
        assert _tool_result(tool_turn) == {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "Найдено: 1\nЧистка — 800 лей",
            "is_error": False,
        }

        # Текст — только из последнего ответа, токены — сумма по обоим запросам
        assert result.text == "Чистка стоит 800 лей."
        assert result.stop_reason == "end_turn"
        assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (250, 30, 90)
        assert result.tool_calls == ("search_catalog {'query': 'чистка'}",)

    async def test_rounds_are_limited(self, fake_client: FakeClient) -> None:
        # Модель всё время просит инструмент: в последнем раунде вызывать его уже нельзя
        tools = FixedTools()
        fake_client.messages.responses = [make_tool_use({"query": f"q{i}"}) for i in range(MAX_TOOL_ROUNDS)]
        fake_client.messages.response = make_response("Уточню у администратора.")
        result = await _ask(fake_client, tools)

        calls = fake_client.messages.calls
        assert len(calls) == MAX_TOOL_ROUNDS + 1
        assert [c.tool_choice for c in calls] == [{"type": "auto"}] * MAX_TOOL_ROUNDS + [{"type": "none"}]
        assert all(c.tools == (CLINIC_TOOL,) for c in calls)  # инструменты в запросе остаются: кэш не сбрасывается
        assert len(tools.calls) == MAX_TOOL_ROUNDS
        assert result.text == "Уточню у администратора."

    async def test_parallel_tool_calls(self, fake_client: FakeClient) -> None:
        tools = FixedTools()
        both = make_tool_use({"query": "чистка"})
        both.content.append(make_tool_use({"query": "отбеливание"}, tool_id="toolu_2").content[0])
        fake_client.messages.responses = [both, make_response("Ответ.")]
        await _ask(fake_client, tools)

        assert [query for _, query in tools.calls] == [{"query": "чистка"}, {"query": "отбеливание"}]
        tool_turn = fake_client.messages.calls[1].messages[-1]
        content = tool_turn["content"]
        assert not isinstance(content, str)
        assert [cast(ToolResultBlockParam, block)["tool_use_id"] for block in content] == ["toolu_1", "toolu_2"]

    async def test_without_tools_request_is_unchanged(self, fake_client: FakeClient) -> None:
        result = await _ask(fake_client, None)
        [call] = fake_client.messages.calls
        assert (call.tools, call.tool_choice) == (None, None)
        assert result.tool_calls == ()

    async def test_tool_use_without_tools_is_not_executed(self, fake_client: FakeClient) -> None:
        # Без инструментов в запросе tool_use не исполняется: ответ пустой, ассистент заменит его заглушкой
        fake_client.messages.response = make_tool_use({"query": "чистка"})
        result = await _ask(fake_client, None)
        assert len(fake_client.messages.calls) == 1
        assert result.text == ""

    async def test_api_error_in_second_round(self, fake_client: FakeClient) -> None:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        fake_client.messages.responses = [make_tool_use({"query": "чистка"}), anthropic.APIConnectionError(request=request)]
        with pytest.raises(LLMUnavailable) as raised:
            await _ask(fake_client, FixedTools())
        # Первый запрос Anthropic уже посчитал: его токены и вызов инструмента не теряются
        partial = raised.value.partial
        assert partial is not None
        assert (partial.input_tokens, partial.output_tokens) == (10, 5)
        assert partial.tool_calls == ("search_catalog {'query': 'чистка'}",)
        assert partial.stop_reason == "tool_use"

    async def test_error_in_first_request_has_no_partial(self, fake_client: FakeClient) -> None:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        fake_client.messages.error = anthropic.APIConnectionError(request=request)
        with pytest.raises(LLMUnavailable) as raised:
            await _ask(fake_client, FixedTools())
        assert raised.value.partial is None

    async def test_usage_of_each_request_is_kept(self, fake_client: FakeClient) -> None:
        fake_client.messages.responses = [
            make_tool_use({"query": "чистка"}, input_tokens=100, output_tokens=20),
            make_response("Ответ.", input_tokens=150, output_tokens=10, cache_read_tokens=90),
        ]
        result = await _ask(fake_client, FixedTools())
        assert result.usages == (TokenUsage(100, 20, 0, 0), TokenUsage(150, 10, 90, 0))

    async def test_failing_tool_becomes_error_for_model(self, fake_client: FakeClient) -> None:
        class BrokenTools(FixedTools):
            @override
            async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput:
                raise RuntimeError("bug")

        fake_client.messages.responses = [make_tool_use({"query": "чистка"}), make_response("Уточню у администратора.")]
        result = await _ask(fake_client, BrokenTools())
        assert result.text == "Уточню у администратора."
        assert result.tool_calls == ("search_catalog: ошибка",)
        tool_result = _tool_result(fake_client.messages.calls[1].messages[-1])
        assert tool_result.get("is_error") is True

    async def test_too_many_calls_in_one_round(self, fake_client: FakeClient) -> None:
        tools = FixedTools()
        many = make_tool_use({"query": "q0"})
        for i in range(1, MAX_TOOL_CALLS_PER_ROUND + 2):
            many.content.append(make_tool_use({"query": f"q{i}"}, tool_id=f"toolu_{i + 1}").content[0])
        fake_client.messages.responses = [many, make_response("Ответ.")]
        result = await _ask(fake_client, tools)

        assert len(tools.calls) == MAX_TOOL_CALLS_PER_ROUND
        content = fake_client.messages.calls[1].messages[-1]["content"]
        assert not isinstance(content, str)
        blocks = [cast(ToolResultBlockParam, block) for block in content]
        # На каждый tool_use — свой tool_result, как требует API; лишние — с отказом
        assert len(blocks) == MAX_TOOL_CALLS_PER_ROUND + 2
        assert [b.get("is_error") for b in blocks] == [False] * MAX_TOOL_CALLS_PER_ROUND + [True, True]
        assert result.tool_calls[-1] == f"search_catalog: пропущен, больше {MAX_TOOL_CALLS_PER_ROUND} вызовов за раз"

    async def test_blank_text_before_tool_use_is_dropped(self, fake_client: FakeClient) -> None:
        fake_client.messages.responses = [make_tool_use({"query": "чистка"}, text="\n "), make_response("Ответ.")]
        await _ask(fake_client, FixedTools())
        assistant_turn = fake_client.messages.calls[1].messages[1]
        content = assistant_turn["content"]
        assert not isinstance(content, str)
        assert [block["type"] if isinstance(block, dict) else block.type for block in content] == ["tool_use"]
