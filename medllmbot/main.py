"""Точка входа бота: собирает зависимости и запускает polling."""

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from anthropic import AsyncAnthropic

from medllmbot.assistant import ClinicAssistant
from medllmbot.config import BotSettings
from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.repository import Repository
from medllmbot.guards import RateLimiter
from medllmbot.handlers import router
from medllmbot.llm import ClinicLLM
from medllmbot.maintenance import purge_periodically
from medllmbot.middlewares import ClientMiddleware, ThrottlingMiddleware
from medllmbot.security import AlertCooldown


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_dispatcher(repo: Repository, assistant: ClinicAssistant, settings: BotSettings) -> Dispatcher:
    # Зависимости попадают в хендлеры по имени аргумента
    dp = Dispatcher(
        assistant=assistant,
        repo=repo,
        settings=settings,
        alerts=AlertCooldown(),
        # Об исчерпанном лимите расходов — не чаще раза в 6 часов
        budget_alerts=AlertCooldown(seconds=6 * 3600),
    )
    # Порядок важен: сначала клиент и настройки, потом rate-limit по этим настройкам
    dp.message.outer_middleware(ClientMiddleware(repo))
    dp.message.outer_middleware(ThrottlingMiddleware(repo, RateLimiter()))
    dp.include_router(router)
    return dp


async def main(settings: BotSettings) -> None:
    engine = create_engine(settings.db_path)
    repo = Repository(create_sessionmaker(engine))
    config = await repo.get_config()

    # Одна повторная попытка: клиент в Telegram не должен ждать ответа дольше минуты
    llm = ClinicLLM(AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), timeout=30.0, max_retries=1))
    assistant = ClinicAssistant(llm, repo)
    bot = Bot(settings.tg_token.get_secret_value())
    dp = build_dispatcher(repo, assistant, settings)

    # Журнал аудита чистится по сроку хранения из настроек бота
    purge_task = asyncio.create_task(purge_periodically(repo))
    try:
        logging.info("Бот запущен, модель: %s (меняется в админке)", config.model)
        # aiogram не типизировал UNSET_TYPE в сигнатуре start_polling — исправить можно
        # только в библиотеке
        await dp.start_polling(bot)  # pyright: ignore[reportUnknownMemberType]
    finally:
        purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task
        await llm.client.close()
        await engine.dispose()
