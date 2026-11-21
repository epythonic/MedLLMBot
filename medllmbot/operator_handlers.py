"""Хендлеры живого оператора (medllmbot/operator.py).

Роутер (`create_router`) подключается раньше основного: ответ администратора клиенту и сообщения клиента,
чей диалог ведёт оператор, не должны попасть к Claude.
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
from medllmbot.handlers import client_name, forward_lead, notify_admin, send, split_message, start
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

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Текущее время, как в БД: UTC без часового пояса."""
    return datetime.now(UTC).replace(tzinfo=None)


def content_of(msg: Message) -> str:
    """Текст сообщения для истории и журнала; для фото и файлов — подпись или тип."""
    return msg.text or msg.caption or f"[{ContentType(msg.content_type).value}]"


def end_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=END_BUTTON, callback_data=end_button_data(chat_id))]]
    )


async def relay_to_admin(bot: Bot, repo: Repository, admin_id: int, chat_id: int, text: str) -> bool:
    """Пишет администратору о клиенте и запоминает сообщения: ответ на любое из них уйдёт клиенту.

    Длинный текст уходит частями, кнопка «Вернуть бота» — у последней. False — администратор недоступен.
    """
    parts = split_message(text)
    for i, part in enumerate(parts):
        keyboard = end_keyboard(chat_id) if i == len(parts) - 1 else None
        sent = await notify_admin(bot, admin_id, part, keyboard)
        if sent is None:
            return False
        await repo.add_relay(sent.chat.id, sent.message_id, chat_id)
    return True


def fits_history(text: str, config: BotConfig) -> bool:
    """Сохранять ли сообщение в историю для Claude: длиннее лимита бот вопросы не берёт и в историю не кладёт."""
    return len(text) <= config.max_len


class OperatorReply(Filter):
    """Администратор ответил (reply) на сообщение бота о клиенте; хендлер получает `client_chat_id`."""

    @override
    async def __call__(self, message: Message, settings: BotSettings, repo: Repository) -> bool | dict[str, object]:
        user, replied = message.from_user, message.reply_to_message
        if user is None or user.id != settings.admin_id or replied is None:
            return False
        chat_id = await repo.relay_chat(message.chat.id, replied.message_id)
        return {"client_chat_id": chat_id} if chat_id is not None else False


class WithOperator(Filter):
    """Диалог клиента ведёт оператор, и срок ещё не прошёл."""

    @override
    async def __call__(self, message: Message, repo: Repository) -> bool:
        return with_operator(await repo.operator_until(message.chat.id), utcnow())


