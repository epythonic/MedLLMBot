"""Прогон оценки магазина: вопросы из cases.py — настоящему боту, ответы — проверкам кодом и судье.

    uv run python -m evals.shop.run --cases price-01,order-02   # пилот на нескольких вопросах
    uv run python -m evals.shop.run --reps 2                    # весь набор
    uv run python -m evals.shop.run --regrade                   # бесплатно: проверки кодом заново по сохранённым ответам

Каждый запуск вызывает Claude API за деньги. Бот тот же, что в Telegram (ClinicAssistant с промптом
и инструментами магазина), на временной копии демо-базы: рабочая БД не затрагивается. Ключ Claude берётся
из окружения или .env, как у бота.

Результаты — в .claude/hillclimb/shop/<variant>/: results.jsonl (строка на ответ), traces/ (что видела
модель), judge/ (вход и ответ судьи), errors.jsonl (попытки без оценки). Повторный запуск продолжает
с места остановки. Отчёт: node <скилл claude-api>/shared/evals/report/build-report-lite.mjs .claude/hillclimb/shop/
"""

import argparse
import asyncio
import hashlib
import json
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override

import anthropic
from anthropic.types import MessageParam
from pydantic import BaseModel, SecretStr, TypeAdapter

from evals.shop.cases import CASE_BY_ID, CASES, Case
from evals.shop.grading import CRITERIA, JudgeError, ToolEvent, Verdict, code_checks, judge, judge_prompt
from medllmbot.assistant import ClinicAssistant
from medllmbot.config import AppSettings
from medllmbot.costs import Price, PriceBook, TokenUsage, match_model
from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.migrate import upgrade_db
from medllmbot.db.models import AuditOutcome, BotConfig, BusinessProfile
from medllmbot.db.repository import Repository
from medllmbot.db.seed import DEMO_PRICES, seed_demo
from medllmbot.llm import ClinicLLM, LLMResult, LLMUnavailable, ToolOutput, ToolRunner

FLOW_DIR = Path(".claude/hillclimb/shop")
DEFAULT_JUDGE = "claude-sonnet-5"
MAX_ATTEMPTS = 3  # попыток на ответ при сбоях API; каждая видна в errors.jsonl
CHAT_ID_BASE = 1_000_000  # chat_id вопросов: у каждой попытки свой чат с пустой историей

# Метрики в порядке важности: первая — итог отчёта. Подписи до 14 символов — так их показывает отчёт
METRICS: list[dict[str, str]] = [
    {"id": "pass", "label": "Итог", "kind": "binary"},
    {"id": "facts", "label": "Факты", "kind": "binary"},
    {"id": "no_leak", "label": "Без утечек", "kind": "binary"},
    {"id": "tools", "label": "Инструменты", "kind": "binary"},
    {"id": "language", "label": "Язык", "kind": "binary"},
    {"id": "format", "label": "Формат", "kind": "binary"},
    {"id": "delivered", "label": "Доставлено", "kind": "binary"},
    {"id": "grounded", "label": "Без выдумок", "kind": "binary"},
    {"id": "rules", "label": "Правила", "kind": "binary"},
    {"id": "helpful", "label": "Польза", "kind": "binary"},
]
PERF_FIELDS: list[dict[str, str]] = [
    {"id": "cost_usd", "label": "$ бот", "unit": "$"},
    {"id": "judge_cost_usd", "label": "$ судья", "unit": "$"},
    {"id": "latency_s", "label": "Ответ, с", "unit": "s"},
    {"id": "tool_calls", "label": "Вызовов"},
    {"id": "out_tokens", "label": "Токенов"},
    {"id": "answer_chars", "label": "Символов"},
]
METRICS_MD = """\
# Метрики оценки магазина

Метрика пишется только там, где относится к вопросу: «—» в отчёте значит «не проверялось», а не ноль.

- **Итог** — все метрики ответа равны 1.
- **Факты** — в ответе есть обязательные цены и условия (любая запись числа: «4 999», «4999», «4.999»).
- **Без утечек** — нет запрещённого: статуса чужого заказа, скидки, посчитанной суммы в месяц.
- **Инструменты** — для цен вызван поиск, для заказа с цифрами — `order_status`, без цифр — не вызван.
- **Язык** — ответ на языке вопроса (русский или румынский).
- **Формат** — без Markdown, который Telegram покажет как есть.
- **Доставлено** — клиент получил ответ модели, а не заглушку «Уточню у менеджера».
- **Без выдумок**, **Правила**, **Польза** — судья (по умолчанию claude-sonnet-5) по рубрике из `grading.py`;
  пояснения судьи — в отчёте и в `judge/`.
"""


