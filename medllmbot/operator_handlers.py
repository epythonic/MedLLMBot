"""Live operator handlers (medllmbot/operator.py).

The router (`create_router`) is attached before the main one: the administrator's reply to a client and messages
from a client whose dialog the operator handles must not reach Claude.
"""

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import override

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji

from medllmbot.config import BotSettings
from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig
from medllmbot.db.repository import Repository
from medllmbot.guards import extract_phone
from medllmbot.handlers import client_name, forward_lead, message_language, notify_admin, send, split_message, start
from medllmbot.i18n import Gettext, gettext_for
from medllmbot.middlewares import incoming_event
from medllmbot.operator import (
    ALREADY_ENDED,
    CLIENT_LEFT,
    DELIVERY_FAILED,
    DELIVERY_FORBIDDEN,
    END_BUTTON,
    ENDED,
    HISTORY_IN_NOTICE,
    NOT_ALLOWED,
    OPERATOR_COMMAND,
    asks_for_operator,
    client_label,
    client_message_notice,
    end_button_data,
    handoff_notice,
    is_end_button,
    operator_deadline,
    parse_end_button,
    with_operator,
)
from medllmbot.profiles import PROFILES
from medllmbot.text import Language, language_of

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Current time as stored in the DB: UTC without a time zone."""
    return datetime.now(UTC).replace(tzinfo=None)


def content_of(msg: Message) -> str:
    """Message text for the history and the audit log; for photos and files, the caption or the type."""
    return msg.text or msg.caption or f"[{ContentType(msg.content_type).value}]"


def end_keyboard(chat_id: int, _: Gettext) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_(END_BUTTON), callback_data=end_button_data(chat_id))]]
    )


async def relay_to_admin(bot: Bot, repo: Repository, admin_id: int, chat_id: int, text: str, _: Gettext) -> bool:
    """Messages the administrator about a client and remembers the messages: a reply to any of them goes to the client.

    A long text goes in parts, with the “Back to bot” button on the last one. False if the admin is unreachable.
    """
    parts = split_message(text)
    for i, part in enumerate(parts):
        keyboard = end_keyboard(chat_id, _) if i == len(parts) - 1 else None
        sent = await notify_admin(bot, admin_id, part, keyboard)
        if sent is None:
            return False
        await repo.add_relay(sent.chat.id, sent.message_id, chat_id)
    return True


async def dialog_language(repo: Repository, chat_id: int) -> Language:
    """The client's language from their latest messages in the history, for a reply the bot sends unprompted.

    Messages that do not tell the language («0102», «S8?») are skipped; if nothing is found, Russian.
    """
    history = await repo.load_history(chat_id, limit=HISTORY_IN_NOTICE)
    for message in reversed(history):
        content = message["content"]
        if message["role"] == "user" and isinstance(content, str) and (language := language_of(content)) is not None:
            return language
    return Language.RU


def fits_history(text: str, config: BotConfig) -> bool:
    """Whether to save the message to the Claude history: the bot neither answers nor stores messages over the limit."""
    return len(text) <= config.max_len


class OperatorReply(Filter):
    """The administrator replied to a bot message about a client; the handler receives `client_chat_id`."""

    @override
    async def __call__(self, message: Message, settings: BotSettings, repo: Repository) -> bool | dict[str, object]:
        user, replied = message.from_user, message.reply_to_message
        if user is None or user.id != settings.admin_id or replied is None:
            return False
        chat_id = await repo.relay_chat(message.chat.id, replied.message_id)
        return {"client_chat_id": chat_id} if chat_id is not None else False


class WithOperator(Filter):
    """The operator handles the client's dialog, and the deadline has not passed yet."""

    @override
    async def __call__(self, message: Message, repo: Repository) -> bool:
        return with_operator(await repo.operator_until(message.chat.id), utcnow())


