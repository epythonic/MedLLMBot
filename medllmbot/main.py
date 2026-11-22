"""Bot entry point: wires up dependencies and starts polling."""

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
from medllmbot.handlers import create_router
from medllmbot.llm import ClinicLLM
from medllmbot.maintenance import purge_periodically
from medllmbot.middlewares import ClientMiddleware, ThrottlingMiddleware
from medllmbot.operator_handlers import create_router as create_operator_router
from medllmbot.security import AlertCooldown


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_dispatcher(repo: Repository, assistant: ClinicAssistant, settings: BotSettings) -> Dispatcher:
    # Dependencies reach handlers by argument name
    dp = Dispatcher(
        assistant=assistant,
        repo=repo,
        settings=settings,
        alerts=AlertCooldown(),
        # Alert about an exhausted spending limit at most once every 6 hours
        budget_alerts=AlertCooldown(seconds=6 * 3600),
    )
    # Order matters: client and settings first, then the rate limit based on those settings
    dp.message.outer_middleware(ClientMiddleware(repo))
    dp.message.outer_middleware(ThrottlingMiddleware(repo, RateLimiter()))
    # Live operator goes before the main router: its messages must not reach Claude
    dp.include_router(create_operator_router())
    dp.include_router(create_router())
    return dp


async def main(settings: BotSettings) -> None:
    engine = create_engine(settings.db_path)
    repo = Repository(create_sessionmaker(engine))
    config = await repo.get_config()

    # A single retry: a Telegram client should not wait more than a minute for a reply
    llm = ClinicLLM(AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), timeout=30.0, max_retries=1))
    assistant = ClinicAssistant(llm, repo)
    bot = Bot(settings.tg_token.get_secret_value())
    dp = build_dispatcher(repo, assistant, settings)

    # The audit log is purged by the retention period from the bot settings
    purge_task = asyncio.create_task(purge_periodically(repo))
    try:
        logging.info("Bot started, model: %s (changed in the admin panel)", config.model)
        # aiogram does not type UNSET_TYPE in the start_polling signature; this can only
        # be fixed in the library
        await dp.start_polling(bot)  # pyright: ignore[reportUnknownMemberType]
    finally:
        purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task
        await llm.client.close()
        await engine.dispose()