class EvalSettings(AppSettings):
    """Только ключ Claude: остальные настройки бота оценке не нужны."""

    anthropic_api_key: SecretStr


# ---------- Запись того, что видела модель ----------


@dataclass
class Record:
    """Что произошло в одном ответе бота: промпт, вызовы инструментов, результат модели."""

    system_prompt: str = ""
    tool_events: list[ToolEvent] = field(default_factory=list[ToolEvent])
    result: LLMResult | None = None
    error: str | None = None


class RecordingTools:
    """Инструменты бота как есть, но каждый вызов и его результат записываются."""

    inner: ToolRunner
    events: list[ToolEvent]

    def __init__(self, inner: ToolRunner, events: list[ToolEvent]) -> None:
        self.inner = inner
        self.events = events

    @property
    def definitions(self) -> Sequence[anthropic.types.ToolParam]:
        return self.inner.definitions

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput:
        output = await self.inner.run(name, tool_input)
        self.events.append(ToolEvent(name, dict(tool_input), output.text, output.is_error))
        return output


class RecordingLLM(ClinicLLM):
    """Тот же клиент Claude, что у бота; запоминает системный промпт, инструменты и результат."""

    record: Record

    def __init__(self, client: anthropic.AsyncAnthropic, record: Record) -> None:
        super().__init__(client)
        self.record = record

    @override
    async def ask(
        self,
        *,
        system_prompt: str,
        history: list[MessageParam],
        text: str,
        model: str,
        max_tokens: int,
        tools: ToolRunner | None = None,
    ) -> LLMResult:
        self.record.system_prompt = system_prompt
        recorder = RecordingTools(tools, self.record.tool_events) if tools is not None else None
        try:
            result = await super().ask(
                system_prompt=system_prompt, history=history, text=text, model=model, max_tokens=max_tokens, tools=recorder
            )
        except LLMUnavailable as error:
            self.record.result = error.partial
            self.record.error = repr(error.__cause__ or error)
            raise
        self.record.result = result
        return result


# ---------- Один ответ ----------


class AttemptError(Exception):
    """Попытка без оценки: сбой API, другая модель, сбой судьи. Идёт в errors.jsonl, а не в оценки."""

    failure_class: str
    usage: TokenUsage | None
    model: str | None

    def __init__(self, failure_class: str, message: str, usage: TokenUsage | None = None, model: str | None = None) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.usage = usage
        self.model = model


@dataclass(frozen=True)
class Context:
    repo: Repository
    config: BotConfig
    client: anthropic.AsyncAnthropic
    judge_client: anthropic.AsyncAnthropic
    judge_model: str
    prices: PriceBook
    out: Path  # каталог варианта: baseline/, v1/…
    timeout_s: float
    runner_sha: str


@dataclass(frozen=True)
class Outcome:
    """Итог одного ответа для сводки в консоли; полная строка — в results.jsonl."""

    case_id: str
    passed: int | None  # None — ответ обрезан и не оценивался
    cost_usd: float
    judge_cost_usd: float


@dataclass(frozen=True)
class Scored:
    row: dict[str, object]
    outcome: Outcome


