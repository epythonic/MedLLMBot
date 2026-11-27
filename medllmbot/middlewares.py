"""aiogram middlewares: client tracking, blocking, settings, the team and spam protection."""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from aiogram import BaseMiddleware
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message, TelegramObject

from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig
from medllmbot.db.repository import Repository
from medllmbot.guards import RateLimiter
from medllmbot.security import detect_injection
from medllmbot.team import Team

log = logging.getLogger(__name__)

# aiogram declares these types via Any; object is safer and compatible with the base class
Handler = Callable[[TelegramObject, dict[str, object]], Awaitable[object]]


def incoming_event(message: Message, outcome: AuditOutcome) -> AuditEvent:
    """Audit event for a message cut off before the handler (photos are logged too)."""
    # content_type is a str enum; .value gives "photo", not "ContentType.PHOTO"
    text = message.text or message.caption or f"[{ContentType(message.content_type).value}]"
    return AuditEvent.incoming(message.chat.id, text, outcome, detect_injection(text))


class ClientMiddleware(BaseMiddleware):
    """Registers the client, cuts off blocked ones and puts the bot settings into `config` and the owner with
    the active staff into `team`: both are edited in the admin panel and apply from the next message."""

    repo: Repository
    owner_id: int

    def __init__(self, repo: Repository, owner_id: int) -> None:
        self.repo = repo
        self.owner_id = owner_id

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
        data["team"] = Team(self.owner_id, await self.repo.active_staff())
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Silently drops messages when a client writes too often.

    Must come after ClientMiddleware: the limits are taken from `config`. Does not limit the owner and the staff:
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
        team = data.get("team")
        is_staff = (
            isinstance(event, Message)
            and isinstance(team, Team)
            and event.from_user is not None
            and team.is_member(event.from_user.id)
        )
        if (
            isinstance(event, Message)
            and isinstance(config, BotConfig)
            and not is_staff
            and self.limiter.too_many(event.chat.id, config.rate_limit, config.rate_window)
        ):
            log.info("Rate limit: chat %s", event.chat.id)
            await self.repo.log_event(incoming_event(event, AuditOutcome.RATE_LIMITED))
            return None
        return await handler(event, data)
