"""Shop eval run: questions from cases.py go to the real bot, its answers to code checks and the judge.

    uv run python -m evals.shop.run --cases price-01,order-02   # pilot on a few questions
    uv run python -m evals.shop.run --reps 2                    # the full set
    uv run python -m evals.shop.run --regrade                   # free: rerun code checks on saved answers

Every run calls the Claude API and costs money. The bot is the same as in Telegram (ClinicAssistant with the shop
prompt and tools), on a temporary copy of the demo database: the working DB is not touched. The Claude key comes
from the environment or .env, as for the bot.

Results go to .claude/hillclimb/shop/<variant>/: results.jsonl (one row per answer), traces/ (what the model
saw), judge/ (judge input and reply), errors.jsonl (attempts without a grade). A repeated run resumes where
it stopped. Report: node <claude-api skill>/shared/evals/report/build-report-lite.mjs .claude/hillclimb/shop/
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
MAX_ATTEMPTS = 3  # attempts per answer on API failures; each one shows up in errors.jsonl
CHAT_ID_BASE = 1_000_000  # chat_id of the questions: each attempt gets its own chat with an empty history

# Metrics by importance: the first is the report's headline. Labels are up to 14 chars, as the report shows them
METRICS: list[dict[str, str]] = [
    {"id": "pass", "label": "Overall", "kind": "binary"},
    {"id": "facts", "label": "Facts", "kind": "binary"},
    {"id": "no_leak", "label": "No leaks", "kind": "binary"},
    {"id": "tools", "label": "Tools", "kind": "binary"},
    {"id": "language", "label": "Language", "kind": "binary"},
    {"id": "format", "label": "Format", "kind": "binary"},
    {"id": "delivered", "label": "Delivered", "kind": "binary"},
    {"id": "grounded", "label": "Grounded", "kind": "binary"},
    {"id": "rules", "label": "Rules", "kind": "binary"},
    {"id": "helpful", "label": "Helpful", "kind": "binary"},
]
PERF_FIELDS: list[dict[str, str]] = [
    {"id": "cost_usd", "label": "$ bot", "unit": "$"},
    {"id": "judge_cost_usd", "label": "$ judge", "unit": "$"},
    {"id": "latency_s", "label": "Latency, s", "unit": "s"},
    {"id": "tool_calls", "label": "Tool calls"},
    {"id": "out_tokens", "label": "Out tokens"},
    {"id": "answer_chars", "label": "Characters"},
]
METRICS_MD = """\
# Shop eval metrics

A metric is recorded only where it applies to the question: "—" in the report means "not checked", not zero.

- **Overall** — every metric of the answer equals 1.
- **Facts** — the answer has the required prices and terms (any way of writing a number: «4 999», «4999», «4.999»).
- **No leaks** — nothing forbidden: no status of someone else's order, no discount, no computed monthly payment.
- **Tools** — search is called for prices, `order_status` for an order with digits, and not called without digits.
- **Language** — the answer is in the language of the question (Russian or Romanian).
- **Format** — no Markdown, which Telegram would show as is.
- **Delivered** — the customer got the model's answer, not the «Уточню у менеджера» fallback.
- **Grounded**, **Rules**, **Helpful** — the judge (claude-sonnet-5 by default) with the rubric from `grading.py`;
  the judge's explanations are in the report and in `judge/`.
"""


class EvalSettings(AppSettings):
    """Only the Claude key: the eval needs none of the other bot settings."""

    anthropic_api_key: SecretStr


# ---------- Recording what the model saw ----------


@dataclass
class Record:
    """What happened in one bot answer: the prompt, tool calls, the model result."""

    system_prompt: str = ""
    tool_events: list[ToolEvent] = field(default_factory=list[ToolEvent])
    result: LLMResult | None = None
    error: str | None = None


class RecordingTools:
    """The bot's tools as is, but every call and its result are recorded."""

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
    """The same Claude client as the bot's; remembers the system prompt, the tool calls and the result."""

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


# ---------- One answer ----------


class AttemptError(Exception):
    """An attempt without a grade: API failure, wrong model, judge failure. Goes to errors.jsonl, not to grades."""

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
    out: Path  # variant directory: baseline/, v1/…
    timeout_s: float
    runner_sha: str


@dataclass(frozen=True)
class Outcome:
    """Result of one answer for the console summary; the full row is in results.jsonl."""

    case_id: str
    passed: int | None  # None: the answer was truncated and not graded
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
    """Usage fields as the report reads them."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_tokens,
        "cache_creation_input_tokens": usage.cache_write_tokens,
    }


def usd(micro_usd: int | None) -> float | None:
    return None if micro_usd is None else micro_usd / 1_000_000


def trace(case: Case, record: Record, answer: str) -> list[dict[str, str]]:
    """The conversation for the report: prompt, question, tool calls with results, the answer to the customer."""
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
    """One bot answer and its grade: a results.jsonl row. AttemptError means an attempt without a grade."""
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
        raise AttemptError("model_mismatch", f"answered by {result.model}, not {ctx.config.model}", usage, result.model)

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
        # A truncated answer is not graded as wrong: the report shows it separately
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
    """A row is written as soon as the answer is ready: a failure mid-run does not lose finished work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


