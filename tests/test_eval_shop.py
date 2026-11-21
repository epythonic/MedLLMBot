"""Оценка магазина без сети: проверки кодом, разбор ответа судьи и прогон с фейковым Claude.

Перед первым платным прогоном набор должен пропускать эталонный ответ и ронять пустой, а сбои API —
попадать в errors.jsonl, а не в оценки.
"""

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from evals.shop.cases import CASE_BY_ID, CASES, ORDER, SEARCH, Case, price
from evals.shop.grading import (
    JUDGE_SCHEMA,
    JUDGE_SYSTEM,
    JudgeError,
    ToolEvent,
    answer_language,
    code_checks,
    judge,
    judge_prompt,
    parse_verdict,
)
from evals.shop.run import Summary, regrade, run_all, select_cases
from medllmbot.db.migrate import upgrade_db
from medllmbot.db.models import AuditOutcome

JUDGE = "claude-sonnet-5"
BOT = "claude-haiku-5-5"
VERDICT_OK = json.dumps(
    {name: {"reason": "ок", "pass": True} for name in ("grounded", "rules", "helpful")}, ensure_ascii=False
)


# ---------- Набор вопросов ----------


def test_cases_match_the_approved_set() -> None:
    assert len(CASES) == 38
    assert len(CASE_BY_ID) == len(CASES)  # id не повторяются
    groups = Counter(case.group for case in CASES)
    assert list(groups.values()) == [7, 6, 4, 6, 6, 5, 4]
    assert {case.lang for case in CASES if case.group == "По-румынски"} == {"ro"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_patterns_compile(case: Case) -> None:
    for pattern in (*case.facts, *case.forbidden):
        re.compile(pattern)
    assert case.expect.strip()


@pytest.mark.parametrize("written", ["4 999", "4999", "4.999", "4,999", "4 999", "4 999", "цена 4 999 лей"])
def test_price_accepts_chat_spellings(written: str) -> None:
    assert re.search(price(4999), written)


@pytest.mark.parametrize("written", ["14 999", "4 9990", "24999", "499", "1.4999"])
def test_price_is_not_part_of_another_number(written: str) -> None:
    assert not re.search(price(4999), written)


def test_price_alternatives_and_small_numbers() -> None:
    assert re.search(price(4299, 4899), "Kivora X5 за 4 899 лей")
    assert re.search(price(150), "Доставка — 150 лей")
    assert not re.search(price(150), "1500 лей")


# ---------- Проверки кодом: эталон и пустышка ----------

GOOD = "Робот-пылесос Brisko Clean S8 стоит 4 999 лей. Наличие и итоговую цену подтвердит менеджер."


def test_oracle_answer_passes_every_code_check() -> None:
    case = CASE_BY_ID["price-01"]
    scores = code_checks(case, GOOD, AuditOutcome.ANSWERED, [SEARCH])
    assert scores == {"delivered": 1, "language": 1, "format": 1, "facts": 1, "tools": 1}


@pytest.mark.parametrize("answer", ["", "Не знаю.", "Уточню у менеджера и вернусь к вам."])
def test_null_answers_fail(answer: str) -> None:
    scores = code_checks(CASE_BY_ID["price-01"], answer, AuditOutcome.ANSWERED, [])
    assert scores["facts"] == 0
    assert scores["tools"] == 0


def test_fallback_is_not_delivered() -> None:
    scores = code_checks(CASE_BY_ID["price-01"], GOOD, AuditOutcome.PRICE_REPLACED, [SEARCH])
    assert scores["delivered"] == 0


def test_order_leak_and_tool_rules() -> None:
    case = CASE_BY_ID["order-02"]  # номер без цифр телефона
    asked = code_checks(case, "Назовите, пожалуйста, последние 4 цифры телефона из заказа.", AuditOutcome.ANSWERED, [])
    assert (asked["facts"], asked["no_leak"], asked["tools"]) == (1, 1, 1)
    # «Для подтверждения» — не статус «Подтверждён»
    polite = code_checks(case, "Для подтверждения назовите последние 4 цифры телефона.", AuditOutcome.ANSWERED, [])
    assert polite["no_leak"] == 1
    leaked = code_checks(case, "Заказ подтверждён: стиральная машина, 7 899 лей.", AuditOutcome.ANSWERED, [ORDER])
    assert (leaked["no_leak"], leaked["tools"]) == (0, 0)


def test_monthly_payment_is_forbidden() -> None:
    case = CASE_BY_ID["rules-01"]
    computed = code_checks(case, "Ноутбук стоит 11 499 лей, это 958 лей в месяц.", AuditOutcome.ANSWERED, [SEARCH])
    assert computed["no_leak"] == 0
    honest = code_checks(case, "Ноутбук стоит 11 499 лей; платёж посчитает менеджер.", AuditOutcome.ANSWERED, [SEARCH])
    assert (honest["facts"], honest["no_leak"]) == (1, 1)


@pytest.mark.parametrize(
    ("answer", "lang"),
    [
        ("Ноутбук Voltix Air 14 стоит 11 499 лей.", "ru"),
        ("Robotul aspirator Brisko Clean S8 costă 4 999 lei.", "ro"),
        ("Livrarea în Moldova costă 150 lei, 2–4 zile lucrătoare.", "ro"),
        ("Laptopul Робот-пылесос costă 4 999 lei și este disponibil.", "ro"),  # русское название из каталога
        ("The vacuum costs 4999 lei.", None),
        ("", None),
    ],
)
def test_answer_language(answer: str, lang: str | None) -> None:
    assert answer_language(answer) == lang


@pytest.mark.parametrize("answer", ["**Цена**: 4 999 лей", "## Ноутбуки", "* Voltix — 11 499", "| Модель | Цена |"])
def test_markdown_fails_format(answer: str) -> None:
    assert code_checks(CASE_BY_ID["rules-04"], answer, AuditOutcome.ANSWERED, [])["format"] == 0


def test_dash_list_is_plain_text() -> None:
    answer = "Есть два варианта:\n- Voltix Air 14 — 11 499 лей\n- Nordwave Study 15 — 7 299 лей"
    assert code_checks(CASE_BY_ID["choice-01"], answer, AuditOutcome.ANSWERED, [SEARCH])["format"] == 1


# ---------- Судья ----------


def test_parse_verdict() -> None:
    data = {
        "grounded": {"reason": "цена из поиска", "pass": True},
        "rules": {"reason": "обещает наличие", "pass": False},
        "helpful": {"reason": "отвечает", "pass": True},
    }
    scores, explanations = parse_verdict(json.dumps(data, ensure_ascii=False))
    assert scores == {"grounded": 1, "rules": 0, "helpful": 1}
    assert explanations["rules"] == "обещает наличие"
    with pytest.raises(ValidationError):
        parse_verdict('{"grounded": {"pass": true}}')
    with pytest.raises(ValidationError):
        parse_verdict("")


def test_judge_prompt_marks_data_and_tool_errors() -> None:
    events = [ToolEvent("search_catalog", {"query": "утюг"}, "Ничего не нашлось.", is_error=False)]
    prompt = judge_prompt(CASE_BY_ID["missing-04"], "ПРАВИЛА", events, "Утюгов у нас нет.")
    assert '<tool_results>\nsearch_catalog({"query": "утюг"}):\nНичего не нашлось.\n</tool_results>' in prompt
    assert "<answer>\nУтюгов у нас нет.\n</answer>" in prompt
    assert "Бот не вызывал инструменты." in judge_prompt(CASE_BY_ID["rules-04"], "ПРАВИЛА", [], "Нет.")


def message(text: str, *, model: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model=model,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


class FakeClaude:
    """Отвечает за бота и за судью по полю model; запоминает запросы судьи."""

    bot_answers: list[SimpleNamespace | Exception]
    bot_answer: SimpleNamespace
    judge_answer: SimpleNamespace
    judge_calls: list[dict[str, object]]
    messages: FakeClaude

    def __init__(self) -> None:
        self.bot_answers = []
        self.bot_answer = message(GOOD, model=BOT)
        self.judge_answer = message(VERDICT_OK, model=JUDGE)
        self.judge_calls = []
        self.messages = self

    async def create(self, **kwargs: object) -> SimpleNamespace:
        if kwargs["model"] == JUDGE:
            self.judge_calls.append(kwargs)
            return self.judge_answer
        if self.bot_answers:
            queued = self.bot_answers.pop(0)
            if isinstance(queued, Exception):
                raise queued
            return queued
        return self.bot_answer

    def with_options(self, **_options: object) -> FakeClaude:
        return self

    def as_anthropic(self) -> AsyncAnthropic:
        return cast(AsyncAnthropic, cast(object, self))


async def test_judge_asks_for_structured_output() -> None:
    fake = FakeClaude()
    verdict = await judge(fake.as_anthropic(), JUDGE, CASE_BY_ID["price-01"], "ПРАВИЛА", [], GOOD)
    assert verdict.scores == {"grounded": 1, "rules": 1, "helpful": 1}
    [call] = fake.judge_calls
    # Ответ строго по схеме, критерии — в системном промпте, ответ клиента — внутри тегов данных
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}}
    assert call["system"] == JUDGE_SYSTEM
    [request] = cast(list[dict[str, str]], call["messages"])
    assert f"<answer>\n{GOOD}\n</answer>" in request["content"]


