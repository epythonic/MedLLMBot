"""Client question processing: checks → knowledge base and history → LLM → reply checks → audit log."""

from dataclasses import dataclass

from anthropic.types import MessageParam

from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig, BusinessProfile
from medllmbot.db.repository import Repository
from medllmbot.guards import PriceChecker
from medllmbot.llm import ClinicLLM, LLMResult, LLMUnavailable
from medllmbot.orders import DatabaseOrders, LookupLimits, OrderBackend
from medllmbot.profiles import PROFILES, Replies
from medllmbot.prompts import build_system_prompt, render_info, rules
from medllmbot.security import LEAK_CODE, LeakDetector, detect_injection
from medllmbot.text import Language, plain_text, reply_language
from medllmbot.tools import CatalogTools, OrderTools, Toolbox

# Each profile has its own canned replies (profiles.py). The constants are the default profile replies:
# the unexpected error handler does not know the settings, and tests compare dental clinic replies with them
_DEFAULT = PROFILES[BusinessProfile.CLINIC].replies_in(Language.RU)
TECH_ERROR = _DEFAULT.tech_error
TECH_ERROR_RO = PROFILES[BusinessProfile.CLINIC].replies_in(Language.RO).tech_error
# Model refusal, truncated or empty reply, system prompt leak
FALLBACK_ANSWER = _DEFAULT.fallback
# When the model named a price that is not in the knowledge base
PRICE_FALLBACK = _DEFAULT.price_fallback
# The daily Claude spending limit is exhausted: the bot replies without the model, leads are still saved
BUDGET_EXCEEDED = _DEFAULT.budget_exceeded


def _chars(count: int) -> str:
    """«1 символ», «2 символа», «5 символов», taking 11–14 and compound numbers into account."""
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        word = "символ"
    elif 2 <= last <= 4 and not 12 <= last_two <= 14:
        word = "символа"
    else:
        word = "символов"
    return f"{count} {word}"


def _chars_ro(count: int) -> str:
    """«1 caracter», «5 caractere», «20 de caractere»: in Romanian «de» follows numbers that end
    in 00 or in 20–99 («101 caractere», but «120 de caractere»).
    """
    if count == 1:
        return "1 caracter"
    rest = count % 100
    return f"{count} de caractere" if count and (rest == 0 or rest >= 20) else f"{count} caractere"


def too_long(length: int, limit: int, language: Language = Language.RU) -> str:
    """Reply to a message that is too long: the client sees how much they wrote and how much is allowed."""
    if language is Language.RO:
        return (
            f"Mesajul este prea lung: {_chars_ro(length)}, iar limita este de {_chars_ro(limit)}. "
            "Vă rog să scrieți întrebarea mai pe scurt."
        )
    return f"Сообщение слишком длинное: {_chars(length)}, а можно не больше {limit}. Пожалуйста, напишите вопрос короче."


@dataclass(frozen=True)
class Reply:
    text: str  # what to send to the client
    outcome: AuditOutcome
    flags: tuple[str, ...] = ()  # signs of a takeover attempt

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)


