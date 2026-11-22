"""Anthropic API wrapper: a client question in, a model reply with audit log metadata out.

If the model is given tools, a reply may take several requests: the model calls a tool,
gets the result and continues. Tokens and time in `LLMResult` are summed over all requests.
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

# How many rounds in a row the model may call tools; after that it can only answer
MAX_TOOL_ROUNDS = 3
# How many tool calls to run from one model response; the rest are refused
MAX_TOOL_CALLS_PER_ROUND = 3


class LLMUnavailable(Exception):
    """The API is unavailable or the request failed, so the client gets a fallback reply.

    `partial` is the result of the requests already made if the failure happened after a tool call:
    Anthropic has already billed them, so their tokens and cost still go to the audit log and daily totals.
    """

    partial: LLMResult | None

    def __init__(self, partial: LLMResult | None = None) -> None:
        super().__init__()
        self.partial = partial


@dataclass(frozen=True)
class LLMResult:
    text: str  # model reply as is, may be empty
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    tool_calls: tuple[str, ...] = ()  # tool call descriptions for the audit log
    # Amounts from tool results (an order total): shop data, not something the model made up
    tool_prices: tuple[int, ...] = ()
    # Tokens of each request: Anthropic picks the prompt-length tier per request, not for the sum
    usages: tuple[TokenUsage, ...] = ()

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    @property
    def truncated(self) -> bool:
        """The reply hit max_tokens or the model context window and may have been cut off mid-word."""
        return self.stop_reason in ("max_tokens", "model_context_window_exceeded")


@dataclass(frozen=True)
class ToolOutput:
    text: str  # result for the model
    summary: str  # audit log line: what was searched and what was found
    is_error: bool = False
    prices: tuple[int, ...] = ()  # amounts in the result that the model may tell the client


class ToolRunner(Protocol):
    """A set of model tools: definitions for the API and their execution."""

    @property
    def definitions(self) -> Sequence[ToolParam]: ...

    async def run(self, name: str, tool_input: dict[str, object]) -> ToolOutput: ...


def _assistant_turn(resp: Message) -> MessageParam:
    """The model response with tool calls, put back into the request history as the API requires."""
    content: list[ContentBlockParam] = []
    for block in resp.content:
        # The API rejects a text block made only of whitespace
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
        """The model and limit come from the settings in the DB, so they are passed on every call."""
        messages: list[MessageParam] = [*history, {"role": "user", "content": text}]
        # The system prompt is the same for all clients until the knowledge base is edited in the admin panel,
        # so it is marked for caching (together with the tool definitions before it).
        # The cache works only if the prefix is at least the model minimum
        # (512 tokens for Haiku 5.5, 4096 for Haiku 4.5).
        system: list[TextBlockParam] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        started: float = time.perf_counter()
        responses: list[Message] = []
        tool_calls: list[str] = []
        tool_prices: list[int] = []
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            # In the last round the tools stay in the request (otherwise the cache is lost), but cannot be called
            choice: ToolChoiceParam = {"type": "none"} if round_ == MAX_TOOL_ROUNDS else {"type": "auto"}
            try:
                resp = await self._create(model, max_tokens, system, messages, tools, choice)
            except LLMUnavailable as e:
                if responses:
                    raise LLMUnavailable(self._result(responses, tool_calls, tool_prices, started)) from e
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
                    tool_prices.extend(output.prices)
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": output.text, "is_error": output.is_error}
                    )
            messages.extend([_assistant_turn(resp), {"role": "user", "content": results}])

        return self._result(responses, tool_calls, tool_prices, started)

    @staticmethod
    async def _run_tool(tools: ToolRunner, name: str, tool_input: dict[str, object]) -> ToolOutput:
        """A tool call. Any error becomes a reply to the model, not an exception: otherwise the client message
        would not reach the audit log, and a lead with a phone number would not be saved.
        """
        try:
            return await tools.run(name, tool_input)
        except Exception:
            log.exception("Tool %s failed", name)
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
        """A single API request; errors are turned into LLMUnavailable."""
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
            # A retry will not help: wrong key, model or request
            log.error("Anthropic API configuration error: %s", e)
            raise LLMUnavailable from e
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            # The SDK has already retried (max_retries in main.py)
            log.warning("Anthropic API is temporarily unavailable (%s): %s", e.status_code, e)
            raise LLMUnavailable from e
        except anthropic.APIConnectionError as e:
            log.warning("Cannot connect to Anthropic API: %s", e)
            raise LLMUnavailable from e
        except anthropic.APIError as e:
            # Anything else, e.g. an API response failed the SDK schema check: the client still gets a fallback
            log.exception("Unexpected Anthropic API error")
            raise LLMUnavailable from e

    @staticmethod
    def _result(responses: list[Message], tool_calls: list[str], tool_prices: list[int], started: float) -> LLMResult:
        """Result over all requests: text and stop reason from the last one, tokens summed."""
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
            tool_prices=tuple(tool_prices),
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