def total_usage(usages: Sequence[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cache_read_tokens=sum(u.cache_read_tokens for u in usages),
        cache_write_tokens=sum(u.cache_write_tokens for u in usages),
    )


def usage_json(usage: TokenUsage) -> dict[str, int]:
    """Поля usage так, как их читает отчёт."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_tokens,
        "cache_creation_input_tokens": usage.cache_write_tokens,
    }


def usd(micro_usd: int | None) -> float | None:
    return None if micro_usd is None else micro_usd / 1_000_000


def trace(case: Case, record: Record, answer: str) -> list[dict[str, str]]:
    """Разговор для отчёта: промпт, вопрос, вызовы инструментов с результатами, ответ клиенту."""
    turns: list[dict[str, str]] = [
        {"role": "system", "content": record.system_prompt},
        {"role": "user", "content": case.question},
    ]
    for event in record.tool_events:
        turns.append(
            {"role": "tool_call", "name": event.name, "content": json.dumps(event.tool_input, ensure_ascii=False, indent=2)}
        )
        turns.append({"role": "tool_result", "name": event.name, "content": event.output})
    turns.append({"role": "assistant", "content": answer})
    return turns


async def judge_with_retries(ctx: Context, case: Case, record: Record, answer: str) -> Verdict:
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await judge(ctx.judge_client, ctx.judge_model, case, record.system_prompt, record.tool_events, answer)
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError, JudgeError) as error:
            last = error
            await asyncio.sleep(min(30.0, 2.0**attempt) + random.uniform(0, 1))
    usage = last.usage if isinstance(last, JudgeError) else None
    raise AttemptError("grader_error", repr(last), usage, ctx.judge_model)


async def attempt_case(ctx: Context, case: Case, rep: int, chat_id: int) -> Scored:
    """Один ответ бота и его оценка: строка results.jsonl. AttemptError — попытка без оценки."""
    await ctx.repo.touch_client(chat_id, None, f"eval {case.id}")
    record = Record()
    assistant = ClinicAssistant(RecordingLLM(ctx.client, record), ctx.repo)
    started = time.monotonic()
    reply = await assistant.reply(chat_id, case.question, ctx.config)
    wall_s = time.monotonic() - started

    result = record.result
    if reply.outcome is AuditOutcome.LLM_ERROR or result is None:
        partial = total_usage(result.usages) if result is not None else None
        raise AttemptError("serving_error", record.error or reply.outcome.name, partial, ctx.config.model)
    usage = total_usage(result.usages)
    if match_model(result.model, [ctx.config.model]) is None:
        raise AttemptError("model_mismatch", f"ответила {result.model}, а не {ctx.config.model}", usage, result.model)

    case_dir = ctx.out
    (case_dir / "traces").mkdir(parents=True, exist_ok=True)
    (case_dir / "traces" / f"{case.id}_rep{rep}.json").write_text(
        json.dumps(trace(case, record, reply.text), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cost = usd(ctx.prices.total_micro_usd(result.model, result.usages))
    row: dict[str, object] = {
        "prompt_id": case.id,
        "rep": rep,
        "prompt": case.question,
        "tags": [case.group, case.lang],
        "status": "ok",
        "stop_reason": result.stop_reason,
        "model": result.model,
        "usage": usage_json(usage),
        "cost_usd": cost,
        "latency_s": round(result.latency_ms / 1000, 2),
        "wall_s": round(wall_s, 2),
        "tool_calls": len(record.tool_events),
        "out_tokens": usage.output_tokens,
        "answer_chars": len(reply.text),
        "outcome": reply.outcome.name,
        "answer": reply.text,
        "llm_answer": result.text,
        "meta": {"expect": case.expect, "chat_id": chat_id, "runner_sha": ctx.runner_sha},
    }
    if reply.outcome is AuditOutcome.TRUNCATED:
        # Обрезанный ответ не оценивается как неверный: он виден в отчёте отдельно
        row |= {"status": "truncated", "grade": {}}
        return Scored(row, Outcome(case.id, None, cost or 0.0, 0.0))

    checks = code_checks(case, reply.text, reply.outcome, [event.name for event in record.tool_events])
    verdict = await judge_with_retries(ctx, case, record, reply.text)
    (case_dir / "judge").mkdir(parents=True, exist_ok=True)
    (case_dir / "judge" / f"{case.id}_rep{rep}.json").write_text(
        json.dumps(
            {"prompt": judge_prompt(case, record.system_prompt, record.tool_events, reply.text), "response": verdict.raw},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    scores = checks | verdict.scores
    passed = int(all(scores.values()))
    judge_cost = usd(ctx.prices.cost_micro_usd(verdict.model, verdict.usage))
    row |= {
        "grade": {"pass": passed, **scores},
        "explanation": verdict.explanations,
        "judge_model": verdict.model,
        "judge_usage": usage_json(verdict.usage),
        "judge_cost_usd": judge_cost,
    }
    return Scored(row, Outcome(case.id, passed, cost or 0.0, judge_cost or 0.0))


def append_jsonl(path: Path, row: dict[str, object]) -> None:
    """Строка пишется сразу, как ответ готов: сбой посреди прогона не теряет готовое."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


async def run_case(ctx: Context, case: Case, index: int, rep: int) -> Outcome | None:
    """Ответ с повторами при сбоях. None — все попытки без оценки (они в errors.jsonl)."""
    for attempt in range(MAX_ATTEMPTS):
        chat_id = CHAT_ID_BASE + index * 100 + rep * 10 + attempt
        try:
            async with asyncio.timeout(ctx.timeout_s):
                scored = await attempt_case(ctx, case, rep, chat_id)
        except TimeoutError:
            error = AttemptError("timeout", f"дольше {ctx.timeout_s:.0f} с")
        except AttemptError as failed:
            error = failed
        else:
            scored.row["attempts"] = attempt + 1
            append_jsonl(ctx.out / "results.jsonl", scored.row)
            return scored.outcome
        append_jsonl(
            ctx.out / "errors.jsonl",
            {
                "prompt_id": case.id,
                "rep": rep,
                "attempt": attempt + 1,
                "failure_class": error.failure_class,
                "error": str(error),
                "model": error.model,
                "usage": usage_json(error.usage) if error.usage is not None else None,
            },
        )
        await asyncio.sleep(min(30.0, 2.0**attempt) + random.uniform(0, 1))
    return None


# ---------- Прогон целиком ----------


def price_book() -> PriceBook:
    """Цены моделей те же, что в демо-базе бота (seed.DEMO_PRICES): бот и судья считаются одинаково."""
    return PriceBook(
        Price(model, from_tokens, input_price, output_price, cache_write, cache_read)
        for model, from_tokens, input_price, cache_write, cache_read, output_price in DEMO_PRICES
    )


class _StoredRow(BaseModel):
    """Поля сохранённой строки, нужные для переоценки; остальные поля строки сохраняются как есть."""

    prompt_id: str
    rep: int
    status: str
    outcome: str
    answer: str
    grade: dict[str, int]


class _TraceTurn(BaseModel):
    role: str
    name: str | None = None


_RAW_ROW = TypeAdapter(dict[str, object])
_TRACE = TypeAdapter(list[_TraceTurn])


def regrade(variant_dir: Path) -> int:
    """Проверки кодом заново по сохранённым ответам — после правки шаблонов в cases.py, без вызовов API.

    Оценки судьи остаются прежними. Возвращает число строк, у которых изменился итог.
    """
    path = variant_dir / "results.jsonl"
    changed = 0
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = _RAW_ROW.validate_json(line)
        row = _StoredRow.model_validate(raw)
        if row.status == "ok":
            trace_file = variant_dir / "traces" / f"{row.prompt_id}_rep{row.rep}.json"
            turns = _TRACE.validate_json(trace_file.read_text(encoding="utf-8"))
            tools = [turn.name for turn in turns if turn.role == "tool_call" and turn.name]
            checks = code_checks(CASE_BY_ID[row.prompt_id], row.answer, AuditOutcome[row.outcome], tools)
            judged = {name: row.grade[name] for name in CRITERIA if name in row.grade}
            scores = checks | judged
            grade = {"pass": int(all(scores.values())), **scores}
            changed += grade["pass"] != row.grade.get("pass")
            raw["grade"] = grade
        lines.append(json.dumps(raw, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


class _RowKey(BaseModel):
    prompt_id: str
    rep: int


def done_keys(results: Path) -> set[tuple[str, int]]:
    """(вопрос, повтор), у которых уже есть оценка, — для продолжения прерванного прогона."""
    if not results.exists():
        return set()
    keys = (_RowKey.model_validate_json(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip())
    return {(key.prompt_id, key.rep) for key in keys}


def runner_sha() -> str:
    """Версия кода оценки в каждой строке: оценки разных версий не сравнивают."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def write_state(flow: Path) -> None:
    """Описание метрик для отчёта — один раз, дальше файл не трогается."""
    flow.mkdir(parents=True, exist_ok=True)
    state = flow / "_state.json"
    if not state.exists():
        state.write_text(json.dumps({"metrics": METRICS, "perf_fields": PERF_FIELDS}, ensure_ascii=False, indent=2))
    metrics_md = flow / "metrics.md"
    if not metrics_md.exists():
        metrics_md.write_text(METRICS_MD, encoding="utf-8")


@dataclass(frozen=True)
class Summary:
    rows: int
    cases: int
    pass_rate: float
    ci_half_width: float
    errors: int
    cost_usd: float
    judge_cost_usd: float
    wall_s: float


def summarize(outcomes: Sequence[Outcome], errors: int, wall_s: float) -> Summary:
    """Доля прошедших с 95% интервалом по вопросам (повторы одного вопроса не независимы)."""
    by_case: dict[str, list[int]] = {}
    for outcome in outcomes:
        if outcome.passed is not None:
            by_case.setdefault(outcome.case_id, []).append(outcome.passed)
    means = [sum(v) / len(v) for v in by_case.values()]
    n = len(means)
    rate = sum(means) / n if n else 0.0
    sd = (sum((m - rate) ** 2 for m in means) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return Summary(
        rows=len(outcomes),
        cases=n,
        pass_rate=rate,
        ci_half_width=1.96 * sd / n**0.5 if n else 0.0,
        errors=errors,
        cost_usd=sum(o.cost_usd for o in outcomes),
        judge_cost_usd=sum(o.judge_cost_usd for o in outcomes),
        wall_s=wall_s,
    )


async def run_all(
    db: Path,
    cases: Sequence[Case],
    reps: int,
    out: Path,
    client: anthropic.AsyncAnthropic,
    judge_model: str,
    concurrency: int,
) -> Summary:
    engine = create_engine(db)
    try:
        repo = Repository(create_sessionmaker(engine))
        await seed_demo(repo.sessions, BusinessProfile.SHOP)
        config = await repo.get_config()
        ctx = Context(
            repo=repo,
            config=config,
            client=client,
            judge_client=client.with_options(timeout=180.0, max_retries=2),
            judge_model=judge_model,
            prices=price_book(),
            out=out,
            timeout_s=300.0,
            runner_sha=runner_sha(),
        )
        done = done_keys(out / "results.jsonl")
        index = {case.id: i for i, case in enumerate(CASES)}
        todo = [(case, rep) for case in cases for rep in range(reps) if (case.id, rep) not in done]
        gate = asyncio.Semaphore(concurrency)

        async def guarded(case: Case, rep: int) -> Outcome | None:
            async with gate:
                return await run_case(ctx, case, index[case.id], rep)

        started = time.monotonic()
        finished = await asyncio.gather(*(guarded(case, rep) for case, rep in todo))
        outcomes = [outcome for outcome in finished if outcome is not None]
        return summarize(outcomes, len(todo) - len(outcomes), time.monotonic() - started)
    finally:
        await engine.dispose()


class Args(argparse.Namespace):
    """Аргументы командной строки с типами: argparse заполняет атрибуты этого объекта."""

    cases: str = ""
    reps: int = 2
    variant: str = "baseline"
    judge_model: str = DEFAULT_JUDGE
    concurrency: int = 4
    regrade: bool = False


def parse_args(argv: Sequence[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(description="Оценка ответов магазина Demo Electro (платные вызовы Claude API)")
    parser.add_argument("--cases", help="id вопросов через запятую; по умолчанию все")
    parser.add_argument("--reps", type=int, help="сколько раз задавать каждый вопрос (по умолчанию 2)")
    parser.add_argument("--variant", help="каталог результатов: baseline, v1, v2…")
    parser.add_argument("--judge-model", help=f"модель-судья (по умолчанию {DEFAULT_JUDGE})")
    parser.add_argument("--concurrency", type=int, help="ответов одновременно (по умолчанию 4)")
    parser.add_argument(
        "--regrade", action="store_true", help="бесплатно: проверки кодом заново по сохранённым ответам, без API"
    )
    return parser.parse_args(argv, namespace=Args())


def select_cases(ids: str) -> list[Case]:
    if not ids:
        return list(CASES)
    wanted = [item.strip() for item in ids.split(",") if item.strip()]
    known = {case.id: case for case in CASES}
    unknown = [item for item in wanted if item not in known]
    if unknown:
        raise SystemExit(f"Нет таких вопросов: {', '.join(unknown)}")
    return [known[item] for item in wanted]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.regrade:
        changed = regrade(FLOW_DIR / args.variant)
        print(f"{args.variant}: проверки кодом пересчитаны, итог изменился у {changed} ответов")
        return 0
    cases = select_cases(args.cases)
    settings = EvalSettings.from_env()
    # Тот же клиент, что у бота в main.py: таймаут 30 с, один повтор SDK
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), timeout=30.0, max_retries=1)
    write_state(FLOW_DIR)
    with TemporaryDirectory(prefix="medllmbot-eval-") as tmp:
        db = Path(tmp) / "eval.db"
        upgrade_db(db)  # миграции — до запуска event loop, как у бота
        summary = asyncio.run(
            run_all(db, cases, args.reps, FLOW_DIR / args.variant, client, args.judge_model, args.concurrency)
        )
    print(
        f"{args.variant}: {summary.pass_rate:.0%} ± {summary.ci_half_width:.0%} прошли все проверки "
        f"({summary.rows} ответов по {summary.cases} вопросам, без оценки: {summary.errors}). "
        f"Стоимость: бот ${summary.cost_usd:.4f}, судья ${summary.judge_cost_usd:.4f}; {summary.wall_s:.0f} с."
    )
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