async def operator_reply(msg: Message, bot: Bot, repo: Repository, config: BotConfig, client_chat_id: int) -> None:
    """Ответ администратора — клиенту, как есть: текст, фото, файл. Срок молчания бота продлевается."""
    content = content_of(msg)
    # В журнале — как входящее сообщение по клиенту: «Отправлено клиенту» пусто, если не дошло
    event = AuditEvent.incoming(client_chat_id, content, AuditOutcome.FROM_OPERATOR)
    try:
        await bot.copy_message(chat_id=client_chat_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
    except TelegramAPIError as error:
        log.exception("Не удалось переслать ответ оператора клиенту %s", client_chat_id)
        await repo.log_event(event)
        await send(msg, DELIVERY_FORBIDDEN if isinstance(error, TelegramForbiddenError) else DELIVERY_FAILED)
        return
    event.final_answer = content
    await repo.log_event(event)
    # Ответ на старое уведомление снова передаёт диалог оператору: он разговаривает с клиентом
    await repo.start_operator(client_chat_id, operator_deadline(utcnow(), config.operator_hours))
    if fits_history(content, config):
        await repo.save_message(client_chat_id, "assistant", content)
    # Отметка «доставлено» на сообщении администратора; не вышло — не страшно
    with suppress(TelegramAPIError):
        await bot.set_message_reaction(msg.chat.id, msg.message_id, reaction=[ReactionTypeEmoji(emoji="👍")])


async def leave_operator(msg: Message, bot: Bot, repo: Repository, settings: BotSettings, config: BotConfig) -> None:
    """/start, пока диалог у оператора: клиент передумал — снова отвечает бот, администратор узнаёт об этом."""
    if await repo.end_operator(msg.chat.id, utcnow()):
        await notify_admin(bot, settings.admin_id, f"{client_label(client_name(msg), msg.chat.id)}: {CLIENT_LEFT}")
    await start(msg, config, repo)


async def to_operator(msg: Message, bot: Bot, repo: Repository, settings: BotSettings, config: BotConfig) -> None:
    """Сообщение клиента, чей диалог ведёт оператор: администратору, без Claude и без ответа бота."""
    event = incoming_event(msg, AuditOutcome.TO_OPERATOR)
    client = client_label(client_name(msg), msg.chat.id)
    # Фото, голосовое, файл: подпись придёт вместе с копией, в заголовке — только тип
    shown = msg.text if msg.text is not None else f"[{ContentType(msg.content_type).value}]"
    delivered = await relay_to_admin(bot, repo, settings.admin_id, msg.chat.id, client_message_notice(client, shown))
    if delivered and msg.text is None:
        try:
            copied = await bot.copy_message(chat_id=settings.admin_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
        except TelegramAPIError:
            log.exception("Не удалось переслать администратору сообщение клиента %s", msg.chat.id)
        else:
            # Ответ на саму копию тоже уйдёт клиенту
            await repo.add_relay(settings.admin_id, copied.message_id, msg.chat.id)
    if not delivered:
        # Администратор недоступен: клиент не должен ждать молча — дальше снова отвечает бот
        await repo.end_operator(msg.chat.id, utcnow())
        answer = PROFILES[config.profile].replies.tech_error
        event.final_answer = answer
        await send(msg, answer)
    await repo.log_event(event)
    content = content_of(msg)
    if not event.is_suspicious and fits_history(content, config):
        await repo.save_message(msg.chat.id, "user", content)

    if phone := extract_phone(msg.text or msg.caption or ""):
        await forward_lead(msg, bot, repo, settings.admin_id, phone)


async def call_operator(msg: Message, bot: Bot, repo: Repository, settings: BotSettings, config: BotConfig) -> None:
    """Клиент просит человека: уведомление администратору, и бот в этом чате замолкает."""
    # Администратор сам себе не оператор, а группе ответить reply от администратора нельзя:
    # в этих случаях отвечает бот, как раньше
    if msg.chat.id == settings.admin_id or settings.admin_id < 0:
        raise SkipHandler
    text = msg.text or ""
    replies = PROFILES[config.profile].replies
    history = await repo.load_history(msg.chat.id, limit=HISTORY_IN_NOTICE)
    notice = handoff_notice(client_label(client_name(msg), msg.chat.id), text, history, config.operator_hours)
    event = incoming_event(msg, AuditOutcome.HANDOFF)
    # Сначала режим оператора, потом уведомление: следующее сообщение клиента, пришедшее, пока уведомление
    # отправляется, уже не уйдёт к Claude
    await repo.start_operator(msg.chat.id, operator_deadline(utcnow(), config.operator_hours))
    if await relay_to_admin(bot, repo, settings.admin_id, msg.chat.id, notice):
        answer = replies.operator_started
    else:
        # Администратор недоступен — не обещаем ответ, которого не будет
        await repo.end_operator(msg.chat.id, utcnow())
        answer = replies.tech_error
    event.final_answer = answer
    await repo.log_event(event)
    await send(msg, answer)
    if not event.is_suspicious and fits_history(text, config):
        await repo.save_pair(msg.chat.id, text, answer)

    if phone := extract_phone(text):
        await forward_lead(msg, bot, repo, settings.admin_id, phone)


async def return_to_bot(callback: CallbackQuery, bot: Bot, repo: Repository, settings: BotSettings) -> None:
    """Кнопка «Вернуть бота»: клиент снова говорит с ботом. Нажать может только администратор."""
    if callback.from_user.id != settings.admin_id:
        await callback.answer(NOT_ALLOWED)
        return
    chat_id = parse_end_button(callback.data)
    if chat_id is None:
        await callback.answer()
        return
    active = await repo.end_operator(chat_id, utcnow())
    if active:
        config = await repo.get_config()
        text = PROFILES[config.profile].replies.operator_ended
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            log.exception("Не удалось сообщить клиенту %s, что диалог вернулся к боту", chat_id)
        else:
            await repo.save_message(chat_id, "assistant", text)
    await callback.answer(ENDED if active else ALREADY_ENDED)


def create_router() -> Router:
    """Роутер живого оператора. Подключается раньше основного; порядок хендлеров важен:
    ответ администратора → /start клиента у оператора → сообщение клиента у оператора → просьба позвать человека.
    """
    router = Router(name="operator")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(operator_reply, OperatorReply())
    router.message.register(leave_operator, CommandStart(), WithOperator())
    router.message.register(to_operator, WithOperator())
    # Только текст: Command проверяет и подписи к фото, а пустой вопрос нельзя сохранить в историю
    router.message.register(call_operator, Command(OPERATOR_COMMAND), F.text)
    # Сначала F.text: у фото и стикеров текста нет, а фильтры проверяются по порядку
    router.message.register(call_operator, F.text, F.text.func(asks_for_operator))
    router.callback_query.register(return_to_bot, F.data.func(is_end_button))
    return router
