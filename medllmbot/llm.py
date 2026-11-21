"""Обёртка над Anthropic API: один запрос — один ответ модели с метаданными для журнала."""

import logging
import time
from dataclasses import dataclass

import anthropic
from anthropic.types import MessageParam, TextBlockParam

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """API недоступен или запрос не удался — клиенту нужно ответить заглушкой."""


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

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    @property
    def truncated(self) -> bool:
        """Ответ упёрся в max_tokens или в окно контекста модели и мог оборваться на полуслове."""
        return self.stop_reason in ("max_tokens", "model_context_window_exceeded")


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
    ) -> LLMResult:
        """Модель и лимит приходят из настроек в БД, поэтому передаются в каждый вызов."""
        messages: list[MessageParam] = [*history, {"role": "user", "content": text}]
        # Системный промпт одинаков для всех клиентов, пока базу знаний не правят в админке,
        # поэтому помечаем его для кэширования. Кэш срабатывает, только если префикс
        # не короче минимума модели (у Haiku 5.5 — 512 токенов, у Haiku 4.5 — 4096).
        system: list[TextBlockParam] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        started: float = time.perf_counter()
        try:
            resp = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
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

        usage = resp.usage
        result = LLMResult(
            text="".join(block.text for block in resp.content if block.type == "text").strip(),
            model=resp.model,
            stop_reason=resp.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
        log.info(
            "LLM: in=%s out=%s cache_read=%s cache_write=%s stop=%s %sms",
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
            result.cache_write_tokens,
            result.stop_reason,
            result.latency_ms,
        )
        return result
