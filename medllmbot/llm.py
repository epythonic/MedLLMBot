"""Обёртка над Anthropic API: вопрос клиента — ответ модели с метаданными для журнала.

Если модели даны инструменты, ответ может занять несколько запросов: модель вызывает инструмент,
получает результат и продолжает. Токены и время в `LLMResult` — сумма по всем запросам.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import anthropic
from anthropic.types import (
    ContentBlockParam,
    Message,
    MessageParam,
    TextBlockParam,
    ToolChoiceParam,
    ToolParam,
    ToolResultBlockParam,
)

from medllmbot.costs import TokenUsage

log = logging.getLogger(__name__)

# Сколько раз подряд модель может вызвать инструменты; после этого ей остаётся только ответить
MAX_TOOL_ROUNDS = 3
# Сколько вызовов инструментов выполнять из одного ответа модели; остальные получают отказ
MAX_TOOL_CALLS_PER_ROUND = 3


class LLMUnavailable(Exception):
    """API недоступен или запрос не удался — клиенту нужно ответить заглушкой.

    `partial` — итог уже выполненных запросов, если сбой случился после вызова инструмента:
    Anthropic их уже посчитал, поэтому токены и стоимость всё равно идут в журнал и в дневные итоги.
    """

    partial: LLMResult | None

    def __init__(self, partial: LLMResult | None = None) -> None:
        super().__init__()
        self.partial = partial


@dataclass(frozen=True)
class LLMResult:
    text: str  # ответ модели как есть, может быть пустым
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    tool_calls: tuple[str, ...] = ()  # описания вызовов инструментов для журнала
    # Токены каждого запроса: тариф по длине промпта Anthropic выбирает для запроса, а не для суммы
    usages: tuple[TokenUsage, ...] = ()

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    @property
    def truncated(self) -> bool:
        """Ответ упёрся в max_tokens или в окно контекста модели и мог оборваться на полуслове."""
        return self.stop_reason in ("max_tokens", "model_context_window_exceeded")


@dataclass(frozen=True)
class ToolOutput:
    text: str  # результат для модели
    summary: str  # строка для журнала: что искали и что нашлось
    is_error: bool = False


class ToolRunner(Protocol):
    """Набор инструментов модели: описания для API и их выполнение."""

    @property
    def definitions(self) -> Sequence[ToolParam]: ...

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput: ...


def _assistant_turn(resp: Message) -> MessageParam:
    """Ответ модели с вызовами инструментов — обратно в историю запроса, как того требует API."""
    content: list[ContentBlockParam] = []
    for block in resp.content:
        # API не принимает текстовый блок из одних пробелов
        if block.type == "text" and block.text.strip():
            content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return {"role": "assistant", "content": content}


class ClinicLLM:
    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self.client: anthropic.AsyncAnthropic = client

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
        """Модель и лимит приходят из настроек в БД, поэтому передаются в каждый вызов."""
        messages: list[MessageParam] = [*history, {"role": "user", "content": text}]
        # Системный промпт одинаков для всех клиентов, пока базу знаний не правят в админке,
        # поэтому помечаем его для кэширования (вместе с описаниями инструментов перед ним).
        # Кэш срабатывает, только если префикс не короче минимума модели
        # (у Haiku 5.5 — 512 токенов, у Haiku 4.5 — 4096).
        system: list[TextBlockParam] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        started: float = time.perf_counter()
        responses: list[Message] = []
        tool_calls: list[str] = []
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            # В последнем раунде инструменты остаются в запросе (иначе сбросится кэш), но вызывать их нельзя
            choice: ToolChoiceParam = {"type": "none"} if round_ == MAX_TOOL_ROUNDS else {"type": "auto"}
            try:
                resp = await self._create(model, max_tokens, system, messages, tools, choice)
            except LLMUnavailable as e:
                if responses:
                    raise LLMUnavailable(self._result(responses, tool_calls, started)) from e
                raise
            responses.append(resp)
            if tools is None or resp.stop_reason != "tool_use":
                break
            results: list[ToolResultBlockParam] = []
            for block in resp.content:
                if block.type == "tool_use":
                    if len(results) < MAX_TOOL_CALLS_PER_ROUND:
                        output = await self._run_tool(tools, block.name, block.input)
                    else:
                        output = ToolOutput(
                            "Слишком много поисков за раз. Используй результаты, которые уже есть.",
                            f"{block.name}: пропущен, больше {MAX_TOOL_CALLS_PER_ROUND} вызовов за раз",
                            is_error=True,
                        )
                    tool_calls.append(output.summary)
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": output.text, "is_error": output.is_error}
                    )
            messages.extend([_assistant_turn(resp), {"role": "user", "content": results}])

        return self._result(responses, tool_calls, started)

    @staticmethod
    async def _run_tool(tools: ToolRunner, name: str, tool_input: dict[str, object]) -> ToolOutput:
        """Вызов инструмента. Любая его ошибка — ответ модели, а не исключение: иначе сообщение клиента
        не попало бы в журнал, а заявка с телефоном не сохранилась бы.
        """
        try:
            return await tools.run(name, tool_input)
        except Exception:
            log.exception("Инструмент %s завершился ошибкой", name)
            return ToolOutput("Инструмент временно недоступен.", f"{name}: ошибка", is_error=True)

    async def _create(
        self,
        model: str,
        max_tokens: int,
        system: list[TextBlockParam],
        messages: list[MessageParam],
        tools: ToolRunner | None,
        tool_choice: ToolChoiceParam,
    ) -> Message:
        """Один запрос к API; ошибки превращаются в LLMUnavailable."""
        try:
            if tools is None:
                return await self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
            return await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools.definitions,
                tool_choice=tool_choice,
            )
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
        ) as e:
            # Повтор не поможет: неверный ключ, модель или запрос
            log.error("Ошибка конфигурации Anthropic API: %s", e)
            raise LLMUnavailable from e
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            # SDK уже сделал повторную попытку (max_retries в main.py)
            log.warning("Anthropic API временно недоступен (%s): %s", e.status_code, e)
            raise LLMUnavailable from e
        except anthropic.APIConnectionError as e:
            log.warning("Нет связи с Anthropic API: %s", e)
            raise LLMUnavailable from e
        except anthropic.APIError as e:
            # Остальное, например ответ API не прошёл проверку схемы SDK: клиент всё равно получит заглушку
            log.exception("Неожиданная ошибка Anthropic API")
            raise LLMUnavailable from e

    @staticmethod
    def _result(responses: list[Message], tool_calls: list[str], started: float) -> LLMResult:
        """Итог по всем запросам: текст и причина остановки — из последнего, токены — сумма."""
        last = responses[-1]
        usages = tuple(
            TokenUsage(
                input_tokens=r.usage.input_tokens,
                output_tokens=r.usage.output_tokens,
                cache_read_tokens=r.usage.cache_read_input_tokens or 0,
                cache_write_tokens=r.usage.cache_creation_input_tokens or 0,
            )
            for r in responses
        )
        result = LLMResult(
            text="".join(block.text for block in last.content if block.type == "text").strip(),
            model=last.model,
            stop_reason=last.stop_reason,
            input_tokens=sum(u.input_tokens for u in usages),
            output_tokens=sum(u.output_tokens for u in usages),
            cache_read_tokens=sum(u.cache_read_tokens for u in usages),
            cache_write_tokens=sum(u.cache_write_tokens for u in usages),
            latency_ms=round((time.perf_counter() - started) * 1000),
            tool_calls=tuple(tool_calls),
            usages=usages,
        )
        log.info(
            "LLM: requests=%s in=%s out=%s cache_read=%s cache_write=%s stop=%s tools=%s %sms",
            len(responses),
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
            result.cache_write_tokens,
            result.stop_reason,
            len(result.tool_calls),
            result.latency_ms,
        )
        return result