async def operator_reply(msg: Message, bot: Bot, repo: Repository, config: BotConfig, client_chat_id: int) -> None:
    """Sends the administrator's reply to the client as is: text, photo, file. Extends the bot's silence period."""
    content = content_of(msg)
    # Logged as an incoming message for the client: «Отправлено клиенту» is empty if it was not delivered
    event = AuditEvent.incoming(client_chat_id, content, AuditOutcome.FROM_OPERATOR)
    try:
        await bot.copy_message(chat_id=client_chat_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
    except TelegramAPIError as error:
        log.exception("Failed to forward the operator reply to client %s", client_chat_id)
        await repo.log_event(event)
        failure = DELIVERY_FORBIDDEN if isinstance(error, TelegramForbiddenError) else DELIVERY_FAILED
        await send(msg, gettext_for(config.admin_language)(failure))
        return
    event.final_answer = content
    await repo.log_event(event)
    # A reply to an old notification hands the dialog to the operator again: they are talking to the client
    await repo.start_operator(client_chat_id, operator_deadline(utcnow(), config.operator_hours))
    if fits_history(content, config):
        await repo.save_message(client_chat_id, "assistant", content)
    # A "delivered" mark on the administrator's message; no harm if it fails
    with suppress(TelegramAPIError):
        await bot.set_message_reaction(msg.chat.id, msg.message_id, reaction=[ReactionTypeEmoji(emoji="👍")])


async def leave_operator(msg: Message, bot: Bot, repo: Repository, settings: BotSettings, config: BotConfig) -> None:
    """/start during an operator dialog: the client changed their mind, the bot answers again, the admin is told."""
    if await repo.end_operator(msg.chat.id, utcnow()):
        left = gettext_for(config.admin_language)(CLIENT_LEFT)
        await notify_admin(bot, settings.admin_id, f"{client_label(client_name(msg), msg.chat.id)}: {left}")
    await start(msg, config, repo)


async def to_operator(msg: Message, bot: Bot, repo: Repository, settings: BotSettings, config: BotConfig) -> None:
    """Message from a client whose dialog the operator handles: to the administrator, no Claude, no bot reply."""
    event = incoming_event(msg, AuditOutcome.TO_OPERATOR)
    client = client_label(client_name(msg), msg.chat.id)
    # Photo, voice message, file: the caption comes with the copy, the header has only the type
    shown = msg.text if msg.text is not None else f"[{ContentType(msg.content_type).value}]"
    _ = gettext_for(config.admin_language)
    delivered = await relay_to_admin(bot, repo, settings.admin_id, msg.chat.id, client_message_notice(client, shown), _)
    if delivered and msg.text is None:
        try:
            copied = await bot.copy_message(chat_id=settings.admin_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
        except TelegramAPIError:
            log.exception("Failed to forward a message from client %s to the administrator", msg.chat.id)
        else:
            # A reply to the copy itself also goes to the client
            await repo.add_relay(settings.admin_id, copied.message_id, msg.chat.id)
    if not delivered:
        # The administrator is unreachable: the client must not wait in silence, so the bot answers again
        await repo.end_operator(msg.chat.id, utcnow())
        answer = PROFILES[config.profile].replies_in(message_language(msg)).tech_error
        event.final_answer = answer
        await send(msg, answer)
    await repo.log_event(event)
    content = content_of(msg)
    if not event.is_suspicious and fits_history(content, config):
        await repo.save_message(msg.chat.id, "user", content)

    if phone := extract_phone(msg.text or msg.caption or ""):
        await forward_lead(msg, bot, repo, settings.admin_id, phone, _)


async def call_operator(msg: Message, bot: Bot, repo: Repository, settings: BotSettings, config: BotConfig) -> None:
    """The client asks for a human: the administrator is notified, and the bot goes quiet in this chat."""
    # The administrator is not their own operator, and a group cannot get the administrator's reply:
    # in these cases the bot answers as before
    if msg.chat.id == settings.admin_id or settings.admin_id < 0:
        raise SkipHandler
    text = msg.text or ""
    replies = PROFILES[config.profile].replies_in(message_language(msg))
    history = await repo.load_history(msg.chat.id, limit=HISTORY_IN_NOTICE)
    _ = gettext_for(config.admin_language)
    notice = handoff_notice(client_label(client_name(msg), msg.chat.id), text, history, config.operator_hours, _)
    event = incoming_event(msg, AuditOutcome.HANDOFF)
    # Operator mode first, then the notification: the next client message that arrives while the notification
    # is being sent no longer goes to Claude
    await repo.start_operator(msg.chat.id, operator_deadline(utcnow(), config.operator_hours))
    if await relay_to_admin(bot, repo, settings.admin_id, msg.chat.id, notice, _):
        answer = replies.operator_started
    else:
        # The administrator is unreachable, so do not promise a reply that will not come
        await repo.end_operator(msg.chat.id, utcnow())
        answer = replies.tech_error
    event.final_answer = answer
    await repo.log_event(event)
    await send(msg, answer)
    if not event.is_suspicious and fits_history(text, config):
        await repo.save_pair(msg.chat.id, text, answer)

    if phone := extract_phone(text):
        await forward_lead(msg, bot, repo, settings.admin_id, phone, _)


async def return_to_bot(callback: CallbackQuery, bot: Bot, repo: Repository, settings: BotSettings) -> None:
    """The “Back to bot” button: the client talks to the bot again. Only the administrator can press it."""
    # Callback queries bypass the message middlewares, so the settings are read here
    config = await repo.get_config()
    _ = gettext_for(config.admin_language)
    if callback.from_user.id != settings.admin_id:
        await callback.answer(_(NOT_ALLOWED))
        return
    chat_id = parse_end_button(callback.data)
    if chat_id is None:
        await callback.answer()
        return
    active = await repo.end_operator(chat_id, utcnow())
    if active:
        text = PROFILES[config.profile].replies_in(await dialog_language(repo, chat_id)).operator_ended
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            log.exception("Failed to tell client %s that the dialog is back with the bot", chat_id)
        else:
            await repo.save_message(chat_id, "assistant", text)
    await callback.answer(_(ENDED if active else ALREADY_ENDED))


def create_router() -> Router:
    """Live operator router. Attached before the main one; the handler order matters:
    administrator reply → /start from a client with the operator → client message to the operator → request for a human.
    """
    router = Router(name="operator")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(operator_reply, OperatorReply())
    router.message.register(leave_operator, CommandStart(), WithOperator())
    router.message.register(to_operator, WithOperator())
    # Text only: Command also checks photo captions, and an empty question cannot be saved to the history
    router.message.register(call_operator, Command(OPERATOR_COMMAND), F.text)
    # F.text first: photos and stickers have no text, and filters are checked in order
    router.message.register(call_operator, F.text, F.text.func(asks_for_operator))
    router.callback_query.register(return_to_bot, F.data.func(is_end_button))
    return router