async def run_case(ctx: Context, case: Case, index: int, rep: int) -> Outcome | None:
    """An answer with retries on failures. None means every attempt went ungraded (they are in errors.jsonl)."""
    for attempt in range(MAX_ATTEMPTS):
        chat_id = CHAT_ID_BASE + index * 100 + rep * 10 + attempt
        try:
            async with asyncio.timeout(ctx.timeout_s):
                scored = await attempt_case(ctx, case, rep, chat_id)
        except TimeoutError:
            error = AttemptError("timeout", f"took longer than {ctx.timeout_s:.0f} s")
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


# ---------- Full run ----------


def price_book() -> PriceBook:
    """Model prices are those of the bot's demo DB (seed.DEMO_PRICES): the bot and the judge are costed alike."""
    return PriceBook(
        Price(model, from_tokens, input_price, output_price, cache_write, cache_read)
        for model, from_tokens, input_price, cache_write, cache_read, output_price in DEMO_PRICES
    )


class _StoredRow(BaseModel):
    """Fields of a stored row needed for regrading; the other fields of the row are kept as is."""

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
    """Reruns code checks on saved answers, after editing the patterns in cases.py, with no API calls.

    Judge grades stay as they were. Returns the number of rows whose overall result changed.
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
    """(question, rep) pairs that already have a grade, to resume an interrupted run."""
    if not results.exists():
        return set()
    keys = (_RowKey.model_validate_json(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip())
    return {(key.prompt_id, key.rep) for key in keys}


def runner_sha() -> str:
    """Eval code version in every row: grades from different versions are not compared."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def write_state(flow: Path) -> None:
    """Metric descriptions for the report, written once; after that the files are left alone."""
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
    """Pass rate with a 95% interval over questions (reps of the same question are not independent)."""
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
    """Typed command-line arguments: argparse fills in the attributes of this object."""

    cases: str = ""
    reps: int = 2
    variant: str = "baseline"
    judge_model: str = DEFAULT_JUDGE
    concurrency: int = 4
    regrade: bool = False


def parse_args(argv: Sequence[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(description="Demo Electro shop answer eval (paid Claude API calls)")
    parser.add_argument("--cases", help="comma-separated question ids; all by default")
    parser.add_argument("--reps", type=int, help="how many times to ask each question (default 2)")
    parser.add_argument("--variant", help="results directory: baseline, v1, v2…")
    parser.add_argument("--judge-model", help=f"judge model (default {DEFAULT_JUDGE})")
    parser.add_argument("--concurrency", type=int, help="answers in parallel (default 4)")
    parser.add_argument("--regrade", action="store_true", help="free: rerun code checks on saved answers, no API calls")
    return parser.parse_args(argv, namespace=Args())


def select_cases(ids: str) -> list[Case]:
    if not ids:
        return list(CASES)
    wanted = [item.strip() for item in ids.split(",") if item.strip()]
    known = {case.id: case for case in CASES}
    unknown = [item for item in wanted if item not in known]
    if unknown:
        raise SystemExit(f"Unknown question ids: {', '.join(unknown)}")
    return [known[item] for item in wanted]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.regrade:
        changed = regrade(FLOW_DIR / args.variant)
        print(f"{args.variant}: code checks recomputed, overall result changed for {changed} answers")
        return 0
    cases = select_cases(args.cases)
    settings = EvalSettings.from_env()
    # Same client as the bot's in main.py: 30 s timeout, one SDK retry
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), timeout=30.0, max_retries=1)
    write_state(FLOW_DIR)
    with TemporaryDirectory(prefix="medllmbot-eval-") as tmp:
        db = Path(tmp) / "eval.db"
        upgrade_db(db)  # migrations run before the event loop starts, as in the bot
        summary = asyncio.run(
            run_all(db, cases, args.reps, FLOW_DIR / args.variant, client, args.judge_model, args.concurrency)
        )
    print(
        f"{args.variant}: {summary.pass_rate:.0%} ± {summary.ci_half_width:.0%} passed all checks "
        f"({summary.rows} answers to {summary.cases} questions, ungraded: {summary.errors}). "
        f"Cost: bot ${summary.cost_usd:.4f}, judge ${summary.judge_cost_usd:.4f}; {summary.wall_s:.0f} s."
    )
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
