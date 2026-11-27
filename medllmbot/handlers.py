"""Telegram handlers: greeting, text questions, other messages."""

import asyncio
import logging
from collections.abc import Sequence

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.types import ErrorEvent, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from medllmbot.assistant import TECH_ERROR, TECH_ERROR_RO, ClinicAssistant
from medllmbot.config import BotSettings
from medllmbot.db.models import AuditOutcome, BotConfig, BusinessProfile
from medllmbot.db.repository import Repository
from medllmbot.guards import extract_phone
from medllmbot.i18n import N_, Gettext, gettext_for
from medllmbot.middlewares import incoming_event
from medllmbot.profiles import PROFILES
from medllmbot.security import FLAG_LABELS, AlertCooldown
from medllmbot.team import Team
from medllmbot.text import Language, reply_language, telegram_language

log = logging.getLogger(__name__)

_TEXT_ONLY = N_("Please type your question as text.")
# The bot does not process a long message but saves the phone number from it, and the client should know that.
# Each profile has its own text; the constant is the dental clinic text
LEAD_SAVED = PROFILES[BusinessProfile.CLINIC].replies_in(Language.RU).lead_saved
# How many characters of a client message to show the administrator in a notification.
# The full text is in the admin panel; Telegram rejects messages longer than 4096 characters
ALERT_PREVIEW = 500
# Telegram rejects messages longer than 4096 characters, so a long reply is sent in parts
TELEGRAM_LIMIT = 4096
# The exhausted spending limit notification is one for all clients, so the key is shared
BUDGET_ALERT_KEY = 0
# How many seconds to wait if Telegram asks to message the administrator less often (flood control): holding
# the message processing any longer is not worth it
MAX_RETRY_AFTER = 30


def create_router() -> Router:
    """Router for client messages. A new one per dispatcher: an aiogram router can be attached to only one."""
    router = Router(name="clinic")
    # The bot works only in private chats
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(start, CommandStart())
    router.message.register(handle_text, F.text)
    router.message.register(other)  # photos, voice messages, stickers
    router.errors.register(on_error)
    return router


def greeting(config: BotConfig, telegram_code: str | None) -> str:
    """Greeting in the client's Telegram language: «/start» has no text to tell the language from.

    Telegram in neither Russian nor Romanian gets both greetings in a row: many clients write in Romanian
    from an English Telegram. No Romanian greeting means everyone gets the shared one, which may hold both languages.
    """
    if not config.greeting_ro:
        return config.greeting
    match telegram_language(telegram_code):
        case Language.RU:
            return config.greeting
        case Language.RO:
            return config.greeting_ro
        case None:
            return f"{config.greeting}\n\n{config.greeting_ro}"


async def start(msg: Message, config: BotConfig, repo: Repository) -> None:
    """Greeting when the client presses «Старт» (Start). The texts are edited in the admin panel."""
    text = greeting(config, msg.from_user.language_code if msg.from_user else None)
    await log_reply(repo, msg, AuditOutcome.GREETING, text)
    await send(msg, text)


async def handle_text(
    msg: Message,
    bot: Bot,
    assistant: ClinicAssistant,
    repo: Repository,
    settings: BotSettings,
    config: BotConfig,
    alerts: AlertCooldown,
    budget_alerts: AlertCooldown,
    team: Team,
) -> None:
    """Main handler: the reply, a suspicious message notification (the owner only), a lead (the owner and staff)."""
    text = msg.text or ""
    language = message_language(msg)

    # While the model is thinking, the client sees "typing…"
    async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
        reply = await assistant.reply(msg.chat.id, text, config, language)
    # The client left a phone number, so it is a lead. Save it even if the LLM is unavailable or the message
    # is too long for the bot: finding a phone costs nothing, and the client must not get lost
    phone = extract_phone(text)
    answer = reply.text
    if phone and reply.outcome is AuditOutcome.TOO_LONG:
        answer = f"{answer}\n\n{PROFILES[config.profile].replies_in(language).lead_saved}"
    # A failure to send the reply (the client blocked the bot, network) must not lose the lead and notifications
    await send(msg, answer)

    # Messages to the administrator are in the language from the bot settings
    _ = gettext_for(config.admin_language)
    if reply.suspicious and alerts.ready(msg.chat.id):
        labels = ", ".join(_(FLAG_LABELS.get(flag, flag)) for flag in reply.flags)
        warning = _("Warning: suspicious message from %(client)s (chat %(chat_id)s)") % {
            "client": client_name(msg),
            "chat_id": msg.chat.id,
        }
        signs = _("Signs: %(signs)s") % {"signs": labels}
        await notify_admin(bot, settings.admin_id, f"{warning}\n{signs}\n\n{preview(text)}")

    if reply.outcome is AuditOutcome.BUDGET_EXCEEDED and budget_alerts.ready(BUDGET_ALERT_KEY):
        budget = _(
            "The daily Claude spending limit ($%(budget)s) is used up. Until midnight UTC the bot answers without "
            "the model; leads are still saved. The limit is set in the admin panel: “Bot settings”."
        )
        await notify_admin(bot, settings.admin_id, budget % {"budget": config.daily_budget_usd})

    if phone:
        await forward_lead(msg, bot, repo, team.recipients, phone, _)


