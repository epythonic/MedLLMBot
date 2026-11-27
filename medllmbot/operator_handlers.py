"""Live operator handlers (medllmbot/operator.py).

The router (`create_router`) is attached before the main one: an operator's reply to a client, messages
from a client whose dialog an operator handles and an operator's own messages while clients wait
must not reach Claude.

Operators are the owner from ADMIN_ID and the active Telegram staff from the admin panel (`medllmbot/team.py`).
They are recognized only by the Telegram user id (`from_user.id`, set by Telegram), never by a name or username,
which anyone can copy. A call goes to every operator; the first one to reply takes the dialog.
"""

import logging
from collections.abc import Sequence
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
from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig, Client
from medllmbot.db.repository import Repository
from medllmbot.guards import extract_phone
from medllmbot.handlers import client_name, forward_lead, message_language, notify_admin, send, split_message, start
from medllmbot.i18n import Gettext, gettext_for
from medllmbot.middlewares import incoming_event
from medllmbot.operator import (
    ALREADY_ENDED,
    BUSY,
    CLIENT_LEFT,
    DELIVERY_FAILED,
    DELIVERY_FORBIDDEN,
    END_BUTTON,
    ENDED,
    HINT_CLIENTS,
    HISTORY_IN_NOTICE,
    NOT_ALLOWED,
    OPERATOR_COMMAND,
    TAKEN,
    asks_for_operator,
    client_label,
    client_message_notice,
    end_button_data,
    end_button_text,
    handoff_notice,
    is_end_button,
    operator_deadline,
    operator_hint,
    parse_end_button,
    with_operator,
)
from medllmbot.profiles import PROFILES
from medllmbot.team import Team
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


def dialog_holder(team: Team, client: Client | None, now: datetime) -> int | None:
    """The operator who took the client's dialog with a reply; None if no one did, the dialog is over,
    or that operator is no longer on the team."""
    if client is None or not with_operator(client.operator_until, now):
        return None
    return team.holder(client.operator_chat_id)


def dialog_operators(team: Team, holder: int | None) -> list[int]:
    """Who gets the client's messages: the operator who took the dialog, otherwise every operator."""
    return [holder] if holder is not None else team.operators


async def relay_to_team(
    bot: Bot, repo: Repository, operators: Sequence[int], chat_id: int, text: str, _: Gettext
) -> list[int]:
    """Messages the operators about a client and remembers the messages: a reply to any of them goes to the client.

    A long text goes in parts, with the “Back to bot” button on the last one. Returns who got the whole text.
    """
    parts = split_message(text)
    delivered: list[int] = []
    for operator in operators:
        for i, part in enumerate(parts):
            keyboard = end_keyboard(chat_id, _) if i == len(parts) - 1 else None
            sent = await notify_admin(bot, operator, part, keyboard)
            if sent is None:
                break
            await repo.add_relay(sent.chat.id, sent.message_id, chat_id)
        else:
            delivered.append(operator)
    return delivered


async def tell(bot: Bot, operators: Sequence[int], text: str) -> None:
    for operator in operators:
        await notify_admin(bot, operator, text)


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
    """An operator replied to a bot message about a client; the handler receives `client_chat_id`."""

    @override
    async def __call__(self, message: Message, team: Team, repo: Repository) -> bool | dict[str, object]:
        user, replied = message.from_user, message.reply_to_message
        if user is None or user.id not in team.operators or replied is None:
            return False
        chat_id = await repo.relay_chat(message.chat.id, replied.message_id)
        return {"client_chat_id": chat_id} if chat_id is not None else False


class ClientsWaiting(Filter):
    """An operator wrote to the bot themselves while clients wait for them: not taken yet or taken by this operator.
    The handler receives `waiting`."""

    @override
    async def __call__(self, message: Message, team: Team, repo: Repository) -> bool | dict[str, object]:
        user = message.from_user
        if user is None or user.id not in team.operators:
            return False
        waiting = [
            client
            for client in await repo.operator_clients(utcnow())
            if team.holder(client.operator_chat_id) in (None, user.id)
        ]
        return {"waiting": waiting} if waiting else False


class WithOperator(Filter):
    """An operator handles the client's dialog, and the deadline has not passed yet."""

    @override
    async def __call__(self, message: Message, repo: Repository) -> bool:
        return with_operator(await repo.operator_until(message.chat.id), utcnow())


