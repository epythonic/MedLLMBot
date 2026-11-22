"""aiogram middlewares: client tracking, blocking, settings and spam protection."""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from aiogram import BaseMiddleware
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message, TelegramObject

from medllmbot.config import BotSettings
from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig
from medllmbot.db.repository import Repository
from medllmbot.guards import RateLimiter
from medllmbot.security import detect_injection

log = logging.getLogger(__name__)

# aiogram declares these types via Any; object is safer and compatible with the base class
Handler = Callable[[TelegramObject, dict[str, object]], Awaitable[object]]


def incoming_event(message: Message, outcome: AuditOutcome) -> AuditEvent:
    """Audit event for a message cut off before the handler (photos are logged too)."""
    # content_type is a str enum; .value gives "photo", not "ContentType.PHOTO"
    text = message.text or message.caption or f"[{ContentType(message.content_type).value}]"
    return AuditEvent.incoming(message.chat.id, text, outcome, detect_injection(text))


class ClientMiddleware(BaseMiddleware):
    """Registers the client, cuts off blocked ones and puts the bot settings into `config`."""

    repo: Repository

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    @override
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, object]) -> object:
        if not isinstance(event, Message):
            return await handler(event, data)
        # The bot works only in private chats
        if event.chat.type != ChatType.PRIVATE:
            return None

        user = event.from_user
        blocked = await self.repo.touch_client(
            event.chat.id,
            username=user.username if user else None,
            full_name=user.full_name if user else "",
        )
        if blocked:
            log.info("Ignored a message from blocked client %s", event.chat.id)
            await self.repo.log_event(incoming_event(event, AuditOutcome.BLOCKED))
            return None

        data["config"] = await self.repo.get_config()
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Silently drops messages when a client writes too often.

    Must come after ClientMiddleware: the limits are taken from `config`. Does not limit the administrator:
    replying to clients as the operator, they may write often.
    """

    repo: Repository
    limiter: RateLimiter

    def __init__(self, repo: Repository, limiter: RateLimiter) -> None:
        self.repo = repo
        self.limiter = limiter

    @override
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, object]) -> object:
        config = data.get("config")
        settings = data.get("settings")
        is_admin = (
            isinstance(event, Message)
            and isinstance(settings, BotSettings)
            and event.from_user is not None
            and event.from_user.id == settings.admin_id
        )
        if (
            isinstance(event, Message)
            and isinstance(config, BotConfig)
            and not is_admin
            and self.limiter.too_many(event.chat.id, config.rate_limit, config.rate_window)
        ):
            log.info("Rate limit: chat %s", event.chat.id)
            await self.repo.log_event(incoming_event(event, AuditOutcome.RATE_LIMITED))
            return None
        return await handler(event, data)