class ClinicAssistant:
    llm: ClinicLLM
    repo: Repository
    orders: OrderBackend
    lookup_limits: LookupLimits
    _leaks: dict[BusinessProfile, LeakDetector]

    def __init__(self, llm: ClinicLLM, repo: Repository, orders: OrderBackend | None = None) -> None:
        self.llm = llm
        self.repo = repo
        # Shop orders: a DB table in the demo, the shop's accounting system in a real shop project
        self.orders = orders if orders is not None else DatabaseOrders(repo)
        # Failed order check limits live in the assistant so brute force across messages is counted too
        self.lookup_limits = LookupLimits()
        # The profile changes in the DB without a restart, so each profile has its own leak detector
        self._leaks = {kind: LeakDetector(rules(kind)) for kind in BusinessProfile}

    async def reply(self, chat_id: int, text: str, config: BotConfig, language: Language | None = None) -> Reply:
        """Reply to the client. Never raises API exceptions: every outcome goes to the audit log.

        `language` is the canned reply language; defaults to the message language (`text.reply_language`).
        """
        flags = detect_injection(text)
        if language is None:
            language = reply_language(text)
        replies = PROFILES[config.profile].replies_in(language)

        # Message too long: do not spend money on the API
        if len(text) > config.max_len:
            reply = Reply(too_long(len(text), config.max_len, language), AuditOutcome.TOO_LONG, flags)
            return await self._log(chat_id, text, reply)

        if config.daily_budget_usd and await self.repo.spent_today_micro_usd() >= config.daily_budget_usd * 1_000_000:
            reply = Reply(replies.budget_exceeded, AuditOutcome.BUDGET_EXCEEDED, flags)
            return await self._log(chat_id, text, reply)

        # The knowledge base is read for every question, so admin panel edits apply at once.
        # A large catalog does not go into the prompt: the model searches it with a tool
        knowledge = await self.repo.load_knowledge(catalog_inline_limit=config.catalog_inline_limit)
        info: str = render_info(knowledge, config.profile)
        prices = PriceChecker(info)
        if knowledge.catalog_search:
            # The model may have taken a catalog price from a search or a past reply in the history; any is allowed
            prices.add_known(await self.repo.catalog_prices())
        history: list[MessageParam] = await self.repo.load_history(chat_id=chat_id, limit=config.history_limit)
        try:
            result = await self.llm.ask(
                system_prompt=build_system_prompt(info, config.profile),
                history=history,
                text=text,
                model=config.model,
                max_tokens=config.max_tokens,
                tools=self._tools(chat_id, config, catalog_search=knowledge.catalog_search),
            )
        except LLMUnavailable as e:
            reply = Reply(replies.tech_error, AuditOutcome.LLM_ERROR, flags)
            # Failure after a catalog search: the requests already made are paid for, so they go to the log and spending
            return await self._log(chat_id, text, reply, result=e.partial, model=config.model)

        prices.add_known(result.tool_prices)  # the order total comes from the shop system, not the model
        # The model may repeat a budget the client named in this dialog: «под бюджет до 10 000 лей»
        customer = [text, *(m["content"] for m in history if m["role"] == "user" and isinstance(m["content"], str))]
        reply = self._check_answer(result, prices, customer, self._leaks[config.profile], replies, flags)
        # Attack attempts are kept out of the model context so a multi-step attack cannot build up
        return await self._log(chat_id, text, reply, result=result, remember=not reply.suspicious)

    def _tools(self, chat_id: int, config: BotConfig, *, catalog_search: bool) -> Toolbox | None:
        """Tools for this question: search if the catalog is not in the prompt, orders if the profile has them."""
        runners: list[CatalogTools | OrderTools] = []
        if catalog_search:
            runners.append(CatalogTools(self.repo, config.profile))
        if PROFILES[config.profile].orders:
            runners.append(OrderTools(self.orders, chat_id, self.lookup_limits))
        return Toolbox(*runners) if runners else None

    @staticmethod
    def _check_answer(
        result: LLMResult,
        prices: PriceChecker,
        customer: list[str],
        leaks: LeakDetector,
        replies: Replies,
        flags: tuple[str, ...],
    ) -> Reply:
        if leaks.leaks(result.text):
            return Reply(replies.fallback, AuditOutcome.LEAK_BLOCKED, (*flags, LEAK_CODE))
        if result.refused:
            return Reply(replies.fallback, AuditOutcome.REFUSAL, flags)
        # A reply cut off mid-word is not sent to the client; if this keeps happening,
        # raise the limit in the bot settings (the audit log shows the «Ответ обрезан» outcome)
        if result.truncated:
            return Reply(replies.fallback, AuditOutcome.TRUNCATED, flags)
        # The model never answered but asked for a tool: its text is a preamble like «Сейчас посмотрю»
        if not result.text or result.stop_reason == "tool_use":
            return Reply(replies.fallback, AuditOutcome.EMPTY, flags)
        if prices.has_unknown_price(result.text, customer):
            return Reply(replies.price_fallback, AuditOutcome.PRICE_REPLACED, flags)
        # The audit log keeps the model reply as is; the client and the history get it without markup
        return Reply(plain_text(result.text), AuditOutcome.ANSWERED, flags)

    async def _log(
        self,
        chat_id: int,
        text: str,
        reply: Reply,
        *,
        result: LLMResult | None = None,
        model: str | None = None,
        remember: bool = False,
    ) -> Reply:
        """Writes the event to the audit log; `remember` also saves the question and answer to the history."""
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
            event.tool_calls = "\n".join(result.tool_calls) or None
        requests = result.usages if result is not None else ()
        if remember:
            # One transaction: if the log write fails, the history does not get an answer the client never saw
            await self.repo.log_event_with_pair(event, question=text, answer=reply.text, requests=requests)
        else:
            await self.repo.log_event(event, requests=requests)
        return reply
