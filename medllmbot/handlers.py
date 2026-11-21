"""Хендлеры Telegram: приветствие, текстовые вопросы, прочие сообщения."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.types import ErrorEvent, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from medllmbot.assistant import TECH_ERROR, ClinicAssistant
from medllmbot.config import BotSettings
from medllmbot.db.models import AuditOutcome, BotConfig, BusinessProfile
from medllmbot.db.repository import Repository
from medllmbot.guards import extract_phone
from medllmbot.middlewares import incoming_event
from medllmbot.profiles import PROFILES
from medllmbot.security import FLAG_LABELS, AlertCooldown

log = logging.getLogger(__name__)

TEXT_ONLY = "Пожалуйста, напишите ваш вопрос текстом."
# Длинное сообщение бот не разбирает, но телефон из него сохраняет — клиент должен это знать.
# У каждого профиля свой текст; константа — текст стоматологии
LEAD_SAVED = PROFILES[BusinessProfile.CLINIC].replies.lead_saved
# Сколько символов сообщения клиента показывать администратору в уведомлении.
# Полный текст — в админке; Telegram не пропускает сообщения длиннее 4096 символов
ALERT_PREVIEW = 500
# Telegram не принимает сообщения длиннее 4096 символов — длинный ответ уходит частями
TELEGRAM_LIMIT = 4096
# Уведомление об исчерпанном лимите расходов одно на всех клиентов — ключ общий
BUDGET_ALERT_KEY = 0
# Сколько секунд подождать, если Telegram просит писать администратору реже (flood control): дольше
# держать обработку сообщения не стоит
MAX_RETRY_AFTER = 30


def create_router() -> Router:
    """Роутер сообщений клиентов. Новый на каждый диспетчер: роутер aiogram подключается только к одному."""
    router = Router(name="clinic")
    # Бот работает только в личных сообщениях
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(start, CommandStart())
    router.message.register(handle_text, F.text)
    router.message.register(other)  # фото, голосовые, стикеры
    router.errors.register(on_error)
    return router


async def start(msg: Message, config: BotConfig, repo: Repository) -> None:
    """Приветствие, когда клиент нажимает «Старт». Текст редактируется в админке."""
    await log_reply(repo, msg, AuditOutcome.GREETING, config.greeting)
    await send(msg, config.greeting)


async def handle_text(
    msg: Message,
    bot: Bot,
    assistant: ClinicAssistant,
    repo: Repository,
    settings: BotSettings,
    config: BotConfig,
    alerts: AlertCooldown,
    budget_alerts: AlertCooldown,
) -> None:
    """Основной обработчик: ответ, уведомление о подозрительном сообщении, заявка."""
    text = msg.text or ""

    # Пока модель думает, клиент видит «печатает…»
    async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
        reply = await assistant.reply(msg.chat.id, text, config)
    # Клиент оставил телефон — это заявка. Сохраняем её, даже если LLM недоступен или сообщение
    # слишком длинное для бота: поиск телефона ничего не стоит, а клиент не должен потеряться
    phone = extract_phone(text)
    answer = reply.text
    if phone and reply.outcome is AuditOutcome.TOO_LONG:
        answer = f"{answer}\n\n{PROFILES[config.profile].replies.lead_saved}"
    # Сбой отправки ответа (клиент заблокировал бота, сеть) не должен терять заявку и уведомления
    await send(msg, answer)

    if reply.suspicious and alerts.ready(msg.chat.id):
        labels = ", ".join(FLAG_LABELS.get(flag, flag) for flag in reply.flags)
        await notify_admin(
            bot,
            settings.admin_id,
            f"Внимание: подозрительное сообщение от {client_name(msg)} (chat {msg.chat.id})\nПризнаки: {labels}\n\n"
            f"{preview(text)}",
        )

    if reply.outcome is AuditOutcome.BUDGET_EXCEEDED and budget_alerts.ready(BUDGET_ALERT_KEY):
        await notify_admin(
            bot,
            settings.admin_id,
            f"Дневной лимит расходов на Claude (${config.daily_budget_usd}) исчерпан. До полуночи UTC бот отвечает "
            "без модели, заявки сохраняются. Лимит меняется в админке: «Настройки бота».",
        )

    if phone:
        await forward_lead(msg, bot, repo, settings.admin_id, phone)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет текст на части не длиннее limit: по абзацам и строкам, затем по словам, в крайнем случае — по символам."""
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
    """Отвечает клиенту, при необходимости частями. Ошибку Telegram пишет в лог, а не роняет обработку."""
    try:
        for part in split_message(text):
            await msg.answer(part)
    except TelegramAPIError:
        log.exception("Не удалось отправить ответ клиенту %s", msg.chat.id)


async def log_reply(repo: Repository, msg: Message, outcome: AuditOutcome, answer: str) -> None:
    """Журнал для сообщений, на которые бот отвечает без Claude."""
    event = incoming_event(msg, outcome)
    event.final_answer = answer
    await repo.log_event(event)


def preview(text: str) -> str:
    """Начало сообщения клиента для уведомления администратору."""
    return text if len(text) <= ALERT_PREVIEW else text[:ALERT_PREVIEW] + "…"


def client_name(msg: Message) -> str:
    user = msg.from_user
    if user is None:
        return "неизвестный клиент"
    return f"@{user.username}" if user.username else user.full_name


async def notify_admin(
    bot: Bot, admin_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Сообщение администратору. None — не дошло: ошибка в лог, обработка продолжается.

    Если Telegram просит подождать (много сообщений подряд), ждёт и пробует ещё раз.
    """
    for attempt in range(2):
        try:
            if reply_markup is None:
                return await bot.send_message(admin_id, text)
            return await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except TelegramRetryAfter as error:
            if attempt or error.retry_after > MAX_RETRY_AFTER:
                log.warning("Telegram просит подождать %s с, сообщение администратору не отправлено", error.retry_after)
                return None
            await asyncio.sleep(error.retry_after)
        except TelegramAPIError:
            # Например, администратор ещё не нажал /start у бота
            log.exception("Не удалось отправить сообщение администратору")
            return None
    return None


async def forward_lead(msg: Message, bot: Bot, repo: Repository, admin_id: int, phone: str) -> None:
    """Сохраняет заявку и пересылает её администратору."""
    client = client_name(msg)
    text = msg.text or msg.caption or ""  # телефон бывает в подписи к фото
    lead = await repo.create_lead(msg.chat.id, client, phone, text)
    sent = await notify_admin(bot, admin_id, f"Новая заявка #{lead.id} от {client}\nТелефон: {phone}\n\n{preview(text)}")
    if sent is not None:
        # Ответ администратора на уведомление о заявке уйдёт клиенту — как живой оператор
        await repo.add_relay(sent.chat.id, sent.message_id, msg.chat.id)


async def other(msg: Message, repo: Repository) -> None:
    """Фото, голосовые, стикеры и т.п."""
    await log_reply(repo, msg, AuditOutcome.NOT_TEXT, TEXT_ONLY)
    await send(msg, TEXT_ONLY)


async def on_error(event: ErrorEvent) -> bool:
    """Последняя линия: непредвиденная ошибка (например, база занята) — клиент всё равно получает ответ."""
    log.error("Ошибка при обработке обновления %s", event.update.update_id, exc_info=event.exception)
    msg = event.update.message
    if msg is not None and msg.chat.type == ChatType.PRIVATE:
        try:
            await msg.answer(TECH_ERROR)
        except TelegramAPIError:
            log.exception("Не удалось сообщить клиенту %s об ошибке", msg.chat.id)
    return True