@pytest.mark.parametrize(
    "reply",
    [
        message(VERDICT_OK, model="claude-opus-5-5"),  # подменили модель
        message(VERDICT_OK, model=JUDGE, stop_reason="max_tokens"),  # обрезан
        message("не JSON", model=JUDGE),  # не по схеме
    ],
)
async def test_judge_failures_are_errors_not_grades(reply: SimpleNamespace) -> None:
    fake = FakeClaude()
    fake.judge_answer = reply
    with pytest.raises(JudgeError) as caught:
        await judge(fake.as_anthropic(), JUDGE, CASE_BY_ID["price-01"], "ПРАВИЛА", [], GOOD)
    assert caught.value.usage is not None  # токены сбоя оплачены — их учитывают в расходах


# ---------- Прогон целиком ----------


def run(tmp_path: Path, fake: FakeClaude, ids: str, reps: int = 1) -> tuple[Summary, Path]:
    db = tmp_path / "eval.db"
    if not db.exists():
        upgrade_db(db)
    out = tmp_path / "flow" / "baseline"
    summary = asyncio.run(run_all(db, select_cases(ids), reps, out, fake.as_anthropic(), JUDGE, concurrency=2))
    return summary, out


def read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_run_writes_rows_traces_and_judge_files(tmp_path: Path) -> None:
    fake = FakeClaude()
    summary, out = run(tmp_path, fake, "price-01,policy-03", reps=2)

    rows = read_rows(out / "results.jsonl")
    assert sorted((row["prompt_id"], row["rep"]) for row in rows) == [
        ("policy-03", 0),
        ("policy-03", 1),
        ("price-01", 0),
        ("price-01", 1),
    ]
    first = next(row for row in rows if row["prompt_id"] == "price-01")
    assert first["model"] == BOT and first["status"] == "ok" and first["tags"] == ["Цены через поиск", "ru"]
    grade = cast(dict[str, int], first["grade"])
    # Фейк отвечает без поиска: факт есть, а инструмент не вызван — итог 0
    assert (grade["facts"], grade["tools"], grade["pass"]) == (1, 0, 0)
    assert first["cost_usd"] and first["judge_cost_usd"]  # не ноль: стоимость посчитана по прайс-листу
    assert first["usage"] == {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    turns = json.loads((out / "traces" / "price-01_rep0.json").read_text(encoding="utf-8"))
    assert [turn["role"] for turn in turns] == ["system", "user", "assistant"]
    assert "магазина электроники" in turns[0]["content"]  # промпт магазина, а не стоматологии
    assert (out / "judge" / "price-01_rep0.json").exists()
    assert (summary.rows, summary.cases, summary.errors) == (4, 2, 0)


def test_api_failure_goes_to_errors_and_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("evals.shop.run.asyncio.sleep", no_sleep)
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake = FakeClaude()
    fake.bot_answers = [anthropic.APIConnectionError(request=request)]
    summary, out = run(tmp_path, fake, "price-01")

    [error] = read_rows(out / "errors.jsonl")
    assert (error["prompt_id"], error["failure_class"], error["attempt"]) == ("price-01", "serving_error", 1)
    [row] = read_rows(out / "results.jsonl")
    assert row["attempts"] == 2  # вторая попытка дала ответ — сбой не стал нулём в оценке
    assert summary.errors == 0


def test_wrong_model_is_not_graded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("evals.shop.run.asyncio.sleep", no_sleep)
    fake = FakeClaude()
    fake.bot_answer = message(GOOD, model="claude-sonnet-4-6")
    summary, out = run(tmp_path, fake, "price-01")

    assert not (out / "results.jsonl").exists()
    errors = read_rows(out / "errors.jsonl")
    assert [e["failure_class"] for e in errors] == ["model_mismatch"] * 3
    assert summary.errors == 1


def test_resume_skips_finished_answers(tmp_path: Path) -> None:
    fake = FakeClaude()
    run(tmp_path, fake, "price-01")
    judged = len(fake.judge_calls)
    summary, out = run(tmp_path, fake, "price-01,policy-03")

    assert len(fake.judge_calls) == judged + 1  # price-01 уже оценён — заново не спрашиваем
    assert len(read_rows(out / "results.jsonl")) == 2
    assert summary.rows == 1


def test_unknown_case_id_is_an_error() -> None:
    with pytest.raises(SystemExit):
        select_cases("price-01,nope")


def test_regrade_recomputes_code_checks_and_keeps_judge(tmp_path: Path) -> None:
    fake = FakeClaude()
    _, out = run(tmp_path, fake, "price-01")
    path = out / "results.jsonl"
    [row] = read_rows(path)
    # Будто старый шаблон фактов не узнал правильный ответ, а судья поставил «правила» в ноль
    stale = cast(dict[str, int], row["grade"]) | {"facts": 0, "rules": 0, "pass": 0}
    path.write_text(json.dumps(row | {"grade": stale}, ensure_ascii=False) + "\n", encoding="utf-8")

    assert regrade(out) == 0  # итог остаётся 0: оценку судьи переоценка не трогает
    [updated] = read_rows(path)
    grade = cast(dict[str, int], updated["grade"])
    assert (grade["facts"], grade["rules"], grade["pass"]) == (1, 0, 0)
    assert updated["answer"] == row["answer"]  # остальные поля строки — как были