async def operator_reply(
    msg: Message, bot: Bot, repo: Repository, config: BotConfig, team: Team, client_chat_id: int
) -> None:
    """Sends the operator's reply to the client as is: text, photo, file. Extends the bot's silence period.

    The first reply takes the dialog: the other operators are told who handles the client and stop getting
    the client's messages; their replies are not delivered until the dialog is back with the bot.
    """
    _ = gettext_for(config.admin_language)
    now = utcnow()
    client = await repo.get_client(client_chat_id)
    holder = dialog_holder(team, client, now)
    label = client_label(str(client) if client is not None else str(client_chat_id), client_chat_id)
    content = content_of(msg)
    # Logged as an incoming message for the client: «Отправлено клиенту» is empty if it was not delivered
    event = AuditEvent.incoming(client_chat_id, content, AuditOutcome.FROM_OPERATOR)
    if holder is not None and holder != msg.chat.id:
        await repo.log_event(event)
        busy = _(BUSY) % {"client": label, "name": team.name_of(holder, _), "button": _(END_BUTTON)}
        await send(msg, busy)
        return
    try:
        await bot.copy_message(chat_id=client_chat_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
    except TelegramAPIError as error:
        log.exception("Failed to forward the operator reply to client %s", client_chat_id)
        await repo.log_event(event)
        failure = DELIVERY_FORBIDDEN if isinstance(error, TelegramForbiddenError) else DELIVERY_FAILED
        await send(msg, _(failure))
        return
    event.final_answer = content
    await repo.log_event(event)
    # A reply to an old notification hands the dialog to the operator again: they are talking to the client
    await repo.start_operator(client_chat_id, operator_deadline(now, config.operator_hours), operator=msg.chat.id)
    if holder is None and client is not None and with_operator(client.operator_until, now):
        # The others got the call too: tell them who took it
        others = [operator for operator in team.operators if operator != msg.chat.id]
        await tell(bot, others, _(TAKEN) % {"client": label, "name": team.name_of(msg.chat.id, _)})
    if fits_history(content, config):
        await repo.save_message(client_chat_id, "assistant", content)
    # A "delivered" mark on the operator's message; no harm if it fails
    with suppress(TelegramAPIError):
        await bot.set_message_reaction(msg.chat.id, msg.message_id, reaction=[ReactionTypeEmoji(emoji="👍")])


async def hint_operator(msg: Message, bot: Bot, repo: Repository, config: BotConfig, waiting: list[Client]) -> None:
    """An operator wrote without replying while clients wait: a plain message cannot tell which client it is for,
    so it goes to no one and Claude is not called. The bot says how to answer and offers a “Back to bot” button
    for each waiting client: until they are back, the operator's own messages get this hint."""
    _ = gettext_for(config.admin_language)
    text = operator_hint([client_label(str(client), client.chat_id) for client in waiting], _)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=end_button_text(str(client), _), callback_data=end_button_data(client.chat_id))]
            for client in waiting[:HINT_CLIENTS]
        ]
    )
    event = incoming_event(msg, AuditOutcome.OPERATOR_HINT)
    if await notify_admin(bot, msg.chat.id, text, keyboard) is not None:
        event.final_answer = text
    await repo.log_event(event)


async def leave_operator(msg: Message, bot: Bot, repo: Repository, config: BotConfig, team: Team) -> None:
    """/start during an operator dialog: the client changed their mind, the bot answers again, the operators
    who were getting the client's messages are told."""
    now = utcnow()
    holder = dialog_holder(team, await repo.get_client(msg.chat.id), now)
    if await repo.end_operator(msg.chat.id, now):
        left = gettext_for(config.admin_language)(CLIENT_LEFT)
        await tell(bot, dialog_operators(team, holder), f"{client_label(client_name(msg), msg.chat.id)}: {left}")
    await start(msg, config, repo)


