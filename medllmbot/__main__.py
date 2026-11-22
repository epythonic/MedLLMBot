import asyncio
import sys

from pydantic import ValidationError

from medllmbot.config import BotSettings, describe_settings_error
from medllmbot.db.migrate import upgrade_db
from medllmbot.main import main, setup_logging

if __name__ == "__main__":
    setup_logging()
    try:
        settings = BotSettings.from_env()
    except ValidationError as error:
        sys.exit(describe_settings_error(error))
    # Migrations run before the event loop starts: Alembic calls asyncio.run itself
    upgrade_db(settings.db_path)
    asyncio.run(main(settings))