def message_language(msg: Message) -> Language:
    """Language of a canned reply: from the text or caption or, if they do not tell, from the client's Telegram."""
    user = msg.from_user
    return reply_language(msg.text or msg.caption or "", user.language_code if user else None)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Splits text into parts no longer than limit: by paragraphs and lines, then by words, at worst by characters."""
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    parts.append(rest)
    return parts


async def send(msg: Message, text: str) -> None:
    """Replies to the client, in parts if needed. A Telegram error is logged instead of breaking the processing."""
    try:
        for part in split_message(text):
            await msg.answer(part)
    except TelegramAPIError:
        log.exception("Failed to send the reply to client %s", msg.chat.id)


async def log_reply(repo: Repository, msg: Message, outcome: AuditOutcome, answer: str) -> None:
    """Audit log entry for messages the bot answers without Claude."""
    event = incoming_event(msg, outcome)
    event.final_answer = answer
    await repo.log_event(event)


def preview(text: str) -> str:
    """Beginning of a client message for a notification to the administrator."""
    return text if len(text) <= ALERT_PREVIEW else text[:ALERT_PREVIEW] + "…"


def client_name(msg: Message) -> str:
    user = msg.from_user
    if user is None:
        return "неизвестный клиент"
    return f"@{user.username}" if user.username else user.full_name


async def notify_admin(
    bot: Bot, admin_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Message to the administrator. None means it was not delivered: the error is logged, processing goes on.

    If Telegram asks to wait (many messages in a row), waits and tries once more.
    """
    for attempt in range(2):
        try:
            if reply_markup is None:
                return await bot.send_message(admin_id, text)
            return await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except TelegramRetryAfter as error:
            if attempt or error.retry_after > MAX_RETRY_AFTER:
                log.warning("Telegram asks to wait %s s, message to the administrator not sent", error.retry_after)
                return None
            await asyncio.sleep(error.retry_after)
        except TelegramAPIError:
            # For example, the administrator has not pressed /start in the bot yet
            log.exception("Failed to send a message to the administrator")
            return None
    return None


async def forward_lead(msg: Message, bot: Bot, repo: Repository, recipients: Sequence[int], phone: str, _: Gettext) -> None:
    """Saves the lead and forwards it to the owner and the staff (`Team.recipients`); `_` translates into
    the administrator's language."""
    client = client_name(msg)
    text = msg.text or msg.caption or ""  # the phone number may be in a photo caption
    lead = await repo.create_lead(msg.chat.id, client, phone, text)
    title = _("New lead #%(id)s from %(client)s") % {"id": lead.id, "client": client}
    phone_line = _("Phone: %(phone)s") % {"phone": phone}
    for recipient in recipients:
        sent = await notify_admin(bot, recipient, f"{title}\n{phone_line}\n\n{preview(text)}")
        if sent is not None:
            # A reply to the lead notification goes to the client, as with the live operator
            await repo.add_relay(sent.chat.id, sent.message_id, msg.chat.id)


def text_only(language: Language) -> str:
    """The reply to a message without text, in the client's language."""
    return gettext_for(language)(_TEXT_ONLY)


async def other(msg: Message, repo: Repository) -> None:
    """Photos, voice messages, stickers, etc."""
    answer = text_only(message_language(msg))
    await log_reply(repo, msg, AuditOutcome.NOT_TEXT, answer)
    await send(msg, answer)


async def on_error(event: ErrorEvent) -> bool:
    """Last line of defense: on an unexpected error (e.g. the DB is busy) the client still gets a reply."""
    log.error("Error while processing update %s", event.update.update_id, exc_info=event.exception)
    msg = event.update.message
    if msg is not None and msg.chat.type == ChatType.PRIVATE:
        # No settings here: the default profile reply, but in the client's language
        answer = TECH_ERROR_RO if message_language(msg) is Language.RO else TECH_ERROR
        try:
            await msg.answer(answer)
        except TelegramAPIError:
            log.exception("Failed to notify client %s about the error", msg.chat.id)
    return True