async def to_operator(msg: Message, bot: Bot, repo: Repository, config: BotConfig, team: Team) -> None:
    """Message from a client whose dialog an operator handles: to that operator or, until someone takes the dialog,
    to every operator. No Claude, no bot reply."""
    event = incoming_event(msg, AuditOutcome.TO_OPERATOR)
    client = client_label(client_name(msg), msg.chat.id)
    operators = dialog_operators(team, dialog_holder(team, await repo.get_client(msg.chat.id), utcnow()))
    # Photo, voice message, file: the caption comes with the copy, the header has only the type
    shown = msg.text if msg.text is not None else f"[{ContentType(msg.content_type).value}]"
    _ = gettext_for(config.admin_language)
    delivered = await relay_to_team(bot, repo, operators, msg.chat.id, client_message_notice(client, shown), _)
    if msg.text is None:
        for operator in delivered:
            try:
                copied = await bot.copy_message(chat_id=operator, from_chat_id=msg.chat.id, message_id=msg.message_id)
            except TelegramAPIError:
                log.exception("Failed to forward a message from client %s to operator %s", msg.chat.id, operator)
            else:
                # A reply to the copy itself also goes to the client
                await repo.add_relay(operator, copied.message_id, msg.chat.id)
    if not delivered:
        # No operator is reachable: the client must not wait in silence, so the bot answers again
        await repo.end_operator(msg.chat.id, utcnow())
        answer = PROFILES[config.profile].replies_in(message_language(msg)).tech_error
        event.final_answer = answer
        await send(msg, answer)
    await repo.log_event(event)
    content = content_of(msg)
    if not event.is_suspicious and fits_history(content, config):
        await repo.save_message(msg.chat.id, "user", content)

    if phone := extract_phone(msg.text or msg.caption or ""):
        await forward_lead(msg, bot, repo, team.recipients, phone, _)


async def call_operator(msg: Message, bot: Bot, repo: Repository, config: BotConfig, team: Team) -> None:
    """The client asks for a human: every operator is notified, and the bot goes quiet in this chat."""
    # An operator is not their own operator, and without a private operator chat no one can reply
    # (ADMIN_ID is a group and there are no staff): in these cases the bot answers as before
    if team.is_member(msg.chat.id) or not team.operators:
        raise SkipHandler
    text = msg.text or ""
    replies = PROFILES[config.profile].replies_in(message_language(msg))
    history = await repo.load_history(msg.chat.id, limit=HISTORY_IN_NOTICE)
    _ = gettext_for(config.admin_language)
    notice = handoff_notice(client_label(client_name(msg), msg.chat.id), text, history, config.operator_hours, _)
    event = incoming_event(msg, AuditOutcome.HANDOFF)
    # Operator mode first, then the notification: the next client message that arrives while the notification
    # is being sent no longer goes to Claude. No one has taken the dialog yet
    await repo.start_operator(msg.chat.id, operator_deadline(utcnow(), config.operator_hours))
    if await relay_to_team(bot, repo, team.operators, msg.chat.id, notice, _):
        answer = replies.operator_started
    else:
        # No operator is reachable, so do not promise a reply that will not come
        await repo.end_operator(msg.chat.id, utcnow())
        answer = replies.tech_error
    event.final_answer = answer
    await repo.log_event(event)
    await send(msg, answer)
    if not event.is_suspicious and fits_history(text, config):
        await repo.save_pair(msg.chat.id, text, answer)

    if phone := extract_phone(text):
        await forward_lead(msg, bot, repo, team.recipients, phone, _)


async def return_to_bot(callback: CallbackQuery, bot: Bot, repo: Repository, settings: BotSettings) -> None:
    """The “Back to bot” button: the client talks to the bot again. Any operator can press it, also for a dialog
    another operator took: that is how a dialog left by someone who is away gets free."""
    # Callback queries bypass the message middlewares, so the settings and the team are read here
    config = await repo.get_config()
    team = Team(settings.admin_id, await repo.active_staff())
    _ = gettext_for(config.admin_language)
    if callback.from_user.id not in team.operators:
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
    operator reply → /start from a client with an operator → client message to the operator →
    an operator's own message while clients wait → request for a human.
    """
    router = Router(name="operator")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(operator_reply, OperatorReply())
    router.message.register(leave_operator, CommandStart(), WithOperator())
    router.message.register(to_operator, WithOperator())
    # Not /start: an operator can still check the greeting
    router.message.register(hint_operator, ~CommandStart(), ClientsWaiting())
    # Text only: Command also checks photo captions, and an empty question cannot be saved to the history
    router.message.register(call_operator, Command(OPERATOR_COMMAND), F.text)
    # F.text first: photos and stickers have no text, and filters are checked in order
    router.message.register(call_operator, F.text, F.text.func(asks_for_operator))
    router.callback_query.register(return_to_bot, F.data.func(is_end_button))
    return router
