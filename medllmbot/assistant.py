"""Обработка вопроса клиента: проверки → база знаний и история → LLM → проверки ответа → журнал."""

from dataclasses import dataclass

from anthropic.types import MessageParam

from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig
from medllmbot.db.repository import Repository
from medllmbot.guards import PriceChecker
from medllmbot.llm import ClinicLLM, LLMResult, LLMUnavailable
from medllmbot.prompts import RULES, build_system_prompt, render_info
from medllmbot.security import LEAK_CODE, LeakDetector, detect_injection

TECH_ERROR = "Извините, произошла техническая ошибка. Администратор свяжется с вами."
# Отказ модели, обрезанный или пустой ответ, утечка системного промпта
FALLBACK_ANSWER = "Уточню у администратора и вернусь к вам."
# Если модель назвала цену, которой нет в базе знаний
PRICE_FALLBACK = "Уточню стоимость у администратора и вернусь к вам."
# Дневной лимит расходов на Claude исчерпан: бот отвечает без модели, заявки по-прежнему сохраняются
BUDGET_EXCEEDED = "Сейчас я не могу ответить подробно. Администратор ответит вам в рабочее время."


def _chars(count: int) -> str:
    """«1 символ», «2 символа», «5 символов» — с учётом 11–14 и составных чисел."""
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        word = "символ"
    elif 2 <= last <= 4 and not 12 <= last_two <= 14:
        word = "символа"
    else:
        word = "символов"
    return f"{count} {word}"


def too_long(length: int, limit: int) -> str:
    """Ответ на слишком длинное сообщение: клиент видит, сколько написал и сколько можно."""
    return f"Сообщение слишком длинное: {_chars(length)}, а можно не больше {limit}. Пожалуйста, напишите вопрос короче."


@dataclass(frozen=True)
class Reply:
    text: str  # что отправить клиенту
    outcome: AuditOutcome
    flags: tuple[str, ...] = ()  # признаки попытки перехватить управление

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)


class ClinicAssistant:
    llm: ClinicLLM
    repo: Repository
    leaks: LeakDetector

    def __init__(self, llm: ClinicLLM, repo: Repository) -> None:
        self.llm = llm
        self.repo = repo
        self.leaks = LeakDetector(RULES)

    async def reply(self, chat_id: int, text: str, config: BotConfig) -> Reply:
        """Ответ клиенту. Исключений API не бросает: любой исход попадает в журнал."""
        flags = detect_injection(text)

        # Слишком длинное сообщение: не тратим деньги на API
        if len(text) > config.max_len:
            reply = Reply(too_long(len(text), config.max_len), AuditOutcome.TOO_LONG, flags)
            return await self._log(chat_id, text, reply)

        if config.daily_budget_usd and await self.repo.spent_today_micro_usd() >= config.daily_budget_usd * 1_000_000:
            reply = Reply(BUDGET_EXCEEDED, AuditOutcome.BUDGET_EXCEEDED, flags)
            return await self._log(chat_id, text, reply)

        # База знаний читается на каждый вопрос: правки из админки видны сразу
        info: str = render_info(knowledge=await self.repo.load_knowledge())
        history: list[MessageParam] = await self.repo.load_history(chat_id=chat_id, limit=config.history_limit)
        try:
            result = await self.llm.ask(
                system_prompt=build_system_prompt(info),
                history=history,
                text=text,
                model=config.model,
                max_tokens=config.max_tokens,
            )
        except LLMUnavailable:
            reply = Reply(TECH_ERROR, AuditOutcome.LLM_ERROR, flags)
            return await self._log(chat_id, text, reply, model=config.model)

        reply = self._check_answer(result, info, flags)
        # Попытки атаки не возвращаем в контекст модели: многошаговая атака не накопится
        if not reply.suspicious:
            await self.repo.save_pair(chat_id=chat_id, question=text, answer=reply.text)
        return await self._log(chat_id, text, reply, result=result)

    def _check_answer(self, result: LLMResult, info: str, flags: tuple[str, ...]) -> Reply:
        if self.leaks.leaks(result.text):
            return Reply(FALLBACK_ANSWER, AuditOutcome.LEAK_BLOCKED, (*flags, LEAK_CODE))
        if result.refused:
            return Reply(FALLBACK_ANSWER, AuditOutcome.REFUSAL, flags)
        # Обрывок на полуслове клиенту не отправляем; если такое повторяется,
        # лимит поднимают в настройках бота (журнал покажет итог «Ответ обрезан»)
        if result.truncated:
            return Reply(FALLBACK_ANSWER, AuditOutcome.TRUNCATED, flags)
        if not result.text:
            return Reply(FALLBACK_ANSWER, AuditOutcome.EMPTY, flags)
        if PriceChecker(info).has_unknown_price(text=result.text):
            return Reply(PRICE_FALLBACK, AuditOutcome.PRICE_REPLACED, flags)
        return Reply(result.text, AuditOutcome.ANSWERED, flags)

    async def _log(
        self,
        chat_id: int,
        text: str,
        reply: Reply,
        *,
        result: LLMResult | None = None,
        model: str | None = None,
    ) -> Reply:
        event = AuditEvent.incoming(chat_id, text, reply.outcome, reply.flags)
        event.final_answer = reply.text
        event.model = model
        if result is not None:
            event.llm_answer = result.text
            event.model = result.model
            event.stop_reason = result.stop_reason
            event.input_tokens = result.input_tokens
            event.output_tokens = result.output_tokens
            event.cache_read_tokens = result.cache_read_tokens
            event.cache_write_tokens = result.cache_write_tokens
            event.latency_ms = result.latency_ms
        await self.repo.log_event(event)
        return reply
